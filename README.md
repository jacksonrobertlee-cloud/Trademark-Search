# 🛡️ Trimark Hybrid

**Trademark Risk & Application Prep System**  
Internal service delivery tool for Fiverr / Upwork trademark engagements.  
Not a product for resale. Not legal advice.

---

## What it does

Trimark Hybrid is a two-module Streamlit app:

**1. Trademark Search & Risk**  
Enter a mark name, goods/services description, and international class numbers. The app queries a local SQLite database loaded from USPTO bulk XML files, scores each candidate record using a hybrid engine (fuzzy string match + phonetic similarity + word overlap + class relationship + live/dead status), and returns a ranked risk report ready to deliver to a client.

**2. Application Prep**  
Structured data entry for USPTO TEAS-style filing packages. Captures owner information, mark details, goods/services, filing basis, additional statements, and signature. Exports a clean JSON file for use in filing preparation.

**3. Bulk Data Management**  
Load USPTO trademark bulk XML files (daily or annual) into a local SQLite database. No API key required for file-based loading.

---

## Scoring logic

| Component | Weight |
|---|---|
| Fuzzy string similarity (token sort ratio) | 0–100 base |
| Phonetic similarity — Double Metaphone, word-level | 0–20 bonus |
| Meaningful word overlap (stopwords excluded) | 0–15 bonus |
| Class relationship (SAME / RELATED / DIFFERENT) | −10 to +15 |
| Live/dead status (USPTO status code < 600 = live) | 0–20 bonus |

Raw scores are normalized to 0–100. Risk tiers: CRITICAL (88+), HIGH (72+), MODERATE (52+), LOW (32+), MINIMAL.

---

## Setup

### Requirements

- Python 3.11+
- pip

### Install

```bash
git clone https://github.com/YOUR_USERNAME/trimark-hybrid.git
cd trimark-hybrid
pip install -r requirements.txt
```

### Run

```bash
streamlit run trimark_full_app.py
```

Opens at `http://localhost:8501`.

---

## Loading USPTO bulk data

The app ships with 6 sample records for testing. To search real trademark data:

1. Go to **https://developer.uspto.gov/product/trademark-daily-xml-file-tdxf-applications**
2. Download any recent daily `.zip` file (5–30 MB, no account required)
3. Open the app → **Bulk Data Management** tab → upload the file
4. The parser extracts mark text, status codes, class numbers, goods/services, and owner from the USPTO XML format and loads them into a local `trimark_data.db` SQLite database

For broader coverage, also load annual backfiles from:  
**https://developer.uspto.gov/product/trademark-annual-xml-applications**

The database persists between sessions. Load once, search many times.

### USPTO XML elements used

| XML element | Field |
|---|---|
| `<mark-identification>` | Mark text |
| `<status-code>` | Live/dead (< 600 = live) |
| `<filing-date>` | Filing date |
| `<registration-date>` | Registration date |
| `<case-file-statement><text>` | Goods/services description |
| `<classification><primary-code>` | International class number |
| `<case-file-owner><party-name>` | Owner name |
| `<serial-number>` | Unique record identifier |

---

## File structure

```
trimark-hybrid/
├── trimark_full_app.py   ← main application
├── requirements.txt      ← Python dependencies
├── README.md             ← this file
└── .gitignore            ← excludes database and bulk data files
```

`trimark_data.db` is created automatically on first run in the project directory.  
It is excluded from version control — each installation maintains its own local database.

---

## Deliverables generated

| File | Format | Description |
|---|---|---|
| Clearance report | `.txt` | Formatted conflict results + filing considerations, ready to attach to a Fiverr/Upwork delivery |
| Application data package | `.json` | Structured TEAS filing data for all owner, mark, goods/services, and signature fields |
| Draft save | `.json` | Full session state for resuming application prep across sessions |

---

## Known limitations

- Search is token-based. Phonetic-only conflicts (e.g. "Fixx" vs "Ficks") may not surface unless a token match brings the record into the candidate set. A future version will add phonetic pre-indexing to the DB.
- Daily files are deltas, not full snapshots. Load multiple years of annual files for comprehensive coverage.
- Class relationship scoring uses class numbers only, not goods/services text analysis.
- This tool supports professional research. A complete clearance search also covers state registrations, common law marks, domain names, and social media handles.

---

## Legal notice

This tool is an internal workflow aid for paralegal service delivery. It does not constitute legal advice and does not create an attorney-client relationship. Results are only as current and complete as the USPTO data loaded into the local database. Users are responsible for verifying results against live USPTO records before advising clients or filing applications.
