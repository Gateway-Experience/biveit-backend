# Avatar tests (services/users/avatars + its endpoints): only real images get in, files land in the
# user's own folder and the old one is cleared, and nothing malformed reaches the filesystem when serving.
# Storage is a temp dir; the users repo is monkeypatched.

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.repositories import users as users_repo
from src.services import users as user_service
from src.services.users.avatars import detect_image_extension
from src.utils.auth import get_current_user

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_USER = {
    "id": 7,
    "hash": "u7hash",
    "username": "a@b.c",
    "user_type": "USER",
    "first_name": "Ana",
    "last_name": "",
    "profile_picture": None,
    "language": "en",
    "platform": "WEB",
    "platform_id": "a@b.c",
    "tags": [],
    "meta": {},
    "created_at": "2026-10-01T00:00:00Z",
    "updated_at": "2026-10-01T00:00:00Z",
    "deleted_at": None,
    "has_password": True,
    "role": None,
}


@pytest.fixture(autouse=True)
def storage(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def saved_rows(monkeypatch):
    rows = {7: dict(_USER)}

    async def fake_update(user_id, updates):
        rows[user_id] = {**rows[user_id], **updates}
        return rows[user_id]

    monkeypatch.setattr(users_repo, "update", fake_update)
    return rows


@pytest.mark.parametrize(
    ("data", "extension"),
    [
        (_PNG, "png"),
        (_JPEG, "jpg"),
        (b"GIF89a....", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None),
        (b"", None),
    ],
)
def test_detects_images_by_their_bytes(data, extension):
    assert detect_image_extension(data) == extension


async def test_a_new_avatar_replaces_the_old_file(saved_rows, storage):
    first = await user_service.save_avatar(_USER, _PNG)
    second = await user_service.save_avatar(_USER, _JPEG)
    assert first["profile_picture"].startswith("/avatars/u7hash/avatar-")
    assert second["profile_picture"].endswith(".jpg")
    files = list((storage / "u7hash" / "profile").iterdir())
    assert [path.name for path in files] == [second["profile_picture"].rsplit("/", 1)[1]]


async def test_non_images_and_oversized_files_are_refused(saved_rows, monkeypatch):
    with pytest.raises(user_service.InvalidAvatar):
        await user_service.save_avatar(_USER, b"<svg onload='alert(1)'/>")
    monkeypatch.setattr("src.services.users.avatars.AVATAR_MAX_BYTES", 10)
    with pytest.raises(user_service.InvalidAvatar):
        await user_service.save_avatar(_USER, _PNG)
    assert saved_rows[7]["profile_picture"] is None


async def test_remove_clears_the_picture_and_the_files(saved_rows, storage):
    await user_service.save_avatar(_USER, _PNG)
    removed = await user_service.remove_avatar(_USER)
    assert removed["profile_picture"] is None
    assert list((storage / "u7hash" / "profile").iterdir()) == []


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _USER
    return TestClient(app)


def test_upload_then_serve(client, saved_rows):
    response = client.put("/api/me/avatar", files={"file": ("me.png", _PNG, "image/png")})
    assert response.status_code == 200
    served = client.get(f"/api{response.json()['profile_picture']}")
    assert served.status_code == 200
    assert served.content == _PNG
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"


def test_upload_of_a_non_image_is_400(client, saved_rows):
    response = client.put("/api/me/avatar", files={"file": ("me.png", b"not really", "image/png")})
    assert (response.status_code, response.json()["detail"]) == (400, "INVALID_AVATAR")


@pytest.mark.parametrize(
    "path", ["u7hash/avatar-0123456789abcdef.png", "..%2F..%2Fetc/passwd", "u7hash/..%2Fsecret.png", "u7hash/me.png"]
)
def test_serving_anything_missing_or_malformed_is_404(client, path):
    assert client.get(f"/api/avatars/{path}").status_code == 404
