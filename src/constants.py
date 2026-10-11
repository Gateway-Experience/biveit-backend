# All enums/constants should live here unless there is a very good reason for them to stay local
# to where they are used.

from enum import StrEnum


class UserType(StrEnum):
    USER = "USER"  # a real, credentialed account
    GUEST = "GUEST"  # anonymous account minted on first visit; promoted to USER on signup


class UserPlatform(StrEnum):
    """users.platform -- how an account signs in; users.platform_id is the id on that platform."""

    WEB = "WEB"  # email (username) + password; platform_id = the username
    GOOGLE = "GOOGLE"  # Google OIDC; platform_id = the `sub`


class Charset(StrEnum):
    ALPHA_NUMERIC = "abcdefghijklmnopqrstuvwxyz0123456789"
    ALPHABET = "abcdefghijklmnopqrstuvwxyz"
    NUMERIC = "0123456789"


# The httpOnly cookie carrying the user's access token (utils/auth) -- the session, Passport-style.
ACCESS_TOKEN_COOKIE = "access_token"


class GoogleLoginError(StrEnum):
    """Codes on the FE redirect when Google login fails: FRONTEND_URL/login?error=<code>."""

    DISABLED = "GOOGLE_LOGIN_DISABLED"  # client id/secret or session secret not configured
    ACCESS_DENIED = "ACCESS_DENIED"  # the user cancelled on Google's consent screen
    INVALID_STATE = "INVALID_STATE"  # state missing/mismatched -- stale tab, replay, or forgery
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    EMAIL_TAKEN = "EMAIL_TAKEN"  # a new Google account's email is already registered (no merging)
    GOOGLE_AUTH_FAILED = "GOOGLE_AUTH_FAILED"  # any other OAuth failure (code exchange, id token check)
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ConfigStatus(StrEnum):
    """system_configurations.status -- whether a setting is in EFFECT. Orthogonal to is_private
    (which controls who may READ it)."""

    ENABLED = "ENABLED"  # the row's value is in effect
    DISABLED = "DISABLED"  # treated as absent -> callers fall back to their code default


class ConfigName(StrEnum):
    """The keys that live in the system_configurations table. Declare each one here rather than
    scattering raw strings at the call sites, so there's one list of what's tunable at runtime.
    Empty in the boilerplate -- add yours as the app grows."""
