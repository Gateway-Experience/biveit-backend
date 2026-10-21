# Intentions: secrets DERIVED from PROJECT_SECRET_SEED and a subject naming what they're for
# ("project:42:jwt_secret", "user:7:mongo") -- the same every time, different for every subject, so
# there's nothing to store and nothing to keep in sync.

import hashlib
import hmac
import itertools
import string

from src.config import settings

_ALNUM = string.ascii_letters + string.digits


def derive_secret(subject: str, length: int) -> str:
    """[Pure-ish] `length` letters and digits derived from PROJECT_SECRET_SEED and `subject`, first one
    always a letter (some tools choke on a leading digit). HMAC-SHA256 keyed by the seed makes the bytes,
    block after block; rejection sampling maps them onto the alphabet without modulo bias."""
    if not settings.project_secret_seed:
        raise ValueError("PROJECT_SECRET_SEED isn't set")
    byte_stream = (
        byte
        for block in itertools.count()
        for byte in hmac.new(
            settings.project_secret_seed.encode(), f"{subject}:{block}".encode(), hashlib.sha256
        ).digest()
    )
    return _pick_char(byte_stream, string.ascii_letters) + "".join(
        _pick_char(byte_stream, _ALNUM) for _ in range(length - 1)
    )


def _pick_char(byte_stream, alphabet: str) -> str:
    """The next character of `alphabet` from the stream. Only bytes below the largest multiple of
    len(alphabet) are used, so every character is equally likely."""
    limit = 256 - 256 % len(alphabet)
    return next(alphabet[byte % len(alphabet)] for byte in byte_stream if byte < limit)
