# The agent service package -- everything about a project's AI agent:
#   adapter.py        its workspace on disk and the long-running session process working inside it
#   conversations.py  the chat: messages in from the session and the user, out to the user's tabs
#   interactions.py   the rules of the agent's questions to the user (pending, valid answers)
#   storage.py        where project/conversation files live on disk (uploads, artifacts) and the guards
#                     on client-given names and paths
#   mock.py           a stand-in agent until the real session is wired up
#
# Callers import THIS package (`from src.services import agent as agent_service`), not the files. The
# exports below are exactly what something outside the package uses -- add to them only when a new
# caller needs it, and drop what stops being used.

from src.services.agent.adapter import ensure_workspace
from src.services.agent.conversations import (
    ConversationNotFound,
    InteractionNotFound,
    InteractionPending,
    ProjectNotFound,
    SessionNotFound,
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
from src.services.agent.interactions import InvalidSubmission
from src.services.agent.storage import FileNotAvailable

__all__ = [
    "ConversationNotFound",
    "FileNotAvailable",
    "InteractionNotFound",
    "InteractionPending",
    "InvalidSubmission",
    "ProjectNotFound",
    "SessionNotFound",
    "UploadRejected",
    "ensure_workspace",
    "get_conversation_file",
    "get_current_conversation",
    "get_owned_conversation",
    "list_artifacts",
    "post_user_message",
    "receive_session_message",
    "upload_files",
    "watch_messages",
]
