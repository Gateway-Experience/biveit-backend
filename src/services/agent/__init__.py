# The agent service package -- everything about a project's AI agent:
#   conversations.py  the chat: messages in from the session and the user, out to the user's tabs
#   interactions.py   the rules of the agent's questions/actions to the user (pending, valid answers),
#                     and of what the agent may send at all
#   signals.py        timestamped markers in the log (BUILDING, IMPLEMENTATION_DONE...), state read off its tail
#   implementation.py the implementing milestones the user's approvals set off (start, sign-off)
#   storage.py        the chat's files in the workspace (uploads, artifacts) and the guards
#                     on client-given names and paths
#   sessions.py       the session as the app sees it: keeping it running, and talking to it over its
#                     channel (translating to and from the channel's format)
#   runners.py        the process holding a conversation's session (vib3-session-runner), in its project's
#                     container: started when needed, found by its socket, talked to over it
#   runner_installs.py the runner installed in a project's container -- copied in, version-checked, kept current
#   model_proxy.py    agents call the model through the backend, which holds the key
#   devenvs.py        the conversation's dev environment as the chat sees it: the session's dev server
#                     requests, and its status for the user's preview screen
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
    NothingToResend,
    ProjectNotFound,
    SignalNotAllowed,
    UploadRejected,
    get_conversation_file,
    get_current_conversation,
    get_owned_conversation,
    list_artifacts,
    post_user_message,
    receive_session_message,
    receive_session_signal,
    resend_last_user_message,
    upload_files,
    watch_messages,
)
from src.services.agent.devenvs import (
    ImplementationNotStarted,
    control_dev_servers,
    get_devenv_status,
    restore_devenv,
    stop_devenv,
)
from src.services.agent.interactions import InvalidSessionMessage, InvalidSubmission
from src.services.agent.model_proxy import ModelPathRefused, ModelUnavailable, open_model_request
from src.services.agent.sessions import SessionUnavailable, ensure_introduced, ensure_session, stop_session
from src.services.agent.storage import FileNotAvailable
from src.services.agent.tokens import get_token_conversation_id

__all__ = [
    "ConversationNotFound",
    "FileNotAvailable",
    "ImplementationNotStarted",
    "InteractionNotFound",
    "InteractionPending",
    "InvalidSessionMessage",
    "InvalidSubmission",
    "ModelPathRefused",
    "ModelUnavailable",
    "NothingToResend",
    "ProjectNotFound",
    "SessionUnavailable",
    "SignalNotAllowed",
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
    "open_model_request",
    "post_user_message",
    "receive_session_message",
    "receive_session_signal",
    "resend_last_user_message",
    "restore_devenv",
    "stop_devenv",
    "stop_session",
    "upload_files",
    "watch_messages",
]
