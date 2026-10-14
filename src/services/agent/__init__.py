# The agent service package -- everything about a project's AI agent:
#   adapter.py        its workspace on disk and the long-running session process working inside it
#   conversations.py  the chat: messages in from the session and the user, out to the user's tabs
#   interactions.py   the rules of the agent's questions to the user (pending, valid answers)
#   storage.py        where project/conversation files live on disk (uploads, artifacts) and the guards
#                     on client-given names and paths
#   sessions.py       the session as the app sees it: keeping it running + its port recorded, and talking
#                     to it over its event channel (translating to and from the channel's format)
#   mock.py           a stand-in agent, used while AGENT_MOCK_REPLIES is on
#
# Callers import THIS package (`from src.services import agent as agent_service`), not the files. The
# exports below are exactly what something outside the package uses -- add to them only when a new
# caller needs it, and drop what stops being used.

from src.services.agent.adapter import seed_workspace
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
from src.services.agent.sessions import SessionUnavailable, ensure_session
from src.services.agent.storage import FileNotAvailable

__all__ = [
    "ConversationNotFound",
    "FileNotAvailable",
    "InteractionNotFound",
    "InteractionPending",
    "InvalidSubmission",
    "ProjectNotFound",
    "SessionNotFound",
    "SessionUnavailable",
    "UploadRejected",
    "ensure_session",
    "get_conversation_file",
    "get_current_conversation",
    "get_owned_conversation",
    "list_artifacts",
    "post_user_message",
    "receive_session_message",
    "seed_workspace",
    "upload_files",
    "watch_messages",
]
