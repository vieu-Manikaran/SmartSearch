# Molster (MoltSets) Email Enrichment

How we fetch business emails from LinkedIn profile URLs using the MoltSets API, and what the validation / “last fetched” fields mean.

## Endpoint

```
POST https://api.moltsets.com/api/v1/tools/linkedin_to_business_email
```

**Auth:** `Authorization: Bearer <MOLSTER_API_KEY>`  
Keys look like `ms_...` (stored as `MOLSTER_API_KEY` in `.env`).

This endpoint returns **business (work) emails only** — no personal inboxes. For business-or-personal fallback, MoltSets also offers `linkedin_to_best_email`.

## Request

Single profile:

```json
{
  "linkedin_url": "https://www.linkedin.com/in/example"
}
```

Batch (up to **100** URLs per call):

```json
{
  "linkedin_urls": [
    "https://www.linkedin.com/in/person-one",
    "https://www.linkedin.com/in/person-two"
  ]
}
```

Optional:

| Field | Default | Meaning |
| --- | --- | --- |
| `use_external_tokens` | `true` | If the email isn’t in MoltSets’ own DB, allow a third-party waterfall (uses **external tokens**). Set `false` for database-only lookups. |

## Response shape (batch)

```json
{
  "results": [
    {
      "input": "https://www.linkedin.com/in/froderosand",
      "data": {
        "email": "frode.rosand@walmart.com",
        "last_validated_at": "2026-05-21",
        "risk_score": "A"
      },
      "status": "ok"
    },
    {
      "input": "https://www.linkedin.com/in/brianboetig",
      "data": {
        "email": null,
        "last_validated_at": null
      },
      "status": "not_found"
    }
  ],
  "metadata": {
    "total_requested": 100,
    "total_processed": 100,
    "total_with_data": 65,
    "total_without_data": 35,
    "total_errored": 0,
    "fair_use": {
      "records_remaining_5h": 934,
      "records_reset_5h": "2026-08-12T20:40:36Z",
      "records_remaining_1w": 4934,
      "records_reset_1w": "2026-08-19T15:40:36Z"
    }
  }
}
```

### Per-result fields

| Field | Description |
| --- | --- |
| `input` | LinkedIn URL (or slug) that was looked up |
| `status` | `ok` when an email is returned; `not_found` when no business email is available |
| `data.email` | Business email string, or `null` |
| `data.last_validated_at` | **Date the email was last validated as deliverable / in good standing** (YYYY-MM-DD). This is MoltSets’ “freshness” signal — treat it as **last validated / last confirmed**, not necessarily the calendar day we called the API |
| `data.risk_score` | Send-safety grade after validation (see below) |

### `last_validated_at` (last fetched / last validated)

- Returned only when an email is found.
- Format: date string, e.g. `"2026-05-21"`.
- Meaning: when MoltSets last confirmed the address (validation freshness), **not** the timestamp of your API call.
- Use it to decide whether to trust/send: a validation from last week is stronger than one from a year ago.
- On misses (`not_found`), both `email` and `last_validated_at` are `null`.

In our export CSVs we typically map this to a column such as:

- `molster_last_validated_at`, or  
- drop it when the consumer only wants a single `email` column.

### `risk_score`

| Grade | Meaning (MoltSets) |
| --- | --- |
| **A** | Validated deliverable |
| **B** | Known engagement, no bounce |
| **C** | Catch-all / unknowable |
| **D** | Hard invalid / bounce / complaint / trap |
| **F** | No signal |

Prefer **A** (and often **B**) for outbound; treat **C/D/F** carefully.

## Billing / limits (paid plan behavior we observed)

On the **$27/mo** plan:

- Core email hits from MoltSets’ own database do **not** burn phone/external tokens.
- Successful finds consume **fair-use records** (plan card: **1,000 records / 5 hours**; weekly headroom also returned in `metadata.fair_use`).
- Misses are free (no fair-use decrement in our runs).
- If `use_external_tokens` causes a third-party email fill, **external tokens** may be charged (separate from phone tokens).

Phone enrichment (`linkedin_to_mobile_phone`) is separate: **10 phone tokens per returned mobile**; not covered here.

## How we run it in this repo

Typical waterfall used for campaign lists:

1. **Molster first** — batch LinkedIn URLs via `linkedin_to_business_email`.
2. **FullEnrich fallback** — only for Molster misses (and rare rows with no LinkedIn URL, using name + company).

Example outputs from recent runs:

| File | Notes |
| --- | --- |
| `fullenrich_pending_molster_emails.csv` | Molster-only; includes `molster_email`, `molster_status`, `molster_risk_score`, `molster_last_validated_at` |
| `algocas_campaign_molster_fullenrich_emails.csv` | Molster → FullEnrich; final consumer file keeps original columns + a single `email` column |

Scripts / one-offs have lived under `scripts/` (e.g. `molster_enrich_ngv_first500.py`) and ad-hoc enrichment jobs; auth key: `MOLSTER_API_KEY`.

## Minimal curl example

```bash
curl --request POST \
  --url https://api.moltsets.com/api/v1/tools/linkedin_to_business_email \
  --header "Authorization: Bearer $MOLSTER_API_KEY" \
  --header 'Content-Type: application/json' \
  --data '{
    "linkedin_url": "https://www.linkedin.com/in/example"
  }'
```

## Official docs

- [LinkedIn to Business Email](https://developer.moltsets.com/api-reference/get-valid-emails/linkedin-to-business-email)
- [Understanding Email Risk Scores](https://developer.moltsets.com/moltsets-data/understanding-email-risk-scores) (related risk-score guidance)
