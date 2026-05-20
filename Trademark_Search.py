"""
Jackson Trimark Pro
USPTO Trademark Conflict Search Engine
Powered by RapidAPI USPTO Trademark API (uspto-trademark.p.rapidapi.com)

API Endpoints used:
  GET /v1/trademarkSearch/{keyword}/active   - live marks only
  GET /v1/trademarkSearch/{keyword}/all      - live + dead marks
  GET /v1/trademarkAvailable/{keyword}       - quick availability check
  GET /v1/databaseStatus                     - verify DB freshness

Secrets required (add to .streamlit/secrets.toml):
  RAPIDAPI_KEY = "your-key-here"
"""

import os
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple

import requests
import pandas as pd
import streamlit as st
from rapidfuzz import fuzz, process
from fpdf import FPDF

# ─────────────────────────────────────────────
# API CONFIGURATION
# ─────────────────────────────────────────────
RAPIDAPI_HOST = "uspto-trademark.p.rapidapi.com"
BASE_URL = f"https://{RAPIDAPI_HOST}"


def load_api_key() -> str:
    """Load API key from Streamlit secrets or environment only. Never hardcode."""
    if "RAPIDAPI_KEY" in st.secrets:
        return st.secrets["RAPIDAPI_KEY"].strip()
    env_key = os.getenv("RAPIDAPI_KEY", "").strip()
    if env_key:
        return env_key
    return ""


def get_headers(api_key: str) -> Dict:
    return {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": api_key,
    }


# ─────────────────────────────────────────────
# NICE CLASSIFICATION
# ─────────────────────────────────────────────
NICE_CLASSES = {
    "001": "Chemicals",
    "002": "Paints & Varnishes",
    "003": "Cosmetics & Cleaning",
    "004": "Lubricants & Fuels",
    "005": "Pharmaceuticals",
    "006": "Metal Goods",
    "007": "Machinery",
    "008": "Hand Tools",
    "009": "Electronics & Software",
    "010": "Medical Devices",
    "011": "Lighting & Heating",
    "012": "Vehicles",
    "013": "Firearms",
    "014": "Jewelry",
    "015": "Musical Instruments",
    "016": "Paper & Printed Matter",
    "017": "Rubber Goods",
    "018": "Leather Goods",
    "019": "Building Materials",
    "020": "Furniture",
    "021": "Housewares",
    "022": "Ropes & Fibers",
    "023": "Yarns & Threads",
    "024": "Fabrics",
    "025": "Clothing & Footwear",
    "026": "Lace & Embroidery",
    "027": "Floor Coverings",
    "028": "Toys & Sporting Goods",
    "029": "Meat & Processed Foods",
    "030": "Staple Foods",
    "031": "Agricultural Products",
    "032": "Beers & Beverages",
    "033": "Alcoholic Beverages",
    "034": "Tobacco",
    "035": "Advertising & Business",
    "036": "Insurance & Finance",
    "037": "Construction & Repair",
    "038": "Telecommunications",
    "039": "Transportation",
    "040": "Material Treatment",
    "041": "Education & Entertainment",
    "042": "Computer & Scientific Services",
    "043": "Food & Drink Services",
    "044": "Medical & Veterinary",
    "045": "Legal & Security Services",
}

# Classes commonly audited together for conflict purposes
RELATED_CLASSES = {
    "009": ["042", "035", "038"],
    "042": ["009", "035", "038"],
    "041": ["035", "016", "028", "009"],
    "035": ["009", "042", "041", "036"],
    "025": ["035", "018", "028"],
    "036": ["035", "042", "045"],
    "045": ["035", "036", "042"],
    "038": ["009", "042", "035"],
}


# ─────────────────────────────────────────────
# USPTO API CALLS
# ─────────────────────────────────────────────
def check_database_status(api_key: str) -> Dict:
    """Verify USPTO database freshness before running search."""
    try:
        r = requests.get(
            f"{BASE_URL}/v1/databaseStatus",
            headers=get_headers(api_key),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def check_availability(keyword: str, api_key: str) -> Dict:
    """Quick availability check — returns available: true/false."""
    r = requests.get(
        f"{BASE_URL}/v1/trademarkAvailable/{keyword}",
        headers=get_headers(api_key),
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def search_trademarks(keyword: str, search_type: str, api_key: str) -> List[Dict]:
    """
    Search USPTO for marks matching keyword.
    search_type: 'active' (live marks only) or 'all' (live + dead)
    Returns list of raw trademark records.
    """
    r = requests.get(
        f"{BASE_URL}/v1/trademarkSearch/{keyword}/{search_type}",
        headers=get_headers(api_key),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    # RapidAPI USPTO returns items under 'items' key
    items = data if isinstance(data, list) else data.get("items", data.get("results", []))
    return items if isinstance(items, list) else []


def normalize_record(item: Dict) -> Dict:
    """Normalize a raw RapidAPI USPTO record into a consistent shape."""
    # RapidAPI USPTO trademark record field names
    return {
        "mark": item.get("keyword", item.get("markLiteralText", "Unknown")).strip(),
        "serial": item.get("serialNumber", "N/A"),
        "registration": item.get("registrationNumber", "N/A"),
        "status": item.get("statusLabel", item.get("status", "Unknown")),
        "status_code": item.get("statusCode", ""),
        "owner": item.get("ownerName", item.get("owner", "Unknown")),
        "filing_date": item.get("filingDate", "N/A"),
        "registration_date": item.get("registrationDate", "N/A"),
        "goods_services": item.get("goodsAndServices", item.get("goodsServices", "N/A")),
        "int_class": item.get("internationalClasses", item.get("classCode", "000")),
        "us_class": item.get("usClasses", ""),
        "description": item.get("description", ""),
    }


# ─────────────────────────────────────────────
# CONFLICT SCORING ENGINE
# ─────────────────────────────────────────────
def phonetic_similarity(a: str, b: str) -> int:
    """
    Multi-method similarity scoring:
    - Token sort ratio (handles word order differences)
    - Partial ratio (handles substring matches)
    - Plain ratio (character-level)
    Returns highest of the three (0-100).
    """
    a, b = a.upper().strip(), b.upper().strip()
    scores = [
        fuzz.token_sort_ratio(a, b),
        fuzz.partial_ratio(a, b),
        fuzz.ratio(a, b),
    ]
    return max(scores)


def class_overlap(target_classes: List[str], record_class: str) -> str:
    """
    Returns 'SAME', 'RELATED', or 'DIFFERENT' based on class overlap.
    """
    record_class = str(record_class).zfill(3)
    if record_class in target_classes:
        return "SAME"
    for tc in target_classes:
        if record_class in RELATED_CLASSES.get(tc, []):
            return "RELATED"
    return "DIFFERENT"


def is_live_mark(status: str) -> bool:
    live_keywords = ["live", "registered", "pending", "published", "active"]
    return any(k in status.lower() for k in live_keywords)


def score_conflict(
    target_mark: str,
    target_classes: List[str],
    record: Dict,
) -> Dict:
    """
    Full conflict scoring for one record against the target mark.
    Returns the record with added scoring fields.
    """
    name_sim = phonetic_similarity(target_mark, record["mark"])
    class_rel = class_overlap(target_classes, record["int_class"])
    live = is_live_mark(record["status"])

    # Base score from name similarity
    score = name_sim

    # Class relationship modifier
    if class_rel == "SAME":
        score = min(100, score + 15)
    elif class_rel == "RELATED":
        score = min(100, score + 5)
    else:
        score = max(0, score - 10)

    # Live marks are more threatening
    if live:
        score = min(100, score + 5)
    else:
        score = max(0, score - 15)

    # Risk classification
    if score >= 80:
        risk = "HIGH"
        risk_color = "🔴"
    elif score >= 55:
        risk = "MODERATE"
        risk_color = "🟡"
    elif score >= 30:
        risk = "LOW"
        risk_color = "🟢"
    else:
        risk = "MINIMAL"
        risk_color = "⚪"

    record.update({
        "name_similarity": name_sim,
        "class_relationship": class_rel,
        "is_live": live,
        "conflict_score": score,
        "risk_level": risk,
        "risk_indicator": risk_color,
    })
    return record


def run_conflict_analysis(
    target_mark: str,
    target_classes: List[str],
    records: List[Dict],
    min_score: int = 20,
) -> List[Dict]:
    """Score all records and return sorted conflicts above threshold."""
    scored = []
    for raw in records:
        record = normalize_record(raw)
        scored_record = score_conflict(target_mark, target_classes, record)
        if scored_record["conflict_score"] >= min_score:
            scored.append(scored_record)
    return sorted(scored, key=lambda x: x["conflict_score"], reverse=True)


# ─────────────────────────────────────────────
# PDF REPORT GENERATION
# ─────────────────────────────────────────────
class JacksonTrimarkReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.set_fill_color(25, 50, 100)
        self.set_text_color(255, 255, 255)
        self.rect(0, 0, 210, 22, "F")
        self.set_xy(0, 5)
        self.cell(0, 12, "JACKSON TRIMARK PRO  |  Trademark Conflict Report", align="C")
        self.set_text_color(0, 0, 0)
        self.ln(18)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 10,
            f"Page {self.page_no()}  |  Preliminary screening only — not legal advice  |  Generated {datetime.now().strftime('%B %d, %Y')}",
            align="C",
        )

    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 12)
        self.set_fill_color(230, 235, 245)
        self.cell(0, 8, f"  {title}", ln=True, fill=True)
        self.ln(2)

    def kv_row(self, key: str, value: str):
        self.set_font("Helvetica", "B", 10)
        self.cell(50, 6, key, ln=False)
        self.set_font("Helvetica", "", 10)
        self.multi_cell(0, 6, str(value))

    def conflict_block(self, r: Dict, index: int):
        risk_colors = {
            "HIGH": (200, 50, 50),
            "MODERATE": (200, 140, 0),
            "LOW": (40, 150, 40),
            "MINIMAL": (150, 150, 150),
        }
        color = risk_colors.get(r["risk_level"], (100, 100, 100))
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*color)
        self.cell(
            0, 7,
            f"{index}. {r['mark']}  [{r['risk_level']} RISK — Score: {r['conflict_score']}/100]",
            ln=True,
        )
        self.set_text_color(0, 0, 0)
        self.set_font("Helvetica", "", 9)
        self.cell(0, 5, f"  Owner: {r['owner']}  |  Status: {r['status']}  |  Class: {r['int_class']}  |  Serial: {r['serial']}", ln=True)
        self.cell(0, 5, f"  Name Similarity: {r['name_similarity']}%  |  Class Relationship: {r['class_relationship']}  |  Live Mark: {'Yes' if r['is_live'] else 'No'}", ln=True)
        goods = str(r["goods_services"])
        if len(goods) > 250:
            goods = goods[:250] + "..."
        self.multi_cell(0, 5, f"  Goods/Services: {goods}")
        self.ln(3)


def generate_report(
    target_mark: str,
    target_classes: List[str],
    availability: Dict,
    db_status: Dict,
    conflicts: List[Dict],
    search_type: str,
) -> bytes:
    pdf = JacksonTrimarkReport()
    pdf.add_page()

    # Search Summary
    pdf.section_title("Search Summary")
    pdf.kv_row("Target Mark:", target_mark.upper())
    pdf.kv_row("Target Classes:", ", ".join(target_classes))
    pdf.kv_row("Search Type:", search_type.upper())
    pdf.kv_row("Search Date:", datetime.now().strftime("%B %d, %Y %I:%M %p"))
    db_date = db_status.get("last_update_date", "Unknown")
    pdf.kv_row("USPTO DB Updated:", str(db_date))
    pdf.ln(4)

    # Availability
    pdf.section_title("Quick Availability Check")
    avail = availability.get("available", availability.get("count", "N/A"))
    pdf.set_font("Helvetica", "", 10)
    if str(avail).lower() == "true" or avail == 0:
        pdf.set_text_color(40, 150, 40)
        pdf.cell(0, 7, "  ✓ No exact match found in USPTO database", ln=True)
    else:
        pdf.set_text_color(200, 50, 50)
        pdf.cell(0, 7, f"  ✗ Existing marks detected (count: {avail})", ln=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # Conflict summary
    high = sum(1 for r in conflicts if r["risk_level"] == "HIGH")
    mod = sum(1 for r in conflicts if r["risk_level"] == "MODERATE")
    low = sum(1 for r in conflicts if r["risk_level"] == "LOW")

    pdf.section_title("Conflict Summary")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"  Total potential conflicts found: {len(conflicts)}", ln=True)
    pdf.set_text_color(200, 50, 50)
    pdf.cell(0, 6, f"  HIGH risk: {high}", ln=True)
    pdf.set_text_color(200, 140, 0)
    pdf.cell(0, 6, f"  MODERATE risk: {mod}", ln=True)
    pdf.set_text_color(40, 150, 40)
    pdf.cell(0, 6, f"  LOW risk: {low}", ln=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # Conflict detail
    if conflicts:
        pdf.section_title("Conflict Detail (Top 20)")
        for i, r in enumerate(conflicts[:20], 1):
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.conflict_block(r, i)
    else:
        pdf.section_title("Conflict Detail")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, "  No conflicts found above scoring threshold.", ln=True)

    # Disclaimer
    pdf.add_page()
    pdf.section_title("Important Disclaimer")
    pdf.set_font("Helvetica", "", 9)
    disclaimer = (
        "This report is generated by Jackson Trimark Pro for preliminary knockout screening "
        "purposes only. It does not constitute legal advice and should not be relied upon as a "
        "complete trademark clearance opinion. A full clearance search requires analysis by a "
        "licensed trademark attorney or agent, review of common law marks, state registrations, "
        "domain names, and social media handles, as well as consideration of the specific goods "
        "and services involved.\n\n"
        "Results are drawn from the USPTO database via RapidAPI and may not reflect the most "
        "current filings. Similarity scoring is algorithmic and does not replicate the legal "
        "standard for likelihood of confusion under the Lanham Act.\n\n"
        "Consult a qualified intellectual property attorney before making any commercial use of "
        "a mark or filing a trademark application."
    )
    pdf.multi_cell(0, 6, disclaimer)

    return bytes(pdf.output())


# ─────────────────────────────────────────────
# STREAMLIT UI
# ─────────────────────────────────────────────
def main():
    st.set_page_config(
        page_title="Jackson Trimark Pro",
        page_icon="🛡️",
        layout="wide",
    )

    st.title("🛡️ Jackson Trimark Pro")
    st.caption("USPTO Trademark Conflict Search & Preliminary Knockout Screening")

    # API key check
    api_key = load_api_key()
    if not api_key:
        st.error(
            "API key not found. Add RAPIDAPI_KEY to `.streamlit/secrets.toml` or set it as an environment variable."
        )
        st.code('[secrets]\nRAPIDAPI_KEY = "your-rapidapi-key-here"', language="toml")
        st.stop()

    # Sidebar: search parameters
    with st.sidebar:
        st.header("🔍 Search Parameters")

        target_mark = st.text_input(
            "Brand / Trademark Name",
            placeholder="e.g. LegalEdge",
            help="Enter the mark you want to clear.",
        )

        target_classes = st.multiselect(
            "International Nice Classes",
            options=sorted(NICE_CLASSES.keys()),
            default=["042", "045"],
            format_func=lambda c: f"{c} — {NICE_CLASSES[c]}",
        )

        search_scope = st.radio(
            "Search Scope",
            ["Active marks only", "All marks (live + dead)"],
            index=0,
            help="Dead marks are lower risk but can be revived or cited.",
        )
        search_type = "active" if "Active" in search_scope else "all"

        min_score = st.slider(
            "Minimum Conflict Score to Display",
            min_value=10, max_value=60, value=25,
            help="Lower = more results. Raise to filter noise.",
        )

        include_related = st.checkbox(
            "Include related class search",
            value=True,
            help="Also searches classes commonly litigated alongside your selected classes.",
        )

        run_btn = st.button("🚀 Run Conflict Search", type="primary", use_container_width=True)

    # Main panel: instructions when idle
    if not run_btn:
        col1, col2, col3 = st.columns(3)
        with col1:
            st.info("**Step 1**\nEnter the trademark name you want to clear.")
        with col2:
            st.info("**Step 2**\nSelect the Nice classes covering your goods/services.")
        with col3:
            st.info("**Step 3**\nClick Run Conflict Search and review results.")

        st.markdown("---")
        st.subheader("Nice Class Reference")
        class_df = pd.DataFrame(
            [{"Class": k, "Category": v} for k, v in NICE_CLASSES.items()]
        )
        st.dataframe(class_df, use_container_width=True, height=400)
        return

    # Validation
    if not target_mark or len(target_mark.strip()) < 2:
        st.error("Please enter a trademark name of at least 2 characters.")
        return

    if not target_classes:
        st.error("Please select at least one Nice class.")
        return

    # Determine all classes to search
    search_classes = list(target_classes)
    if include_related:
        for tc in target_classes:
            for rc in RELATED_CLASSES.get(tc, []):
                if rc not in search_classes:
                    search_classes.append(rc)

    # Run search
    with st.spinner("Connecting to USPTO database via RapidAPI..."):
        try:
            db_status = check_database_status(api_key)
        except Exception:
            db_status = {}

    db_date = db_status.get("last_update_date", "Unknown")
    if db_date != "Unknown":
        st.caption(f"USPTO database last updated: {db_date}")

    with st.spinner(f"Searching USPTO for '{target_mark}'..."):
        try:
            availability = check_availability(target_mark, api_key)
        except Exception as e:
            availability = {}
            st.warning(f"Availability check failed: {e}")

        try:
            raw_results = search_trademarks(target_mark, search_type, api_key)
        except requests.HTTPError as e:
            st.error(f"USPTO API error: {e}")
            st.stop()
        except Exception as e:
            st.error(f"Search failed: {e}")
            st.stop()

    # Score conflicts
    conflicts = run_conflict_analysis(
        target_mark=target_mark,
        target_classes=search_classes,
        records=raw_results,
        min_score=min_score,
    )

    # ── Results display ──
    st.markdown("---")

    # Availability banner
    avail_val = availability.get("available", availability.get("count"))
    if str(avail_val).lower() == "true" or avail_val == 0:
        st.success("✅ Quick check: No exact match found in USPTO database.")
    elif avail_val is not None:
        st.error(f"⚠️ Quick check: Existing marks found (count: {avail_val}).")

    # Summary metrics
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Records Returned", len(raw_results))
    col2.metric("Conflicts Above Threshold", len(conflicts))
    col3.metric("🔴 HIGH", sum(1 for r in conflicts if r["risk_level"] == "HIGH"))
    col4.metric("🟡 MODERATE", sum(1 for r in conflicts if r["risk_level"] == "MODERATE"))
    col5.metric("🟢 LOW", sum(1 for r in conflicts if r["risk_level"] == "LOW"))

    if not conflicts:
        st.balloons()
        st.success(
            f"No conflicts found above a score of {min_score} for '{target_mark}'. "
            "This is a positive preliminary indicator — still recommend attorney review."
        )
    else:
        st.subheader(f"Conflict Results for: {target_mark.upper()}")

        # Color-coded risk table
        df = pd.DataFrame(conflicts)[
            ["risk_indicator", "risk_level", "conflict_score", "name_similarity",
             "class_relationship", "mark", "owner", "status", "int_class",
             "serial", "filing_date", "goods_services"]
        ].rename(columns={
            "risk_indicator": "",
            "risk_level": "Risk",
            "conflict_score": "Score",
            "name_similarity": "Name Sim %",
            "class_relationship": "Class Rel.",
            "mark": "Mark",
            "owner": "Owner",
            "status": "Status",
            "int_class": "Class",
            "serial": "Serial #",
            "filing_date": "Filed",
            "goods_services": "Goods/Services",
        })

        st.dataframe(df, use_container_width=True, height=450)

        # Expandable detail per conflict
        st.subheader("Conflict Detail")
        for r in conflicts[:15]:
            risk_emoji = r["risk_indicator"]
            with st.expander(f"{risk_emoji} {r['mark']} — {r['risk_level']} RISK (Score: {r['conflict_score']})"):
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"**Owner:** {r['owner']}")
                c1.markdown(f"**Status:** {r['status']}")
                c2.markdown(f"**Serial #:** {r['serial']}")
                c2.markdown(f"**Filed:** {r['filing_date']}")
                c3.markdown(f"**Class:** {r['int_class']} — {NICE_CLASSES.get(str(r['int_class']).zfill(3), 'Unknown')}")
                c3.markdown(f"**Registration:** {r['registration']}")
                st.markdown(f"**Goods/Services:** {r['goods_services']}")
                st.markdown(
                    f"Name Similarity: **{r['name_similarity']}%** | "
                    f"Class Relationship: **{r['class_relationship']}** | "
                    f"Live Mark: **{'Yes' if r['is_live'] else 'No'}**"
                )

    # PDF download
    st.markdown("---")
    with st.spinner("Generating report..."):
        pdf_bytes = generate_report(
            target_mark=target_mark,
            target_classes=target_classes,
            availability=availability,
            db_status=db_status,
            conflicts=conflicts,
            search_type=search_type,
        )

    st.download_button(
        label="📄 Download Jackson Trimark Report (PDF)",
        data=pdf_bytes,
        file_name=f"{target_mark.replace(' ', '_')}_trademark_report_{datetime.now().strftime('%Y%m%d')}.pdf",
        mime="application/pdf",
        use_container_width=True,
    )

    st.caption(
        "⚠️ Preliminary screening only. Not legal advice. "
        "Consult a licensed trademark attorney for full clearance."
    )


if __name__ == "__main__":
    main()
