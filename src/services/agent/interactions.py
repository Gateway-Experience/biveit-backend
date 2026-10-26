# Intentions: the rules of user interactions -- the agent asking the user something through a
# user-facing tool (ask_user: multiple-choice questions; request_actions: something to do in the UI, like
# approving the spec) and the user answering. Also what the agent may put in a message in the first place.
# All pure: everything here is derived from the conversation's message log, so there's no "pending
# interaction" state to keep in sync anywhere.
#
# References: the shapes are documented on ASK_USER_TOOL_NAME / REQUEST_ACTIONS_TOOL_NAME / AgentAction /
# InteractionStatus in constants.py.

import re
from typing import Any

import pydash

from src.constants import (
    ASK_USER_TOOL_NAME,
    SIGNAL_BLOCK_TYPE,
    USER_FACING_TOOLS,
    AgentAction,
    AiMessageRole,
    ApprovalDecision,
    InteractionStatus,
    ProjectStage,
)

# Status -> one letter, so the ordering rule can be a regex over the whole array (see below).
_STATUS_LETTERS = {InteractionStatus.SUBMITTED: "s", InteractionStatus.ABORTED: "a", InteractionStatus.UNTOUCHED: "u"}


class InvalidSubmission(Exception):
    """The results don't fit the interaction (wrong length, bad ordering, an answer that isn't one of
    the options...). The message says what's wrong. 422."""


class InvalidSessionMessage(Exception):
    """A message from the agent's session breaks the rules of what it may send (a signal smuggled in, an
    unknown action, an action that makes no sense at the project's stage). The message says what. 422,
    so the agent sees why its tool failed."""


def find_pending_interactions(messages: list) -> list[dict]:
    """[Pure] The user-facing tool_use blocks still waiting on the user: every one in the assistant
    messages after the latest USER message. An answer is itself a user message, so once the user has
    answered, nothing's pending -- and since text is refused while something is pending, the only way
    past it is answering. Messages the agent (or we) add after asking -- signals, a bit of text -- don't
    hide what it asked."""
    last_user_index = pydash.find_last_index(messages, lambda message: message["role"] == AiMessageRole.USER)
    return [
        block
        for message in messages[last_user_index + 1 :]
        for block in message["content"]
        if block.get("type") == "tool_use" and block.get("name") in USER_FACING_TOOLS
    ]


def validate_interaction_results(tool_use: dict, results: list[dict]) -> None:
    """[Pure] Raise InvalidSubmission unless `results` is a valid answer to one pending tool_use: as many
    results as it has items (questions or actions), statuses all SUBMITTED or SUBMITTED* ABORTED UNTOUCHED*,
    and every submitted answer fits its item."""
    is_questions = tool_use["name"] == ASK_USER_TOOL_NAME
    items = tool_use["input"]["questions" if is_questions else "actions"]
    check_answer = _check_question_answer if is_questions else _check_action_answer
    if len(results) != len(items):
        raise InvalidSubmission(f"expected {len(items)} results, got {len(results)}")
    status_pattern = "".join(_STATUS_LETTERS[result["status"]] for result in results)
    if not re.fullmatch(r"s*(au*)?", status_pattern):
        raise InvalidSubmission("statuses must be all submitted, or submitted... then one aborted then untouched...")
    errors = pydash.compact(
        [
            check_answer(index, item, result.get("value"))
            for index, (item, result) in enumerate(zip(items, results, strict=True))
            if result["status"] == InteractionStatus.SUBMITTED
        ]
    )
    if errors:
        raise InvalidSubmission("; ".join(errors))


def get_approved_actions(answered: list[tuple[dict, list[dict]]]) -> list[AgentAction]:
    """[Pure] The approval actions the user approved, out of `answered` -- [(tool_use, its results)] of one
    submission, already validated. Only these have an effect beyond the agent hearing the answer."""
    return [
        AgentAction(action["name"])
        for tool_use, results in answered
        if tool_use["name"] != ASK_USER_TOOL_NAME
        for action, result in zip(tool_use["input"]["actions"], results, strict=True)
        if result["status"] == InteractionStatus.SUBMITTED and result["value"]["decision"] == ApprovalDecision.APPROVED
    ]


def validate_session_content(content: list[dict], stage: ProjectStage) -> None:
    """[Pure] Raise InvalidSessionMessage unless a message from the session (channel format) is one the
    agent may send: no signal blocks (signals go through POST /agent/signals, which checks who may send
    which), only known actions, and APPROVE_BUILD only once there's a build to approve.
    The MCP's own schema already keeps the agent in line -- this is the backend not trusting it to."""
    if any(block.get("type") == SIGNAL_BLOCK_TYPE for block in content):
        raise InvalidSessionMessage("signals can't be sent as a message -- use the send_signal tool")
    action_names = [
        action.get("name")
        for block in content
        if block.get("type") == "tool_use"
        for action in pydash.get(block, "input.actions") or []
    ]
    unknown = [name for name in action_names if name not in list(AgentAction)]
    if unknown:
        raise InvalidSessionMessage(f"unknown actions: {unknown}; known: {list(AgentAction)}")
    if AgentAction.APPROVE_BUILD in action_names and stage != ProjectStage.BUILDING:
        raise InvalidSessionMessage("APPROVE_BUILD only once the Building stage has started (approve the plan first)")


def _check_question_answer(index: int, question: dict, value: Any) -> str | None:
    """[Pure] What's wrong with one submitted answer, or None if it's fine. A single-select question
    takes exactly one answer: one option OR a custom text. A multi-select one takes any number of
    options, none included, plus an optional custom text."""
    if not isinstance(value, dict) or not isinstance(value.get("selected"), list):
        return f"#{index}: value must be {{selected: [...], custom: str | null}}"
    selected, custom = value["selected"], value.get("custom")
    unknown = [option for option in selected if option not in get_option_labels(question)]
    if unknown:
        return f"#{index}: not among the options: {unknown}"
    if len(set(selected)) != len(selected):
        return f"#{index}: an option is selected twice"
    if custom is not None and not (isinstance(custom, str) and custom.strip()):
        return f"#{index}: custom must be a non-empty string or null"
    if custom is not None and not question.get("allow_custom", True):
        return f"#{index}: this question doesn't take a custom answer"
    if not question.get("multi_select", False) and len(selected) + (custom is not None) != 1:
        return f"#{index}: single-select takes exactly one answer (an option or a custom text)"
    return None


def get_option_labels(question: dict) -> list[str]:
    """[Pure] A question's option labels -- what an answer selects. An option is either its label (a plain
    string) or {label, description?}, the description being a subtitle the FE shows under it."""
    return [option if isinstance(option, str) else option["label"] for option in question["options"]]


def _check_action_answer(index: int, action: dict, value: Any) -> str | None:
    """[Pure] What's wrong with one submitted action answer, or None if it's fine. Every action so far is
    an approval, answered {decision: ApprovalDecision}."""
    if not isinstance(value, dict) or value.get("decision") not in list(ApprovalDecision):
        return f"#{index} ({action.get('name')}): value must be {{decision: {' | '.join(ApprovalDecision)}}}"
    return None


def to_tool_result_block(tool_use_id: str, results: list[dict]) -> dict:
    """[Pure] The tool_result block that carries a submission back to the agent: content is the results
    array, `value` kept only on submitted items (the others just say their status).

    Stored as real JSON, not stringified. The LLM API only takes a string (or text blocks) as
    tool_result content, so whatever sends the conversation to the model must json.dumps it there --
    with sort_keys=True, so replays produce identical bytes and prompt caching keeps hitting."""
    trimmed_results = [
        result if result["status"] == InteractionStatus.SUBMITTED else {"status": result["status"]}
        for result in results
    ]
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": trimmed_results}
