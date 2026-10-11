# Intentions: S3 uploads. boto3 is SYNCHRONOUS, so every call here goes through asyncio.to_thread --
# a bare boto3 call would block the event loop for the whole upload, stalling every other request in
# the process. That wrapper is the entire reason this module exists rather than callers using boto3
# directly.
#
# The client is built lazily (not at import) so the module can be imported without AWS credentials
# configured -- nothing pays for S3 unless something actually uploads. Credentials come from settings
# when set, else boto3's default chain (instance profile / task role), which is what you want on AWS
# compute. References: src/config (aws_* + s3_bucket).

import asyncio

import boto3

from src.config import settings

_client = None


def client():
    """The shared boto3 S3 client, built on first use. boto3 clients are thread-safe, so one instance
    serves every to_thread call."""
    global _client
    if _client is None:
        # Empty keys -> pass neither, and boto3 falls back to its default credential chain.
        credentials = (
            {
                "aws_access_key_id": settings.aws_access_key_id,
                "aws_secret_access_key": settings.aws_secret_access_key,
            }
            if settings.aws_access_key_id and settings.aws_secret_access_key
            else {}
        )
        _client = boto3.client("s3", region_name=settings.aws_region, **credentials)
    return _client


async def upload(
    data: bytes,
    key: str,
    *,
    bucket: str | None = None,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload `data` under `key` and return the key. `bucket` defaults to settings.s3_bucket. Raises if
    no bucket is resolvable -- an upload with nowhere to go is a config bug, not something to paper
    over. [I/O]"""
    target_bucket = bucket or settings.s3_bucket
    if not target_bucket:
        raise RuntimeError("no s3 bucket configured; set S3_BUCKET or pass bucket=")
    await asyncio.to_thread(
        client().put_object,
        Bucket=target_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key
