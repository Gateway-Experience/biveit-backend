# The agent service package -- everything about a project's AI agent:
#   adapter.py        the long-running session process working inside the workspace (services/devenv)
#   conversations.py  the chat: messages in from the session and the user, out to the user's tabs
#   interactions.py   the rules of the agent's questions to the user (pending, valid answers)
#   storage.py        the chat's files in the workspace (uploads, artifacts) and the guards
#                     on client-given names and paths
#   sessions.py       the session as the app sees it: keeping it running + its port recorded, and talking
#                     to it over its communication channel (translating to and from the channel's format)
#   devenvs.py        the conversation's dev environment as the chat sees it: the session's setup and
#                     dev server requests, and its status for the user's preview screen
#   building.py       the agent's "building" flag: a big change underway, for the FE's building state
#   tokens.py         the token a session calls back into us with -- good for its own conversation only
#   mock.py           a stand-in agent, used while AGENT_MOCK_REPLIES is on
#
# Callers import THIS package (`from src.services import agent as agent_service`), not the files. The
# exports below are exactly what something outside the package uses -- add to them only when a new
# caller needs it, and drop what stops being used.

from src.services.agent.conversations import (
    ConversationNotFound,
    InteractionNotFound,
    InteractionPending,
    ProjectNotFound,
    UploadRejected,
    get_conversation_file,
    get_current_conversation,
    get_owned_conversation,
    list_artifacts,
    post_user_message,
    receive_session_message,
    upload_files,
    watch_messages,
)
from src.services.agent.devenvs import (
    ImplementationNotStarted,
    control_dev_servers,
    get_devenv_status,
    mark_implementing_done,
    restore_devenv,
    set_building_status,
    start_implementation,
    stop_devenv,
)
from src.services.agent.interactions import InvalidSubmission
from src.services.agent.sessions import SessionUnavailable, ensure_introduced, ensure_session, stop_session
from src.services.agent.storage import FileNotAvailable
from src.services.agent.tokens import get_token_conversation_id

__all__ = [
    "ConversationNotFound",
    "FileNotAvailable",
    "ImplementationNotStarted",
    "InteractionNotFound",
    "InteractionPending",
    "InvalidSubmission",
    "ProjectNotFound",
    "SessionUnavailable",
    "UploadRejected",
    "control_dev_servers",
    "ensure_introduced",
    "ensure_session",
    "get_conversation_file",
    "get_current_conversation",
    "get_devenv_status",
    "get_owned_conversation",
    "get_token_conversation_id",
    "list_artifacts",
    "mark_implementing_done",
    "post_user_message",
    "receive_session_message",
    "restore_devenv",
    "set_building_status",
    "start_implementation",
    "stop_devenv",
    "stop_session",
    "upload_files",
    "watch_messages",
]
