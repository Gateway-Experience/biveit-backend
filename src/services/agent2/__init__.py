# agent2: the Claude Agent SDK runtime for a conversation's agent -- the successor to the tmux + development
# channel runtime in services/agent/adapter.py + src/mcps/communication (AGENT_RUNTIME picks which).
#   sessions.py   each conversation's runner (vib3-session-runner), in its project's container: started when
#                 needed, found by its socket, talked to over it
#   runners.py    the runner installed in a project's container -- copied in, version-checked, kept current
#   models.py     the model proxy: agents call the model through the backend, which holds the key
#
# Only the runtime lives here. What to say to the agent, the chat, signals, approvals -- all of
# services/agent -- stays as is; the backend endpoints the agent calls (controllers/agent) are shared.
# Switching over means services/agent/sessions calling this package instead of its adapter.
#
# Callers import THIS package, not the files.

from src.services.agent2.models import ModelPathRefused, ModelUnavailable, open_model_request
from src.services.agent2.sessions import (
    RunnerUnavailable,
    ensure_session,
    get_log_path,
    get_process_name,
    get_socket_path,
    is_session_up,
    send,
    stop_session,
)

__all__ = [
    "ModelPathRefused",
    "ModelUnavailable",
    "RunnerUnavailable",
    "ensure_session",
    "get_log_path",
    "get_process_name",
    "get_socket_path",
    "is_session_up",
    "open_model_request",
    "send",
    "stop_session",
]
