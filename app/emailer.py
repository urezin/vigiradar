"""
Email via Resend — digests + high-impact alerts.

Safe when RESEND_API_KEY is unset: emails are logged instead of sent, so the app
runs end-to-end without an email account. Sending sets a Reply-To so replies land
in the monitored inbox (info@vigi-eye.com, forwarded like Competrain).
"""
import logging

from .config import settings

log = logging.getLogger("vigiradar.email")


def send(to: str | list[str], subject: str, html: str) -> dict:
    recipients = [to] if isinstance(to, str) else to
    if not settings.email_enabled:
        log.info("[email:dev] to=%s subject=%s (not sent — no RESEND_API_KEY)", recipients, subject)
        return {"status": "logged", "to": recipients, "subject": subject}
    import resend
    resend.api_key = settings.resend_api_key
    payload = {"from": settings.email_from, "to": recipients, "subject": subject, "html": html}
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to
    result = resend.Emails.send(payload)
    return {"status": "sent", "id": result.get("id"), "to": recipients}
