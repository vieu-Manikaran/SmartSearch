# Sending messages (and files) as Stakeholder Movement

Use this when **another service** should post into Slack as the **Stakeholder Movement** bot. The other service does **not** talk to Render or `python -m slack_intel_bot serve`. It calls Slack’s Web API with the same bot token.

Socket Mode (`SLACK_APP_TOKEN` / `xapp-…`) is only for *incoming* events (mentions, slash commands, buttons). Outbound posts need only the bot user token.

## What you need

| Item | Where it lives | Notes |
|------|----------------|--------|
| Bot token `xoxb-…` | Render env `SLACK_BOT_TOKEN` (same as this bot) | Give this to the other service as a secret. Never commit it. |
| Channel ID | You will pass this later | Looks like `C…` (public/private channel) or `G…` (some private groups) |
| Bot membership | Slack UI | Invite **Stakeholder Movement** to that channel first |

The other service should read these from env, for example:

```
SLACK_BOT_TOKEN=xoxb-…
SLACK_CHANNEL_ID=C0A7XQEJLNP   # set when you have the channel
```

## One-time Slack setup

1. Invite the bot to the destination channel: `/invite @Stakeholder Movement`
2. Confirm the bot token has **`chat:write`** (already granted for this app).
3. If you will **upload a file** (CSV, PDF, etc.), add bot scope **`files:write`** at [api.slack.com/apps](https://api.slack.com/apps) → the Stakeholder Movement app → **OAuth & Permissions** → Bot Token Scopes → reinstall the app to the workspace. Copy the token again if Slack rotated it.

Without `files:write`, text messages still work; file upload returns `missing_scope`.

### Finding the channel ID

In Slack: open the channel → channel name at the top → **View channel details** → scroll to the bottom → **Channel ID**. Or right-click the channel → **Copy link**; the ID is the last path segment (`C…`).

## Post a text message

`POST https://slack.com/api/chat.postMessage`

```python
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
channel = os.environ["SLACK_CHANNEL_ID"]

try:
    resp = client.chat_postMessage(
        channel=channel,
        text="Stakeholder file is ready.",
    )
    print(resp["ts"])  # message timestamp; use as thread_ts for replies
except SlackApiError as e:
    print(e.response["error"])
```

curl:

```bash
curl -sS -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"channel\":\"$SLACK_CHANNEL_ID\",\"text\":\"Stakeholder file is ready.\"}"
```

Optional: Block Kit (`blocks=[...]`) for richer layout. Keep a fallback `text=` for notifications.

## Upload a file into the channel

This is the path for “service created a file → send it to Slack.”

Use `files_upload_v2` (wraps Slack’s current upload flow). `files.upload` is deprecated.

```python
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
channel = os.environ["SLACK_CHANNEL_ID"]
path = "/path/to/generated.csv"  # file your other service just wrote

try:
    resp = client.files_upload_v2(
        channel=channel,
        file=path,
        filename="stakeholders.csv",
        title="Stakeholder export",
        initial_comment="New stakeholder file.",  # appears as the message text
    )
except SlackApiError as e:
    print(e.response["error"])
```

From bytes (no temp file needed):

```python
client.files_upload_v2(
    channel=channel,
    content=csv_bytes,           # bytes
    filename="stakeholders.csv",
    initial_comment="New stakeholder file.",
)
```

curl equivalent is two steps (`files.getUploadURLExternal` then `files.completeUploadExternal`). Prefer `slack_sdk` in Python.

## Message then file in a thread

Post a parent, then attach the file as a reply:

```python
parent = client.chat_postMessage(
    channel=channel,
    text="Stakeholder export is ready.",
)
client.files_upload_v2(
    channel=channel,
    file=path,
    filename="stakeholders.csv",
    thread_ts=parent["ts"],
)
```

## Limits and constraints

- The bot **must already be in the channel**. Posting to a channel it has not been invited to fails with `not_in_channel`.
- Channel ID is required; channel *names* (`#general`) are unreliable for bots. Pass `C…`.
- Slack message `text` max is ~40,000 characters. Large CSVs should be **files**, not pasted into `text`.
- File upload: typical workspace limit is **1 GB** per file (workspace settings can be lower). Prefer CSV/TSV over huge Excel dumps.
- Rate limit: ~1 message/second per channel in practice; back off on `ratelimited`.
- The running Render worker is unrelated. Two processes can both post with the same `xoxb-` token.

## Common errors

| Slack `error` | Meaning | Fix |
|---------------|---------|-----|
| `not_in_channel` | Bot is not a member | `/invite @Stakeholder Movement` in that channel |
| `channel_not_found` | Bad ID, or bot cannot see a private channel | Recheck ID; invite the bot |
| `missing_scope` | Token lacks `chat:write` or `files:write` | Add scope, reinstall app, refresh token |
| `invalid_auth` / `token_revoked` | Wrong or rotated token | Copy current `xoxb-` from OAuth & Permissions |
| `msg_too_long` | Body too large | Upload a file instead of inlining |
| `ratelimited` | Too many posts | Retry after `Retry-After` |

Success JSON has `"ok": true`. Always check that (or catch `SlackApiError`).

## Wiring it into the other service

1. Add `SLACK_BOT_TOKEN` (same value as Stakeholder Movement on Render) and `SLACK_CHANNEL_ID` (later) to that service’s secrets.
2. After the file is written, call `files_upload_v2` (or `chat_postMessage` if it is only text).
3. Do not call this repo’s HTTP health URL or Socket Mode process — there is no internal “send message” HTTP API on the bot today.
4. If you later want a shared helper in *this* repo, it would be a thin `WebClient(token=...).chat_postMessage / files_upload_v2` wrapper; the other service can copy the snippets above until then.

Python dependency: `slack-sdk>=3.33.0` (already in `slack_intel_bot/requirements.txt`).
