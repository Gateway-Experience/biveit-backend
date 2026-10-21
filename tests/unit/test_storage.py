# Storage tests: the on-disk layout and the guards on client-given names/paths. Real files in a temp
# STORAGE_DIR -- this module IS the file handling, so faking the disk would test nothing.

import io
import os

import pydash
import pytest

from src.config import settings
from src.services.agent import storage
from src.utils import paths

_PROJECT = {"id": 42, "hash": "p42hash", "user_id": 7, "user_hash": "u7hash"}


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        ("logo.png", "logo.png"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\menu.pdf", "menu.pdf"),
        (".env", "env"),
        ("thực đơn.pdf", "thực đơn.pdf"),  # non-English names stay readable
        ("a<b>|c?.txt", "a_b__c_.txt"),
        ("", "file"),
        ("...", "file"),
    ],
)
def test_file_names_are_made_safe(name, safe):
    assert storage.to_safe_file_name(name) == safe


async def test_same_name_twice_never_overwrites(tmp_path):
    first = await storage.save_upload(_PROJECT, 9, "logo.png", io.BytesIO(b"v1"))
    second = await storage.save_upload(_PROJECT, 9, "logo.png", io.BytesIO(b"v2"))

    assert first != second
    assert first.startswith("uploads/") and first.endswith("_logo.png")
    assert (tmp_path / "u7hash/p42hash/9" / first).read_bytes() == b"v1"
    assert (tmp_path / "u7hash/p42hash/9" / second).read_bytes() == b"v2"


async def test_saved_files_are_described_and_listed_oldest_first(tmp_path):
    second = await storage.save_upload(_PROJECT, 9, "menu.pdf", io.BytesIO(b"x"))
    first = await storage.save_upload(_PROJECT, 9, "logo.png", io.BytesIO(b"12345"))
    # Back-to-back writes can share a timestamp tick, so set the times explicitly.
    os.utime(tmp_path / "u7hash/p42hash/9" / first, (1000, 1000))
    os.utime(tmp_path / "u7hash/p42hash/9" / second, (2000, 2000))

    files = await storage.list_files(_PROJECT, 9, storage.UPLOADS_DIR)
    assert [file["name"] for file in files] == ["logo.png", "menu.pdf"]
    assert pydash.omit(files[0], "version") == {"path": first, "name": "logo.png", "mime": "image/png", "size": 5}
    assert files[0]["version"] != files[1]["version"]


async def test_listing_a_folder_that_doesnt_exist_yet_is_empty():
    assert await storage.list_files(_PROJECT, 9, storage.ARTIFACTS_DIR) == []


async def test_only_files_in_the_allowed_folders_can_be_opened(tmp_path):
    path = await storage.save_upload(_PROJECT, 9, "logo.png", io.BytesIO(b"x"))
    (tmp_path / "u7hash/p42hash/9/secret.txt").write_text("not for clients")
    (tmp_path / "u7hash/p42hash/site.html").write_text("project file")

    os.close(storage.open_conversation_file(_PROJECT, 9, path))
    for bad_path in [
        "secret.txt",
        "uploads/../secret.txt",
        "uploads/../../site.html",
        "uploads/nope.png",
        "uploads",
        "/etc/passwd",
    ]:
        with pytest.raises(storage.FileNotAvailable):
            storage.open_conversation_file(_PROJECT, 9, bad_path)
    with pytest.raises(storage.FileNotAvailable):  # right file, but only artifacts allowed here
        storage.open_conversation_file(_PROJECT, 9, path, [storage.ARTIFACTS_DIR])


@pytest.fixture
def outside(tmp_path):
    """A file and a folder outside the project, for planted symlinks to point at."""
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/secret.txt").write_text("platform secret")
    (tmp_path / "u7hash/p42hash/9").mkdir(parents=True)
    return tmp_path / "outside"


async def test_a_symlinked_file_is_never_served(tmp_path, outside):
    (tmp_path / "u7hash/p42hash/9/uploads").mkdir()
    (tmp_path / "u7hash/p42hash/9/uploads/link.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(storage.FileNotAvailable):
        storage.open_conversation_file(_PROJECT, 9, "uploads/link.txt")


@pytest.mark.parametrize("target", ["absolute", "relative"])
async def test_a_symlinked_folder_is_never_served_from(tmp_path, outside, target):
    # The old check resolved the allowed folder too, so a symlinked artifacts/ passed it.
    artifacts = tmp_path / "u7hash/p42hash/9/artifacts"
    artifacts.symlink_to(outside if target == "absolute" else "../../../outside")
    with pytest.raises(storage.FileNotAvailable):
        storage.open_conversation_file(_PROJECT, 9, "artifacts/secret.txt")
    assert await storage.list_files(_PROJECT, 9, storage.ARTIFACTS_DIR) == []


async def test_writes_never_go_through_a_planted_symlink(tmp_path, outside):
    workspace = tmp_path / "u7hash/p42hash/9"
    (workspace / "artifacts").mkdir()
    (workspace / "artifacts/spec.md").symlink_to(outside / "secret.txt")
    await storage.write_artifact(_PROJECT, 9, "spec.md", b"artifact")
    assert (outside / "secret.txt").read_text() == "platform secret"  # the link was replaced, not followed
    assert (workspace / "artifacts/spec.md").read_bytes() == b"artifact"

    (workspace / "uploads").symlink_to(outside)
    with pytest.raises(paths.UnsafePath):
        await storage.save_upload(_PROJECT, 9, "logo.png", io.BytesIO(b"x"))
    assert sorted(entry.name for entry in outside.iterdir()) == ["secret.txt"]


async def test_a_fifo_is_never_opened(tmp_path, outside):
    (tmp_path / "u7hash/p42hash/9/uploads").mkdir()
    os.mkfifo(tmp_path / "u7hash/p42hash/9/uploads/pipe")  # a read would hang
    with pytest.raises(storage.FileNotAvailable):
        storage.open_conversation_file(_PROJECT, 9, "uploads/pipe")


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.webp", "image/webp"),
        ("A.JPG", "image/jpeg"),
        ("a.mov", "video/quicktime"),
        ("a.pdf", "application/pdf"),
        ("noext", "application/octet-stream"),
    ],
)
async def test_mime_types_dont_depend_on_the_host(name, mime):
    path = await storage.save_upload(_PROJECT, 9, name, io.BytesIO(b"x"))
    assert storage.describe_file(path, storage.stat_conversation_file(_PROJECT, 9, path))["mime"] == mime


async def test_writing_an_artifact_again_replaces_it(tmp_path):
    path = await storage.write_artifact(_PROJECT, 9, "spec.md", b"v1")
    assert await storage.write_artifact(_PROJECT, 9, "spec.md", b"v2") == path == "artifacts/spec.md"
    assert (tmp_path / "u7hash/p42hash/9/artifacts/spec.md").read_bytes() == b"v2"
