"""Send result CSVs via SMTP (e.g. Gmail app password)."""

from __future__ import annotations

import logging
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


def smtp_configured() -> bool:
    return bool(settings.smtp_user and settings.smtp_password and settings.smtp_from)


def send_results_email(
    to_email: str,
    subject: str,
    body: str,
    attachment_path: Path,
) -> tuple[bool, str | None]:
    """Attach CSV and send. Returns (ok, error_message)."""
    if not smtp_configured():
        return False, "Email is not configured (SMTP_USER, SMTP_PASSWORD, SMTP_FROM)."

    if not attachment_path.is_file():
        return False, f"Result file not found: {attachment_path.name}"

    from_addr = settings.smtp_from or settings.smtp_user
    host = settings.smtp_host or "smtp.gmail.com"
    port = int(settings.smtp_port or 587)

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with attachment_path.open("rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{attachment_path.name}"')
    msg.attach(part)

    try:
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(from_addr, [to_email], msg.as_string())
        logger.info("Results email sent to %s (%s)", to_email, attachment_path.name)
        return True, None
    except smtplib.SMTPException as exc:
        logger.exception("SMTP failed sending to %s", to_email)
        return False, str(exc)
    except OSError as exc:
        logger.exception("Network error sending email to %s", to_email)
        return False, str(exc)
