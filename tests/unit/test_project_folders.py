# Project folders API tests (controllers/project_folders + services/project_folders). The repo is
# monkeypatched into an in-memory table, so what's under test is the API and the tree rules: the owner
# always comes from the session, someone else's folder (or parent) is a 404, and a move can't land a
# folder inside its own subtree.

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.repositories import project_folders as project_folders_repo
from src.services import project_folders as project_folder_service
from src.utils.auth import get_current_user

_CALLER = {"id": 7, "username": "alice", "user_type": "USER"}


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _CALLER
    return TestClient(app)


@pytest.fixture
def folders(monkeypatch):
    """A tiny in-memory project_folders table: the caller has 1 (root) > 4 > 5, folder 2 is someone else's."""
    rows = {
        1: {
            "id": 1,
            "user_id": 7,
            "parent_id": None,
            "name": "Shops",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "deleted_at": None,
        },
        2: {
            "id": 2,
            "user_id": 8,
            "parent_id": None,
            "name": "Not yours",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
            "deleted_at": None,
        },
        4: {
            "id": 4,
            "user_id": 7,
            "parent_id": 1,
            "name": "Cafes",
            "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T00:00:00Z",
            "deleted_at": None,
        },
        5: {
            "id": 5,
            "user_id": 7,
            "parent_id": 4,
            "name": "Hanoi",
            "created_at": "2026-10-02T00:00:00Z",
            "updated_at": "2026-10-02T00:00:00Z",
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

    async def fake_delete(folder_id):
        rows[folder_id] = {**rows[folder_id], "deleted_at": "2026-10-04T00:00:00Z"}
        return [folder_id]

    monkeypatch.setattr(project_folders_repo, "update", fake_update)
    monkeypatch.setattr(project_folders_repo, "delete", fake_delete)
    return rows


def test_list_is_only_the_callers(client, folders):
    assert [folder["id"] for folder in client.get("/api/project-folders").json()] == [1, 4, 5]


def test_create_trims_the_name_and_owns_it_by_the_caller(client, folders):
    response = client.post("/api/project-folders", json={"name": "  Clients  "})
    assert response.status_code == 201
    assert folders[3]["name"] == "Clients"
    assert folders[3]["user_id"] == 7
    assert folders[3]["parent_id"] is None


def test_create_inside_my_folder(client, folders):
    assert client.post("/api/project-folders", json={"name": "Saigon", "parent_id": 4}).status_code == 201
    assert folders[3]["parent_id"] == 4


def test_create_inside_someone_elses_folder_is_404(client, folders):
    response = client.post("/api/project-folders", json={"name": "Sneaky", "parent_id": 2})
    assert response.status_code == 404
    assert response.json()["detail"] == "PARENT_FOLDER_NOT_FOUND"
    assert 3 not in folders


@pytest.mark.parametrize("body", [{"name": "   "}, {"name": "x" * 201}, {"name": "ok", "user_id": 8}])
def test_bad_bodies_are_422(body, client, folders):
    assert client.post("/api/project-folders", json=body).status_code == 422


def test_rename_and_delete_my_folder(client, folders):
    assert client.patch("/api/project-folders/1", json={"name": "Stores"}).json()["name"] == "Stores"
    assert client.delete("/api/project-folders/1").status_code == 204
    assert folders[1]["deleted_at"] is not None


def test_someone_elses_folder_is_404(client, folders):
    assert client.patch("/api/project-folders/2", json={"name": "mine"}).status_code == 404
    assert client.delete("/api/project-folders/2").status_code == 404
    assert folders[2]["deleted_at"] is None


def test_move_to_another_parent_and_back_to_the_root(client, folders):
    assert client.patch("/api/project-folders/5", json={"parent_id": 1}).json()["parent_id"] == 1
    assert client.patch("/api/project-folders/5", json={"parent_id": None}).json()["parent_id"] is None


def test_rename_alone_leaves_the_parent_alone(client, folders):
    assert client.patch("/api/project-folders/5", json={"name": "HN"}).json()["parent_id"] == 4


@pytest.mark.parametrize("parent_id", [1, 4, 5])
def test_move_into_itself_or_below_is_409(parent_id, client, folders):
    response = client.patch("/api/project-folders/1", json={"parent_id": parent_id})
    assert response.status_code == 409
    assert response.json()["detail"] == "FOLDER_INTO_OWN_SUBTREE"
    assert folders[1]["parent_id"] is None


def test_move_into_someone_elses_folder_is_404(client, folders):
    response = client.patch("/api/project-folders/5", json={"parent_id": 2})
    assert response.json()["detail"] == "PARENT_FOLDER_NOT_FOUND"
    assert folders[5]["parent_id"] == 4


def test_empty_update_is_422(client, folders):
    assert client.patch("/api/project-folders/1", json={}).status_code == 422


def test_name_taken_is_409(client, folders, monkeypatch):
    async def taken(_data):
        raise project_folders_repo.DuplicateFolderName("Shops")

    monkeypatch.setattr(project_folders_repo, "insert", taken)
    response = client.post("/api/project-folders", json={"name": "shops"})
    assert response.status_code == 409
    assert response.json()["detail"] == "FOLDER_NAME_TAKEN"


def test_ancestors_are_nearest_first_and_survive_a_loop():
    tree = {1: {"parent_id": None}, 4: {"parent_id": 1}, 5: {"parent_id": 4}}
    assert project_folder_service.get_ancestor_ids(tree, 5) == (4, 1)
    assert project_folder_service.get_ancestor_ids(tree, 1) == ()
    assert project_folder_service.get_ancestor_ids({1: {"parent_id": 2}, 2: {"parent_id": 1}}, 1) == (2, 1)
