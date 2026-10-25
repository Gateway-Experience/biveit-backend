# User-service tests: the authentication decisions, the guest lifecycle, Google sign-in, and the
# client-input allowlist. The repo is monkeypatched, so no db is involved -- what's under test is the
# service's own logic.

import bcrypt
import pytest
from pydantic import ValidationError

from src.config import settings
from src.constants import UserTour, UserType
from src.dto.users import RegisterRequest, UpdateMeRequest
from src.repositories import users as users_repo
from src.services import users as user_service
from src.services.users.users import to_profile

# See tests/unit/test_auth.py -- HS256 wants >= 32 bytes.
_SECRET = "test-jwt-secret-of-a-realistic-length-0001"


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    # Every path here mints a token at the end, which needs a secret.
    monkeypatch.setattr(settings, "jwt_secret", _SECRET)
    monkeypatch.setattr(settings, "master_password", "")


def _user(**overrides):
    return {
        "id": 1,
        "username": "alice",
        "user_type": UserType.USER,
        "tags": [],
        "deleted_at": None,
        **overrides,
    }


# --- authenticate ---


async def test_authenticate_accepts_the_right_password(monkeypatch):
    hashed = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()

    async def fake_auth_row(username):
        return {"id": 1, "password_hash": hashed}

    async def fake_get(user_id):
        return _user()

    monkeypatch.setattr(users_repo, "get_auth_by_username", fake_auth_row)
    monkeypatch.setattr(users_repo, "get", fake_get)

    result = await user_service.authenticate("alice", "hunter2")
    assert result["user"]["username"] == "alice"
    assert result["token"]


async def test_authenticate_rejects_the_wrong_password(monkeypatch):
    hashed = bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode()

    async def fake_auth_row(username):
        return {"id": 1, "password_hash": hashed}

    monkeypatch.setattr(users_repo, "get_auth_by_username", fake_auth_row)
    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate("alice", "wrong")


async def test_authenticate_rejects_unknown_username_the_same_way(monkeypatch):
    # Same exception as a wrong password -- that's what stops this endpoint being a username oracle.
    async def fake_auth_row(username):
        return None

    monkeypatch.setattr(users_repo, "get_auth_by_username", fake_auth_row)
    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate("nobody", "hunter2")


async def test_authenticate_rejects_a_passwordless_account(monkeypatch):
    # A Google-only user has no local password; presenting any password must fail closed rather than
    # comparing against an empty hash.
    async def fake_auth_row(username):
        return {"id": 1, "password_hash": None}

    monkeypatch.setattr(users_repo, "get_auth_by_username", fake_auth_row)
    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate("alice", "anything")


async def test_master_password_authenticates_as_any_user(monkeypatch):
    monkeypatch.setattr(settings, "master_password", "skeleton")

    async def fake_by_username(username):
        return _user(username=username)

    monkeypatch.setattr(users_repo, "get_by_username", fake_by_username)
    result = await user_service.authenticate("alice", "skeleton")
    assert result["user"]["username"] == "alice"


async def test_master_password_tolerates_a_non_ascii_password(monkeypatch):
    # hmac.compare_digest REJECTS non-ASCII str with a TypeError, so a comparison done on raw strings
    # would turn a perfectly ordinary password into a 500. It must just fail to match.
    monkeypatch.setattr(settings, "master_password", "skeleton")

    async def fake_auth_row(username):
        return None

    monkeypatch.setattr(users_repo, "get_auth_by_username", fake_auth_row)
    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate("alice", "pässwörd")


async def test_master_password_is_off_when_unset(monkeypatch):
    # The empty default must not become a password that matches an empty input.
    async def fake_auth_row(username):
        return None

    monkeypatch.setattr(users_repo, "get_auth_by_username", fake_auth_row)
    with pytest.raises(user_service.InvalidCredentials):
        await user_service.authenticate("alice", "")


# --- client input allowlist ---


def test_to_profile_never_lets_a_client_pick_a_privileged_column():
    profile = to_profile({"first_name": "Alice", "platform_id": "victim-sub", "user_type": "USER", "colour": "red"})
    assert profile == {
        "first_name": "Alice",
        "meta": {"platform_id": "victim-sub", "user_type": "USER", "colour": "red"},
    }


def test_to_profile_without_extras_has_no_meta():
    assert to_profile({"first_name": "Alice"}) == {"first_name": "Alice"}
    assert to_profile(None) == {}


async def test_register_vets_user_data(monkeypatch):
    captured = {}

    async def fake_insert(data):
        captured.update(data)
        return _user(**data)

    monkeypatch.setattr(users_repo, "insert", fake_insert)
    await user_service.register("alice", "hunter2", {"platform_id": "victim-sub", "first_name": "Alice"})
    assert captured["platform_id"] == "alice"  # the WEB identity, not the injected value
    assert captured["platform"] == "WEB"
    assert captured["first_name"] == "Alice"


# --- the guest lifecycle ---


async def test_promote_guest_keeps_the_same_row(monkeypatch):
    # The whole point of promotion: the guest's id survives, so whatever they accumulated is still
    # theirs afterwards.
    captured = {}

    async def fake_update(user_id, updates):
        captured["user_id"] = user_id
        captured["updates"] = updates
        return _user(id=user_id, user_type=UserType.USER)

    monkeypatch.setattr(users_repo, "update", fake_update)
    result = await user_service.promote_guest(_user(user_type=UserType.GUEST), "alice", "hunter2")

    assert captured["user_id"] == 1
    assert captured["updates"]["user_type"] == UserType.USER
    assert captured["updates"]["password_hash"] != "hunter2"  # stored hashed, never raw
    assert (captured["updates"]["platform"], captured["updates"]["platform_id"]) == ("WEB", "alice")
    assert result["user"]["id"] == 1


async def test_promote_guest_refuses_a_real_user():
    with pytest.raises(user_service.NotAGuest):
        await user_service.promote_guest(_user(user_type=UserType.USER), "alice", "hunter2")


async def test_promote_guest_reports_a_vanished_guest(monkeypatch):
    async def fake_update(user_id, updates):
        return None

    monkeypatch.setattr(users_repo, "update", fake_update)
    with pytest.raises(user_service.UserNotFound):
        await user_service.promote_guest(_user(user_type=UserType.GUEST), "alice", "hunter2")


# --- Google sign-in (claims are what Authlib verified) ---


def _claims(**overrides):
    return {
        "sub": "1234567890",
        "email": "a@b.c",
        "email_verified": True,
        "name": "Alice",
        "picture": "https://pic",
        **overrides,
    }


class _FakeUsersRepo:
    """An in-memory users table, so the Google branches can be asserted on outcomes (rows) rather than
    on which repo calls happened."""

    def __init__(self, rows):
        self.rows = {row["id"]: row for row in rows}

    async def get(self, user_id):
        return self.rows.get(user_id)

    async def get_by_platform(self, platform, platform_id):
        return next(
            (
                row
                for row in self.rows.values()
                if (row.get("platform"), row.get("platform_id")) == (platform, platform_id)
            ),
            None,
        )

    async def get_by_username(self, username):
        return next((row for row in self.rows.values() if row.get("username") == username), None)

    async def update(self, user_id, updates):
        if user_id not in self.rows:
            return None
        self.rows = {**self.rows, user_id: {**self.rows[user_id], **updates}}
        return self.rows[user_id]

    async def insert(self, data):
        new_id = max(self.rows, default=0) + 1
        self.rows = {**self.rows, new_id: _user(id=new_id, **data)}
        return self.rows[new_id]


@pytest.fixture
def fake_repo(monkeypatch):
    def install(rows):
        repo = _FakeUsersRepo(rows)
        for name in ["get", "get_by_platform", "get_by_username", "update", "insert"]:
            monkeypatch.setattr(users_repo, name, getattr(repo, name))
        return repo

    return install


async def test_google_creates_a_new_user_keyed_on_sub(fake_repo):
    repo = fake_repo([])
    result = await user_service.signin_google(_claims())
    assert result["is_new"] is True
    assert (result["user"]["platform"], result["user"]["platform_id"]) == ("GOOGLE", "1234567890")  # the sub
    assert result["user"]["username"] == "a@b.c"
    assert result["user"]["tags"] == ["GOOGLE"]
    assert len(repo.rows) == 1


async def test_google_existing_sub_signs_in_without_duplicating(fake_repo):
    repo = fake_repo([_user(id=7, platform="GOOGLE", platform_id="1234567890", username="a@b.c")])
    result = await user_service.signin_google(_claims(picture="https://new-pic"))
    assert result["is_new"] is False
    assert result["user"]["id"] == 7
    assert result["user"]["profile_picture"] == "https://new-pic"
    assert len(repo.rows) == 1


async def test_google_leaves_an_uploaded_avatar_alone(fake_repo):
    uploaded = "/avatars/u7hash/avatar-0123456789abcdef.png"
    fake_repo([_user(id=7, platform="GOOGLE", platform_id="1234567890", username="a@b.c", profile_picture=uploaded)])
    result = await user_service.signin_google(_claims(picture="https://new-pic"))
    assert result["user"]["profile_picture"] == uploaded


async def test_google_promotes_the_guest_when_sub_is_new(fake_repo):
    repo = fake_repo([_user(id=3, username=None, user_type=UserType.GUEST, tags=["EARLY"])])
    result = await user_service.signin_google(_claims(), guest_id=3)
    assert result["is_new"] is True
    assert result["user"]["id"] == 3  # promoted in place
    assert repo.rows[3]["user_type"] == UserType.USER
    assert repo.rows[3]["tags"] == ["EARLY", "GOOGLE"]
    assert len(repo.rows) == 1


async def test_google_leaves_the_guest_alone_when_sub_exists(fake_repo):
    guest = _user(id=3, username=None, user_type=UserType.GUEST)
    repo = fake_repo([guest, _user(id=7, platform="GOOGLE", platform_id="1234567890")])
    result = await user_service.signin_google(_claims(), guest_id=3)
    assert result["user"]["id"] == 7
    assert repo.rows[3] == guest


async def test_google_never_promotes_a_real_user_named_as_guest(fake_repo):
    repo = fake_repo([_user(id=3, user_type=UserType.USER, username="bob")])
    result = await user_service.signin_google(_claims(), guest_id=3)
    assert result["user"]["id"] != 3
    assert repo.rows[3]["username"] == "bob"


async def test_google_rejects_an_unverified_email(fake_repo):
    repo = fake_repo([])
    with pytest.raises(user_service.GoogleEmailNotVerified):
        await user_service.signin_google(_claims(email_verified=False))
    assert repo.rows == {}


async def test_google_new_account_with_a_registered_email_fails(fake_repo):
    # No merging: a password account already uses this email as its username.
    password_account = _user(id=1, username="a@b.c")
    repo = fake_repo([password_account])
    with pytest.raises(user_service.GoogleEmailTaken):
        await user_service.signin_google(_claims())
    assert repo.rows == {1: password_account}


async def test_google_registered_email_leaves_the_guest_untouched(fake_repo):
    # The check fails a GUEST promotion too -- the guest stays a guest.
    guest = _user(id=3, username=None, user_type=UserType.GUEST)
    repo = fake_repo([guest, _user(id=1, username="a@b.c")])
    with pytest.raises(user_service.GoogleEmailTaken):
        await user_service.signin_google(_claims(), guest_id=3)
    assert repo.rows[3] == guest


async def test_google_existing_sub_signs_in_even_if_email_matches_a_username(fake_repo):
    # Their own Google account's username IS the email -- that's a sign-in, not a collision.
    fake_repo([_user(id=7, platform="GOOGLE", platform_id="1234567890", username="a@b.c")])
    result = await user_service.signin_google(_claims())
    assert result["user"]["id"] == 7


async def test_google_username_is_the_email(fake_repo):
    fake_repo([])
    result = await user_service.signin_google(_claims())
    assert result["user"]["username"] == "a@b.c"


# --- language (PATCH /me) ---


@pytest.mark.parametrize(
    ("body", "valid"),
    [
        ({"language": "vi"}, True),
        ({"language": "en"}, True),
        ({}, True),  # left out: unchanged
        ({"language": "fr"}, False),  # not one the FE offers
        ({"language": None}, False),  # a user always has one
    ],
)
def test_the_language_can_be_set_to_a_supported_one(body, valid):
    if valid:
        assert UpdateMeRequest(**body).model_dump(exclude_unset=True) == body
    else:
        with pytest.raises(ValidationError):
            UpdateMeRequest(**body)


# --- names ---


def test_signup_needs_a_first_name_but_not_a_last_one():
    request = RegisterRequest(username="a@b.c", password="password1", first_name="  Ana ")
    assert (request.first_name, request.last_name) == ("Ana", "")
    with pytest.raises(ValidationError):
        RegisterRequest(username="a@b.c", password="password1", first_name="   ")
    with pytest.raises(ValidationError):
        RegisterRequest(username="a@b.c", password="password1")


def test_profile_edit_refuses_a_null_name():
    assert UpdateMeRequest(last_name="").model_dump(exclude_unset=True) == {"last_name": ""}
    with pytest.raises(ValidationError):
        UpdateMeRequest(first_name=None)


# --- change_password ---


def _patch_auth(monkeypatch, password_hash):
    state = {"password_hash": password_hash}

    async def fake_auth(user_id):
        return {"id": 1, "password_hash": state["password_hash"]} if user_id == 1 else None

    async def fake_update(user_id, updates):
        state["password_hash"] = updates["password_hash"]
        return _user()

    monkeypatch.setattr(users_repo, "get_auth", fake_auth)
    monkeypatch.setattr(users_repo, "update", fake_update)
    return state


async def test_change_password_needs_the_current_one(monkeypatch):
    state = _patch_auth(monkeypatch, bcrypt.hashpw(b"old-password", bcrypt.gensalt()).decode())
    with pytest.raises(user_service.WrongPassword):
        await user_service.change_password(1, "not-it", "new-password")
    with pytest.raises(user_service.WrongPassword):
        await user_service.change_password(1, None, "new-password")
    await user_service.change_password(1, "old-password", "new-password")
    assert bcrypt.checkpw(b"new-password", state["password_hash"].encode())


async def test_a_google_only_account_sets_its_first_password_without_one(monkeypatch):
    state = _patch_auth(monkeypatch, None)
    await user_service.change_password(1, None, "first-password")
    assert bcrypt.checkpw(b"first-password", state["password_hash"].encode())


# --- guided tours ---


async def test_finishing_a_tour_merges_its_time_into_meta(monkeypatch):
    calls = []

    async def fake_update(user_id, row):
        calls.append((user_id, row))
        return _user(meta={"colour": "red", **row["meta"]})

    monkeypatch.setattr(users_repo, "update", fake_update)

    user = await user_service.finish_tour(1, UserTour.PROJECTS)
    # Only the tour's own key is sent: the repo merges it in, so the rest of meta is left alone.
    assert list(calls[0][1]["meta"]) == ["projects_tour_finished_at"]
    assert user["meta"]["colour"] == "red"
    assert user["meta"]["projects_tour_finished_at"]


async def test_finishing_a_tour_for_a_vanished_user_fails(monkeypatch):
    async def fake_update(user_id, row):
        return None

    monkeypatch.setattr(users_repo, "update", fake_update)

    with pytest.raises(user_service.UserNotFound):
        await user_service.finish_tour(1, UserTour.PROJECTS)
