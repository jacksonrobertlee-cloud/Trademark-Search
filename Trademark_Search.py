import os
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from fpdf import FPDF
from rapidfuzz import fuzz

RAPIDAPI_HOST = "uspto-trademark.p.rapidapi.com"
RAPIDAPI_URL = f"https://{RAPIDAPI_HOST}/v1/suggestClass"

CLASS_MAP = {
    "t-shirt": "025",
    "shirt": "025",
    "hoodie": "025",
    "clothing": "025",
}

def load_api_key():
    if "RAPIDAPI_KEY" in st.secrets:
        return st.secrets["RAPIDAPI_KEY"].strip()

    env_key = os.getenv("RAPIDAPI_KEY", "").strip()
    if env_key:
        return env_key

    for filename in ("api.txt", "api.key"):
        path = Path(filename)
        if path.exists():
            return path.read_text().strip()

    return ""

def normalize(text):
    return text.lower().strip()

def get_class(description):
    desc = (description or "").lower()
    for k, v in CLASS_MAP.items():
        if k in desc:
            return v
    return "UNKNOWN"

def suggest_class(description, keyword="TrademarkSearch", serials=None, owner=""):
    api_key = load_api_key()
    if not api_key:
        raise ValueError("Missing RapidAPI key")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": api_key,
    }

    data = {
        "description": description,
        "keyword": keyword,
        "serials": serials or "[]",
        "owner": owner,
    }

    response = requests.post(RAPIDAPI_URL, headers=headers, data=data, timeout=30)
    response.raise_for_status()
    return response.json()

def fetch_trademark_search(term):
    return {
        "mark": term,
        "similarity": fuzz.ratio(term.lower(), term.lower()),
        "status": "N/A",
    }

def generate_pdf(mark, suggested_class, api_result):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, txt="Trademark Class Suggestion Report", ln=True)
    pdf.cell(0, 10, txt=f"Mark: {mark}", ln=True)
    pdf.cell(0, 10, txt=f"Suggested Class: {suggested_class}", ln=True)
    pdf.ln(6)

    pdf.multi_cell(0, 8, txt="RapidAPI Response:")
    pdf.set_font("Arial", size=10)
    pdf.multi_cell(0, 6, txt=str(api_result))

    out = pdf.output(dest="S")
    if isinstance(out, str):
        return out.encode("latin-1")
    return out

def main():
    st.set_page_config(page_title="Jackson Trimark Pro", layout="wide")
    st.title("⚖️ Jackson Trimark Pro")
    st.caption("Trademark Class Suggestion Engine")

    api_key = load_api_key()
    if not api_key:
        st.error("Missing API key")
        st.stop()

    mark = st.text_input("Trademark Name")
    description = st.text_area("Goods/Services Description")
    keyword = st.text_input("Keyword", value="TrademarkSearch")
    owner = st.text_input("Owner", value="")
    serials_text = st.text_input("Serials (JSON list)", value="[]")

    if st.button("Run Search"):
        if not mark.strip():
            st.warning("Enter a trademark name.")
            st.stop()

        normalized_mark = normalize(mark)
        suggested_class = get_class(description)

        st.write(f"Suggested Class (local heuristic): {suggested_class}")

        with st.spinner("Calling RapidAPI..."):
            try:
                api_result = suggest_class(
                    description=description,
                    keyword=keyword,
                    serials=serials_text,
                    owner=owner,
                )
                st.subheader("RapidAPI Response")
                st.json(api_result)

                pdf_bytes = generate_pdf(normalized_mark, suggested_class, api_result)
                st.download_button(
                    "Download Report",
                    data=pdf_bytes,
                    file_name="trademark_report.pdf",
                    mime="application/pdf",
                )
            except requests.RequestException as e:
                st.error(f"API request failed: {e}")
            except ValueError as e:
                st.error(str(e))

if __name__ == "__main__":
    main()