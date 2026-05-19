import os
import requests
import streamlit as st
import pandas as pd
from rapidfuzz import fuzz

# =========================
# CONFIG
# =========================
USPTO_SEARCH_URL = "https://api.uspto.gov/api/v1/trademarks/search"

# Optional RapidAPI (only for class suggestion)
RAPIDAPI_URL = "https://uspto-trademark.p.rapidapi.com/v1/suggestClass"

# =========================
# HELPERS
# =========================

def normalize_text(text):
    return text.lower().strip()

def suggest_class(description):
    """Optional: Suggest Nice Class using RapidAPI"""
    try:
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-RapidAPI-Key": st.secrets["RAPIDAPI_KEY"],
            "X-RapidAPI-Host": "uspto-trademark.p.rapidapi.com"
        }

        payload = {
            "description": description,
            "keyword": "TrademarkSearch"
        }

        response = requests.post(RAPIDAPI_URL, data=payload, headers=headers)

        if response.status_code == 200:
            return response.json()
        else:
            return {"error": response.text}

    except Exception as e:
        return {"error": str(e)}

def search_uspto(mark):
    """Search USPTO trademarks"""
    params = {
        "searchText": mark,
        "rows": 20,
        "start": 0
    }

    response = requests.get(USPTO_SEARCH_URL, params=params)

    if response.status_code != 200:
        return []

    data = response.json()

    results = []
    for item in data.get("trademarks", []):
        results.append({
            "mark": item.get("markLiteral", ""),
            "owner": item.get("ownerName", ""),
            "status": item.get("status", ""),
            "serial": item.get("serialNumber", "")
        })

    return results

def score_similarity(input_mark, results):
    """Add similarity scores"""
    scored = []

    for r in results:
        score = fuzz.token_sort_ratio(input_mark, r["mark"])
        r["similarity"] = score
        scored.append(r)

    return sorted(scored, key=lambda x: x["similarity"], reverse=True)

def risk_level(score):
    if score >= 85:
        return "HIGH"
    elif score >= 70:
        return "MODERATE"
    else:
        return "LOW"

# =========================
# UI
# =========================

st.title("⚖️ Jackson Trimark Pro")
st.subheader("Trademark Clearance & Risk Screening Engine")

mark = st.text_input("Trademark Name")
description = st.text_input("Goods/Services (e.g., t-shirts)")

if st.button("Run Clearance Search"):

    if not mark:
        st.warning("Enter a trademark name")
        st.stop()

    normalized_mark = normalize_text(mark)

    # 1. Suggest Class (optional)
    st.write("### Suggested Class")
    class_result = suggest_class(description)

    if "error" in class_result:
        st.write("Class suggestion unavailable")
    else:
        st.json(class_result)

    # 2. USPTO Search
    st.write("### USPTO Search Results")
    results = search_uspto(normalized_mark)

    if not results:
        st.warning("No results found or API issue")
        st.stop()

    # 3. Similarity Scoring
    scored_results = score_similarity(normalized_mark, results)

    df = pd.DataFrame(scored_results)

    df["risk"] = df["similarity"].apply(risk_level)

    st.dataframe(df)

    # 4. Summary Risk
    top_score = df.iloc[0]["similarity"]

    st.write("### Overall Risk Assessment")

    if top_score >= 85:
        st.error("⚠️ HIGH RISK of conflict")
    elif top_score >= 70:
        st.warning("⚠️ MODERATE RISK")
    else:
        st.success("✅ LOW RISK")
