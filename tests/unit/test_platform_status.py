# How a conversation's session is read off what can be seen of it (services/platform_status): the container,
# the runner's pm2 process, and what the runner says on its socket (/health).

import pytest

from src.constants import TurnState
from src.services import platform_status

_NOW = 10_000.0
_ONLINE = {"name": "runner_5", "pm2_env": {"status": "online", "restart_time": 0}}


@pytest.mark.parametrize(
    ("is_container_running", "pm2_process", "health", "verdict_start"),
    [
        (
            True,
            _ONLINE,
            {"runner": "up", "turn": TurnState.IDLE, "since": _NOW - 120, "last_turn": None},
            "idle -- resting for 2 min",
        ),
        (
            True,
            _ONLINE,
            {"runner": "up", "turn": TurnState.WORKING, "since": _NOW - 30, "last_turn": None},
            "WORKING on a turn for 30s",
        ),
        (
            True,
            _ONLINE,
            {"runner": "up", "turn": TurnState.WAITING_ON_USER, "since": _NOW, "last_turn": None},
            "waiting on the user",
        ),
        (True, None, {"runner": "up"}, "up (an older runner"),  # answers, whatever pm2 shows: it's alive
        (False, None, {"runner": "down"}, "OFF -- the project's container"),
        (True, None, {"runner": "down"}, "OFF -- no runner"),
        (True, _ONLINE, {"runner": "hung"}, "HUNG"),
        (
            True,
            {"pm2_env": {"status": "errored", "restart_time": 4}},
            {"runner": "down"},
            "CRASHED -- pm2 says errored, 4 restarts",
        ),
        (True, _ONLINE, {"runner": "down"}, "NOT ANSWERING"),
    ],
)
def test_the_session_verdict(is_container_running, pm2_process, health, verdict_start):
    assert platform_status.to_session_verdict(is_container_running, pm2_process, health, _NOW).startswith(verdict_start)


def test_a_failed_last_turn_is_called_out():
    health = {
        "runner": "up",
        "turn": TurnState.IDLE,
        "since": _NOW,
        "last_turn": {"subtype": "error_during_execution", "is_error": True, "ended_at": _NOW},
    }
    assert platform_status.describe_turn(health, _NOW).endswith("its last turn FAILED (error_during_execution)")
