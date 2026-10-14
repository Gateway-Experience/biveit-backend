# Workspace tests: the on-disk layout and seeding from the template. Real files in temp dirs -- this IS
# the file handling, so faking the disk would test nothing.

import pytest

from src.config import settings
from src.services import devenv
from src.services.devenv import workspaces

_PROJECT = {"id": 42, "user_id": 7}


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))


def test_layout_is_user_project_conversation(tmp_path):
    assert devenv.get_project_dir(_PROJECT) == f"{tmp_path}/storage/7/42"
    assert devenv.get_conversation_dir(_PROJECT, 9) == f"{tmp_path}/storage/7/42/9"


def test_copy_missing_files_never_overwrites(tmp_path):
    template = tmp_path / "template"
    (template / "src").mkdir(parents=True)
    (template / "index.html").write_text("template index")
    (template / "src" / "app.js").write_text("template app")

    destination = tmp_path / "ws"
    destination.mkdir()
    (destination / "index.html").write_text("user edited")

    workspaces._copy_missing_files(str(template), str(destination))

    assert (destination / "index.html").read_text() == "user edited"
    assert (destination / "src" / "app.js").read_text() == "template app"


async def test_ensure_workspace_creates_and_seeds_the_conversation_dir(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "index.html").write_text("template index")
    monkeypatch.setattr(settings, "template_dir", str(template))

    await devenv.ensure_workspace(_PROJECT, 9)

    assert (tmp_path / "storage/7/42/9/index.html").read_text() == "template index"
