# Project folders API tests (controllers/project_folders). The repo is monkeypatched into an in-memory
# table, so what's under test is the controller: the owner always comes from the session, someone else's
# folder is a 404, and deleting a folder is a soft delete that leaves its projects alone.

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.repositories import project_folders as project_folders_repo
from src.utils.auth import get_current_user

_CALLER = {"id": 7, "username": "alice", "user_type": "USER"}


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _CALLER
    return TestClient(app)


@pytest.fixture
def folders(monkeypatch):
    """A tiny in-memory project_folders table: folder 1 is the caller's, folder 2 someone else's."""
    rows = {
        1: {
            "id": 1,
            "user_id": 7,
            "name": "Shops",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "deleted_at": None,
        },
        2: {
            "id": 2,
            "user_id": 8,
            "name": "Not yours",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "deleted_at": None,
        },
    }

    async def fake_find(criteria):
        return [row for row in rows.values() if row["deleted_at"] is None and criteria.items() <= row.items()]

    async def fake_insert(data):
        rows[3] = {
            "id": 3,
            "created_at": "2026-10-03T00:00:00Z",
            "updated_at": "2026-10-03T00:00:00Z",
            "deleted_at": None,
            **data,
        }
        return rows[3]

    async def fake_update(folder_id, updates):
        rows[folder_id] = {**rows[folder_id], **updates}
        return rows[folder_id]

    monkeypatch.setattr(project_folders_repo, "find", fake_find)
    monkeypatch.setattr(project_folders_repo, "insert", fake_insert)
    monkeypatch.setattr(project_folders_repo, "update", fake_update)
    return rows


def test_list_is_only_the_callers(client, folders):
    assert [folder["id"] for folder in client.get("/api/project-folders").json()] == [1]


def test_create_trims_the_name_and_owns_it_by_the_caller(client, folders):
    response = client.post("/api/project-folders", json={"name": "  Clients  "})
    assert response.status_code == 201
    assert folders[3]["name"] == "Clients"
    assert folders[3]["user_id"] == 7


@pytest.mark.parametrize("body", [{"name": "   "}, {"name": "x" * 201}, {"name": "ok", "user_id": 8}])
def test_bad_bodies_are_422(body, client, folders):
    assert client.post("/api/project-folders", json=body).status_code == 422


def test_rename_and_delete_my_folder(client, folders):
    assert client.patch("/api/project-folders/1", json={"name": "Stores"}).json()["name"] == "Stores"
    assert client.delete("/api/project-folders/1").status_code == 204
    assert folders[1]["deleted_at"] is not None
    assert client.get("/api/project-folders").json() == []


def test_someone_elses_folder_is_404(client, folders):
    assert client.patch("/api/project-folders/2", json={"name": "mine"}).status_code == 404
    assert client.delete("/api/project-folders/2").status_code == 404
    assert folders[2]["deleted_at"] is None
