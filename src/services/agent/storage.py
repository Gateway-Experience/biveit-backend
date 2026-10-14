# Intentions: the chat's files inside a conversation's workspace (services/devenv owns the layout above
# that), and the file operations on them:
#
#   <conversation dir>/
#       uploads/      files the user attached, as <upload id>_<name>
#       artifacts/    files the agent shows the user
#
# Messages reference files by a path RELATIVE to the conversation dir ("uploads/3f2a91c0_logo.png"),
# never an absolute one, so the storage root can move without touching the message log. Any path that
# comes from a client is checked by resolve_conversation_file before it's used.

import asyncio
import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from src.dto.projects import Project
from src.services import devenv

UPLOADS_DIR = "uploads"
ARTIFACTS_DIR = "artifacts"
# The folders a client may read from. Anything else in a conversation dir is off limits.
_READABLE_DIRS = [UPLOADS_DIR, ARTIFACTS_DIR]

# Characters allowed in a stored file name: unicode letters/digits (non-English names stay readable),
# plus . - _ and space. Everything else becomes _.
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\- ]")
_MAX_NAME_LENGTH = 120

# mimetypes partly reads the HOST's tables (/etc/mime.types), so the same file can get a different type
# on a laptop than in a slim container -- e.g. .webp is unknown on some boxes and would reach the FE as
# application/octet-stream. Pin the types the FE renders specially (images, video, audio) so they don't
# depend on the machine.
_PINNED_MIME_TYPES = {
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
}


class FileNotAvailable(Exception):
    """A client-given path doesn't point at an existing file in a readable folder of this conversation
    (missing, outside it, or in a folder that's off limits). 404 -- don't say which."""


def to_safe_file_name(name: str) -> str:
    """[Pure] A client's file name made safe to store: the last path component only (no "../" tricks),
    unsafe characters replaced, no leading dots (no hidden files), length capped. Never empty."""
    base_name = _UNSAFE_NAME_CHARS.sub("_", re.split(r"[\\/]", name)[-1]).lstrip(". ")
    return base_name[-_MAX_NAME_LENGTH:] or "file"


async def save_upload(project: Project, conversation_id: int, name: str, source: BinaryIO) -> str:
    """Store an uploaded file in the conversation's uploads folder and return its relative path.

    Each upload gets a fresh id prefix, so the same name uploaded twice never overwrites the first --
    an older message keeps pointing at exactly the file it was sent with."""
    relative_path = f"{UPLOADS_DIR}/{uuid.uuid4().hex[:8]}_{to_safe_file_name(name)}"
    destination = Path(devenv.get_conversation_dir(project, conversation_id), relative_path)
    await asyncio.to_thread(_copy_to_new_file, source, destination)
    return relative_path


async def write_artifact(project: Project, conversation_id: int, name: str, content: bytes) -> str:
    """Write a file into the conversation's artifacts folder and return its relative path. Unlike uploads
    this OVERWRITES a same-named file on purpose: an artifact is a living document (the spec gets
    revised), so every place that shows it opens the latest version."""
    relative_path = f"{ARTIFACTS_DIR}/{to_safe_file_name(name)}"
    destination = Path(devenv.get_conversation_dir(project, conversation_id), relative_path)

    def _write() -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    await asyncio.to_thread(_write)
    return relative_path


def _copy_to_new_file(source: BinaryIO, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as target:  # "x": fail rather than overwrite, should an id ever repeat
        shutil.copyfileobj(source, target)


def resolve_conversation_file(
    project: Project, conversation_id: int, relative_path: str, folders: list[str] = _READABLE_DIRS
) -> Path:
    """The absolute path of a client-given relative path, if it's an existing file directly inside one
    of the conversation's `folders` (default: every readable one). Raises FileNotAvailable otherwise. The check runs on the
    RESOLVED path, so "uploads/../../other" or a symlink pointing elsewhere can't get out."""
    conversation_dir = Path(devenv.get_conversation_dir(project, conversation_id))
    resolved = (conversation_dir / relative_path).resolve()
    allowed_parents = [(conversation_dir / folder).resolve() for folder in folders]
    if resolved.parent not in allowed_parents or not resolved.is_file():
        raise FileNotAvailable(relative_path, {"conversation_id": conversation_id})
    return resolved


def describe_file(relative_path: str, absolute_path: Path) -> dict:
    """[Pure-ish] {path, name, mime, size} for a stored file. `name` is the stored name minus its upload
    id prefix, i.e. what the user called it (after to_safe_file_name)."""
    stored_name = absolute_path.name
    return {
        "path": relative_path,
        "name": stored_name.split("_", 1)[1] if relative_path.startswith(f"{UPLOADS_DIR}/") else stored_name,
        "mime": _PINNED_MIME_TYPES.get(absolute_path.suffix.lower())
        or mimetypes.guess_type(stored_name)[0]
        or "application/octet-stream",
        "size": absolute_path.stat().st_size,
    }


def get_file_version(absolute_path: Path) -> str:
    """[Pure-ish] A short token that changes whenever the file does (modification time in ns + size), for
    cache validation: the ETag of the download, and what clients key their caches on."""
    stat = absolute_path.stat()
    return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"


async def list_files(project: Project, conversation_id: int, folder: str) -> list[dict]:
    """Every file in one of the conversation's folders, described, by last modification (oldest first;
    files written within the same filesystem timestamp tick fall back to name order). Empty if the
    folder doesn't exist yet."""
    folder_path = Path(devenv.get_conversation_dir(project, conversation_id), folder)

    def _list() -> list[dict]:
        if not folder_path.is_dir():
            return []
        files = sorted(
            (entry for entry in folder_path.iterdir() if entry.is_file()),
            key=lambda entry: (entry.stat().st_mtime_ns, entry.name),
        )
        return [describe_file(f"{folder}/{entry.name}", entry) for entry in files]

    return await asyncio.to_thread(_list)
