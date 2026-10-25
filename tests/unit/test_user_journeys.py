# User journeys API tests (controllers/users -> repositories/user_journey_steps). The repo is monkeypatched,
# so what's under test is the API: the owner always comes from the session, only known journeys are taken,
# and the read hands back each journey's latest step keyed by journey.

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.repositories import user_journey_steps as user_journey_steps_repo
from src.utils.auth import get_current_user

_CALLER = {"id": 7, "username": "alice", "user_type": "USER"}


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _CALLER
    return TestClient(app)


def test_a_step_is_logged_for_the_caller(client, monkeypatch):
    inserted = []

    async def fake_insert(data):
        inserted.append(data)
        return {"id": 1, "created_at": "2026-10-25T00:00:00Z", "deleted_at": None, **data}

    monkeypatch.setattr(user_journey_steps_repo, "insert", fake_insert)

    response = client.post(
        "/api/me/journeys/projects_intro/steps", json={"step": "finished", "data": {"skipped": True}}
    )

    assert response.status_code == 201
    assert inserted == [{"user_id": 7, "journey": "projects_intro", "step": "finished", "data": {"skipped": True}}]
    assert response.json()["step"] == "finished"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/me/journeys/no_such_journey/steps", {"step": "finished"}),
        ("/api/me/journeys/projects_intro/steps", {"step": "  "}),
        # The owner comes from the session, never the body.
        ("/api/me/journeys/projects_intro/steps", {"step": "finished", "user_id": 8}),
    ],
)
def test_a_bad_step_is_refused(client, monkeypatch, path, body):
    async def fake_insert(_data):
        raise AssertionError("nothing should be written")

    monkeypatch.setattr(user_journey_steps_repo, "insert", fake_insert)

    assert client.post(path, json=body).status_code == 422


def test_journeys_come_back_keyed_by_journey(client, monkeypatch):
    async def fake_find_latest(user_id):
        assert user_id == 7
        return [
            {
                "id": 3,
                "user_id": 7,
                "journey": "projects_intro",
                "step": "finished",
                "data": {},
                "created_at": "2026-10-25T00:00:00Z",
                "deleted_at": None,
            }
        ]

    monkeypatch.setattr(user_journey_steps_repo, "find_latest", fake_find_latest)

    response = client.get("/api/me/journeys")

    assert response.status_code == 200
    assert response.json()["projects_intro"]["step"] == "finished"
