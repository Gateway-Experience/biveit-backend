# agent2: the Claude Agent SDK runtime for a conversation's agent -- the successor to the tmux + development
# channel runtime in services/agent/adapter.py + src/mcps/communication. Built alongside it and not wired
# in yet: services/agent still runs every session the old way.
#   sessions.py   each conversation's runner (vib3-session-runner): started when needed, found by its socket,
#                 talked to over it
#
# Only the runtime lives here. What to say to the agent, the chat, signals, approvals -- all of
# services/agent -- stays as is; the backend endpoints the agent calls (controllers/agent) are shared.
# Switching over means services/agent/sessions calling this package instead of its adapter.
#
# Callers import THIS package, not the files.

from src.services.agent2.sessions import (
    RunnerUnavailable,
    ensure_session,
    get_log_path,
    get_socket_dir,
    get_socket_path,
    get_unit_name,
    is_session_up,
    send,
    stop_session,
)

__all__ = [
    "RunnerUnavailable",
    "ensure_session",
    "get_log_path",
    "get_socket_dir",
    "get_socket_path",
    "get_unit_name",
    "is_session_up",
    "send",
    "stop_session",
]
