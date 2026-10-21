# Agent chat tests (controllers/conversations + services/agent/conversations) against the mock agent.
# Repos and redis are faked over in-memory state, so this covers the full request flow -- ownership,
# what gets stored, the mock's replies, wake-ups -- without a db. watch_messages is tested directly.

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import devenv as conversations_devenv
from src.services.agent import conversations, mock, sessions, tokens, wakeups
from src.utils import redis
from src.utils.auth import get_current_user

_ALICE = {"id": 7, "username": "alice", "user_type": "USER"}
_PROJECTS = [
    {
        "id": 3,
        "hash": "p3hash",
        "user_id": 7,
        "user_hash": "u7hash",
        "stage": "implementing",
        "implementation_done_at": None,
    },
    {"id": 4, "hash": "p4hash", "user_id": 99, "user_hash": "u99hash", "stage": "specing"},
]  # 4 is someone else's


@pytest.fixture
def db(monkeypatch):
    state = {"conversations": [], "messages": [], "published": [], "spawned": []}

    async def find_projects(criteria):
        return [project for project in _PROJECTS if all(project.get(key) == value for key, value in criteria.items())]

    async def get_conversation(conversation_id):
        return next((conv for conv in state["conversations"] if conv["id"] == conversation_id), None)

    async def find_conversations(criteria):
        return [conv for conv in state["conversations"] if conv["project_id"] == criteria["project_id"]][::-1]

    async def insert_conversation(data):
        conversation = {
            "id": len(state["conversations"]) + 1,
            "title": None,
            "meta": {},
            "created_at": "t",
            "updated_at": "t",
            "deleted_at": None,
            **data,
        }
        state["conversations"] = [*state["conversations"], conversation]
        return conversation

    async def find_messages(conversation_id, after_seq=0, limit=None):
        newer = [m for m in state["messages"] if m["conversation_id"] == conversation_id and m["seq"] > after_seq]
        return newer[:limit]

    async def find_messages_before(conversation_id, before_seq, limit):
        # Only routing is under test here; the page-start rule is SQL, checked against a real db.
        older = [
            m
            for m in state["messages"]
            if m["conversation_id"] == conversation_id and (before_seq is None or m["seq"] < before_seq)
        ]
        return older[-limit:]

    async def insert_message(data):
        seq = len([m for m in state["messages"] if m["conversation_id"] == data["conversation_id"]]) + 1
        message = {
            "id": len(state["messages"]) + 1,
            "seq": seq,
            "model": None,
            "stop_reason": None,
            "usage": None,
            "created_at": "t",
            **data,
        }
        state["messages"] = [*state["messages"], message]
        return message

    async def publish(channel, payload=""):
        state["published"] = [*state["published"], (channel, payload)]

    @asynccontextmanager
    async def lock(key, ttl_seconds, wait_seconds):
        yield

    monkeypatch.setattr(projects_repo, "find", find_projects)
    monkeypatch.setattr(ai_conversations_repo, "get", get_conversation)
    monkeypatch.setattr(ai_conversations_repo, "find", find_conversations)
    monkeypatch.setattr(ai_conversations_repo, "insert", insert_conversation)
    monkeypatch.setattr(ai_messages_repo, "find", find_messages)
    monkeypatch.setattr(ai_messages_repo, "insert", insert_message)
    monkeypatch.setattr(ai_messages_repo, "find_before", find_messages_before)
    monkeypatch.setattr(redis, "publish", publish)
    monkeypatch.setattr(redis, "lock", lock)
    # The mock replies in the background after a pause: capture the background job instead, with no
    # pause, and let each test run it (_settle) when it wants the reply stored.
    monkeypatch.setattr(conversations, "spawn", lambda job: state["spawned"].append(job))
    monkeypatch.setattr(mock, "REPLY_DELAY_SECONDS", 0)
    # These tests exercise the mock agent, whatever the switch is set to in constants.py.
    monkeypatch.setattr(conversations, "AGENT_MOCK_REPLIES", True)
    return state


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(db) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _ALICE
    return TestClient(app)


def _open_conversation(client) -> int:
    return client.get("/api/projects/3/conversation").json()["id"]


def _say(client, conversation_id, text):
    return client.post(f"/api/conversations/{conversation_id}/messages", json={"type": "text", "text": text})


async def _settle(db):
    """Run the captured background replies, i.e. let the mock answer."""
    jobs, db["spawned"] = db["spawned"], []
    await asyncio.gather(*jobs)


async def _say_and_settle(client, db, conversation_id, text):
    response = _say(client, conversation_id, text)
    await _settle(db)
    return response


def _submission(*answers):
    """A submission body from (interaction_id, results) pairs."""
    return {
        "type": "submission",
        "answers": [{"interaction_id": interaction_id, "results": results} for interaction_id, results in answers],
    }


def _pending_tool_use(db):
    return db["messages"][-1]["content"][-1]


# --- conversation + ownership ---


def test_current_conversation_is_created_once(client, db):
    first = _open_conversation(client)
    assert _open_conversation(client) == first
    assert len(db["conversations"]) == 1


def test_someone_elses_project_or_conversation_is_404(client, db):
    assert client.get("/api/projects/4/conversation").status_code == 404
    db["conversations"] = [{"id": 50, "project_id": 4}]
    assert client.get("/api/conversations/50/messages").status_code == 404
    assert _say(client, 50, "hi").status_code == 404
    assert client.get("/api/conversations/50/events").status_code == 404


# --- text ---


async def test_text_is_stored_and_echoed_by_the_mock(client, db):
    conversation_id = _open_conversation(client)
    response = _say(client, conversation_id, "make it blue")

    assert response.status_code == 201
    assert [m["role"] for m in db["messages"]] == ["user"]  # the reply isn't inline...
    await _settle(db)  # ...it lands in the background
    assert response.json()["content"] == [{"type": "text", "text": "make it blue"}]
    assert [(m["role"], m["content"][0]["text"]) for m in db["messages"]] == [
        ("user", "make it blue"),
        ("assistant", "(mock agent) You said: make it blue"),
    ]
    assert db["published"] == [(f"ai_conversation:{conversation_id}", "1"), (f"ai_conversation:{conversation_id}", "2")]


async def test_messages_are_paged_by_seq_cursors(client, db):
    conversation_id = _open_conversation(client)
    for text in ["one", "two", "three"]:
        await _say_and_settle(client, db, conversation_id, text)  # 6 messages: each text + its echo

    def seqs(query):
        return [m["seq"] for m in client.get(f"/api/conversations/{conversation_id}/messages?{query}").json()]

    assert seqs("") == [1, 2, 3, 4, 5, 6]  # newest page; first seq is 1, so nothing older
    assert seqs("limit=2") == [5, 6]
    assert seqs("before_seq=5&limit=2") == [3, 4]
    assert seqs("after_seq=1&limit=2") == [2, 3]


@pytest.mark.parametrize("query", ["before_seq=3&after_seq=1", "limit=0", "limit=201", "before_seq=0"])
def test_bad_page_queries_are_422(query, client, db):
    assert client.get(f"/api/conversations/{_open_conversation(client)}/messages?{query}").status_code == 422


async def test_text_is_refused_while_a_question_is_pending(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "question")
    response = _say(client, conversation_id, "never mind")
    assert response.status_code == 409
    assert response.json()["detail"] == "INTERACTION_PENDING"


# --- submissions ---


async def test_answers_are_stored_as_a_tool_result_and_echoed(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "questions")
    tool_use = _pending_tool_use(db)
    results = [
        {"status": "submitted", "value": {"selected": [], "custom": "Earthy"}},
        {"status": "submitted", "value": {"selected": ["No"], "custom": None}},
        {"status": "submitted", "value": {"selected": [], "custom": None}},
        {"status": "submitted", "value": {"selected": ["About", "Blog"], "custom": None}},
    ]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json=_submission((tool_use["id"], results)),
    )

    assert response.status_code == 201
    tool_result = response.json()["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == tool_use["id"]
    assert tool_result["content"][1] == results[1]
    await _settle(db)
    assert db["messages"][-1]["role"] == "assistant"
    assert "Earthy" in db["messages"][-1]["content"][0]["text"]
    assert (await _say_and_settle(client, db, conversation_id, "thanks")).status_code == 201  # no longer pending


async def test_an_abort_closes_the_question_without_a_reply(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "question")
    tool_use = _pending_tool_use(db)
    client.post(
        f"/api/conversations/{conversation_id}/messages",
        json=_submission((tool_use["id"], [{"status": "aborted"}])),
    )
    await _settle(db)
    assert db["messages"][-1]["role"] == "user"  # stored the abort, the mock said nothing
    assert (await _say_and_settle(client, db, conversation_id, "back to chatting")).status_code == 201


async def test_resend_hands_the_latest_user_message_over_again_without_storing_a_copy(client, db):
    conversation_id = _open_conversation(client)
    _say(client, conversation_id, "make it blue")
    # Pretend that first delivery never got through: drop the mock's reply (closed, so it isn't left unawaited).
    for job in db["spawned"]:
        job.close()
    db["spawned"] = []

    response = client.post(f"/api/conversations/{conversation_id}/resend")
    assert response.status_code == 200
    assert response.json()["content"] == [{"type": "text", "text": "make it blue"}]
    await _settle(db)
    assert [(m["role"], m["content"][0]["text"]) for m in db["messages"]] == [
        ("user", "make it blue"),
        ("assistant", "(mock agent) You said: make it blue"),
    ]


async def test_resend_is_409_once_the_agent_has_replied(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "hello")
    response = client.post(f"/api/conversations/{conversation_id}/resend")
    assert response.status_code == 409
    assert response.json()["detail"] == "NOTHING_TO_RESEND"


async def test_submission_to_an_unknown_interaction_is_409(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "question")
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json=_submission(("toolu_nope", [{"status": "aborted"}])),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "INTERACTION_NOT_PENDING"


async def test_an_invalid_submission_is_422_with_a_reason(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "question")  # single-select
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json=_submission(
            (
                _pending_tool_use(db)["id"],
                [{"status": "submitted", "value": {"selected": ["Minimal", "Bold & colorful"], "custom": None}}],
            )
        ),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_SUBMISSION"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "text", "text": ""},
        {"type": "shout", "text": "hi"},
        {"type": "submission", "answers": []},
        {"type": "submission", "answers": [{"interaction_id": "x", "results": []}]},
        {
            "type": "submission",
            "interaction_id": "x",
            "results": [{"status": "aborted"}],
        },  # the old per-interaction shape
    ],
)
def test_malformed_bodies_are_422(body, client, db):
    assert client.post(f"/api/conversations/{_open_conversation(client)}/messages", json=body).status_code == 422


# --- watch_messages ---


async def test_watch_replays_then_waits_then_delivers_new_messages(db, monkeypatch):
    db["messages"] = [{"conversation_id": 1, "seq": seq} for seq in (1, 2, 3)]
    wakeups = []

    @asynccontextmanager
    async def fake_subscription(channel):
        async def receive(timeout_seconds):
            wakeups.append(timeout_seconds)
            # Something lands while we "wait": the next read must pick it up, from the watermark.
            db["messages"] = [*db["messages"], {"conversation_id": 1, "seq": len(db["messages"]) + 1}]
            return "4"

        yield receive

    monkeypatch.setattr(redis, "subscription", fake_subscription)

    stream = agent_service.watch_messages(1, after_seq=1, idle_seconds=15, max_age_seconds=60)
    first, first_devenv_changed = await anext(stream)
    second, second_devenv_changed = await anext(stream)
    await stream.aclose()

    assert [m["seq"] for m in first] == [2, 3]  # replay after the client's seq
    assert [m["seq"] for m in second] == [4]  # only what's new since
    assert not first_devenv_changed and not second_devenv_changed
    assert wakeups == [15]


async def test_watch_yields_an_empty_batch_when_idle_and_ends_at_max_age(db, monkeypatch):
    @asynccontextmanager
    async def fake_subscription(channel):
        async def receive(timeout_seconds):
            return None  # nothing within the timeout

        yield receive

    monkeypatch.setattr(redis, "subscription", fake_subscription)

    yields = [item async for item in agent_service.watch_messages(1, after_seq=0, idle_seconds=0, max_age_seconds=0.01)]
    assert yields and all(item == ([], False) for item in yields)


async def test_watch_passes_a_devenv_wakeup_on_as_a_hint(db, monkeypatch):
    @asynccontextmanager
    async def fake_subscription(channel):
        async def receive(timeout_seconds):
            return "devenv"

        yield receive

    monkeypatch.setattr(redis, "subscription", fake_subscription)
    stream = agent_service.watch_messages(1, after_seq=0, idle_seconds=15, max_age_seconds=60)
    assert await anext(stream) == ([], False)  # the first read: nothing has happened yet
    assert await anext(stream) == ([], True)
    await stream.aclose()


async def test_publishing_a_devenv_change_wakes_the_conversation(db):
    await wakeups.publish_devenv_changed(1)
    assert db["published"] == [("ai_conversation:1", "devenv")]


async def test_the_devenv_status_route_checks_ownership(client, db, monkeypatch):
    async def fake_status(project, conversation_id):
        return {"busy": False, "preview_url": "http://localhost:10102", "servers": []}

    monkeypatch.setattr(conversations_devenv, "get_devenv_status", fake_status)
    conversation_id = _open_conversation(client)
    response = client.get(f"/api/conversations/{conversation_id}/devenv")
    assert response.status_code == 200
    assert response.json() == {
        "stage": "implementing",
        "implementation_done_at": None,
        "busy": False,
        "preview_url": "http://localhost:10102",
        "servers": [],
    }
    assert client.get("/api/conversations/999/devenv").status_code == 404


# --- attachments ---


def _upload(client, conversation_id, *files):
    return client.post(
        f"/api/conversations/{conversation_id}/uploads",
        files=[("files", (name, content, mime)) for name, content, mime in files],
    )


async def test_upload_then_send_lists_the_whole_uploads_folder(client, db):
    conversation_id = _open_conversation(client)
    earlier = _upload(client, conversation_id, ("brief.pdf", b"%PDF-1.4", "application/pdf")).json()
    uploaded = _upload(client, conversation_id, ("logo.png", b"png-bytes", "image/png")).json()

    assert uploaded[0]["name"] == "logo.png" and uploaded[0]["mime"] == "image/png" and uploaded[0]["size"] == 9
    assert db["messages"] == []  # uploading alone posts nothing

    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"type": "text", "text": "use this logo", "attachments": [uploaded[0]["path"]]},
    )
    assert response.status_code == 201
    content = response.json()["content"]
    assert content[0] == {"type": "attachment", **uploaded[0]}
    assert content[1] == {"type": "text", "text": "use this logo"}

    await _settle(db)
    reply = db["messages"][-1]["content"][0]["text"]
    assert "brief.pdf" in reply and "logo.png" in reply  # everything in the folder, not just this message's
    assert earlier[0]["path"] not in [block.get("path") for block in content]


async def test_attachments_alone_are_a_valid_message(client, db):
    conversation_id = _open_conversation(client)
    path = _upload(client, conversation_id, ("a.txt", b"hi", "text/plain")).json()[0]["path"]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"type": "text", "attachments": [path]}
    )
    assert [block["type"] for block in response.json()["content"]] == ["attachment"]
    await _settle(db)


def test_a_message_needs_text_or_attachments(client, db):
    conversation_id = _open_conversation(client)
    response = client.post(f"/api/conversations/{conversation_id}/messages", json={"type": "text", "text": "  "})
    assert response.status_code == 422


@pytest.mark.parametrize("bad_path", ["uploads/never-uploaded.png", "uploads/../../../etc/passwd", "artifacts/x.md"])
def test_attachments_must_be_this_conversations_uploads(bad_path, client, db):
    conversation_id = _open_conversation(client)
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"type": "text", "text": "x", "attachments": [bad_path]}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_ATTACHMENT"


def test_too_many_files_is_413_and_writes_nothing(client, db, tmp_path):
    conversation_id = _open_conversation(client)
    response = _upload(client, conversation_id, *[(f"{index}.txt", b"x", "text/plain") for index in range(11)])
    assert response.status_code == 413
    assert not any(tmp_path.rglob("*.txt"))


def test_an_oversized_file_is_413(client, db, monkeypatch):
    monkeypatch.setattr(conversations, "UPLOAD_MAX_BYTES", 4)
    response = _upload(client, _open_conversation(client), ("big.bin", b"12345", "application/octet-stream"))
    assert response.status_code == 413


def test_uploaded_files_can_be_downloaded_safely(client, db):
    conversation_id = _open_conversation(client)
    path = _upload(client, conversation_id, ("page.html", b"<script>alert(1)</script>", "text/html")).json()[0]["path"]

    response = client.get(f"/api/conversations/{conversation_id}/files", params={"path": path})
    assert response.status_code == 200
    assert response.content == b"<script>alert(1)</script>"
    assert response.headers["content-security-policy"] == "sandbox"  # can't run script on our origin
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'filename="page.html"' in response.headers["content-disposition"]


def test_downloads_support_ranges_and_revalidation_without_leaking_descriptors(client, db):
    conversation_id = _open_conversation(client)
    path = _upload(client, conversation_id, ("clip.mp4", b"0123456789", "video/mp4")).json()[0]["path"]
    url = f"/api/conversations/{conversation_id}/files"
    open_fds = len(os.listdir("/proc/self/fd"))

    ranged = client.get(url, params={"path": path}, headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206 and ranged.content == b"2345"  # video seeking still works
    etag = client.get(url, params={"path": path}).headers["etag"]
    assert client.get(url, params={"path": path}, headers={"If-None-Match": etag}).status_code == 304
    assert len(os.listdir("/proc/self/fd")) == open_fds  # every served file's descriptor got closed


def test_a_file_the_agent_symlinked_out_of_the_project_is_404(client, db, tmp_path):
    conversation_id = _open_conversation(client)
    path = _upload(client, conversation_id, ("a.txt", b"x", "text/plain")).json()[0]["path"]
    secret = tmp_path / "platform.env"
    secret.write_text("SECRET=1")
    uploaded = next(tmp_path.rglob(path.split("/")[-1]))
    uploaded.unlink()
    uploaded.symlink_to(secret)
    response = client.get(f"/api/conversations/{conversation_id}/files", params={"path": path})
    assert response.status_code == 404 and b"SECRET" not in response.content


@pytest.mark.parametrize("bad_path", ["uploads/nope.png", "../../../../etc/passwd", "uploads/../../x"])
def test_downloads_outside_the_conversation_folders_are_404(bad_path, client, db):
    response = client.get(f"/api/conversations/{_open_conversation(client)}/files", params={"path": bad_path})
    assert response.status_code == 404


def test_uploads_and_downloads_check_ownership(client, db):
    db["conversations"] = [{"id": 50, "project_id": 4}]  # someone else's
    assert _upload(client, 50, ("a.txt", b"x", "text/plain")).status_code == 404
    assert client.get("/api/conversations/50/files", params={"path": "uploads/a.txt"}).status_code == 404


# --- artifacts ---


async def test_artifact_command_writes_the_file_and_shows_it(client, db, tmp_path):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "artifact")

    tool_use = db["messages"][-1]["content"][-1]
    assert tool_use["type"] == "tool_use" and tool_use["name"] == "show_artifact"
    assert tool_use["input"] == {"path": "artifacts/website-spec.md", "title": "Website spec"}
    file = client.get(f"/api/conversations/{conversation_id}/files", params={"path": tool_use["input"]["path"]})
    assert file.status_code == 200 and file.headers["content-type"].startswith("text/markdown")


async def test_showing_an_artifact_doesnt_block_the_chat(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "artifacts")
    assert (await _say_and_settle(client, db, conversation_id, "nice")).status_code == 201  # not pending


async def test_artifact_list_has_files_with_their_latest_titles(client, db):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "artifacts")
    # The agent re-shows the spec under a new title later -- the list should pick that up.
    shown_again = {
        **db["messages"][-1],
        "seq": 99,
        "content": [
            {
                "type": "tool_use",
                "id": "t9",
                "name": "show_artifact",
                "input": {"path": "artifacts/website-spec.md", "title": "Website spec v2"},
            }
        ],
    }
    db["messages"] = [*db["messages"], shown_again]

    artifacts = client.get(f"/api/conversations/{conversation_id}/artifacts").json()
    assert {(a["name"], a["title"], a["mime"]) for a in artifacts} == {
        ("website-spec.md", "Website spec v2", "text/markdown"),
        ("color-palette.svg", "Color palette", "image/svg+xml"),
    }


def test_artifact_list_is_empty_before_any_and_checks_ownership(client, db):
    assert client.get(f"/api/conversations/{_open_conversation(client)}/artifacts").json() == []
    db["conversations"] = [*db["conversations"], {"id": 50, "project_id": 4}]
    assert client.get("/api/conversations/50/artifacts").status_code == 404


# --- mock vs. real agent ---


async def test_without_the_mock_messages_go_to_the_agent_and_nothing_replies_here(client, db, monkeypatch):
    sent = []

    async def fake_send_to_agent(conversation, user_message):
        sent.append(user_message)

    monkeypatch.setattr(conversations, "AGENT_MOCK_REPLIES", False)
    monkeypatch.setattr(conversations, "_send_to_agent", fake_send_to_agent)

    response = _say(client, _open_conversation(client), "hello agent")
    assert response.status_code == 201
    assert [message["content"] for message in sent] == [[{"type": "text", "text": "hello agent"}]]
    assert db["spawned"] == []  # no mock reply queued
    assert [message["role"] for message in db["messages"]] == ["user"]


# --- caching ---


def _get_file(client, conversation_id, path, **headers):
    return client.get(f"/api/conversations/{conversation_id}/files", params={"path": path}, headers=headers)


def test_uploads_are_cached_for_good(client, db):
    conversation_id = _open_conversation(client)
    path = _upload(client, conversation_id, ("logo.png", b"png", "image/png")).json()[0]["path"]
    assert _get_file(client, conversation_id, path).headers["cache-control"] == "private, max-age=31536000, immutable"


async def test_artifacts_revalidate_and_get_304_until_they_change(client, db, monkeypatch):
    conversation_id = _open_conversation(client)
    await _say_and_settle(client, db, conversation_id, "artifact")
    path = "artifacts/website-spec.md"

    first = _get_file(client, conversation_id, path)
    assert first.headers["cache-control"] == "private, no-cache"
    etag = first.headers["etag"]
    unchanged = _get_file(client, conversation_id, path, **{"If-None-Match": etag})
    assert unchanged.status_code == 304 and unchanged.content == b""

    # The agent revises the spec (the mock stamps the time, so force a different stamp).
    version_before = client.get(f"/api/conversations/{conversation_id}/artifacts").json()[0]["version"]
    monkeypatch.setattr(mock, "utc_now", lambda: datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC))
    await _say_and_settle(client, db, conversation_id, "artifact")

    changed = _get_file(client, conversation_id, path, **{"If-None-Match": etag})
    assert changed.status_code == 200 and b"12:00:00" in changed.content
    assert changed.headers["etag"] != etag
    assert client.get(f"/api/conversations/{conversation_id}/artifacts").json()[0]["version"] != version_before


# --- the real agent (AGENT_MOCK_REPLIES off) ---


@pytest.fixture
def real_agent(monkeypatch):
    """The mock off, the session faked: records what gets pushed into it."""
    pushed = []

    async def fake_ensure_session(project, conversation_id):
        return 8899

    async def fake_send(project, conversation_id, content):
        pushed.append(content)

    monkeypatch.setattr(conversations, "AGENT_MOCK_REPLIES", False)
    monkeypatch.setattr(sessions, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(sessions, "send", fake_send)
    return pushed


def test_text_is_pushed_into_the_session(client, db, real_agent):
    assert _say(client, _open_conversation(client), "make it blue").status_code == 201
    assert real_agent == [[{"type": "text", "text": "make it blue"}]]


def test_session_questions_can_be_answered_and_the_answers_go_back_in(client, db, real_agent, monkeypatch):
    conversation_id = _open_conversation(client)
    # The session asks something -- the channel sends tool_use items with no id or name.
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")
    asked = client.post(
        "/api/agent/messages",
        json={
            "content": [
                {
                    "type": "tool_use",
                    "input": {
                        "questions": [
                            {"question": "Style?", "options": ["A", "B"], "multi_select": False, "allow_custom": False}
                        ]
                    },
                }
            ]
        },
        headers={"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"},
    ).json()
    tool_use = asked["content"][0]
    assert tool_use["name"] == "ask_user" and tool_use["id"].startswith("toolu_")

    answer = [{"status": "submitted", "value": {"selected": ["B"], "custom": None}}]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json=_submission((tool_use["id"], answer)),
    )
    assert response.status_code == 201
    assert real_agent == [[{"type": "tool_result", "tool_use_id": tool_use["id"], "content": answer}]]


def test_an_unreachable_session_is_503_but_the_message_is_kept(client, db, real_agent, monkeypatch):
    async def unreachable(project, conversation_id, content):
        raise sessions.SessionUnavailable("nobody home")

    monkeypatch.setattr(sessions, "send", unreachable)
    response = _say(client, _open_conversation(client), "hello?")
    assert response.status_code == 503 and response.json()["detail"] == "AGENT_UNAVAILABLE"
    assert [m["content"][0]["text"] for m in db["messages"]] == ["hello?"]


# --- answering several questions of one message ---


def _agent_asks(db, conversation_id, *tool_ids):
    """Store an agent message asking one single-select question per tool id."""
    question = {"question": "Pick?", "options": ["A", "B"], "multi_select": False, "allow_custom": False}
    db["messages"] = [
        *db["messages"],
        {
            "id": 900,
            "conversation_id": conversation_id,
            "seq": len(db["messages"]) + 1,
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "ask_user", "input": {"questions": [question]}}
                for tool_id in tool_ids
            ],
            "meta": {},
        },
    ]


_PICK_A = [{"status": "submitted", "value": {"selected": ["A"], "custom": None}}]
_PICK_B = [{"status": "submitted", "value": {"selected": ["B"], "custom": None}}]


async def test_a_whole_batch_is_stored_in_the_order_the_agent_asked(client, db):
    conversation_id = _open_conversation(client)
    _agent_asks(db, conversation_id, "t1", "t2")
    # answered in the other order -- stored in the agent's order anyway
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json=_submission(("t2", _PICK_B), ("t1", _PICK_A))
    )
    assert response.status_code == 201
    stored = response.json()["content"]
    assert [(block["tool_use_id"], block["content"]) for block in stored] == [("t1", _PICK_A), ("t2", _PICK_B)]
    await _settle(db)


@pytest.mark.parametrize(
    "answers",
    [
        [("t1", _PICK_A)],  # t2 left out
        [("t1", _PICK_A), ("t1", _PICK_A), ("t2", _PICK_B)],  # t1 twice
    ],
)
def test_a_batch_must_answer_every_pending_question_exactly_once(answers, client, db):
    conversation_id = _open_conversation(client)
    _agent_asks(db, conversation_id, "t1", "t2")
    response = client.post(f"/api/conversations/{conversation_id}/messages", json=_submission(*answers))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_SUBMISSION"


# --- approvals (request_actions) ---


def _agent_requests_approval(client, conversation_id, action_name):
    """The session asks for an approval through its channel; returns the stored tool_use."""
    asked = client.post(
        "/api/agent/messages",
        json={"content": [{"type": "tool_use", "input": {"actions": [{"name": action_name, "params": {}}]}}]},
        headers={"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"},
    )
    assert asked.status_code == 201
    return asked.json()["content"][0]


@pytest.fixture
def approvals(monkeypatch):
    """Records what the user's approvals set off, instead of setting anything up."""
    applied = []

    async def fake_apply(project, conversation_id, approved_actions):
        applied.append((project["id"], conversation_id, approved_actions))

    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")
    monkeypatch.setattr(conversations.implementation, "apply_approvals", fake_apply)
    return applied


def test_approving_takes_effect_and_the_agent_hears_it(client, db, real_agent, approvals):
    conversation_id = _open_conversation(client)
    tool_use = _agent_requests_approval(client, conversation_id, "APPROVE_IMPLEMENTATION")  # project 3 implements
    approved = [{"status": "submitted", "value": {"decision": "approved"}}]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json=_submission((tool_use["id"], approved))
    )
    assert response.status_code == 201
    assert approvals == [(3, conversation_id, ["APPROVE_IMPLEMENTATION"])]
    assert real_agent == [[{"type": "tool_result", "tool_use_id": tool_use["id"], "content": approved}]]


def test_keep_iterating_has_no_effect_beyond_the_answer(client, db, real_agent, approvals):
    conversation_id = _open_conversation(client)
    tool_use = _agent_requests_approval(client, conversation_id, "APPROVE_IMPLEMENTATION")
    iterate = [{"status": "submitted", "value": {"decision": "iterate"}}]
    client.post(f"/api/conversations/{conversation_id}/messages", json=_submission((tool_use["id"], iterate)))
    assert approvals == []
    assert real_agent == [[{"type": "tool_result", "tool_use_id": tool_use["id"], "content": iterate}]]


def test_an_approval_holds_even_when_the_session_is_down(client, db, real_agent, approvals, monkeypatch):
    async def unreachable(project, conversation_id, content):
        raise sessions.SessionUnavailable("nobody home")

    conversation_id = _open_conversation(client)
    tool_use = _agent_requests_approval(client, conversation_id, "APPROVE_SPEC")
    monkeypatch.setattr(sessions, "send", unreachable)
    approved = [{"status": "submitted", "value": {"decision": "approved"}}]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json=_submission((tool_use["id"], approved))
    )
    assert response.status_code == 503
    assert approvals == [(3, conversation_id, ["APPROVE_SPEC"])]  # the click counted anyway


def test_a_signal_after_the_question_doesnt_hide_it(client, db, real_agent, approvals):
    conversation_id = _open_conversation(client)
    tool_use = _agent_requests_approval(client, conversation_id, "APPROVE_SPEC")
    client.post(
        "/api/agent/signals",
        json={"slug": "BUILDING_DONE"},
        headers={"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"},
    )
    assert _say(client, conversation_id, "just text").status_code == 409  # still pending: answer it first
    approved = [{"status": "submitted", "value": {"decision": "approved"}}]
    response = client.post(
        f"/api/conversations/{conversation_id}/messages", json=_submission((tool_use["id"], approved))
    )
    assert response.status_code == 201


def test_resend_skips_the_signals_written_after_the_users_message(client, db, real_agent, approvals):
    conversation_id = _open_conversation(client)
    _say(client, conversation_id, "make it blue")
    client.post(
        "/api/agent/signals",
        json={"slug": "BUILDING"},
        headers={"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"},
    )
    response = client.post(f"/api/conversations/{conversation_id}/resend")
    assert response.status_code == 200 and response.json()["content"] == [{"type": "text", "text": "make it blue"}]
