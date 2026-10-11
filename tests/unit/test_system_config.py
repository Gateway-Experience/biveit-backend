# Runtime-settings tests: the TTL refresh behaviour of the in-memory snapshot. The repo is
# monkeypatched -- no db. (The visibility gate is SQL now -- see repositories/system_configurations.)

import pytest

from src.services import system_config


@pytest.fixture(autouse=True)
def _reset_cache():
    # The snapshot is module-global, so each test starts from a cold one.
    system_config._cache = {}
    system_config._loaded_at = 0.0
    yield
    system_config._cache = {}
    system_config._loaded_at = 0.0


def _rows(calls: list, value):
    async def fake_get_all_enabled():
        calls.append(1)
        return value

    return fake_get_all_enabled


# --- The snapshot + its TTL ---


async def test_get_loads_on_a_cold_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", _rows(calls, {"A": 1}))
    assert await system_config.get("A") == 1
    assert len(calls) == 1


async def test_reads_within_the_ttl_dont_requery(monkeypatch):
    # The point of the snapshot: a read is a dict lookup, not a round trip.
    calls = []
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", _rows(calls, {"A": 1}))
    await system_config.get("A")
    await system_config.get("A")
    await system_config.get("A")
    assert len(calls) == 1


async def test_a_stale_snapshot_reloads(monkeypatch):
    calls = []
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", _rows(calls, {"A": 1}))
    await system_config.get("A")
    # Age the snapshot past the window.
    system_config._loaded_at -= system_config._TTL_SECONDS + 1
    await system_config.get("A")
    assert len(calls) == 2


async def test_a_failed_refresh_serves_the_previous_snapshot(monkeypatch):
    # A db hiccup during a refresh must not turn a working request into a 500 -- stale beats broken.
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", _rows([], {"A": 1}))
    await system_config.get("A")

    async def exploding():
        raise RuntimeError("db is down")

    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", exploding)
    system_config._loaded_at -= system_config._TTL_SECONDS + 1
    assert await system_config.get("A") == 1


async def test_force_load_ignores_the_ttl_and_propagates_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", _rows(calls, {"A": 1}))
    await system_config.force_load()
    await system_config.force_load()
    assert len(calls) == 2  # no TTL gate on the explicit path

    async def exploding():
        raise RuntimeError("db is down")

    # Unlike the lazy path, an explicit reload tells its caller it failed.
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", exploding)
    with pytest.raises(RuntimeError):
        await system_config.force_load()


async def test_absent_and_disabled_settings_fall_back_to_the_default(monkeypatch):
    # get_all_enabled excludes DISABLED rows, so "disabled" and "absent" are the same thing here.
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", _rows([], {"A": 1}))
    assert await system_config.get("NOPE", "fallback") == "fallback"


async def test_get_int_coerces_and_falls_back(monkeypatch):
    monkeypatch.setattr(system_config.system_configurations, "get_all_enabled", _rows([], {"N": "42", "BAD": "abc"}))
    assert await system_config.get_int("N") == 42
    assert await system_config.get_int("BAD", 5) == 5  # a typo shouldn't take down the caller
    assert await system_config.get_int("MISSING", 3) == 3
