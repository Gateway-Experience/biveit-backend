# Session port bookkeeping (services/agent/sessions.ensure_session) + projects repo-update tests. No db
# and no real sessions: the adapter and postgres are faked, so what's under test is when current_port gets
# written and what the update SQL touches.

import pytest

from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import devenv
from src.services import projects as projects_service
from src.services.agent import adapter, sessions
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

    async def fake_ensure_workspace(project, conversation_id):
        pass

    async def fake_ensure_session(project, conversation_id):
        return port

    async def fake_find(criteria):
        assert criteria == {"current_port": port}
        return list(port_holders)

    monkeypatch.setattr(devenv, "ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr(adapter, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(projects_repo, "find", fake_find)


# --- ensure_session ---


async def test_ensure_session_records_a_new_port(monkeypatch, updates):
    _fake_session_on(monkeypatch, 8899)
    assert await sessions.ensure_session({"id": 1, "current_port": None}, 5) == 8899
    assert updates == [(1, {"current_port": 8899})]


async def test_ensure_session_corrects_a_stale_port(monkeypatch, updates):
    _fake_session_on(monkeypatch, 8900)
    await sessions.ensure_session({"id": 1, "current_port": 8899}, 5)
    assert updates == [(1, {"current_port": 8900})]


async def test_ensure_session_skips_the_write_when_unchanged(monkeypatch, updates):
    _fake_session_on(monkeypatch, 8899, port_holders=[{"id": 1}])
    await sessions.ensure_session({"id": 1, "current_port": 8899}, 5)
    assert updates == []


async def test_ensure_session_takes_the_port_from_a_stale_holder(monkeypatch, updates):
    # Project 2's session died and the OS gave its port to project 1's new session.
    _fake_session_on(monkeypatch, 8899, port_holders=[{"id": 2}])
    await sessions.ensure_session({"id": 1, "current_port": None}, 5)
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


# --- open_project ---


async def _record(calls, what):
    calls.append(what)


@pytest.mark.parametrize("stage", ["specing", "implementing"])
async def test_open_project_ensures_the_devenv_only_once_implementing(stage, monkeypatch):
    calls = []

    async def fake_current_conversation(project_id, user_id):
        return {"id": 5}

    async def fake_ensure_session(project, conversation_id):
        calls.append("session")

    monkeypatch.setattr(projects_service, "AGENT_MOCK_REPLIES", False)
    monkeypatch.setattr(projects_service.agent_service, "get_current_conversation", fake_current_conversation)
    monkeypatch.setattr(projects_service.agent_service, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(
        projects_service.agent_service,
        "ensure_introduced",
        lambda project, conversation_id: _record(calls, "introduced"),
    )
    monkeypatch.setattr(
        projects_service.agent_service, "restore_devenv", lambda project, conversation_id: ("devenv", conversation_id)
    )
    monkeypatch.setattr(projects_service, "spawn", lambda job: calls.append(job))

    await projects_service.open_project({"id": 1, "user_id": 7, "stage": stage})
    assert calls == (["session", "introduced", ("devenv", 5)] if stage == "implementing" else ["session", "introduced"])


async def test_the_agent_is_introduced_once_and_retried_after_a_failure(monkeypatch):
    claimed = set()
    introduced = []

    async def fake_claim(conversation_id, key):
        if (conversation_id, key) in claimed:
            return False
        claimed.add((conversation_id, key))
        return True

    async def fake_delete(conversation_id, key):
        claimed.discard((conversation_id, key))

    failures = [sessions.SessionUnavailable("not yet")]

    async def fake_introduce(project, conversation_id):
        if failures:
            raise failures.pop()
        introduced.append(conversation_id)

    monkeypatch.setattr(sessions.ai_conversations_repo, "claim_meta_key", fake_claim)
    monkeypatch.setattr(sessions.ai_conversations_repo, "delete_meta_key", fake_delete)
    monkeypatch.setattr(sessions, "introduce_project", fake_introduce)

    with pytest.raises(sessions.SessionUnavailable):
        await sessions.ensure_introduced({"id": 1}, 5)  # fails: the claim is given back
    await sessions.ensure_introduced({"id": 1}, 5)  # the next open tries again
    await sessions.ensure_introduced({"id": 1}, 5)  # and from then on it's done
    assert introduced == [5]


# --- close_project ---


async def test_close_project_stops_every_session_and_the_dev_servers(monkeypatch):
    calls = []

    async def fake_find(criteria):
        assert criteria == {"project_id": 1}
        return [{"id": 6}, {"id": 5}]  # newest first

    async def fake_stop_session(project, conversation_id):
        calls.append(("session", conversation_id))
        return {"tmux_session": True, "pids": [100 + conversation_id]}

    async def fake_stop_devenv(project, conversation_id):
        calls.append(("devenv", conversation_id))
        return ["project_1_be"]

    monkeypatch.setattr(projects_service.ai_conversations_repo, "find", fake_find)
    monkeypatch.setattr(projects_service.agent_service, "stop_session", fake_stop_session)
    monkeypatch.setattr(projects_service.agent_service, "stop_devenv", fake_stop_devenv)

    stopped = await projects_service.close_project({"id": 1})
    assert calls == [("session", 6), ("session", 5), ("devenv", 6)]
    assert stopped == {
        "sessions": {6: {"tmux_session": True, "pids": [106]}, 5: {"tmux_session": True, "pids": [105]}},
        "dev_servers": ["project_1_be"],
    }


async def test_closing_a_project_without_conversations_does_nothing(monkeypatch):
    async def no_conversations(criteria):
        return []

    monkeypatch.setattr(projects_service.ai_conversations_repo, "find", no_conversations)
    assert await projects_service.close_project({"id": 1}) == {"sessions": {}, "dev_servers": []}


async def test_stopping_a_session_clears_the_current_port(monkeypatch, updates):
    async def fake_adapter_stop(project, conversation_id):
        return {"tmux_session": False, "pids": []}

    async def fake_clear_building(conversation_id):
        pass

    monkeypatch.setattr(adapter, "stop_session", fake_adapter_stop)
    monkeypatch.setattr(sessions.building, "clear_building", fake_clear_building)
    await sessions.stop_session({"id": 1, "current_port": 47000}, 5)
    await sessions.stop_session({"id": 2, "current_port": None}, 5)
    assert updates == [(1, {"current_port": None})]


# --- search_prompts ---


def test_excerpt_shows_the_match_with_some_context():
    text = "i want a shop selling niche perfumes from brands like Amouage and Diptyque, and also a blog " * 3
    excerpt = projects_service.to_excerpt(text, "diptyque")
    assert "Diptyque" in excerpt
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert len(excerpt) < len(text)


async def test_search_ignores_too_short_queries_and_trims_whitespace(monkeypatch):
    calls = []

    async def fake_find(user_id, text):
        calls.append(text)
        return [{"project_id": 3, "matched_text": "Selling niche perfumes online"}]

    monkeypatch.setattr(ai_messages_repo, "find_prompt_matches", fake_find)
    assert await projects_service.search_prompts(7, " a ") == []
    assert await projects_service.search_prompts(7, "  niche   perfumes ") == [
        {"project_id": 3, "excerpt": "Selling niche perfumes online"}
    ]
    assert calls == ["niche perfumes"]
