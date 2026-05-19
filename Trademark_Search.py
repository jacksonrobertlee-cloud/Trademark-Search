import os
from datetime import datetime
from typing import List, Dict
from pathlib import Path

import requests
import pandas as pd
import streamlit as st
from rapidfuzz import fuzz
from fpdf import FPDF

# --- CONFIG ---
API_BASE_URL = "https://api.uspto.gov/api/v1/trademarks/search"

RELATED_CLASSES = {
    "009": ["042", "035", "038"],
    "042": ["009", "035", "038"],
    "041": ["035", "016", "028"],
    "035": ["009", "042", "041", "036"],
    "025": ["035", "018"],
    "036": ["035", "042"],
}

# --- API KEY ---
def load_api_key():
    env_key = os.getenv("USPTO_API_KEY", "qpjkcadejalavcfabfwksdkisnoibl").strip()
    if env_key:
        return env_key

    for filename in ("api.txt", "api.key"):
        path = Path(filename)
        if path.exists():
            return path.read_text().strip()
    return ""

# --- USPTO SEARCH ---
def search_uspto_api(markname: str, api_key: str):
    query = f'markLiteralText:"{markname}" OR markLiteralText:*{markname}*'
    params = {"q": query}
    headers = {"USPTO-API-KEY": api_key}

    response = requests.get(API_BASE_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

# --- NORMALIZE ---
def normalize_payload(payload: Dict):
    results = payload.get("results", [])
    normalized = []

    for item in results:
        normalized.append({
            "mark": item.get("markLiteralText", ""),
            "goods": item.get("goodsServices", ""),
            "class": str(item.get("internationalClasses", "")).zfill(3),
            "status": item.get("status", ""),
            "owner": item.get("ownerName", ""),
        })
    return normalized

# --- GOODS SIMILARITY ---
def calculate_goods_similarity(user_goods: str, existing_goods: str):
    if not user_goods or not existing_goods:
        return 0

    return fuzz.partial_ratio(user_goods.lower(), existing_goods.lower())

# --- CORE RISK ENGINE ---
def calculate_risk(target_mark, target_classes, results, goods_desc):
    analyzed = []

    for item in results:
        name_score = fuzz.token_sort_ratio(target_mark.upper(), item["mark"].upper())

        # Class scoring
        class_score = 0
        if item["class"] in target_classes:
            class_score = 40
        elif any(item["class"] in RELATED_CLASSES.get(tc, []) for tc in target_classes):
            class_score = 20

        # Goods similarity
        goods_score = calculate_goods_similarity(goods_desc, item["goods"]) / 2  # scale

        # Status weighting
        status_score = 15 if "LIVE" in item["status"].upper() else 0

        total_score = name_score + class_score + goods_score + status_score

        # Risk classification
        if total_score > 100:
            risk = "CRITICAL"
        elif total_score > 80:
            risk = "HIGH"
        elif total_score > 60:
            risk = "MODERATE"
        else:
            risk = "LOW"

        item.update({
            "score": round(total_score, 1),
            "risk": risk,
            "name_similarity": name_score,
            "goods_similarity": round(goods_score, 1),
            "class_score": class_score,
            "status_weight": status_score
        })

        if total_score > 50:
            analyzed.append(item)

    return sorted(analyzed, key=lambda x: x["score"], reverse=True)

# --- PDF REPORT ---
class PDFReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 10, "Trademark Risk Assessment Report", ln=True, align="C")
        self.set_font("Helvetica", "", 10)
        self.cell(0, 8, f"Generated: {datetime.now().strftime('%Y-%m-%d')}", ln=True, align="C")
        self.ln(5)

def generate_pdf(mark, results):
    pdf = PDFReport()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 10, f"Mark: {mark}", ln=True)

    # Recommendation logic
    if any(r["risk"] == "CRITICAL" for r in results):
        recommendation = "DO NOT PROCEED"
    elif any(r["risk"] == "HIGH" for r in results):
        recommendation = "PROCEED WITH CAUTION"
    else:
        recommendation = "LOW RISK"

    pdf.cell(0, 10, f"Recommendation: {recommendation}", ln=True)

    pdf.ln(5)

    for r in results[:10]:
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 8, f"{r['mark']} | Risk: {r['risk']} | Score: {r['score']}")

    return bytes(pdf.output())

# --- STREAMLIT UI ---
def main():
    st.set_page_config(page_title="Jackson Trimark Pro", layout="wide")

    st.title("⚖️ Jackson Trimark Pro")
    st.caption("Trademark Clearance Screening Engine")

    api_key = load_api_key(qpjkcadejalavcfabfwksdkisnoibl)
    if not api_key:
        st.error("Missing API key")
        return

    mark = st.text_input("Trademark Name")
    goods = st.text_area("Goods/Services Description")

    classes = st.multiselect("Classes", ["009", "025", "035", "041", "042"], default=["042"])

    if st.button("Run Search"):
        with st.spinner("Analyzing..."):
            payload = search_uspto_api(mark, api_key)
            normalized = normalize_payload(payload)
            results = calculate_risk(mark, classes, normalized, goods)

            if results:
                df = pd.DataFrame(results)
                st.dataframe(df)

                pdf = generate_pdf(mark, results)
                st.download_button("Download Report", pdf, "report.pdf")
            else:
                st.success("No significant conflicts found.")

if __name__ == "__main__":
    main()