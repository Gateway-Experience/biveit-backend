# Intentions: the rules of user interactions -- the agent asking the user something through a
# user-facing tool (for now only ask_user, i.e. multiple-choice questions) and the user answering.
# All pure: everything here is derived from the conversation's message log, so there's no "pending
# interaction" state to keep in sync anywhere.
#
# References: the shapes are documented on ASK_USER_TOOL_NAME / InteractionStatus in constants.py.

import re
from typing import Any

import pydash

from src.constants import USER_FACING_TOOLS, AiMessageRole, InteractionStatus

# Status -> one letter, so the ordering rule can be a regex over the whole array (see below).
_STATUS_LETTERS = {InteractionStatus.SUBMITTED: "s", InteractionStatus.ABORTED: "a", InteractionStatus.UNTOUCHED: "u"}


class InvalidSubmission(Exception):
    """The results don't fit the interaction (wrong length, bad ordering, an answer that isn't one of
    the options...). The message says what's wrong. 422."""


def find_pending_interactions(messages: list) -> list[dict]:
    """[Pure] The user-facing tool_use blocks still waiting on the user: those in the LATEST assistant
    message with no tool_result answering them yet. Only the latest assistant message counts -- the
    agent only ever continues once everything it asked has been answered."""
    last_assistant_index = pydash.find_last_index(messages, lambda message: message["role"] == AiMessageRole.ASSISTANT)
    if last_assistant_index < 0:
        return []
    answered_ids = {
        block.get("tool_use_id")
        for message in messages[last_assistant_index + 1 :]
        for block in message["content"]
        if block.get("type") == "tool_result"
    }
    return [
        block
        for block in messages[last_assistant_index]["content"]
        if block.get("type") == "tool_use"
        and block.get("name") in USER_FACING_TOOLS
        and block["id"] not in answered_ids
    ]


def validate_question_results(questions: list[dict], results: list[dict]) -> None:
    """[Pure] Raise InvalidSubmission unless `results` is a valid answer to `questions`: same length,
    statuses all SUBMITTED or SUBMITTED* ABORTED UNTOUCHED*, and every submitted answer fits its question."""
    if len(results) != len(questions):
        raise InvalidSubmission(f"expected {len(questions)} results, got {len(results)}")
    status_pattern = "".join(_STATUS_LETTERS[result["status"]] for result in results)
    if not re.fullmatch(r"s*(au*)?", status_pattern):
        raise InvalidSubmission("statuses must be all submitted, or submitted... then one aborted then untouched...")
    errors = pydash.compact(
        [
            _check_answer(index, question, result.get("value"))
            for index, (question, result) in enumerate(zip(questions, results, strict=True))
            if result["status"] == InteractionStatus.SUBMITTED
        ]
    )
    if errors:
        raise InvalidSubmission("; ".join(errors))


def _check_answer(index: int, question: dict, value: Any) -> str | None:
    """[Pure] What's wrong with one submitted answer, or None if it's fine. A single-select question
    takes exactly one answer: one option OR a custom text. A multi-select one takes any number of
    options, none included, plus an optional custom text."""
    if not isinstance(value, dict) or not isinstance(value.get("selected"), list):
        return f"#{index}: value must be {{selected: [...], custom: str | null}}"
    selected, custom = value["selected"], value.get("custom")
    unknown = [option for option in selected if option not in question["options"]]
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
