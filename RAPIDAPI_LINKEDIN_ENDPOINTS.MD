# RapidAPI LinkedIn integration

This project uses the **linkedin-data-scraper** API on RapidAPI (`linkedin-data-scraper.p.rapidapi.com`). All calls share the same host and header pattern:

| Header | Value |
|--------|--------|
| `x-rapidapi-key` | `RAPIDAPI_KEY` or `RAPIDAPI_KEY2` (see below) |
| `x-rapidapi-host` | `linkedin-data-scraper.p.rapidapi.com` |
| `Content-Type` | `application/json` |

Config lives in `config.py` and `.env`. See also `RAPIDAPI_PERSON_DEEP.md` for a deeper dive on person profile change detection.

---

## Two RapidAPI keys

### Environment variables

| Variable | Required? | Purpose |
|----------|-----------|---------|
| `RAPIDAPI_KEY` | **Yes** | Primary key. Every script fails without it. |
| `RAPIDAPI_KEY2` | No | Second subscription key for **parallel workers** on large batches. |

Defined in `config.py` as `rapidapi_key` and `rapidapi_key2`.

### How the second key is used

The second key is **not** a fallback when the first fails. Both keys hit the same API; we use them to **run two requests at once** and stay under per-key rate limits.

Scripts collect keys with the same pattern:

```python
def collect_rapidapi_keys() -> List[str]:
    seen, out = set(), []
    for raw in (settings.rapidapi_key, settings.rapidapi_key2):
        k = (raw or "").strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out
```

(`run_incode_connections_unmatched.py`, `fill_company_employee_size.py`, `enrich_xbow_target_accounts.py`, `run_honecomb_stakeholder_export.py`)

**Assignment:** requests are round-robined — `keys[i % len(keys)]`. Worker 1 uses key 1, worker 2 uses key 2.

**Concurrency is capped at the number of keys:**

- `run_incode_connections_unmatched.py` / `fill_unmatched_column.py`: `--concurrency` defaults to `len(keys)` (max 2 with two keys). Each async worker owns one key.
- `fill_company_employee_size.py`: `--concurrency` (default `2`) with `ThreadPoolExecutor`; intended for two keys.
- `run_honecomb_stakeholder_export.py`: fetches profiles in batches of `batch_size` (default 2), one key per concurrent `get_person_deep` call.
- `enrich_xbow_target_accounts.py`: up to 2 parallel `/company` workers when two keys are set.

**Single-key modules:** `rapidapi_linkedin_company.py`, `run_ciso_rest_slug_resolve.py`, and most one-off scripts only pass `settings.rapidapi_key`. They do not use `RAPIDAPI_KEY2` unless you extend them.

### When to set `RAPIDAPI_KEY2`

- Large `person_deep` batches (connections unmatched, stakeholder exports, fill-unmatched flows).
- Parallel `/company` enrichment (`fill_company_employee_size.py`, XBOW target accounts).

With only one key, set `--concurrency 1` or rely on the default cap — behavior is correct, just slower.

---

## Person endpoint: `POST /person_deep`

**URL:** `https://linkedin-data-scraper.p.rapidapi.com/person_deep`  
**Override:** `rapidapi_person_deep_url` in `config.py`  
**Implementation:** `rapidapi_person_deep.py` (`fetch_person_deep`, `get_person_deep`)

### Input

One LinkedIn **profile** URL per request:

```json
{"link": "https://www.linkedin.com/in/<slug-or-urn>"}
```

Accepted URL shapes in this project:

- Vanity slug: `https://www.linkedin.com/in/jane-doe/`
- URN-style member id (from Apollo / Sales Nav exports): `https://www.linkedin.com/in/ACwAAACH0QcBJJ6rWWPQOtkcPZ_uowmGGzval58/`

The code normalizes URLs via `linkedin_batch_profiles.normalize_linkedin_profile_url()`.

CSV columns we read profile URLs from (varies by script): `Linkedin Bio`, `li_url`, `Target LinkedIn`, `Profile LinkedIn URL`, `profileUrl`, `linked_in_url`, etc.

### What you get back

**Success (HTTP 200):** JSON with `success: true` and a `data` object. Async helper returns `{"success": True, "data": {...}}`.

Profile-level fields we use across the repo:

| Field | Use in this project |
|-------|---------------------|
| `publicIdentifier` | Public vanity slug (e.g. `jane-doe`) — used to build normal `/in/` URLs |
| `urn` | LinkedIn member URN (e.g. `ACoAAA...`) |
| `fullName`, `firstName`, `lastName` | Display / contact fill |
| `headline` | Current title line on profile |
| `about` | Profile summary |
| `addressWithCountry`, `addressWithoutCountry` | Location |
| `experiences` | Full employment history (see below) |

**Experience entries** (`experiences[]`) typically include:

- `companyId` — numeric LinkedIn org id (used to match employer)
- `companyLink1` / `companyUrl` — company page URL
- `title`, `subtitle` — role and company name (API shape varies; `person_experience.py` normalizes)
- `caption` — date range string (e.g. `Feb 2021 - Present`)
- `breakdown` + `subComponents` — nested roles under one company block

`person_experience.flatten_person_deep_response()` expands these into one row per role with parsed `startDate`, `endDate`, `isCurrent`, `monthsInRole`, `changeType`, etc.

**Errors:**

| Response | Meaning |
|----------|---------|
| 403 / 404 | Profile not found or not accessible → `{"success": False, "error": "profile_not_found"}` |
| 429 | Rate limited — retried with exponential backoff |
| 5xx / network | Retried; eventually `max_retries_exceeded` |

### What we do with it

| Flow | Script | Outcome |
|------|--------|---------|
| Job change / promotion detection | `run_stakeholder_changes.py` | Compare live profile vs checkpoint |
| Still at company? | `run_stakeholders_still_at_company.py` | Match current role to expected employer |
| Unmatched column | `run_incode_connections_unmatched.py`, `fill_unmatched_column.py` | Per-row company match |
| URN → vanity slug | `run_ciso_rest_slug_resolve.py` | `publicIdentifier` → `linkedin_url_resolved` |
| Full experience export | `run_csv_full_experience.py` | One CSV row per role |
| Stakeholder JSON bundles | `run_honecomb_stakeholder_export.py`, `fetch_profiles_experience_and_posts.py` | Rich JSON per person |

Raw responses are often saved under `output/<run>/person_deep_json/` for resume and audit.

---

## Company endpoint: `POST /company`

**URL:** `https://linkedin-data-scraper.p.rapidapi.com/company`  
**Override:** `rapidapi_company_url` in `config.py`  
**Implementation:** `rapidapi_linkedin_company.py`, plus inline callers in `fill_apollo_company_country.py`, `fill_company_employee_size.py`, `enrich_xbow_target_accounts.py`

### Input

One LinkedIn **company page** URL per request:

```json
{"link": "https://www.linkedin.com/company/<slug-or-numeric-id>"}
```

Examples:

- `https://www.linkedin.com/company/microsoft/`
- `https://www.linkedin.com/company/1035/` (numeric slug)

### What you get back

**Success:** top-level `{"success": true, "data": {...}}`. Helpers unwrap `data`.

Fields this project reads from `data`:

| Field | Use in this project |
|-------|---------------------|
| `companyId` | **Numeric LinkedIn organization id** — Sales Navigator, Search Jobs, matching person `experiences[].companyId` (`rapidapi_linkedin_company.fetch_linkedin_org_id_from_company_url`, `sync_target_accounts_sales_nav.py`) |
| `employeeCount` | Headcount integer → `Employee Count` column and derived size band (`fill_company_employee_size.py`) |
| `employeeCountRange` | Present in API but often stale; we prefer `employeeCount` for size bands |
| `headquarters` | Address object; country parsed from `city__State_PostalCode_Country` or similar (`fill_apollo_company_country.py`) |
| `locations` | Fallback list of office locations for country when HQ is missing |

`fetch_linkedin_company_raw()` returns the full JSON for scripts that need more than `companyId`.

**Errors:** 429 and 5xx are retried. 403/404 return failure without raising (`fetch_linkedin_company_raw`).

### What we do with it

| Flow | Script | Outcome |
|------|--------|---------|
| Resolve `org_id` for jobs search | `sync_target_accounts_sales_nav.py`, `fetch_company_ids_from_postgres.py` | `companyId` on target accounts |
| Fill HQ country | `fill_apollo_company_country.py` | Apollo `Company Country` from headquarters |
| Employee count / size band | `fill_company_employee_size.py` | `Employee Count`, `Company Size` columns |
| Target account enrichment | `enrich_xbow_target_accounts.py` | `LinkedIn Company ID` + employee fields |

Company responses are cached in JSON files (e.g. `data/company_employee_size_cache.json`) so reruns skip already-fetched URLs.

---

## Not the same as `/company`: `/company_jobs`

`POST /company_jobs` returns **open job postings** for a company, not company metadata.

| | `POST /company` | `POST /company_jobs` |
|--|-----------------|----------------------|
| Input | `{"link": "<company page url>"}` | `{"company_url", "starts_from", "count"}` |
| Output | Company page metadata (`companyId`, size, HQ, …) | Job listing pages |
| Module | `rapidapi_linkedin_company.py` | `rapidapi_company_job_postings.py` |

Documented in `config.py` and `stakeholder-calendar-signals/README.md`.

---

## Quick reference

| Item | Location |
|------|----------|
| Settings | `config.py` |
| Person API | `rapidapi_person_deep.py` |
| Company API | `rapidapi_linkedin_company.py` |
| Experience flattening | `person_experience.py` |
| Dual-key person batch (async) | `run_incode_connections_unmatched.py` |
| Dual-key company batch (threads) | `fill_company_employee_size.py` |
| Person deep details | `RAPIDAPI_PERSON_DEEP.md` |

### Minimal examples

**Person — resolve URN to vanity URL:**

```bash
# Input CSV column: li_url or Linkedin Bio
python3 run_ciso_rest_slug_resolve.py resolve-slugs \
  --input "/path/to/rows.csv" \
  --profile-column "li_url" \
  --output-dir output/my_run
# → linkedin_slug, linkedin_url_resolved columns + person_deep_json/
```

**Company — employee count with two keys:**

```bash
python3 fill_company_employee_size.py \
  --input accounts.csv \
  --url-column "Company LinkedIn URL" \
  --concurrency 2
# Requires RAPIDAPI_KEY and RAPIDAPI_KEY2 in .env
```

**Person — unmatched check with two keys:**

```bash
python3 fill_unmatched_column.py \
  --input-csv leads.csv \
  --concurrency 2
```
