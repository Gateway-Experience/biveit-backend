# Intentions: the chat's files inside a conversation's workspace (services/devenv owns the layout above
# that), and the file operations on them:
#
#   <conversation dir>/
#       uploads/      files the user attached, as <upload id>_<name>
#       artifacts/    files the agent shows the user
#
# Messages reference files by a path RELATIVE to the conversation dir ("uploads/3f2a91c0_logo.png"),
# never an absolute one, so the storage root can move without touching the message log. Any path that
# comes from a client is checked by _to_file_path before it's used.
#
# The project's folder belongs to its agent (it's mounted into the agent's container), so every file
# operation here goes through utils/paths, rooted at the project folder: a symlink the agent planted is
# never followed. See "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md.

import asyncio
import mimetypes
import os
import re
import shutil
import stat
import uuid
from typing import BinaryIO

from src.dto.projects import Project
from src.services import devenv
from src.utils import paths

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
    await asyncio.to_thread(_copy_to_new_file, source, project, f"{conversation_id}/{relative_path}")
    return relative_path


async def write_artifact(project: Project, conversation_id: int, name: str, content: bytes) -> str:
    """Write a file into the conversation's artifacts folder and return its relative path. Unlike uploads
    this OVERWRITES a same-named file on purpose: an artifact is a living document (the spec gets
    revised), so every place that shows it opens the latest version."""
    relative_path = f"{ARTIFACTS_DIR}/{to_safe_file_name(name)}"
    await asyncio.to_thread(
        paths.replace_file_within, devenv.get_project_dir(project), f"{conversation_id}/{relative_path}", content
    )
    return relative_path


def _copy_to_new_file(source: BinaryIO, project: Project, project_relative_path: str) -> None:
    # O_EXCL: fail rather than overwrite, should an id ever repeat.
    file_fd = paths.open_within(
        devenv.get_project_dir(project),
        project_relative_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
        make_dirs=True,
    )
    with os.fdopen(file_fd, "wb") as target:
        shutil.copyfileobj(source, target)


def open_conversation_file(
    project: Project, conversation_id: int, relative_path: str, folders: list[str] = _READABLE_DIRS
) -> int:
    """A read descriptor for a client-given relative path, if it's an existing regular file directly inside
    one of the conversation's `folders` (default: every readable one). The caller closes it. Raises
    FileNotAvailable otherwise -- "uploads/../../other", a symlink anywhere on the way, a missing file."""
    try:
        return paths.open_within(
            devenv.get_project_dir(project), _to_file_path(conversation_id, relative_path, folders), os.O_RDONLY
        )
    except (paths.UnsafePath, FileNotFoundError, NotADirectoryError, IsADirectoryError) as error:
        raise FileNotAvailable(relative_path, {"conversation_id": conversation_id}) from error


def stat_conversation_file(
    project: Project, conversation_id: int, relative_path: str, folders: list[str] = _READABLE_DIRS
) -> os.stat_result:
    """open_conversation_file's checks, for just the file's stat. Raises FileNotAvailable."""
    file_fd = open_conversation_file(project, conversation_id, relative_path, folders)
    try:
        return os.fstat(file_fd)
    finally:
        os.close(file_fd)


def _to_file_path(conversation_id: int, relative_path: str, folders: list[str]) -> str:
    """[Pure] The project-relative path of a client-given one -- if it's exactly <folder>/<name> with an
    allowed folder (files sit directly in it). Raises FileNotAvailable otherwise."""
    folder, _, name = relative_path.partition("/")
    if folder not in folders or not name or "/" in name or name in (".", ".."):
        raise FileNotAvailable(relative_path, {"conversation_id": conversation_id})
    return f"{conversation_id}/{folder}/{name}"


def describe_file(relative_path: str, file_stat: os.stat_result) -> dict:
    """[Pure] {path, name, mime, size} for a stored file. `name` is the stored name minus its upload id
    prefix, i.e. what the user called it (after to_safe_file_name)."""
    stored_name = relative_path.rsplit("/", 1)[-1]
    return {
        "path": relative_path,
        "name": stored_name.split("_", 1)[1] if relative_path.startswith(f"{UPLOADS_DIR}/") else stored_name,
        "mime": _PINNED_MIME_TYPES.get(os.path.splitext(stored_name)[1].lower())
        or mimetypes.guess_type(stored_name)[0]
        or "application/octet-stream",
        "size": file_stat.st_size,
    }


def get_file_version(file_stat: os.stat_result) -> str:
    """[Pure] A short token that changes whenever the file does (modification time in ns + size), for
    cache validation: the ETag of the download, and what clients key their caches on."""
    return f"{file_stat.st_mtime_ns:x}-{file_stat.st_size:x}"


async def list_files(project: Project, conversation_id: int, folder: str) -> list[dict]:
    """Every regular file in one of the conversation's folders, described, plus its `version`, by last
    modification (oldest first; files written within the same filesystem timestamp tick fall back to
    name order). Symlinks are skipped, not followed. Empty if the folder doesn't exist yet, or is a symlink."""
    try:
        entries = await asyncio.to_thread(
            paths.list_within, devenv.get_project_dir(project), f"{conversation_id}/{folder}"
        )
    except paths.UnsafePath:
        return []  # the folder itself is a symlink: nothing in it is ours to show
    files = sorted(
        ((name, entry) for name, entry in entries if stat.S_ISREG(entry.st_mode)),
        key=lambda file: (file[1].st_mtime_ns, file[0]),
    )
    return [{**describe_file(f"{folder}/{name}", entry), "version": get_file_version(entry)} for name, entry in files]
