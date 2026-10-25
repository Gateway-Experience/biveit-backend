# Site preview tests (services/devenv/previews): the capture itself runs a browser in a project's container,
# so here the container is faked -- what's under test is what we do around it: only the last of a burst of
# requests captures, a picture is stored from the capture's output, and a failed capture keeps the old one.

import asyncio
import base64
from pathlib import Path

import pytest

from src.config import settings
from src.services.devenv import previews
from src.utils.processes import CommandFailed

_PROJECT = {"id": 1, "hash": "p1hash"}


@pytest.fixture(autouse=True)
def previews_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "site_previews_dir", str(tmp_path / "site_previews"))


@pytest.fixture
def container(monkeypatch):
    """A running container whose capture prints `state["output"]` (or fails, if it's an exception)."""
    state = {"output": "", "runs": 0}

    async def fake_is_running(project_id):
        return True

    async def fake_run(project_id, argv, **kwargs):
        state["runs"] += 1
        if isinstance(state["output"], Exception):
            raise state["output"]
        return state["output"]

    monkeypatch.setattr(previews, "is_container_running", fake_is_running)
    monkeypatch.setattr(previews, "run_in_container", fake_run)
    return state


async def test_a_capture_stores_the_picture_from_between_the_markers(container):
    picture = b"\xff\xd8 a jpeg"
    container["output"] = f"some chromium warning\n<<<preview:{base64.b64encode(picture).decode()}>>>"

    assert previews.get_taken_at(_PROJECT) is None
    assert await previews.capture(_PROJECT)
    assert Path(previews.get_preview_path(_PROJECT)).read_bytes() == picture
    assert previews.get_taken_at(_PROJECT) is not None


async def test_a_failed_capture_keeps_the_old_picture(container):
    container["output"] = f"<<<preview:{base64.b64encode(b'old').decode()}>>>"
    await previews.capture(_PROJECT)

    container["output"] = CommandFailed("the site answered 500", {})
    assert not await previews.capture(_PROJECT)
    container["output"] = "no markers at all"
    assert not await previews.capture(_PROJECT)
    assert Path(previews.get_preview_path(_PROJECT)).read_bytes() == b"old"


async def test_only_the_last_request_of_a_burst_captures(container, monkeypatch):
    monkeypatch.setattr(previews, "SITE_PREVIEW_DELAY_SECONDS", 0.05)
    container["output"] = f"<<<preview:{base64.b64encode(b'pic').decode()}>>>"

    await asyncio.gather(*[previews.capture_later(_PROJECT) for _ in range(3)])
    assert container["runs"] == 1
