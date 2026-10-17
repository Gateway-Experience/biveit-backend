# Intentions: profile pictures the user uploads from their profile page. Stored on disk in their own
# storage folder (<STORAGE_DIR>/<user hash>/profile/), served back by GET /avatars/<user hash>/<file>, and
# pointed at from users.profile_picture -- the same column Google's picture lives in, so everything that
# shows an avatar keeps working unchanged.
#
# ONLY REAL IMAGES: the type is read from the file's first bytes, never from its name or the browser's
# say-so, and only PNG / JPEG / GIF / WebP pass. SVG is out on purpose -- it can carry script.
# Each upload gets a fresh random name, so a browser never shows a stale cached picture, and the
# previous file is removed once the row points at the new one.
#
# References: services/users/google.py (won't overwrite an uploaded avatar), controllers/users.py.

import asyncio
import re
import secrets
from pathlib import Path

from src.config import settings
from src.constants import AVATAR_DIR, AVATAR_MAX_BYTES, AVATAR_URL_PREFIX
from src.dto.users import User
from src.repositories import users as users_repo
from src.services.users.users import UserNotFound

# What a stored avatar's file name and its owner's hash look like -- anything else is refused before
# it gets near the filesystem.
_FILE_NAME = re.compile(r"^avatar-[0-9a-f]{16}\.(png|jpg|gif|webp)$")
_USER_HASH = re.compile(r"^[0-9a-z]{1,32}$")

_MIME_BY_EXTENSION = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


class InvalidAvatar(Exception):
    """Not an image we take (wrong type, empty, or over AVATAR_MAX_BYTES). 400."""


class AvatarNotFound(Exception):
    """No such avatar file. 404."""


def detect_image_extension(data: bytes) -> str | None:
    """[Pure] The file extension for `data` going by its magic bytes, or None if it isn't one of ours."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _get_avatar_dir(user_hash: str) -> Path:
    return Path(settings.storage_dir).resolve() / user_hash / AVATAR_DIR


def is_uploaded_avatar(profile_picture: str | None) -> bool:
    """[Pure] Whether the picture is one the user uploaded here (rather than Google's, or none)."""
    return bool(profile_picture) and profile_picture.startswith(f"{AVATAR_URL_PREFIX}/")


def _write_avatar(directory: Path, file_name: str, data: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / file_name).write_bytes(data)


def _remove_avatars(directory: Path, keep: str | None = None) -> None:
    """Delete the user's avatar files, except `keep`. Best effort: a leftover file only wastes space."""
    if directory.is_dir():
        for path in directory.iterdir():
            if path.name != keep and _FILE_NAME.match(path.name):
                path.unlink(missing_ok=True)


async def save_avatar(user: User, data: bytes) -> User:
    """Make `data` the user's profile picture and return the updated user. Raises InvalidAvatar for
    anything but a PNG / JPEG / GIF / WebP of at most AVATAR_MAX_BYTES."""
    if not data or len(data) > AVATAR_MAX_BYTES:
        raise InvalidAvatar("empty or too large", {"size": len(data)})
    extension = detect_image_extension(data)
    if extension is None:
        raise InvalidAvatar("not a png / jpeg / gif / webp")
    directory = _get_avatar_dir(user["hash"])
    file_name = f"avatar-{secrets.token_hex(8)}.{extension}"
    await asyncio.to_thread(_write_avatar, directory, file_name, data)
    updated = await users_repo.update(
        user["id"], {"profile_picture": f"{AVATAR_URL_PREFIX}/{user['hash']}/{file_name}"}
    )
    if updated is None:
        raise UserNotFound("no such user", {"user_id": user["id"]})
    # Only now that nothing points at the old one.
    await asyncio.to_thread(_remove_avatars, directory, file_name)
    return updated


async def remove_avatar(user: User) -> User:
    """Go back to no picture (the initial-letter fallback). The uploaded files are deleted too."""
    updated = await users_repo.update(user["id"], {"profile_picture": None})
    if updated is None:
        raise UserNotFound("no such user", {"user_id": user["id"]})
    await asyncio.to_thread(_remove_avatars, _get_avatar_dir(user["hash"]))
    return updated


def resolve_avatar_file(user_hash: str, file_name: str) -> tuple[Path, str]:
    """[Pure-ish] The avatar's path on disk and its mime type. Raises AvatarNotFound for a malformed name
    (so nothing like ../ ever reaches the filesystem) or a file that isn't there."""
    if not _USER_HASH.match(user_hash) or not _FILE_NAME.match(file_name):
        raise AvatarNotFound("malformed avatar path", {"user_hash": user_hash, "file_name": file_name})
    path = _get_avatar_dir(user_hash) / file_name
    if not path.is_file():
        raise AvatarNotFound("no such avatar", {"user_hash": user_hash, "file_name": file_name})
    return path, _MIME_BY_EXTENSION[file_name.rsplit(".", 1)[1]]
