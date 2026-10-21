# Intentions: file access inside a folder we don't trust (a project's folder, which its agent controls
# from inside its container) without ever being led outside it. Generic -- `root` is any folder whose
# OWN path is trusted; everything below it may be hostile.
#
# The danger is symlinks: one planted below `root` (artifacts/spec.md -> ../../../../.env) is harmless
# where the agent made it, but plain open()/copytree()/write_bytes() on the host follow it, as us. So
# every path here is walked one component at a time from `root`, each through the previous folder's
# descriptor with O_NOFOLLOW: no symlink is followed anywhere on the way, and there's no gap between
# checking a path and using it for the agent to swap something in. Absolute paths and `..` are refused
# up front; reads also refuse anything but a regular file (a FIFO would hang the read).
#
# References: "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md.

import contextlib
import errno
import os
import shutil
import stat
import uuid

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(Exception):
    """The path would leave the folder it's under (absolute, `..`, or a symlink somewhere on the way), or
    isn't the kind of file the caller asked for. Callers treat it like "not there"."""


def open_dir_within(root: str, relative_dir: str, make_dirs: bool = False) -> int:
    """A descriptor for `relative_dir` under `root` ("" = root itself), creating missing folders on the
    way with `make_dirs` -- `root` included, a plain makedirs being fine there since its path is trusted.
    The caller closes it. Raises UnsafePath, or FileNotFoundError without `make_dirs`."""
    if make_dirs:
        os.makedirs(root, exist_ok=True)
    return _descend(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC), _split(relative_dir), make_dirs)


def open_within(root: str, relative_path: str, flags: int, mode: int = 0o600, make_dirs: bool = False) -> int:
    """os.open for `relative_path` under `root`, never following a symlink. Opened for reading (no write
    flag), it must be a regular file. The caller closes the descriptor. Raises UnsafePath, or the usual
    OSErrors (FileNotFoundError, FileExistsError with O_EXCL...)."""
    *parent_parts, name = _split(relative_path) or [""]
    if not name:
        raise UnsafePath(relative_path)
    parent_fd = open_dir_within(root, "/".join(parent_parts), make_dirs)
    try:
        is_read = not flags & (os.O_WRONLY | os.O_RDWR)
        file_fd = _open_entry(name, flags | (os.O_NONBLOCK if is_read else 0), mode, parent_fd)
    finally:
        os.close(parent_fd)
    if is_read and not stat.S_ISREG(os.fstat(file_fd).st_mode):
        os.close(file_fd)
        raise UnsafePath(relative_path)
    return file_fd


def read_within(root: str, relative_path: str) -> bytes:
    """A regular file's content under `root`. Raises UnsafePath / FileNotFoundError."""
    with os.fdopen(open_within(root, relative_path, os.O_RDONLY), "rb") as file:
        return file.read()


def lstat_within(root: str, relative_path: str) -> os.stat_result | None:
    """What's at `relative_path` under `root`, the entry itself if it's a symlink -- or None when nothing is
    (a folder on the way missing included). Raises UnsafePath for a symlinked folder on the way."""
    *parent_parts, name = _split(relative_path) or [""]
    try:
        parent_fd = open_dir_within(root, "/".join(parent_parts))
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False) if name else os.fstat(parent_fd)
    except FileNotFoundError:
        return None
    finally:
        os.close(parent_fd)


def list_within(root: str, relative_dir: str) -> list[tuple[str, os.stat_result]]:
    """Every entry of `relative_dir` under `root` as (name, lstat) -- symlinks reported as themselves, never
    followed. Empty when the folder doesn't exist."""
    try:
        dir_fd = open_dir_within(root, relative_dir)
    except (FileNotFoundError, NotADirectoryError):
        return []
    try:
        with os.scandir(dir_fd) as entries:
            return [(entry.name, entry.stat(follow_symlinks=False)) for entry in entries]
    finally:
        os.close(dir_fd)


def replace_file_within(root: str, relative_path: str, data: bytes, mode: int = 0o644) -> None:
    """Write `data` to `relative_path` under `root` atomically: into a temp file next to it, then renamed
    over it -- a rename replaces whatever entry is there (a planted symlink included) without following
    it, and a write cut off halfway never leaves a truncated file. Missing folders are created."""
    *parent_parts, name = _split(relative_path) or [""]
    if not name:
        raise UnsafePath(relative_path)
    parent_fd = open_dir_within(root, "/".join(parent_parts), make_dirs=True)
    temp_name = f".{name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with os.fdopen(_open_entry(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, parent_fd), "wb") as file:
            file.write(data)
        os.rename(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=parent_fd)
        raise
    finally:
        os.close(parent_fd)


def rename_within(root: str, relative_dir: str, old_name: str, new_name: str) -> None:
    """Rename one entry of `relative_dir` under `root` (names only, no paths)."""
    if "/" in old_name or "/" in new_name:
        raise UnsafePath(f"{old_name} -> {new_name}")
    dir_fd = open_dir_within(root, relative_dir)
    try:
        os.rename(old_name, new_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def remove_tree_within(root: str, relative_path: str) -> None:
    """Delete `relative_path` under `root`, a folder with everything in it (or a single entry). Never follows
    a symlink: rmtree with a dir_fd works through descriptors too. Gone already is fine."""
    *parent_parts, name = _split(relative_path) or [""]
    if not name:
        raise UnsafePath(relative_path)
    try:
        parent_fd = open_dir_within(root, "/".join(parent_parts))
    except FileNotFoundError:
        return
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            shutil.rmtree(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(parent_fd)


def copy_tree_within(
    source_dir: str, root: str, relative_dest: str, skip_existing: bool, ignored_names: tuple[str, ...] = ()
) -> None:
    """Copy the TRUSTED tree `source_dir` (ours: the template, the boilerplate) to `relative_dest` under
    `root`, creating folders as needed. Files keep their permission bits; symlinks in the source are
    recreated as symlinks. `skip_existing`: leave every entry that's already there alone (whatever it is),
    else a file already there is an error. Names in `ignored_names` are skipped at any depth."""
    dest_fd = open_dir_within(root, relative_dest, make_dirs=True)
    try:
        _copy_entries(source_dir, dest_fd, skip_existing, ignored_names)
    finally:
        os.close(dest_fd)


def _copy_entries(source_dir: str, dest_fd: int, skip_existing: bool, ignored_names: tuple[str, ...]) -> None:
    """Recursive: copy source_dir's entries into the folder `dest_fd` (one level, then down)."""
    with os.scandir(source_dir) as scanned:
        entries = [entry for entry in scanned if entry.name not in ignored_names]
    for entry in entries:  # one side effect per entry: that's the copy
        existing = _lstat_at(entry.name, dest_fd)
        if entry.is_symlink():
            if existing is None:
                os.symlink(os.readlink(entry.path), entry.name, dir_fd=dest_fd)
        elif entry.is_dir():
            if existing is not None and not stat.S_ISDIR(existing.st_mode):
                # Something else where the folder goes (a planted symlink, say): leave it, or refuse.
                if skip_existing:
                    continue
                raise UnsafePath(entry.name)
            if existing is None:
                os.mkdir(entry.name, dir_fd=dest_fd)
            child_fd = _open_entry(entry.name, _DIR_FLAGS, 0, dest_fd)
            try:
                _copy_entries(entry.path, child_fd, skip_existing, ignored_names)
            finally:
                os.close(child_fd)
        elif not (existing is not None and skip_existing):
            permissions = stat.S_IMODE(entry.stat().st_mode)
            with (
                open(entry.path, "rb") as source,
                os.fdopen(
                    _open_entry(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, permissions, dest_fd), "wb"
                ) as dest,
            ):
                shutil.copyfileobj(source, dest)


def _lstat_at(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _split(relative_path: str) -> list[str]:
    """[Pure] The path's components, or UnsafePath for an absolute one or one with `..`."""
    parts = [part for part in relative_path.split("/") if part not in ("", ".")]
    if relative_path.startswith("/") or ".." in parts:
        raise UnsafePath(relative_path)
    return parts


def _descend(dir_fd: int, parts: list[str], make_dirs: bool) -> int:
    """Recursive: from the folder `dir_fd` (closed on the way down), a descriptor for parts' folder."""
    if not parts:
        return dir_fd
    try:
        if make_dirs:
            with contextlib.suppress(FileExistsError):
                os.mkdir(parts[0], dir_fd=dir_fd)
        next_fd = _open_entry(parts[0], _DIR_FLAGS, 0, dir_fd)
    finally:
        os.close(dir_fd)
    return _descend(next_fd, parts[1:], make_dirs)


def _open_entry(name: str, flags: int, mode: int, dir_fd: int) -> int:
    """os.open of one entry of `dir_fd`, refusing a symlink (UnsafePath)."""
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
    except OSError as error:
        # A symlink with O_NOFOLLOW: ELOOP -- or ENOTDIR when a folder was asked for.
        if error.errno in (errno.ELOOP, errno.ENOTDIR) and _is_symlink_at(name, dir_fd):
            raise UnsafePath(name) from error
        raise


def _is_symlink_at(name: str, dir_fd: int) -> bool:
    entry = _lstat_at(name, dir_fd)
    return entry is not None and stat.S_ISLNK(entry.st_mode)
