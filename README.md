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

Associates upload a stakeholder CSV at `/vendor-file` (RapidAPI) or `/vendor-file-graph` (Seeqe Postgres only). Both emit the same 27-column `{UID}_vendor.csv` (plus rejects/QA sidecars) over Gmail SMTP.

- RapidAPI (`/vendor-file`, UID `VEN-…`): RapidAPI fills titles, websites, current company, and current headcount. Names are split from the associate CSV only. Location/country prefer graph `person.loc` / `loc_country_code`, then RapidAPI. Vieu IDs and historical headcount (`company_history_employee_ct`) come from Postgres. People not in graph stay without a Vieu ID and are listed in `{UID}_not_in_graph.csv` for ingest.
- Graph (`/vendor-file-graph`, UID `VNG-…`): all columns from Postgres (`POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`). Website comes from `company.email_domains`. Last Profile Refresh Date is `MAX(experience.updated_at)`. Historical headcount is `company_history_employee_ct` for the target start year (19xx/20xx only). If they have a present (non-board) role at the target, current company is the target only. If they have left the target, board/advisor present roles are skipped when another present employer exists; if board/advisor is the only current role, it is kept.

One RapidAPI-lock job at a time (URN resolver, company employee count, and both vendor workflows share the lock). Max 500 rows per upload. Graph misses stay blank — IDs are never invented.
