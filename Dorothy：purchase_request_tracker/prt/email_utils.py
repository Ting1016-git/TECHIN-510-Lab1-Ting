from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .db import get_email_settings

logger = logging.getLogger(__name__)


def _send_smtp_message(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    sender_name: str,
    to_email: str,
    subject: str,
    inner_html: str,
) -> bool:
    em = (to_email or "").strip()
    if not em:
        return True
    user = (smtp_user or "").strip()
    pwd = (smtp_password or "").strip()
    if not user or not pwd:
        logger.info("SMTP send skipped: missing user or password.")
        return False
    host = (smtp_host or "").strip() or "smtp.gmail.com"
    port = int(smtp_port or 587)
    full_html = wrap_email_html(inner_html)
    sn = (sender_name or "").strip()
    from_addr = f"{sn} <{user}>" if sn else user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = em
    msg.set_content("This message requires an HTML-capable email client.")
    msg.add_alternative(full_html, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=45) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(user, pwd)
            smtp.send_message(msg)
        logger.info("Email sent successfully to %s (subject: %s)", em, subject[:80])
        return True
    except Exception as e:
        logger.warning("Email send failed to %s: %s", em, e, exc_info=True)
        return False


def wrap_email_html(inner_html: str) -> str:
    """Simple HTML layout: UW purple header, white body, standard footer."""
    safe_inner = inner_html or ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Segoe UI,system-ui,sans-serif;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
  style="max-width:600px;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;">
<tr>
  <td style="background:#4b2e83;color:#ffffff;padding:18px 22px;font-size:18px;font-weight:700;">
    Purchase Request Tracker
  </td>
</tr>
<tr>
  <td style="padding:22px;color:#1f2937;font-size:15px;line-height:1.55;">
{safe_inner}
  </td>
</tr>
<tr>
  <td style="padding:14px 22px;background:#fafafa;color:#6b7280;font-size:12px;border-top:1px solid #e5e7eb;">
    University of Washington — Purchase Request Tracker
  </td>
</tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def send_notification(to_email: str, subject: str, body_html: str) -> bool:
    """
    Load SMTP settings from DB. If disabled or incomplete, return True (silent).
    If enabled, send via smtplib with STARTTLS. Log errors; return False on send failure.
    """
    em = (to_email or "").strip()
    if not em:
        return True

    settings = get_email_settings()
    if not settings.get("enabled"):
        return True

    host = (settings.get("smtp_host") or "").strip() or "smtp.gmail.com"
    port = int(settings.get("smtp_port") or 587)
    user = (settings.get("smtp_user") or "").strip()
    password = str(settings.get("smtp_password") or "").strip()
    sender_name = (settings.get("sender_name") or "").strip()

    if not user or not password:
        logger.info("Email notification skipped: SMTP user or app password not configured.")
        return True

    return _send_smtp_message(
        smtp_host=host,
        smtp_port=port,
        smtp_user=user,
        smtp_password=password,
        sender_name=sender_name,
        to_email=em,
        subject=subject,
        inner_html=body_html,
    )


def send_test_email(
    to_email: str,
    *,
    smtp_user: str,
    app_password: str,
    sender_name: str,
) -> bool:
    """
    Send a test message using the given credentials. Uses host/port from DB defaults.
    Does not require notifications to be enabled.
    """
    settings = get_email_settings()
    host = (settings.get("smtp_host") or "").strip() or "smtp.gmail.com"
    port = int(settings.get("smtp_port") or 587)
    pwd = (app_password or "").strip() or str(settings.get("smtp_password") or "").strip()
    inner = (
        "<p>This is a test message from the <strong>Purchase Request Tracker</strong> admin settings.</p>"
        "<p>If you received this email, your SMTP configuration is working.</p>"
    )
    return _send_smtp_message(
        smtp_host=host,
        smtp_port=port,
        smtp_user=smtp_user,
        smtp_password=pwd,
        sender_name=sender_name,
        to_email=to_email,
        subject="Purchase Request Tracker — test email",
        inner_html=inner,
    )
