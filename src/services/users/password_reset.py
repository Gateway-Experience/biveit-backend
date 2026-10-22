# Intentions: "forgot password" -- email someone a link, and let that link set a new password.
#
# THE TOKEN IS STATELESS: a short-lived JWT carrying the user id plus a fingerprint of their CURRENT
# password hash. Nothing is stored, so there's no reset table to keep in step with the users table.
# Once the password changes, the fingerprint no longer matches, so a link works exactly once, and every
# older link dies with it. It's signed with a key derived from jwt_secret (not jwt_secret itself), so a
# reset token can never pass as an access token, or the other way round.
#
# NO ACCOUNT ENUMERATION: request_password_reset behaves the same whether or not the email has an
# account -- same response, and the email goes out in the background so timing doesn't tell either.
# Accounts without a password (Google-only) get nothing: they have no password to reset.
#
# References: services/users/users.py (hashing, issue_session), utils/aws/ses.py (delivery).

import hashlib
import hmac
import logging
from datetime import timedelta
from urllib.parse import urlencode

import jwt

from src.config import settings
from src.constants import (
    PASSWORD_RESET_FE_PATH,
    PASSWORD_RESET_THROTTLE_SECONDS,
    PASSWORD_RESET_TOKEN_TTL_SECONDS,
    UserLanguage,
)
from src.repositories import users as users_repo
from src.services.users.users import UserNotFound, hash_password, issue_session
from src.utils import redis
from src.utils.aws import ses
from src.utils.common import spawn, utc_now

_log = logging.getLogger(__name__)


class InvalidResetToken(Exception):
    """The reset link is expired, tampered with, or already used (the password changed since). 400."""


# The reset email, per user language. {link} and {minutes} are filled in at send time.
_EMAIL_COPY = {
    UserLanguage.EN: {
        "subject": "Reset your biveit password",
        "text": (
            "Someone (hopefully you) asked to reset the password for your biveit account.\n\n"
            "Set a new one here: {link}\n\n"
            "The link works once and expires in {minutes} minutes. "
            "If you didn't ask for this, ignore this email -- your password stays the same."
        ),
        "button": "Set a new password",
    },
    UserLanguage.VI: {
        "subject": "Đặt lại mật khẩu biveit",
        "text": (
            "Có người (hy vọng là bạn) đã yêu cầu đặt lại mật khẩu cho tài khoản biveit của bạn.\n\n"
            "Đặt mật khẩu mới tại đây: {link}\n\n"
            "Liên kết chỉ dùng được một lần và hết hạn sau {minutes} phút. "
            "Nếu bạn không yêu cầu, hãy bỏ qua email này -- mật khẩu của bạn vẫn giữ nguyên."
        ),
        "button": "Đặt mật khẩu mới",
    },
}


def _get_signing_key() -> str:
    """[Pure-ish] The reset-token key: derived from jwt_secret so there's no extra secret to manage, but
    different from it, so access tokens and reset tokens can't stand in for each other."""
    if not settings.jwt_secret:
        raise RuntimeError("jwt_secret not configured; cannot sign password reset tokens")
    return hmac.new(settings.jwt_secret.encode(), b"password-reset", hashlib.sha256).hexdigest()


def _fingerprint(password_hash: str) -> str:
    """[Pure] A short, one-way stand-in for the password hash, so the token never carries the hash itself."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def mint_reset_token(user_id: int, password_hash: str) -> str:
    """[Pure-ish] A reset token for this user, valid until it expires or their password changes."""
    now = utc_now()
    payload = {
        "sub": str(user_id),
        "pwd": _fingerprint(password_hash),
        "iat": now,
        "exp": now + timedelta(seconds=PASSWORD_RESET_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(payload, _get_signing_key(), algorithm="HS256")


def _render_email(language: str, link: str) -> dict:
    """[Pure] Subject + text + html for the reset email, in the user's language (English if we don't have theirs)."""
    copy = _EMAIL_COPY.get(language, _EMAIL_COPY[UserLanguage.EN])
    text = copy["text"].format(link=link, minutes=PASSWORD_RESET_TOKEN_TTL_SECONDS // 60)
    paragraphs = "".join(f"<p>{line}</p>" for line in text.split("\n\n") if link not in line)
    button = f'<p><a href="{link}" style="background:#7c3aed;color:#fff;padding:10px 18px;border-radius:999px;text-decoration:none">{copy["button"]}</a></p>'
    return {
        "subject": copy["subject"],
        "text": text,
        "html": f'<div style="font-family:sans-serif">{button}{paragraphs}</div>',
    }


async def _send_reset_email(email: str, language: str, link: str) -> None:
    """Deliver the reset link. With no sender configured on a local machine, print the link instead, so
    the flow can be tried without SES. Anywhere else a missing sender raises (and spawn logs it)."""
    if not settings.email_from and settings.env == "local":
        _log.warning("password reset link for %s (EMAIL_FROM not set, so not emailed): %s", email, link)
        return
    rendered = _render_email(language, link)
    await ses.send_email(email, rendered["subject"], rendered["text"], rendered["html"])


async def request_password_reset(email: str) -> None:
    """Email a reset link to `email` if it belongs to an account with a password. Always returns quietly,
    whatever the outcome -- see the module note on enumeration. At most one email per address per
    PASSWORD_RESET_THROTTLE_SECONDS."""
    # Throttle first, before any lookup, so a known and an unknown email take the same path.
    if not await redis.set_if_absent(f"password_reset_throttle:{email}", PASSWORD_RESET_THROTTLE_SECONDS):
        return
    auth_row = await users_repo.get_auth_by_username(email)
    if auth_row is None or not auth_row.get("password_hash"):
        return
    user = await users_repo.get(auth_row["id"])
    if user is None:
        return
    token = mint_reset_token(auth_row["id"], auth_row["password_hash"])
    link = f"{settings.frontend_url}{PASSWORD_RESET_FE_PATH}?{urlencode({'token': token})}"
    spawn(_send_reset_email(email, user["language"], link))


def _decode_reset_token(token: str) -> dict | None:
    """[Pure-ish] The token's claims if the signature and expiry check out, else None."""
    try:
        return jwt.decode(token, _get_signing_key(), algorithms=["HS256"], options={"require": ["sub", "pwd", "exp"]})
    except jwt.InvalidTokenError:
        return None


async def reset_password(token: str, new_password: str) -> dict:
    """Set a new password from a reset link and sign the user in (the standard {user, token} response).
    Raises InvalidResetToken if the link is expired, forged, or already used."""
    claims = _decode_reset_token(token)
    if claims is None or not str(claims["sub"]).isdigit():
        raise InvalidResetToken("invalid or expired reset token")
    user_id = int(claims["sub"])
    auth_row = await users_repo.get_auth(user_id)
    # A changed password (including by this very link, earlier) changes the fingerprint -> the link is spent.
    if (
        auth_row is None
        or not auth_row.get("password_hash")
        or not hmac.compare_digest(claims["pwd"], _fingerprint(auth_row["password_hash"]))
    ):
        raise InvalidResetToken("reset token already used or account changed", {"user_id": user_id})
    user = await users_repo.update(user_id, {"password_hash": await hash_password(new_password)})
    if user is None:
        raise UserNotFound("no such user", {"user_id": user_id})
    return issue_session(user)
