# The users service package -- everything about accounts: password signup/login, the guest lifecycle,
# Google sign-in, and profile edits. Protocol-agnostic: every failure is a domain exception that the
# caller maps to its protocol.
#
# THE GUEST LIFECYCLE is the thing worth understanding here. A visitor gets a GUEST account on first
# visit (register_guest) with no username and no password, so whatever they do is already
# attached to a real row. When they sign up, we PROMOTE that same row in place -- same id, credentials
# filled in, user_type flipped to USER -- rather than creating a new user and migrating. That's what
# keeps their accumulated data theirs. promote_guest (password) and signin_google (OAuth)
# are the two ways in. The guest is always the CALLER's own session, never an id from a request body --
# ids are sequential, so trusting one would let anyone claim someone else's guest.
#
# Layout:
#   users.py   the core: password signup/login, guests, profile edits, the shared sign-in response
#   google.py  signin_google (from Authlib-verified OIDC claims)
#   password_reset.py  forgot password: the emailed link, and setting a new password from it
#   avatars.py  profile pictures uploaded from the profile page: stored, served, replaced
#
# Callers import THIS package (`from src.services import users as user_service`), not the files, so the
# layout can move without touching them. Add to __all__ when a new function is meant for outside use.

from src.services.users.avatars import AvatarNotFound, InvalidAvatar, remove_avatar, resolve_avatar_file, save_avatar
from src.services.users.google import GoogleEmailNotVerified, GoogleEmailTaken, signin_google
from src.services.users.password_reset import InvalidResetToken, request_password_reset, reset_password
from src.services.users.users import (
    DuplicateUser,
    InvalidCredentials,
    NotAGuest,
    UserNotFound,
    WrongPassword,
    authenticate,
    change_password,
    finish_tour,
    promote_guest,
    register,
    register_guest,
    update,
)

__all__ = [
    "AvatarNotFound",
    "DuplicateUser",
    "GoogleEmailNotVerified",
    "GoogleEmailTaken",
    "InvalidAvatar",
    "InvalidCredentials",
    "InvalidResetToken",
    "NotAGuest",
    "UserNotFound",
    "WrongPassword",
    "authenticate",
    "change_password",
    "finish_tour",
    "promote_guest",
    "register",
    "register_guest",
    "remove_avatar",
    "request_password_reset",
    "reset_password",
    "resolve_avatar_file",
    "save_avatar",
    "signin_google",
    "update",
]
