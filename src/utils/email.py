# Intentions: sending email through Resend (resend.com), a hosted sending service: one HTTP call per email,
# over the shared pool (utils/http). Generic -- what to send, and to whom, is the caller's business.
# Deliverability rests on the sending domain: it must be verified in Resend (its SPF/DKIM records in our DNS),
# or mail is refused or lands in spam.
# References: src/config (resend_api_key + email_from), https://resend.com/docs/api-reference/emails/send-email.

from src.config import settings
from src.constants import EMAIL_REPLY_TO, RESEND_API_URL
from src.utils import http


class EmailNotSent(Exception):
    """Resend refused the email (bad key, unverified domain, rate limit...) or there's no sender configured."""


async def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> str:
    """Send one email from settings.email_from (replies go to EMAIL_REPLY_TO) and return Resend's id for it. Raises
    EmailNotSent if no sender is configured -- an email with no From is a config bug, not something to skip quietly --
    or Resend says no. [I/O]"""
    if not settings.email_from or not settings.resend_api_key:
        raise EmailNotSent("no email sender configured; set EMAIL_FROM and RESEND_API_KEY")
    email = {"from": settings.email_from, "to": [to], "reply_to": EMAIL_REPLY_TO, "subject": subject, "text": text_body}
    status, body, _headers = await http.request(
        "POST",
        f"{RESEND_API_URL}/emails",
        json={**email, "html": html_body} if html_body else email,
        headers={"Authorization": f"Bearer {settings.resend_api_key}"},
    )
    if status >= 300:
        raise EmailNotSent(f"Resend answered {status}: {body.get('message') or body}")
    return body["id"]
