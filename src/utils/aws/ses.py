# Intentions: sending email through AWS SES. Same shape as s3.py: boto3 is SYNCHRONOUS, so the send runs in
# a thread (a bare call would block the event loop), and the client is built lazily so importing this
# needs no AWS credentials. Generic -- what to send, and to whom, is the caller's business.
# References: src/utils/aws/s3.py (the pattern), src/config (aws_* + email_from).

import asyncio

import boto3

from src.config import settings

_client = None


def client():
    """The shared boto3 SES client, built on first use. Credentials work like s3.client: explicit keys if
    set, else boto3's default chain (instance profile / task role)."""
    global _client
    if _client is None:
        credentials = (
            {
                "aws_access_key_id": settings.aws_access_key_id,
                "aws_secret_access_key": settings.aws_secret_access_key,
            }
            if settings.aws_access_key_id and settings.aws_secret_access_key
            else {}
        )
        _client = boto3.client("ses", region_name=settings.aws_region, **credentials)
    return _client


async def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    """Send one email from settings.email_from. Raises if no sender is configured -- an email with no
    From is a config bug, not something to skip quietly. The sender (or its domain) must be verified in
    SES, and while the SES account is in sandbox mode, so must every recipient. [I/O]"""
    if not settings.email_from:
        raise RuntimeError("no email sender configured; set EMAIL_FROM")
    body = {"Text": {"Data": text_body, "Charset": "UTF-8"}}
    if html_body:
        body = {**body, "Html": {"Data": html_body, "Charset": "UTF-8"}}
    await asyncio.to_thread(
        client().send_email,
        Source=settings.email_from,
        Destination={"ToAddresses": [to]},
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body},
    )
