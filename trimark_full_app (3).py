"""
Trimark Hybrid v2.0 — Trademark Risk & Application Prep System
Service delivery tool for Fiverr / Upwork engagements.
Supports USPTO bulk XML data (TDXF / annual files from data.uspto.gov).
Not a product for resale. Internal workflow use only.
"""

import json
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from io import BytesIO, StringIO

import streamlit as st
from rapidfuzz import fuzz
from metaphone import doublemetaphone

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

VERSION = "2.0.0"
DB_PATH = Path("trimark_data.db")

# USPTO status codes — codes below 600 are live/pending; 600+ are dead/abandoned
# Source: USPTO Trademark Case Files Dataset documentation
LIVE_STATUS_CODES = {
    "100", "101", "102", "103", "104",   # new application
    "200", "201", "202", "203",           # publication-related
    "300", "301", "302", "303", "304",   # registration-related
    "400", "401", "402", "403",           # post-registration
    "500", "501", "502",                  # renewal
}

# Any code starting with 6, 7, 8, 9 is dead/abandoned
def is_live_status(code: str) -> bool:
    try:
        return int(code) < 600
    except (ValueError, TypeError):
        return False

STOPWORDS = {
    "legal", "law", "pro", "plus", "solutions", "services", "group",
    "tech", "the", "and", "for", "of", "in", "at", "llc", "inc",
    "corp", "co", "brand", "mark", "trade", "global", "national",
    "united", "american", "premier", "elite", "smart", "digital",
    "online", "web", "cloud", "data", "systems", "associates",
    "partners", "consulting", "management", "network", "direct",
}

RELATED_CLASS_PAIRS = {
    frozenset({9, 42}),   frozenset({9, 38}),   frozenset({35, 36}),
    frozenset({41, 35}),  frozenset({44, 45}),  frozenset({25, 18}),
    frozenset({29, 30}),  frozenset({5, 44}),   frozenset({36, 45}),
    frozenset({41, 42}),  frozenset({16, 41}),  frozenset({35, 42}),
    frozenset({9, 35}),   frozenset({38, 42}),  frozenset({9, 41}),
}

DESCRIPTIVE_SIGNAL_WORDS = [
    "software", "legal", "law", "app", "platform", "service",
    "consulting", "management", "tool", "system", "solution",
    "analytics", "search", "tracker", "checker", "generator",
]

# ─────────────────────────────────────────────
# DATABASE — SQLite local store
# ─────────────────────────────────────────────

def init_db():
    """Create the marks table if it doesn't exist."""
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS marks (
            serial_number   TEXT PRIMARY KEY,
            mark_text       TEXT NOT NULL,
            status_code     TEXT,
            is_live         INTEGER,
            filing_date     TEXT,
            registration_date TEXT,
            goods_services  TEXT,
            classes         TEXT,
            owner           TEXT,
            source_file     TEXT,
            loaded_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_mark_text ON marks(mark_text COLLATE NOCASE)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_classes   ON marks(classes)")
    con.commit()
    con.close()


def db_count() -> int:
    if not DB_PATH.exists():
        return 0
    con = sqlite3.connect(DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    con.close()
    return n


def db_search(query: str, class_filter: list[int] | None = None, limit: int = 200) -> list[dict]:
    """
    Full-text-ish search against the local DB.
    Returns records whose mark_text contains any token from the query.
    Then the scoring engine ranks them.
    """
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Build a LIKE clause for each non-stopword token
    tokens = [t for t in query.upper().split() if t.lower() not in STOPWORDS] or [query.upper()]
    like_clauses = " OR ".join(["UPPER(mark_text) LIKE ?" for _ in tokens])
    params = [f"%{t}%" for t in tokens]

    if class_filter:
        # classes stored as comma-separated string: "9,42"
        class_clauses = " OR ".join(["classes LIKE ?" for _ in class_filter])
        sql = f"""
            SELECT * FROM marks
            WHERE ({like_clauses}) AND ({class_clauses})
            LIMIT ?
        """
        params += [f"%{c}%" for c in class_filter] + [limit]
    else:
        sql = f"SELECT * FROM marks WHERE ({like_clauses}) LIMIT ?"
        params += [limit]

    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def db_insert_batch(records: list[dict], source_file: str):
    """Bulk upsert a list of parsed mark records."""
    con = sqlite3.connect(DB_PATH)
    con.executemany("""
        INSERT OR REPLACE INTO marks
            (serial_number, mark_text, status_code, is_live, filing_date,
             registration_date, goods_services, classes, owner, source_file)
        VALUES
            (:serial_number, :mark_text, :status_code, :is_live, :filing_date,
             :registration_date, :goods_services, :classes, :owner, :source_file)
    """, [dict(r, source_file=source_file) for r in records])
    count = con.execute("SELECT changes()").fetchone()[0]
    con.commit()
    con.close()
    return count


def db_clear():
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        con.execute("DELETE FROM marks")
        con.commit()
        con.close()


# ─────────────────────────────────────────────
# USPTO BULK XML PARSER
# ─────────────────────────────────────────────

def txt(el, tag: str, default: str = "") -> str:
    """Safe text extraction from an XML element."""
    found = el.find(tag)
    return (found.text or "").strip() if found is not None else default


def parse_uspto_xml_stream(xml_bytes: bytes, source_name: str) -> tuple[list[dict], int, int]:
    """
    Parse USPTO trademark bulk XML (TDXF / annual format v2.0 DTD).

    The file contains a root element (e.g. <trademark-applications-daily>
    or <trademark-applications-annual>) with <application> children.
    Each <application> contains a <case-file> with the mark data.

    Key elements used:
      <serial-number>            — unique identifier
      <mark-identification>      — the mark text (word mark)
      <status-code>              — USPTO status code (live if < 600)
      <filing-date>              — YYYYMMDD
      <registration-date>        — YYYYMMDD
      <case-file-statements>     — goods/services text
      <classifications>          — class numbers
        <classification>
          <primary-code>         — international class number
      <case-file-owners>
        <case-file-owner>
          <party-name>           — owner name

    Returns (records, parsed_count, skipped_count)
    """
    records = []
    parsed = 0
    skipped = 0

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f"XML parse error: {e}")

    # Handle both daily (<trademark-applications-daily>) and
    # annual (<trademark-applications-annual>) wrapper elements.
    # Applications sit at root > application, or root itself may be application.
    applications = root.findall(".//application")
    if not applications:
        # Some files wrap differently — try case-file directly
        applications = root.findall(".//case-file")

    for app in applications:
        cf = app.find("case-file") if app.tag != "case-file" else app
        if cf is None:
            skipped += 1
            continue

        serial = txt(cf, "serial-number")
        if not serial:
            skipped += 1
            continue

        mark_text = txt(cf, "mark-identification")
        if not mark_text:
            skipped += 1
            continue

        status_code = txt(cf, "status-code")
        live = 1 if is_live_status(status_code) else 0

        filing_date     = txt(cf, "filing-date")
        registration_date = txt(cf, "registration-date")

        # Goods/services — concatenate all statement texts
        gs_parts = []
        for stmt in cf.findall(".//case-file-statement"):
            gs_text = txt(stmt, "text")
            if gs_text:
                gs_parts.append(gs_text)
        goods_services = " | ".join(gs_parts)

        # International class numbers
        class_nums = []
        for cls in cf.findall(".//classification"):
            code = txt(cls, "primary-code")
            if code and code.isdigit():
                class_nums.append(code.lstrip("0") or "0")
        classes = ",".join(sorted(set(class_nums)))

        # Owner
        owner_el = cf.find(".//case-file-owner/party-name")
        owner = (owner_el.text or "").strip() if owner_el is not None else ""

        records.append({
            "serial_number":      serial,
            "mark_text":          mark_text,
            "status_code":        status_code,
            "is_live":            live,
            "filing_date":        filing_date,
            "registration_date":  registration_date,
            "goods_services":     goods_services,
            "classes":            classes,
            "owner":              owner,
        })
        parsed += 1

    return records, parsed, skipped


def load_zip_file(zip_bytes: bytes, source_name: str) -> tuple[int, int, int]:
    """
    Accept a .zip upload, extract XML files from it, parse and load to DB.
    Returns (total_records, total_parsed, total_skipped).
    """
    total_records = total_parsed = total_skipped = 0
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        xml_names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_names:
            raise ValueError("No XML files found in the zip archive.")
        for xml_name in xml_names:
            xml_bytes = zf.read(xml_name)
            records, parsed, skipped = parse_uspto_xml_stream(xml_bytes, xml_name)
            if records:
                inserted = db_insert_batch(records, xml_name)
                total_records += inserted
            total_parsed += parsed
            total_skipped += skipped
    return total_records, total_parsed, total_skipped


def load_xml_file(xml_bytes: bytes, source_name: str) -> tuple[int, int, int]:
    records, parsed, skipped = parse_uspto_xml_stream(xml_bytes, source_name)
    inserted = db_insert_batch(records, source_name) if records else 0
    return inserted, parsed, skipped


# ─────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────

def phonetic_similarity(a: str, b: str) -> float:
    words_a = [w for w in a.upper().split() if w.lower() not in STOPWORDS]
    words_b = [w for w in b.upper().split() if w.lower() not in STOPWORDS]
    if not words_a or not words_b:
        return 0.0
    matched = 0
    total_pairs = max(len(words_a), len(words_b))
    for wa in words_a:
        codes_a = set(filter(None, doublemetaphone(wa)))
        for wb in words_b:
            codes_b = set(filter(None, doublemetaphone(wb)))
            if codes_a & codes_b:
                matched += 1
                break
    return matched / total_pairs


def meaningful_word_overlap(a: str, b: str) -> float:
    tokens_a = {w.lower() for w in a.split() if w.lower() not in STOPWORDS}
    tokens_b = {w.lower() for w in b.split() if w.lower() not in STOPWORDS}
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def derive_class_rel(target_classes: list[int], record_classes_str: str) -> str:
    try:
        record_set = {int(c) for c in record_classes_str.split(",") if c.strip()}
    except ValueError:
        record_set = set()
    target_set = set(target_classes)
    if target_set & record_set:
        return "SAME"
    for pair in RELATED_CLASS_PAIRS:
        if pair & target_set and pair & record_set:
            return "RELATED"
    return "DIFFERENT"


def enhanced_score(
    target_mark: str,
    record_mark: str,
    is_live: int,
    class_rel: str,
) -> tuple[int, str]:
    fuzzy = fuzz.token_sort_ratio(target_mark.upper(), record_mark.upper())
    phonetic_bonus  = round(phonetic_similarity(target_mark, record_mark) * 20)
    overlap_bonus   = round(meaningful_word_overlap(target_mark, record_mark) * 15)
    class_score     = {"SAME": 15, "RELATED": 8, "DIFFERENT": -10}.get(class_rel, 0)
    status_score    = 20 if is_live else 0
    raw             = fuzzy + phonetic_bonus + overlap_bonus + class_score + status_score
    total           = max(0, min(100, round(raw * 100 / 120)))

    if   total >= 88: risk = "CRITICAL"
    elif total >= 72: risk = "HIGH"
    elif total >= 52: risk = "MODERATE"
    elif total >= 32: risk = "LOW"
    else:             risk = "MINIMAL"

    return total, risk


def risk_badge(risk: str) -> str:
    icons = {"CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡", "LOW": "🟢", "MINIMAL": "⚪"}
    return f"{icons.get(risk,'⚪')} {risk}"


# ─────────────────────────────────────────────
# REFUSAL FLAG ENGINE
# ─────────────────────────────────────────────

def refusal_flags(mark: str, goods: str, classes: list[int]) -> list[dict]:
    flags = []
    mark_lower  = mark.lower()
    goods_lower = goods.lower()

    if re.search(r'\b(inc|llc|corp|ltd|co)\b', mark_lower):
        flags.append({"level": "WARNING", "msg":
            "Entity designation (LLC, Inc, Corp) detected. USPTO requires a disclaimer "
            "separating the entity suffix from the registrable portion of the mark."})

    if len(goods.strip()) < 20:
        flags.append({"level": "WARNING", "msg":
            "Goods/services description is very short. USPTO requires definite, "
            "unambiguous language — vague descriptions are routinely refused."})

    mark_tokens = set(mark_lower.split())
    for word in DESCRIPTIVE_SIGNAL_WORDS:
        if word in mark_tokens and word in goods_lower:
            flags.append({"level": "WARNING", "msg":
                f"The term '{word}' in your mark may be merely descriptive of the "
                f"goods/services (§2(e)(1)). Consider whether secondary meaning "
                f"or a different mark would be stronger."})
            break

    if re.search(r'\b(american|national|united states|us|texas|california|global|international|worldwide)\b', mark_lower):
        flags.append({"level": "WARNING", "msg":
            "Mark contains a geographic or national term. Primarily geographic marks "
            "face refusal under §2(e)(2) unless secondary meaning is shown."})

    for term in ["best", "premier", "elite", "superior", "ultimate", "top", "first"]:
        if term in mark_lower:
            flags.append({"level": "INFO", "msg":
                f"Laudatory term '{term}' detected. Laudatory/self-congratulatory terms "
                f"are considered weak and may be difficult to register without a showing "
                f"of acquired distinctiveness."})
            break

    if len(mark.split()) == 1 and mark[0].isupper():
        flags.append({"level": "INFO", "msg":
            "Single-word mark. If the term is primarily merely a surname, registration "
            "may be refused under §2(e)(4). Consider surname significance in context."})

    if 45 in classes:
        flags.append({"level": "INFO", "msg":
            "Class 45 (legal/personal services) identified. Ensure the services "
            "description accurately reflects the nature of services offered."})

    return flags


# ─────────────────────────────────────────────
# REPORT GENERATOR
# ─────────────────────────────────────────────

def build_text_report(mark, goods, classes, results, flags, client_name=""):
    now = datetime.now().strftime("%B %d, %Y %I:%M %p")
    class_str = ", ".join(str(c) for c in classes) if classes else "Not specified"
    db_size = db_count()

    lines = [
        "=" * 65,
        "  TRIMARK HYBRID — TRADEMARK CLEARANCE REPORT",
        "  Prepared for professional service delivery.",
        "  This report does not constitute legal advice.",
        "=" * 65,
        "",
        f"  Report Date     : {now}",
        f"  Version         : {VERSION}",
        f"  Database records: {db_size:,}",
    ]
    if client_name:
        lines.append(f"  Client          : {client_name}")
    lines += [
        "",
        "─" * 65,
        "  SEARCH PARAMETERS",
        "─" * 65,
        f"  Mark            : {mark}",
        f"  Goods/Services  : {goods}",
        f"  Int'l Class(es) : {class_str}",
        "",
        "─" * 65,
        "  CONFLICT RESULTS  (sorted by risk score, descending)",
        "─" * 65,
        f"  {'Mark':<25} {'Score':>6}  {'Risk':<10}  {'Status':<5}  {'Classes'}",
        f"  {'─'*25} {'─'*6}  {'─'*10}  {'─'*5}  {'─'*12}",
    ]
    for r in results:
        status_str = "LIVE" if r.get("is_live") else "DEAD"
        lines.append(
            f"  {r['mark_text']:<25} {r['score']:>6}  {r['risk']:<10}  "
            f"{status_str:<5}  {r['classes']}"
        )

    if flags:
        lines += ["", "─" * 65, "  FILING CONSIDERATIONS", "─" * 65]
        for f in flags:
            prefix = "⚠  " if f["level"] == "WARNING" else "ℹ  "
            words = (prefix + f["msg"]).split()
            line = "  "
            wrapped = []
            for w in words:
                if len(line) + len(w) + 1 > 63:
                    wrapped.append(line)
                    line = "     " + w + " "
                else:
                    line += w + " "
            wrapped.append(line)
            lines += wrapped + [""]

    lines += [
        "─" * 65,
        "  DISCLAIMER",
        "─" * 65,
        "  This report is generated by an automated tool and provided",
        "  for informational purposes only. It does not constitute",
        "  legal advice. Consult a licensed trademark attorney before",
        "  making filing decisions.",
        "",
        "=" * 65,
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# SAMPLE (FALLBACK) DATA
# ─────────────────────────────────────────────

SAMPLE_RECORDS = [
    {"serial_number": "SAMPLE-001", "mark_text": "LegalEdge",   "is_live": 1, "status_code": "400", "classes": "45",    "goods_services": "Legal services", "owner": "Sample Corp"},
    {"serial_number": "SAMPLE-002", "mark_text": "Legal Edges", "is_live": 1, "status_code": "300", "classes": "45,42", "goods_services": "Legal and tech services", "owner": "Sample LLC"},
    {"serial_number": "SAMPLE-003", "mark_text": "Edge Legal",  "is_live": 0, "status_code": "602", "classes": "45",    "goods_services": "Legal services", "owner": "Old Corp"},
    {"serial_number": "SAMPLE-004", "mark_text": "LawEdge Pro", "is_live": 1, "status_code": "300", "classes": "35",    "goods_services": "Business consulting", "owner": "Pro Inc"},
    {"serial_number": "SAMPLE-005", "mark_text": "EdgeLaw",     "is_live": 1, "status_code": "400", "classes": "45",    "goods_services": "Attorney services", "owner": "Edge Firm"},
    {"serial_number": "SAMPLE-006", "mark_text": "Legal Eagle", "is_live": 1, "status_code": "300", "classes": "45",    "goods_services": "Legal information services", "owner": "Eagle Legal"},
]


# ─────────────────────────────────────────────
# APPLICATION PREP MODULE
# ─────────────────────────────────────────────

def render_app_prep():
    st.subheader("Application Prep")
    st.caption("Complete the fields below to generate a structured application data package for USPTO TEAS filing.")

    with st.expander("📋 Owner / Applicant", expanded=True):
        c1, c2 = st.columns(2)
        owner_name  = c1.text_input("Legal name *", key="ap_owner_name")
        entity_type = c2.selectbox("Entity type *",
            ["", "Individual", "Corporation", "LLC", "Partnership", "Other"], key="ap_entity_type")
        place_of_org = st.text_input("Place of organization / citizenship", key="ap_place_org")
        addr1 = st.text_input("Mailing address line 1 *", key="ap_addr1")
        c3, c4, c5 = st.columns([2, 1, 1])
        city    = c3.text_input("City *", key="ap_city")
        state_  = c4.text_input("State", key="ap_state")
        zip_    = c5.text_input("Zip", key="ap_zip")
        c6, c7  = st.columns(2)
        country = c6.text_input("Country", value="United States", key="ap_country")
        email   = c7.text_input("Email *", key="ap_email")

    with st.expander("🔤 Trademark Details", expanded=True):
        mark_text    = st.text_input("Mark text *", key="ap_mark_text")
        mark_format  = st.radio("Mark format",
            ["Standard character", "Special form / design", "Sound mark"],
            horizontal=True, key="ap_mark_format")
        mark_description = st.text_area("Mark description (required for design/sound)", key="ap_mark_desc")
        color_claim  = st.text_input("Color claim (if applicable)", key="ap_color_claim")

    with st.expander("📦 Goods and Services", expanded=True):
        gs_basis    = st.radio("Filing basis",
            ["Section 1(a) — Use in commerce", "Section 1(b) — Intent to use"],
            horizontal=True, key="ap_basis")
        gs_classes  = st.text_input("International class number(s) *", placeholder="e.g. 42, 45", key="ap_classes")
        gs_desc     = st.text_area("Goods/services description *", key="ap_gs_desc")
        first_use = first_use_commerce = specimen = None
        if "1(a)" in gs_basis:
            ca, cb = st.columns(2)
            first_use           = ca.date_input("Date of first use anywhere", key="ap_first_use")
            first_use_commerce  = cb.date_input("Date of first use in commerce", key="ap_first_use_commerce")
            st.info("Specimen required for 1(a) basis.")
            specimen = st.file_uploader("Upload specimen", type=["jpg","jpeg","png","pdf"], key="ap_specimen")

    with st.expander("⚖️ Additional Statements (optional)"):
        include_translation  = st.checkbox("Translation", key="ap_trans_cb")
        translation_text     = st.text_area("Translation statement", key="ap_trans_txt") if include_translation else ""
        include_disclaimer   = st.checkbox("Disclaimer", key="ap_disc_cb")
        disclaimer_text      = st.text_input("Disclaimer text", key="ap_disc_txt") if include_disclaimer else ""
        include_prior_reg    = st.checkbox("Ownership of prior registration(s)", key="ap_prior_cb")
        prior_reg_text       = st.text_input("Prior registration number(s)", key="ap_prior_txt") if include_prior_reg else ""

    with st.expander("✍️ Signature"):
        sig_name     = st.text_input("Signatory name *", key="ap_sig_name")
        sig_position = st.text_input("Signatory position / title *", key="ap_sig_pos")
        sig_text     = st.text_input("Electronic signature * (e.g. /Jane Smith/)", key="ap_sig_text")
        sig_date     = st.date_input("Date signed", value=date.today(), key="ap_sig_date")

    with st.expander("👤 Client / Delivery Info"):
        client_ref     = st.text_input("Client name / reference (for report header)", key="ap_client")
        st.text_area("Internal notes (not included in export)", key="ap_notes")

    st.markdown("---")
    if st.button("Generate Application Package", key="ap_generate"):
        errors = []
        if not owner_name:    errors.append("Owner name is required.")
        if not entity_type:   errors.append("Entity type is required.")
        if not addr1 or not city: errors.append("Mailing address (line 1 and city) required.")
        if not email or "@" not in email: errors.append("Valid email required.")
        if not mark_text:     errors.append("Mark text is required.")
        if not gs_classes:    errors.append("At least one international class is required.")
        if not gs_desc:       errors.append("Goods/services description required.")
        if not sig_name or not sig_text: errors.append("Signatory name and signature required.")
        for e in errors:
            st.error(e)
        if errors:
            return

        try:
            parsed_classes = [int(c.strip()) for c in gs_classes.split(",") if c.strip()]
        except ValueError:
            st.error("Class numbers must be integers.")
            return

        payload = {
            "meta": {"generated": datetime.now().isoformat(), "version": VERSION, "client_ref": client_ref},
            "owner": {"name": owner_name, "entity_type": entity_type, "place_of_org": place_of_org,
                      "address": {"line1": addr1, "city": city, "state": state_, "zip": zip_, "country": country},
                      "email": email},
            "mark": {"text": re.sub(r"[™®©]", "", mark_text).strip(),
                     "format": mark_format, "description": mark_description, "color_claim": color_claim},
            "goods_services": {"basis": gs_basis, "classes": parsed_classes, "description": gs_desc,
                               "first_use_date": str(first_use) if first_use else None,
                               "first_use_commerce_date": str(first_use_commerce) if first_use_commerce else None,
                               "specimen_filename": specimen.name if specimen else None},
            "additional_statements": {"translation": translation_text or None,
                                      "disclaimer": disclaimer_text or None,
                                      "prior_registrations": prior_reg_text or None},
            "signature": {"signatory_name": sig_name, "signatory_position": sig_position,
                          "electronic_signature": sig_text, "date_signed": str(sig_date)},
        }

        st.success("Application package generated.")
        st.download_button("⬇ Download Application Data (JSON)",
            data=json.dumps(payload, indent=2).encode("utf-8"),
            file_name=f"trimark_app_{re.sub(r'\\W+','_', mark_text.lower())}.json",
            mime="application/json")
        with st.expander("Preview application data"):
            st.json(payload)


# ─────────────────────────────────────────────
# DATA MANAGEMENT TAB
# ─────────────────────────────────────────────

def render_data_management():
    st.subheader("Bulk Data Management")
    count = db_count()
    using_sample = count == 0

    col1, col2, col3 = st.columns(3)
    col1.metric("Records in database", f"{count:,}")
    col2.metric("Data source", "Sample data" if using_sample else "USPTO bulk XML")
    col3.metric("DB file", str(DB_PATH))

    if using_sample:
        st.info(
            "**No USPTO data loaded yet.** The search tab will use the built-in 6-record sample. "
            "Load a real USPTO bulk XML file below to search against actual trademark records."
        )

    st.markdown("---")
    st.markdown("### Load USPTO Bulk XML Data")
    st.markdown("""
**How to get the files:**

1. Go to **[https://developer.uspto.gov/product/trademark-daily-xml-file-tdxf-applications](https://developer.uspto.gov/product/trademark-daily-xml-file-tdxf-applications)**
2. Download a daily `.zip` file (approx 5–30 MB each, covers one day's updates)
3. Or for backfile data, go to **[https://developer.uspto.gov/product/trademark-annual-xml-applications](https://developer.uspto.gov/product/trademark-annual-xml-applications)**
   - Annual files are large (100MB–1GB+ zipped) — start with a recent daily file for testing
4. Upload the `.zip` (or unzipped `.xml`) here — no API key needed for file-based loading

**File formats supported:** `.zip` containing XML files, or raw `.xml` directly

**What gets stored locally:**
- Mark text, serial number, status code (live/dead), filing date, registration date, goods/services description, class numbers, owner name
- Images and PDF documents are NOT stored (text-only bulk data)
    """)

    uploaded = st.file_uploader(
        "Upload USPTO bulk XML file (.zip or .xml)",
        type=["zip", "xml"],
        key="bulk_upload",
        help="Daily files: ~5–30 MB. Annual files: can be 100MB+. Start with a recent daily file."
    )

    if uploaded is not None:
        file_bytes = uploaded.read()
        fname = uploaded.name.lower()

        with st.spinner(f"Parsing {uploaded.name} — this may take a minute for large files..."):
            try:
                if fname.endswith(".zip"):
                    inserted, parsed, skipped = load_zip_file(file_bytes, uploaded.name)
                else:
                    inserted, parsed, skipped = load_xml_file(file_bytes, uploaded.name)

                st.success(
                    f"✅ Loaded **{inserted:,} records** from {uploaded.name} "
                    f"(parsed: {parsed:,}, skipped: {skipped:,}). "
                    f"Total database: **{db_count():,} records**."
                )
                st.rerun()
            except Exception as e:
                st.error(f"Error loading file: {e}")

    st.markdown("---")
    st.markdown("### Database Actions")
    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**Export database to CSV**")
        if st.button("Export all records as CSV", key="export_csv") and db_count() > 0:
            con = sqlite3.connect(DB_PATH)
            rows = con.execute("SELECT * FROM marks LIMIT 100000").fetchall()
            cols = [d[0] for d in con.execute("SELECT * FROM marks LIMIT 1").description]
            con.close()
            csv_lines = [",".join(cols)]
            for row in rows:
                csv_lines.append(",".join(f'"{str(v or "")}"' for v in row))
            st.download_button("⬇ Download CSV", "\n".join(csv_lines).encode(),
                file_name="trimark_marks.csv", mime="text/csv")

    with c2:
        st.markdown("**Clear database**")
        if st.button("🗑 Clear all records", key="clear_db"):
            db_clear()
            st.success("Database cleared.")
            st.rerun()


# ─────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────

def main():
    st.set_page_config(page_title="Trimark Hybrid", page_icon="🛡️", layout="wide")
    init_db()

    for key, default in {
        "search_results": [], "search_flags": [], "search_mark": "",
        "search_goods": "", "search_classes": [], "search_client": "",
    }.items():
        if key not in st.session_state:
            st.session_state[key] = default

    # Header
    db_size = db_count()
    data_label = f"{db_size:,} USPTO records" if db_size > 0 else "sample data"
    st.title("🛡️ Trimark Hybrid")
    st.caption(f"Trademark Risk & Application Prep  ·  v{VERSION}  ·  Database: {data_label}  ·  Service delivery tool — not legal advice.")

    tab_search, tab_prep, tab_data = st.tabs([
        "🔍 Trademark Search & Risk",
        "📄 Application Prep",
        "🗄️ Bulk Data Management"
    ])

    # ────────────────────────
    # TAB 1: SEARCH & RISK
    # ────────────────────────
    with tab_search:
        st.subheader("Trademark Clearance Search")

        using_sample = db_count() == 0
        if using_sample:
            st.warning(
                "Running on **sample data** (6 records). Load USPTO bulk XML files in "
                "the **Bulk Data Management** tab for real search results."
            )

        with st.form("search_form"):
            c1, c2 = st.columns([2, 3])
            mark_input   = c1.text_input("Trademark name *", placeholder="e.g. LegalEdge")
            goods_input  = c2.text_area("Goods / services description *",
                placeholder="e.g. Online legal document preparation software", height=80)
            c3, c4 = st.columns([1, 3])
            classes_input  = c3.text_input("Int'l class(es)", placeholder="e.g. 42, 45",
                help="Comma-separated USPTO international class numbers")
            client_input   = c4.text_input("Client / reference (optional)")
            submitted = st.form_submit_button("🔍 Run Search", use_container_width=True)

        if submitted:
            if not mark_input:
                st.error("Trademark name is required.")
            else:
                try:
                    target_classes = [int(c.strip()) for c in classes_input.split(",") if c.strip()] if classes_input.strip() else []
                except ValueError:
                    st.error("Class numbers must be integers.")
                    target_classes = []

                # Pull candidates from DB (or sample if empty)
                if using_sample:
                    candidates = SAMPLE_RECORDS
                else:
                    candidates = db_search(mark_input, target_classes if target_classes else None, limit=300)
                    if not candidates:
                        st.info("No candidate records found matching those tokens and classes. "
                                "Try broader search terms or load more data.")

                results = []
                for rec in candidates:
                    class_rel = derive_class_rel(target_classes, rec.get("classes","")) if target_classes else "UNKNOWN"
                    score, risk = enhanced_score(mark_input, rec["mark_text"], rec.get("is_live",0), class_rel)
                    results.append({
                        "mark_text":   rec["mark_text"],
                        "score":       score,
                        "risk":        risk,
                        "risk_badge":  risk_badge(risk),
                        "is_live":     rec.get("is_live", 0),
                        "classes":     rec.get("classes",""),
                        "class_rel":   class_rel,
                        "owner":       rec.get("owner",""),
                        "serial":      rec.get("serial_number",""),
                        "goods":       (rec.get("goods_services","") or "")[:120],
                    })

                results.sort(key=lambda x: x["score"], reverse=True)
                flags = refusal_flags(mark_input, goods_input, target_classes)

                st.session_state.search_results = results
                st.session_state.search_flags   = flags
                st.session_state.search_mark    = mark_input
                st.session_state.search_goods   = goods_input
                st.session_state.search_classes = target_classes
                st.session_state.search_client  = client_input

        # Display results
        if st.session_state.search_results:
            results   = st.session_state.search_results
            flags     = st.session_state.search_flags
            mark_val  = st.session_state.search_mark
            goods_val = st.session_state.search_goods
            cls_val   = st.session_state.search_classes
            client_val= st.session_state.search_client

            critical = sum(1 for r in results if r["risk"] == "CRITICAL")
            high     = sum(1 for r in results if r["risk"] == "HIGH")
            moderate = sum(1 for r in results if r["risk"] == "MODERATE")

            m1,m2,m3,m4 = st.columns(4)
            m1.metric("Results returned", len(results))
            m2.metric("🔴 Critical",  critical)
            m3.metric("🟠 High",      high)
            m4.metric("🟡 Moderate",  moderate)
            st.markdown("---")

            display_df = [{
                "Mark":       r["mark_text"],
                "Score":      r["score"],
                "Risk":       r["risk_badge"],
                "Status":     "LIVE" if r["is_live"] else "DEAD",
                "Classes":    r["classes"],
                "Class Rel.": r["class_rel"],
                "Owner":      r["owner"],
                "Serial":     r["serial"],
                "G/S (abbr)": r["goods"],
            } for r in results]

            st.dataframe(display_df, use_container_width=True, hide_index=True)

            if flags:
                st.markdown("---")
                st.subheader("Filing Considerations")
                for f in flags:
                    (st.warning if f["level"] == "WARNING" else st.info)(f["msg"])

            st.markdown("---")
            report = build_text_report(mark_val, goods_val, cls_val, results, flags, client_val)
            st.download_button("⬇ Download Clearance Report (.txt)",
                data=report.encode("utf-8"),
                file_name=f"trimark_clearance_{re.sub(r'\\W+','_',mark_val.lower())}.txt",
                mime="text/plain")
            st.info("Search complete. Switch to **Application Prep** to build the TEAS filing package.")

    # ────────────────────────
    # TAB 2: APP PREP
    # ────────────────────────
    with tab_prep:
        render_app_prep()

    # ────────────────────────
    # TAB 3: DATA MANAGEMENT
    # ────────────────────────
    with tab_data:
        render_data_management()


if __name__ == "__main__":
    main()
