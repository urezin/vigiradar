"""
Email via Resend — digests + high-impact alerts.

Safe when RESEND_API_KEY is unset: emails are logged instead of sent, so the app
runs end-to-end without an email account. Sending sets a Reply-To so replies land
in the monitored inbox (info@vigi-eye.com, forwarded like Competrain).
"""
import logging

from .config import settings

log = logging.getLogger("vigiradar.email")


def send(to: str | list[str], subject: str, html: str, sender: str | None = None, reply_to: str | None = None) -> dict:
    recipients = [to] if isinstance(to, str) else to
    if not settings.email_enabled:
        log.info("[email:dev] to=%s subject=%s (not sent — no RESEND_API_KEY)", recipients, subject)
        return {"status": "logged", "to": recipients, "subject": subject}
    import resend
    resend.api_key = settings.resend_api_key
    payload = {"from": sender or settings.email_from, "to": recipients, "subject": subject, "html": html}
    if reply_to or settings.email_reply_to:
        payload["reply_to"] = reply_to or settings.email_reply_to
    try:
        result = resend.Emails.send(payload)
    except Exception as e:
        if not sender:
            raise
        # sender domain not verified (yet): keep its display name, use the verified default address
        import re as _re
        name = (_re.match(r"\s*([^<]+?)\s*<", sender) or [None, ""])[1] or "VigiEye"
        addr = (_re.search(r"<([^>]+)>", settings.email_from) or [None, settings.email_from])[1]
        log.warning("send as %s failed (%s); retrying as %s <%s>", sender, e, name, addr)
        payload["from"] = f"{name} <{addr}>"
        result = resend.Emails.send(payload)
    return {"status": "sent", "id": result.get("id"), "to": recipients}
