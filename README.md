# Serper Pair Dashboard

Local + Render-ready dashboard to run pair-based Serper searches and download per-query CSV files.

## Included files

- `serper_dashboard.py`
- `serper_search.py`
- `config.py`
- `requirements.txt`
- `Procfile`

## Local run

1. Install dependencies:
   - `pip install -r requirements.txt`
2. Set env var:
   - `SERPER_API_KEY=...`
3. Start app:
   - `python serper_dashboard.py`
4. Open:
   - `http://127.0.0.1:5055`

## Render setup

- Root Directory: `ashutosh`
- Build Command: `pip install -r requirements.txt`
- Start Command: `python serper_dashboard.py` (or rely on `Procfile`)
- Environment variable:
  - `SERPER_API_KEY`

## Vendor email file

Associates upload a stakeholder CSV at `/vendor-file`. The app uses existing RapidAPI (`RAPIDAPI_KEY` / `RAPIDAPI_KEY2`) for profile/company fields, Seeqe graph Postgres (`POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`) for Vieu IDs (`PERS-…` / `COMP-…`), and Gmail SMTP (`SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`) to email `{UID}_vendor.csv` (plus rejects/QA sidecars). One RapidAPI job at a time, shared with the URN resolver and company employee count. Max 500 rows per upload. Graph misses stay blank — IDs are never invented.
