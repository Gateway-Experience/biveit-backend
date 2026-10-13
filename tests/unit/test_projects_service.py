# Project service + repo-update tests. No db and no real sessions: the agent service and postgres are
# faked, so what's under test is when current_port gets written and what the update SQL touches.

import pytest

from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import projects as project_service
from src.utils import postgres


@pytest.fixture
def updates(monkeypatch):
    """Records every projects_repo.update call as (project_id, updates)."""
    calls = []

    async def fake_update(project_id, project_updates):
        calls.append((project_id, project_updates))

    monkeypatch.setattr(projects_repo, "update", fake_update)
    return calls


def _fake_session_on(monkeypatch, port, port_holders=()):
    """The session comes up on `port`; `port_holders` are the projects the db says hold that port."""

    async def fake_ensure_workspace(project):
        return port

    async def fake_find(criteria):
        assert criteria == {"current_port": port}
        return list(port_holders)

    monkeypatch.setattr(agent_service, "ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr(projects_repo, "find", fake_find)


# --- ensure_session ---


async def test_ensure_session_records_a_new_port(monkeypatch, updates):
    _fake_session_on(monkeypatch, 8899)
    assert await project_service.ensure_session({"id": 1, "current_port": None}) == 8899
    assert updates == [(1, {"current_port": 8899})]


async def test_ensure_session_corrects_a_stale_port(monkeypatch, updates):
    _fake_session_on(monkeypatch, 8900)
    await project_service.ensure_session({"id": 1, "current_port": 8899})
    assert updates == [(1, {"current_port": 8900})]


async def test_ensure_session_skips_the_write_when_unchanged(monkeypatch, updates):
    _fake_session_on(monkeypatch, 8899, port_holders=[{"id": 1}])
    await project_service.ensure_session({"id": 1, "current_port": 8899})
    assert updates == []


async def test_ensure_session_takes_the_port_from_a_stale_holder(monkeypatch, updates):
    # Project 2's session died and the OS gave its port to project 1's new session.
    _fake_session_on(monkeypatch, 8899, port_holders=[{"id": 2}])
    await project_service.ensure_session({"id": 1, "current_port": None})
    assert updates == [(2, {"current_port": None}), (1, {"current_port": 8899})]


# --- projects_repo.update ---


@pytest.fixture
def captured_sql(monkeypatch):
    captured = {}

    async def fake_fetchrow(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return {"id": params["project_id"]}

    monkeypatch.setattr(postgres, "fetchrow", fake_fetchrow)
    return captured


async def test_update_never_touches_updated_at_implicitly(captured_sql):
    await projects_repo.update(1, {"name": "Renamed", "current_port": 8899})
    assert "updated_at" not in captured_sql["sql"].split("RETURNING")[0]


async def test_update_sets_updated_at_when_given(captured_sql):
    await projects_repo.update(1, {"name": "Renamed", "updated_at": "2026-10-12T00:00:00Z"})
    assert "updated_at = $updated_at" in captured_sql["sql"]


async def test_update_drops_unwritable_columns(captured_sql):
    await projects_repo.update(1, {"name": "Renamed", "id": 99})
    assert "id" not in captured_sql["params"]
