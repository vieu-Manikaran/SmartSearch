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

- Root Directory: leave blank — this folder is the root of the `SmartSearch` repo
- Build Command: `pip install -r requirements.txt`
- Start Command: `python serper_dashboard.py` (or rely on `Procfile`)
- Environment variables:
  - `SERPER_API_KEY`
  - `SLACK_BOT_TOKEN` (bot user token `xoxb-…`)
  - `SLACK_CHANNEL_ID` (channel ID `C…`, bot must already be a member)
  - `RAPIDAPI_KEY` (and optional `RAPIDAPI_KEY2` / `RAPIDAPI_KEY3`)

## Daily HubSpot CRO posts (12:00 AM IST)

Do **not** hang this on a laptop `launchd`/cron job. That only fires if the Mac is awake. Add a **Render Cron Job** on the same repo so it runs every day even when nobody is logged in.

Render cron schedules are **UTC**. 12:00 AM India Standard Time is **18:30 UTC the previous calendar day**.

1. Render Dashboard → New → Cron Job
2. Same repo / branch as the web service, Root Directory blank
3. Build Command: `pip install -r requirements.txt`
4. Command: `python hubspot_cro_daily_posts.py`
5. Schedule: `30 18 * * *`
6. Copy env vars from the web service: `RAPIDAPI_KEY` (plus `RAPIDAPI_KEY2`/`RAPIDAPI_KEY3` if used), `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`

The job reads `data/hubspot_cro_outreach/people_days_since_last_post.csv`, fetches `/profile_updates` for each person, writes last-IST-calendar-day posts in the same columns as `posts_last_30_days.csv`, and uploads that CSV to the existing Slack channel (or a text-only “nobody posted” message). Vendor files stay email-only.

The watchlist CSV and both job modules must be **committed and pushed** — the cron container only has what is in the branch, and nothing is read from your laptop.

Local dry run:

```bash
python hubspot_cro_daily_posts.py --no-slack --limit 3
```

## Vendor email file

Associates upload a stakeholder CSV at `/vendor-file`. Seeqe product contacts are checked first: email-only rows with an existing email are omitted from the vendor file and returned in `{UID}_existing_emails.csv`. If a phone is also needed, the row remains in the vendor file with email disabled. The remaining rows produce the 27-column `{UID}_vendor.csv`. Email attaches the vendor file and non-empty existing-email, reject, and not-in-graph sidecars. QA is written to disk but not emailed. Slack (`SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`) is used for the daily HubSpot CRO last-24-hours posts digest, not vendor files.

- RapidAPI (`/vendor-file`, UID `VEN-…`): RapidAPI fills names, titles, websites, current company, and current headcount. Names use cleaned RapidAPI profile fields when they match the associate's input identity, with graph/input fallback. Location/country prefer graph `person.loc` / `loc_country_code`, then RapidAPI. Vieu IDs and historical headcount (`company_history_employee_ct`) come from Postgres. People not in graph are omitted from the vendor file and listed in `{UID}_not_in_graph.csv` for ingest.
- Graph (`/vendor-file-graph`, UID `VNG-…`): all columns from Postgres (`POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`). Website comes from `company.email_domains`. Last Profile Refresh Date is `MAX(experience.updated_at)`. Historical headcount is `company_history_employee_ct` for the target start year (19xx/20xx only). If they have a present (non-board) role at the target, current company is the target only. If they have left the target, board/advisor present roles are skipped when another present employer exists; if board/advisor is the only current role, it is kept.

Before a RapidAPI vendor file is sent, duplicate profiles are removed, names are cleaned from the
RapidAPI profile, and target websites prefer curated or graph email-domain data over junk links.
Initial-only surnames, profile/name mismatches, non-employers, missing corporate websites, and
companies with fewer than five requested profiles are held in `{UID}_qa_hold.csv`. Current-company
differences remain in the vendor file and are not a hold condition.

One RapidAPI-lock job at a time (URN resolver, company employee count, and both vendor workflows share the lock). Graph misses stay blank — IDs are never invented.
