# AI conversation typing (see dto/__init__ for the two kinds):
#  - INTERFACES over the ai_conversations / ai_messages rows the repos return.
#  - The request CONTRACTS: a message a project's agent session posts back, and what the user posts.
# Schema: migrations/0006_ai_conversations.sql, migrations/0007_ai_messages.sql.

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator

from src.constants import UPLOAD_MAX_FILES, InteractionStatus


class AiConversation(TypedDict):
    id: int
    project_id: int
    user_id: int  # who started it (not "the owner" -- see the migration)
    title: str | None
    meta: dict[str, Any]
    created_at: Any  # datetime
    updated_at: Any  # datetime
    deleted_at: Any  # datetime | None -- always None on anything a read returns


class AiMessage(TypedDict):
    id: int
    conversation_id: int
    seq: int  # 1, 2, 3... within the conversation
    role: str  # AiMessageRole
    content: list[dict[str, Any]]  # the API's content blocks, verbatim
    model: str | None
    stop_reason: str | None
    usage: dict[str, Any] | None
    meta: dict[str, Any]
    created_at: Any  # datetime


class ContentBlock(BaseModel):
    """One content block, checked only as far as "it's an object with a string `type`". Every other
    field is kept as-is (extra="allow"): blocks are stored verbatim, and the set of block types keeps
    growing, so a stricter model would silently drop the next new one."""

    model_config = {"extra": "allow"}

    type: str = Field(min_length=1)


class PostAgentMessageRequest(BaseModel):
    """POST /agent/{port}/messages body: one message from the project's agent session."""

    model_config = {"extra": "forbid"}

    content: list[ContentBlock] = Field(min_length=1)


class InteractionResult(BaseModel):
    """One item's outcome in a submission. `value` is only meaningful (and required) when SUBMITTED; for
    a question it's {"selected": [...], "custom": str | None}."""

    model_config = {"extra": "forbid"}

    status: InteractionStatus
    value: Any = None


class TextMessageRequest(BaseModel):
    """A chat message: text, attached files (paths returned by POST /conversations/{id}/uploads), or
    both -- but not neither."""

    model_config = {"extra": "forbid"}

    type: Literal["text"]
    text: str = ""
    attachments: list[str] = Field(default_factory=list, max_length=UPLOAD_MAX_FILES)

    @model_validator(mode="after")
    def _require_text_or_attachments(self):
        if not self.text.strip() and not self.attachments:
            raise ValueError("a message needs text or attachments")
        return self


class InteractionAnswer(BaseModel):
    """The results for one interaction (one tool_use of the agent's), one per question/action, in order."""

    model_config = {"extra": "forbid"}

    interaction_id: str  # the tool_use id
    results: list[InteractionResult] = Field(min_length=1)


class SubmissionRequest(BaseModel):
    """The user's answers to EVERY pending interaction of the agent's message, sent together in one go --
    the FE collects them all before posting. Aborting is a submission too: one whose results contain an
    ABORTED item."""

    model_config = {"extra": "forbid"}

    type: Literal["submission"]
    answers: list[InteractionAnswer] = Field(min_length=1)


# POST /conversations/{id}/messages body.
PostUserMessageRequest = Annotated[TextMessageRequest | SubmissionRequest, Field(discriminator="type")]
