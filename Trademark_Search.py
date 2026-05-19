import os
import json
import time
import socket
import requests
import pandas as pd
import streamlit as st
from fpdf import FPDF
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from rapidfuzz import fuzz

# ── API ────────────────────────────────────────────────────────────────────────
API_BASE_URL = "https://api.uspto.gov/api/v1/trademarks/search"

# ── Related class map ──────────────────────────────────────────────────────────
RELATED_CLASSES = {
    "009": ["042", "035", "038"],
    "042": ["009", "035", "038"],
    "041": ["035", "016", "028"],
    "035": ["009", "042", "041", "036"],
    "025": ["035", "018"],
    "036": ["035", "042"],
}

# ── All 16 USPTO refusal types ─────────────────────────────────────────────────
REFUSAL_CHECKS = [
    {
        "id": "2d",
        "name": "Likelihood of Confusion – §2(d)",
        "severity": "HIGH",
        "description": (
            "The mark is similar in sound, appearance, or meaning to a registered mark, "
            "and the goods/services are related enough to cause consumer confusion."
        ),
        "trigger": lambda mark, goods, classes, results: any(r["score"] > 60 for r in results),
    },
    {
        "id": "2e1",
        "name": "Merely Descriptive – §2(e)(1)",
        "severity": "MODERATE",
        "description": (
            "The mark immediately describes an ingredient, quality, feature, purpose, or use "
            "of the goods/services. Descriptive marks may qualify for the Supplemental Register "
            "or gain registration after 5+ years of acquired distinctiveness."
        ),
        "trigger": lambda mark, goods, classes, results: _is_descriptive(mark, goods),
    },
    {
        "id": "generic",
        "name": "Generic Refusal",
        "severity": "CRITICAL",
        "description": (
            "The mark is the common everyday name for the goods/services themselves. "
            "Generic marks can never be registered on any register."
        ),
        "trigger": lambda mark, goods, classes, results: _is_generic(mark, goods),
    },
    {
        "id": "ornament",
        "name": "Ornamentality / Failure to Function – §2(e)",
        "severity": "MODERATE",
        "description": (
            "The USPTO may view the mark as decorative rather than a source identifier, "
            "especially if displayed prominently on apparel. Fix: use mark on neck labels, "
            "hangtags, or point-of-sale displays."
        ),
        "trigger": lambda mark, goods, classes, results: "025" in classes,
    },
    {
        "id": "2e4",
        "name": "Primarily Merely a Surname – §2(e)(4)",
        "severity": "MODERATE",
        "description": (
            "If the primary significance of the mark to the public is a last name, "
            "registration will be refused absent proof of secondary meaning."
        ),
        "trigger": lambda mark, goods, classes, results: _is_surname(mark),
    },
    {
        "id": "2e2",
        "name": "Primarily Geographically Descriptive – §2(e)(2)",
        "severity": "MODERATE",
        "description": (
            "The mark names a well-known geographic location and the goods/services "
            "actually originate from that location."
        ),
        "trigger": lambda mark, goods, classes, results: _is_geographic(mark),
    },
    {
        "id": "2e3",
        "name": "Geographically Deceptively Misdescriptive – §2(e)(3)",
        "severity": "HIGH",
        "description": (
            "The mark names a place famous for specific goods, but the product does not "
            "actually originate there, creating a misleading geographic impression."
        ),
        "trigger": lambda mark, goods, classes, results: False,  # Requires human review
    },
    {
        "id": "2a_deceptive",
        "name": "Deceptive / Deceptively Misdescriptive – §2(a)",
        "severity": "HIGH",
        "description": (
            "The mark includes a term that plausibly misdescribes a characteristic "
            "of the goods in a way that could deceive consumers."
        ),
        "trigger": lambda mark, goods, classes, results: False,  # Requires human review
    },
    {
        "id": "functional",
        "name": "Functionality – §2(e)(5)",
        "severity": "MODERATE",
        "description": (
            "Applies primarily to trade dress. If a design feature is essential to the use "
            "or purpose of the product, it cannot be trademarked (belongs in patent law)."
        ),
        "trigger": lambda mark, goods, classes, results: False,  # Trade dress only
    },
    {
        "id": "informational",
        "name": "Informational / Widely-Used Common Phrase",
        "severity": "MODERATE",
        "description": (
            "Marks consisting entirely of common slogans or social movement phrases "
            "(e.g., 'Be Kind', 'Drive Safely') fail to function as brand identifiers."
        ),
        "trigger": lambda mark, goods, classes, results: _is_common_phrase(mark),
    },
    {
        "id": "2a_false",
        "name": "False Suggestion of Connection / Insignia – §§2(a) & 2(b)",
        "severity": "HIGH",
        "description": (
            "Cannot register a mark that falsely suggests a connection with institutions, "
            "persons, beliefs, or national symbols/insignia."
        ),
        "trigger": lambda mark, goods, classes, results: _suggests_institution(mark),
    },
    {
        "id": "2c",
        "name": "Living Individual's Name / Portrait – §2(c)",
        "severity": "HIGH",
        "description": (
            "A mark identifying a specific living person requires that person's explicit "
            "written consent in the application."
        ),
        "trigger": lambda mark, goods, classes, results: False,  # Requires human review
    },
    {
        "id": "specimen",
        "name": "Specimen Refusal (Defective Use in Commerce)",
        "severity": "LOW",
        "description": (
            "Specimens must show real-world use: no mockups, website screenshots must "
            "include price/cart, and mark must exactly match the application text."
        ),
        "trigger": lambda mark, goods, classes, results: True,  # Always flag as reminder
    },
    {
        "id": "id_goods",
        "name": "Vague Identification of Goods/Services",
        "severity": "LOW",
        "description": (
            "The USPTO requires precise ID Manual language. Overly broad or vague "
            "descriptions will be rejected and must use USPTO-approved wording."
        ),
        "trigger": lambda mark, goods, classes, results: len(goods.split()) < 4 if goods else True,
    },
    {
        "id": "disclaimer",
        "name": "Disclaimer Requirement – §6(a)",
        "severity": "LOW",
        "description": (
            "If the mark contains generic or descriptive words alongside unique elements, "
            "the USPTO will require a disclaimer of exclusive rights to those words."
        ),
        "trigger": lambda mark, goods, classes, results: _needs_disclaimer(mark),
    },
    {
        "id": "ownership",
        "name": "Ownership / Entity Mistake",
        "severity": "LOW",
        "description": (
            "The application must be filed by the true owner. Filing under a personal "
            "name when an LLC owns the mark can void the application."
        ),
        "trigger": lambda mark, goods, classes, results: True,  # Always flag as reminder
    },
    {
        "id": "phantom",
        "name": "Phantom Mark (More Than One Mark)",
        "severity": "LOW",
        "description": (
            "Applications with changeable/blank elements (e.g., 'XYZ [Year]') are refused "
            "because each application may only cover one single, static mark."
        ),
        "trigger": lambda mark, goods, classes, results: _is_phantom(mark),
    },
]

# ── Refusal helper functions ───────────────────────────────────────────────────
DESCRIPTIVE_SUFFIXES = [
    "pro", "plus", "express", "direct", "online", "digital", "smart",
    "fast", "quick", "easy", "best", "top", "fresh", "clean", "clear",
]

COMMON_PHRASES = [
    "be kind", "drive safely", "stay strong", "proud parent", "live laugh love",
    "work hard", "play hard", "no pain no gain", "just do it",
]

INSTITUTION_KEYWORDS = [
    "university", "college", "institute", "school", "academy", "hbcu",
    "federal", "national", "american", "united states", "us government",
]

SURNAME_INDICATORS = []  # Populated dynamically

GEOGRAPHIC_TERMS = [
    "paris", "london", "new york", "tokyo", "milan", "nashville", "chicago",
    "texas", "california", "florida", "new england", "southern", "western",
]


def _is_descriptive(mark: str, goods: str) -> bool:
    mark_lower = mark.lower()
    if any(mark_lower.endswith(s) or f" {s}" in mark_lower for s in DESCRIPTIVE_SUFFIXES):
        return True
    if goods:
        goods_words = set(goods.lower().split())
        mark_words = set(mark_lower.split())
        if len(goods_words & mark_words) > 0:
            return True
    return False


def _is_generic(mark: str, goods: str) -> bool:
    if not goods:
        return False
    goods_lower = goods.lower()
    mark_lower = mark.lower()
    core_words = [w for w in goods_lower.split() if len(w) > 3]
    return any(w in mark_lower for w in core_words)


def _is_surname(mark: str) -> bool:
    words = mark.split()
    if len(words) == 1:
        # Single-word marks ending in common surname patterns
        endings = ["son", "man", "berg", "stein", "ski", "sky", "ez", "oz"]
        return any(mark.lower().endswith(e) for e in endings)
    return False


def _is_geographic(mark: str) -> bool:
    mark_lower = mark.lower()
    return any(geo in mark_lower for geo in GEOGRAPHIC_TERMS)


def _is_common_phrase(mark: str) -> bool:
    mark_lower = mark.lower().strip()
    return any(phrase in mark_lower for phrase in COMMON_PHRASES)


def _suggests_institution(mark: str) -> bool:
    mark_lower = mark.lower()
    return any(kw in mark_lower for kw in INSTITUTION_KEYWORDS)


def _needs_disclaimer(mark: str) -> bool:
    words = mark.upper().split()
    generic_words = [
        "INC", "LLC", "CO", "CORP", "GROUP", "SERVICES", "SOLUTIONS",
        "TECH", "TECHNOLOGIES", "SYSTEMS", "CONSULTING", "GLOBAL",
        "INTERNATIONAL", "NETWORK", "NETWORKS", "MEDIA", "DIGITAL",
        "ONLINE", "DIRECT", "EXPRESS", "PLUS", "PRO",
    ]
    return any(w in generic_words for w in words) and len(words) > 1


def _is_phantom(mark: str) -> bool:
    import re
    return bool(re.search(r"\[|\]|\{|\}|__+|\*\*", mark))


# ── API key ────────────────────────────────────────────────────────────────────
def load_api_key():
    if "USPTO_API_KEY" in st.secrets:
        return st.secrets["USPTO_API_KEY"].strip()
    env_key = os.getenv("USPTO_API_KEY", "").strip()
    if env_key:
        return env_key
    for filename in ("api.txt", "api.key"):
        path = Path(filename)
        if path.exists():
            return path.read_text().strip()
    return ""


# ── USPTO search ───────────────────────────────────────────────────────────────
def search_uspto_api(markname: str, api_key: str = "", rows: int = 100):
    """
    Search the USPTO Trademark Search API (api.uspto.gov).

    Requires an ODP API key from data.uspto.gov that has the
    Trademark Search product enabled (separate from patent products).

    How to enable:
      1. Log in at data.uspto.gov/myodp
      2. Go to My Apps -> your app -> Edit -> Add Product
      3. Add "Trademark Search" and save
      4. Use the same API key — it will now work for trademarks
    """
    query = f'markLiteralText:"{markname}" OR markLiteralText:*{markname}*'
    params = {"q": query, "rows": rows, "start": 0}
    headers = {
        "Accept": "application/json",
        "USPTO-API-KEY": api_key,
    }

    try:
        response = requests.get(API_BASE_URL, params=params, headers=headers, timeout=30)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 403:
            raise ValueError(
                "403 Forbidden — Your ODP API key is not enabled for the Trademark Search product.\n\n"
                "Fix (takes 2 minutes):\n"
                "1. Go to https://data.uspto.gov/myodp\n"
                "2. Click your app name → Edit\n"
                "3. Under Products, add \"Trademark Search\"\n"
                "4. Save — your existing key will now work for trademarks\n\n"
                "Your current key is only provisioned for Patent File Wrapper / Meta Data products."
            )
        elif response.status_code == 401:
            raise ValueError(
                "401 Unauthorized — Invalid or missing API key.\n"
                "Enter your ODP key from data.uspto.gov/myodp in the sidebar."
            )
        elif response.status_code == 429:
            raise ValueError(
                "429 Too Many Requests — Rate limit exceeded. Please wait a few minutes."
            )
        else:
            response.raise_for_status()
    except ValueError:
        raise
    except requests.exceptions.Timeout:
        raise ValueError("Request timed out. The USPTO API may be slow — please try again.")
    except requests.exceptions.ConnectionError:
        raise ValueError("Cannot reach the USPTO API. Check your internet connection.")
    except Exception as e:
        raise ValueError(f"USPTO API error: {e}")


def normalize_payload(payload: Dict):
    results = payload.get("results", [])
    normalized = []
    for item in results:
        classes = item.get("internationalClasses", [])
        class_text = (
            ", ".join(str(c).zfill(3) for c in classes)
            if isinstance(classes, list)
            else str(classes).zfill(3)
        )
        normalized.append({
            "mark": item.get("markLiteralText", ""),
            "goods": item.get("goodsServices", ""),
            "class": class_text,
            "status": item.get("status", ""),
            "owner": item.get("ownerName", ""),
        })
    return normalized


# ── Risk scoring ───────────────────────────────────────────────────────────────
def calculate_goods_similarity(user_goods: str, existing_goods: str) -> float:
    if not user_goods or not existing_goods:
        return 0
    return fuzz.partial_ratio(user_goods.lower(), existing_goods.lower())


def calculate_risk(target_mark, target_classes, results, goods_desc):
    analyzed = []
    for item in results:
        class_tokens = [c.strip() for c in item["class"].split(",") if c.strip()]
        name_score   = min(fuzz.token_sort_ratio(target_mark.upper(), item["mark"].upper()), 40)
        class_score  = 0
        if any(c in target_classes for c in class_tokens):
            class_score = 30
        elif any(any(c in RELATED_CLASSES.get(tc, []) for c in class_tokens) for tc in target_classes):
            class_score = 15
        goods_score   = calculate_goods_similarity(goods_desc, item["goods"]) * 0.20
        status_score  = 10 if "LIVE" in item["status"].upper() else 0
        total_score   = name_score + class_score + goods_score + status_score

        risk = (
            "CRITICAL" if total_score > 80
            else "HIGH"     if total_score > 60
            else "MODERATE" if total_score > 40
            else "LOW"
        )

        item.update({
            "score":            round(total_score, 1),
            "risk":             risk,
            "name_similarity":  name_score,
            "goods_similarity": round(goods_score, 1),
            "class_score":      class_score,
            "status_weight":    status_score,
        })
        if total_score > 30:
            analyzed.append(item)

    return sorted(analyzed, key=lambda x: x["score"], reverse=True)


# ── Refusal analysis ───────────────────────────────────────────────────────────
def analyze_refusals(mark: str, goods: str, classes: List[str], results: List[Dict]):
    flagged = []
    for check in REFUSAL_CHECKS:
        try:
            if check["trigger"](mark, goods, classes, results):
                flagged.append({
                    "id":          check["id"],
                    "name":        check["name"],
                    "severity":    check["severity"],
                    "description": check["description"],
                })
        except Exception:
            pass
    return flagged


# ── Domain name checks (RDAP – free, no key) ──────────────────────────────────
TLDS = [".com", ".net", ".org", ".io", ".co", ".app", ".law"]

def check_domain(domain: str) -> Dict:
    """Use RDAP to check domain registration status."""
    try:
        resp = requests.get(
            f"https://rdap.verisign.com/com/v1/domain/{domain}",
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", [])
            return {"domain": domain, "registered": True, "status": ", ".join(status)}
        elif resp.status_code == 404:
            return {"domain": domain, "registered": False, "status": "Available"}
        else:
            # Try generic RDAP
            resp2 = requests.get(
                f"https://rdap.org/domain/{domain}",
                timeout=8,
            )
            if resp2.status_code == 200:
                return {"domain": domain, "registered": True, "status": "Registered"}
            return {"domain": domain, "registered": False, "status": "Available"}
    except Exception:
        return {"domain": domain, "registered": None, "status": "Check failed"}


def search_domains(mark: str) -> List[Dict]:
    base = mark.lower().replace(" ", "").replace("-", "")
    base_hyphen = mark.lower().replace(" ", "-")
    domains_to_check = []
    for tld in TLDS:
        domains_to_check.append(base + tld)
    for tld in [".com", ".net", ".org"]:
        domains_to_check.append(base_hyphen + tld)

    results = []
    for domain in domains_to_check:
        result = check_domain(domain)
        results.append(result)
        time.sleep(0.1)  # Be polite to RDAP servers
    return results


# ── Business name search (OpenCorporates) ─────────────────────────────────────
def search_business_names(mark: str) -> List[Dict]:
    """Search OpenCorporates for business name conflicts (free tier)."""
    try:
        resp = requests.get(
            "https://api.opencorporates.com/v0.4/companies/search",
            params={"q": mark, "jurisdiction_code": "us", "per_page": 20},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        companies = data.get("results", {}).get("companies", [])
        out = []
        for c in companies:
            co = c.get("company", {})
            name = co.get("name", "")
            score = fuzz.token_sort_ratio(mark.upper(), name.upper())
            if score > 40:
                out.append({
                    "name":         name,
                    "jurisdiction": co.get("jurisdiction_code", "").upper(),
                    "status":       co.get("current_status", ""),
                    "incorporated": co.get("incorporation_date", ""),
                    "similarity":   score,
                    "url":          co.get("opencorporates_url", ""),
                })
        return sorted(out, key=lambda x: x["similarity"], reverse=True)
    except Exception:
        return []


# ── Common law / web presence (Google Custom Search) ──────────────────────────
def search_common_law(mark: str, google_api_key: str = "", google_cx: str = "") -> List[Dict]:
    """
    Search for common law use of the mark on the web.
    Requires Google Custom Search API key + CX (Search Engine ID).
    Falls back gracefully if not configured.
    """
    if not google_api_key or not google_cx:
        return []
    try:
        resp = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": google_api_key,
                "cx":  google_cx,
                "q":   f'"{mark}" trademark',
                "num": 10,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
        return [
            {
                "title":   item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "url":     item.get("link", ""),
            }
            for item in items
        ]
    except Exception:
        return []


# ── PDF report ─────────────────────────────────────────────────────────────────
class PDFReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "Trademark Risk Assessment Report", ln=True, align="C")
        self.set_font("Helvetica", "", 10)
        self.cell(0, 8, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=True, align="C")
        self.ln(4)

    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 12)
        self.set_fill_color(230, 230, 230)
        self.cell(0, 8, f"  {title}", ln=True, fill=True)
        self.ln(2)

    def body_line(self, text: str, bold: bool = False):
        self.set_font("Helvetica", "B" if bold else "", 10)
        self.multi_cell(0, 6, text)


def generate_pdf(mark, classes, goods, trademark_results, refusals, domains, businesses):
    pdf = PDFReport()
    pdf.add_page()

    # Header summary
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 9, f"Mark: {mark}", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"Classes: {', '.join(classes)}   |   Goods/Services: {goods[:80] if goods else 'N/A'}", ln=True)
    pdf.ln(3)

    # Overall recommendation
    if any(r["risk"] == "CRITICAL" for r in trademark_results):
        rec = "DO NOT PROCEED"
    elif any(r["risk"] == "HIGH" for r in trademark_results):
        rec = "PROCEED WITH CAUTION"
    elif trademark_results:
        rec = "LOW-MODERATE RISK"
    else:
        rec = "LOW RISK — No Significant Conflicts Found"
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 9, f"Overall Recommendation: {rec}", ln=True)
    pdf.ln(4)

    # USPTO conflicts
    pdf.section_title("I. USPTO Trademark Conflicts")
    if trademark_results:
        for r in trademark_results[:15]:
            pdf.body_line(
                f"  [{r['risk']}] {r['mark']}  |  Score: {r['score']}  |  "
                f"Class: {r['class']}  |  Owner: {r['owner'][:40]}"
            )
    else:
        pdf.body_line("  No significant USPTO conflicts identified.")
    pdf.ln(3)

    # Refusal analysis
    pdf.section_title("II. Potential USPTO Refusal Grounds")
    if refusals:
        for ref in refusals:
            pdf.body_line(f"  [{ref['severity']}] {ref['name']}", bold=True)
            pdf.body_line(f"    {ref['description'][:200]}")
            pdf.ln(1)
    else:
        pdf.body_line("  No automatic refusal triggers identified.")
    pdf.ln(3)

    # Domain names
    pdf.section_title("III. Domain Name Availability")
    if domains:
        for d in domains:
            status_icon = "✗ TAKEN" if d["registered"] else ("✓ AVAILABLE" if d["registered"] is False else "? UNKNOWN")
            pdf.body_line(f"  {d['domain']:35s}  {status_icon}  {d.get('status','')}")
    else:
        pdf.body_line("  Domain search not performed.")
    pdf.ln(3)

    # Business names
    pdf.section_title("IV. Business Name Conflicts (OpenCorporates)")
    if businesses:
        for b in businesses[:10]:
            pdf.body_line(
                f"  [{b['similarity']}%] {b['name'][:45]}  |  "
                f"{b['jurisdiction']}  |  {b['status']}"
            )
    else:
        pdf.body_line("  No significant business name conflicts identified.")
    pdf.ln(3)

    # Search limitations
    pdf.section_title("V. Search Limitations")
    pdf.body_line(
        "  This report is generated by automated tools and does not constitute legal advice. "
        "Databases may contain errors or omissions. New applications or uses may arise after "
        "the search date. Common law uses may not be fully discoverable via automated search. "
        "Consult a licensed trademark attorney before filing."
    )

    out = pdf.output(dest="S")
    if isinstance(out, str):
        out = out.encode("latin-1")
    return out


# ── Streamlit UI ───────────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="Jackson Trimark Pro", layout="wide", page_icon="⚖️")

    # ── Custom CSS ──
    st.markdown("""
    <style>
      .risk-critical { color: #c0392b; font-weight: bold; }
      .risk-high     { color: #e67e22; font-weight: bold; }
      .risk-moderate { color: #f1c40f; font-weight: bold; }
      .risk-low      { color: #27ae60; font-weight: bold; }
      .section-header {
        background: #1a1a2e; color: #e0e0e0; padding: 8px 14px;
        border-radius: 4px; font-weight: 600; margin: 16px 0 8px 0;
      }
      .refusal-card {
        border-left: 4px solid #3498db; padding: 8px 12px;
        margin: 6px 0; background: #f8f9fa; border-radius: 0 4px 4px 0;
      }
    </style>
    """, unsafe_allow_html=True)

    st.title("⚖️ Jackson Trimark Pro")
    st.caption("Comprehensive Trademark Clearance & Risk Screening Engine")

    # ── Sidebar config ──
    with st.sidebar:
        st.header("Configuration")
        api_key = load_api_key()
        if not api_key:
            api_key = st.text_input("ODP API Key", type="password",
                                     help="Your key from data.uspto.gov/myodp")
            st.caption(
                "⚠️ Your ODP key must have the **Trademark Search** product enabled. "
                "Log in at data.uspto.gov/myodp → your app → Edit → Add Products → "
                "Trademark Search."
            )
        else:
            st.success("✅ ODP API key loaded")
            st.caption(
                "Ensure your app at data.uspto.gov/myodp has the "
                "**Trademark Search** product enabled — not just patents."
            )

        st.divider()
        st.subheader("Optional: Common Law Search")
        st.caption("Requires Google Custom Search API credentials")
        google_api_key = st.text_input("Google API Key", type="password",
                                        help="Google Custom Search API key")
        google_cx      = st.text_input("Google CX (Search Engine ID)",
                                        help="Custom Search Engine ID from Google")

        st.divider()
        st.subheader("Search Options")
        run_domains   = st.checkbox("Search Domain Names", value=True)
        run_biznames  = st.checkbox("Search Business Names (OpenCorporates)", value=True)
        run_commonlaw = st.checkbox("Search Common Law / Web Presence", value=bool(google_api_key and google_cx))

    # ── Main inputs ──
    col1, col2 = st.columns([2, 1])
    with col1:
        mark = st.text_input("Trademark Name", placeholder="e.g. THE ELITE 6 HBCU LAW SCHOOLS")
        goods = st.text_area("Goods/Services Description",
                              placeholder="e.g. Educational services, namely law school rankings and related publications",
                              height=100)
    with col2:
        classes = st.multiselect(
            "International Classes",
            ["009", "016", "025", "035", "036", "038", "041", "042", "045"],
            default=["041"],
            help="Select all relevant classes for your goods/services",
        )

    run_btn = st.button("🔍 Run Full Clearance Search", type="primary", use_container_width=True)

    if not run_btn:
        # Show refusal reference guide while waiting
        with st.expander("📋 USPTO Refusal Reference Guide", expanded=False):
            for check in REFUSAL_CHECKS:
                color = {
                    "CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡", "LOW": "🔵"
                }.get(check["severity"], "⚪")
                st.markdown(f"**{color} {check['name']}**")
                st.caption(check["description"])
                st.divider()
        return

    # ── Validation ──
    if not mark.strip():
        st.warning("Please enter a trademark name.")
        return
    if not api_key:
        st.error("Please enter your ODP API key in the sidebar.")
        return
    if not classes:
        st.warning("Please select at least one international class.")
        return

    # ── Run searches ──
    trademark_results = []
    refusals          = []
    domain_results    = []
    biz_results       = []
    common_law        = []

    progress = st.progress(0, text="Starting search...")

    # 1 — USPTO
    with st.spinner("Searching USPTO trademark database..."):
        try:
            payload           = search_uspto_api(mark, api_key)
            normalized        = normalize_payload(payload)
            trademark_results = calculate_risk(mark, classes, normalized, goods)
            progress.progress(30, text="USPTO search complete")
        except ValueError as e:
            progress.empty()
            st.error(str(e))
            st.info(
                "💡 **Get a free USPTO API key:** Visit https://developer.uspto.gov, "
                "create an account, and register an app for the *Trademark Search* product. "
                "Keys are typically issued instantly."
            )
            return
        except Exception as e:
            progress.empty()
            st.error(f"Unexpected error during USPTO search: {e}")
            return

    # 2 — Refusal analysis
    refusals = analyze_refusals(mark, goods, classes, trademark_results)
    progress.progress(45, text="Refusal analysis complete")

    # 3 — Domain names
    if run_domains:
        with st.spinner("Checking domain name availability..."):
            domain_results = search_domains(mark)
        progress.progress(65, text="Domain check complete")

    # 4 — Business names
    if run_biznames:
        with st.spinner("Searching business name registrations..."):
            biz_results = search_business_names(mark)
        progress.progress(80, text="Business name search complete")

    # 5 — Common law
    if run_commonlaw and google_api_key and google_cx:
        with st.spinner("Searching for common law / web use..."):
            common_law = search_common_law(mark, google_api_key, google_cx)
        progress.progress(95, text="Common law search complete")

    progress.progress(100, text="Analysis complete!")
    time.sleep(0.5)
    progress.empty()

    # ── Results Tabs ──
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "⚠️ USPTO Conflicts",
        "🚫 Refusal Analysis",
        "🌐 Domain Names",
        "🏢 Business Names",
        "📜 Common Law / Web",
    ])

    # Tab 1 — USPTO
    with tab1:
        st.markdown(f'<div class="section-header">USPTO Trademark Conflicts — {len(trademark_results)} results above threshold</div>', unsafe_allow_html=True)
        if trademark_results:
            df = pd.DataFrame(trademark_results)[
                ["mark", "risk", "score", "class", "status", "owner",
                 "name_similarity", "goods_similarity", "class_score"]
            ]
            st.dataframe(df, use_container_width=True, height=400)

            # Risk summary
            risk_counts = pd.Series([r["risk"] for r in trademark_results]).value_counts()
            cols = st.columns(4)
            for i, level in enumerate(["CRITICAL", "HIGH", "MODERATE", "LOW"]):
                cols[i].metric(level, risk_counts.get(level, 0))
        else:
            st.success("✅ No significant USPTO conflicts found.")

    # Tab 2 — Refusals
    with tab2:
        st.markdown('<div class="section-header">Potential USPTO Refusal Grounds</div>', unsafe_allow_html=True)
        st.caption("These are automated flags based on the mark and application details. Not legal advice.")
        if refusals:
            severity_order = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "LOW": 3}
            refusals_sorted = sorted(refusals, key=lambda x: severity_order.get(x["severity"], 4))
            for ref in refusals_sorted:
                icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MODERATE": "🟡", "LOW": "🔵"}.get(ref["severity"], "⚪")
                with st.expander(f"{icon} [{ref['severity']}] {ref['name']}", expanded=ref["severity"] in ("CRITICAL", "HIGH")):
                    st.write(ref["description"])
        else:
            st.success("✅ No automatic refusal triggers identified. Manual review still recommended.")

    # Tab 3 — Domains
    with tab3:
        st.markdown('<div class="section-header">Domain Name Availability</div>', unsafe_allow_html=True)
        if domain_results:
            for d in domain_results:
                col_a, col_b, col_c = st.columns([3, 2, 3])
                col_a.write(f"**{d['domain']}**")
                if d["registered"] is True:
                    col_b.error("TAKEN")
                elif d["registered"] is False:
                    col_b.success("AVAILABLE")
                else:
                    col_b.warning("UNKNOWN")
                col_c.caption(d.get("status", ""))
        elif not run_domains:
            st.info("Domain search was not enabled.")
        else:
            st.info("No domain results returned.")

    # Tab 4 — Business names
    with tab4:
        st.markdown('<div class="section-header">Business Name Conflicts (OpenCorporates)</div>', unsafe_allow_html=True)
        if biz_results:
            df_biz = pd.DataFrame(biz_results)[
                ["name", "similarity", "jurisdiction", "status", "incorporated", "url"]
            ]
            st.dataframe(df_biz, use_container_width=True)
        elif not run_biznames:
            st.info("Business name search was not enabled.")
        else:
            st.success("✅ No significant business name conflicts found.")

    # Tab 5 — Common law
    with tab5:
        st.markdown('<div class="section-header">Common Law / Web Presence</div>', unsafe_allow_html=True)
        if common_law:
            for item in common_law:
                st.markdown(f"**[{item['title']}]({item['url']})**")
                st.caption(item["snippet"])
                st.divider()
        elif not run_commonlaw:
            st.info(
                "Common law search requires Google Custom Search API credentials. "
                "Add your Google API Key and CX in the sidebar to enable this feature."
            )
        else:
            st.success("✅ No significant common law web presence found.")

    # ── PDF download ──
    st.divider()
    with st.spinner("Generating PDF report..."):
        try:
            pdf_bytes = generate_pdf(
                mark, classes, goods,
                trademark_results, refusals,
                domain_results, biz_results,
            )
            st.download_button(
                label="📄 Download Full PDF Report",
                data=pdf_bytes,
                file_name=f"trademark_report_{mark.replace(' ', '_')[:30]}_{datetime.now().strftime('%Y%m%d')}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"PDF generation error: {e}")


if __name__ == "__main__":
    main()
