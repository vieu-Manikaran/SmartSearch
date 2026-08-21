"""Post vendor-file CSVs to Slack as the Stakeholder Movement bot."""

from __future__ import annotations

import argparse
import logging
import ssl
from pathlib import Path

import certifi
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import settings

logger = logging.getLogger(__name__)


def slack_configured() -> bool:
    return bool(settings.slack_bot_token and settings.slack_channel_id)


def _client() -> WebClient:
    ctx = ssl.create_default_context(cafile=certifi.where())
    return WebClient(token=settings.slack_bot_token, ssl=ctx)


def upload_file(
    path: Path,
    *,
    initial_comment: str,
    filename: str | None = None,
    title: str | None = None,
) -> tuple[bool, str | None]:
    """Upload a local file to SLACK_CHANNEL_ID. Returns (ok, error)."""
    if not slack_configured():
        return False, "Slack is not configured (SLACK_BOT_TOKEN, SLACK_CHANNEL_ID)."
    if not path.is_file():
        return False, f"File not found: {path}"

    client = _client()
    name = filename or path.name
    try:
        resp = client.files_upload_v2(
            channel=settings.slack_channel_id,
            file=str(path),
            filename=name,
            title=title or name,
            initial_comment=initial_comment,
        )
    except SlackApiError as exc:
        err = (exc.response or {}).get("error") or str(exc)
        logger.error("Slack file upload failed: %s", err)
        return False, str(err)
    except OSError as exc:
        logger.exception("Slack file upload network error")
        return False, str(exc)

    if not resp.get("ok"):
        err = resp.get("error") or "Slack upload returned ok=false"
        logger.error("Slack file upload failed: %s", err)
        return False, str(err)

    logger.info("Slack file uploaded: %s", name)
    return True, None


def post_vendor_file(path: Path, *, email: str, summary: str) -> tuple[bool, str | None]:
    """Post the vendor CSV to Slack. No-op if Slack env is missing."""
    if not slack_configured():
        logger.warning("Slack not configured; skipped vendor file post")
        return True, None
    uid = path.name.replace("_vendor.csv", "")
    return upload_file(
        path,
        initial_comment=(
            f"Vendor file ready — `{uid}`\n"
            f"Requested by {email}\n\n"
            f"{summary}"
        ),
        filename=path.name,
        title=path.name,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Upload a test CSV to Slack.")
    parser.add_argument(
        "--file",
        type=Path,
        help="CSV to upload. If omitted, a tiny probe file is created.",
    )
    args = parser.parse_args()

    if args.file is not None:
        path = args.file
        comment = f"Vendor file Slack probe — uploaded `{path.name}`."
    else:
        path = Path("/tmp/vendor_file_slack_probe.csv")
        path.write_text(
            "UID,Stakeholder Name,note\n"
            "PROBE,Slack test,Ignore this file — vendor Slack hook probe.\n",
            encoding="utf-8",
        )
        comment = (
            "Vendor file Slack probe (ignore). "
            "If you see this CSV, `files:write` and channel invite are working."
        )

    ok, err = upload_file(
        path,
        initial_comment=comment,
        filename=path.name,
        title="Vendor file Slack probe",
    )
    if ok:
        print(f"Uploaded {path.name} to Slack channel {settings.slack_channel_id}.")
        return 0
    print(f"Slack upload failed: {err}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
