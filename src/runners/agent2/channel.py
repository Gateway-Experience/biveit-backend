# Intentions: the contract between the agent and the platform, from the runner's side -- translating between
# Claude Code's own mechanics and the chat's message format (the one services/agent stores and the FE renders,
# shared with the first runtime). All pure.
#   Claude Code's AskUserQuestion  <->  an ask_user tool_use in the chat, and the user's answers back
#   the user's messages             ->  the user turn the agent reads
# References: services/agent/sessions (to_session_content / to_stored_content, the backend's half),
# AskUserQuestion's shape: https://code.claude.com/docs/en/agent-sdk/user-input.md

import json

from src.constants import ApprovalDecision, InteractionStatus

# Appended to Claude Code's own system prompt: the MECHANICS of this runtime -- how the agent's text reaches
# the user, questions, approvals, platform messages, blocked tools. The playbook (tone, spec-ing,
# implementing) is the workspace's CLAUDE.md. Each rule lives in one of the two, never both: see "Agent
# instructions" in the repo's CLAUDE.md.
INSTRUCTIONS = (
    "You're working for a user who is NOT watching this session: they see a chat. The text you end each turn "
    "with is what they get as your reply, and nothing else is: text followed by a tool call is NOT shown to "
    "them. So ALWAYS end every turn with a short, friendly message to them -- never on a tool call or its "
    "result (an edit, a command, a check), or they get no reply at all -- and keep anything technical out "
    "of it. "
    "To have them choose between options, use the AskUserQuestion tool (your text right before it is shown "
    "with the questions). To have them approve the spec or the finished site, use the request_approval tool: "
    "they see Approve / Keep iterating buttons, and an approval takes effect on the platform's side by itself "
    "(approving the spec starts implementing, approving the implementation signs it off) -- you only ask, and "
    "hear the answer. Both wait until the user answers. Use send_signal to mark a big change on their screen. "
    "Some tools are blocked in this environment: if one is refused, find another way or tell the user. "
    "Messages starting with a bracketed tag like [Dev environment ready] come from the platform, not the user."
)


def to_ask_user_block(tool_use_id: str, ask_input: dict) -> dict:
    """[Pure] Claude Code's AskUserQuestion input as the chat's ask_user tool_use (channel format), keeping
    Claude's own call id so the answer can be matched back to it. Every option keeps its description, as
    the option's subtitle. AskUserQuestion has no "Other" option of its own -- the user can always type
    their own answer instead (allow_custom), which goes back as that question's answer."""
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "input": {
            "questions": [
                {
                    "question": question["question"],
                    "options": [
                        {
                            "label": option["label"],
                            **({"description": option["description"]} if option.get("description") else {}),
                        }
                        for option in question["options"]
                    ],
                    "multi_select": bool(question.get("multiSelect")),
                    "allow_custom": True,
                }
                for question in ask_input["questions"]
            ]
        },
    }


def to_answers(ask_input: dict, results: list[dict]) -> dict[str, str | list[str]] | None:
    """[Pure] The user's results (one per question, the chat's submission format) as AskUserQuestion's
    `answers`: question text -> the picked label (a list for multi-select), the typed answer standing in for
    or alongside them. None unless every question was answered -- a partial answer goes back as a refusal
    instead (to_refusal)."""
    if any(result["status"] != InteractionStatus.SUBMITTED for result in results):
        return None
    return {
        question["question"]: _to_answer(bool(question.get("multiSelect")), result["value"])
        for question, result in zip(ask_input["questions"], results, strict=True)
    }


def _to_answer(is_multi_select: bool, value: dict) -> str | list[str]:
    picked = [*value["selected"], *([value["custom"]] if value.get("custom") else [])]
    return picked if is_multi_select else picked[0]


def to_refusal(ask_input: dict, results: list[dict]) -> str:
    """[Pure] What the agent hears when the user didn't answer every question: which ones they did answer,
    and that the rest are unanswered -- not to be guessed."""
    answered = [
        f"{question['question']} -> {_to_answer(bool(question.get('multiSelect')), result['value'])}"
        for question, result in zip(ask_input["questions"], results, strict=True)
        if result["status"] == InteractionStatus.SUBMITTED
    ]
    return "The user didn't answer all of these questions. " + (
        f"They answered: {'; '.join(answered)}. The rest are unanswered -- don't assume a choice."
        if answered
        else "They skipped them -- don't assume a choice; carry on from what they say next."
    )


def to_decision(results: list[dict]) -> str:
    """[Pure] request_approval's result for the agent: the user's decision, or that they dismissed it."""
    result = results[0]
    if result["status"] != InteractionStatus.SUBMITTED:
        return "The user dismissed the approval without deciding."
    is_approved = result["value"]["decision"] == ApprovalDecision.APPROVED
    return "approved" if is_approved else "iterate: the user wants to keep iterating"


def to_user_turn(items: list[dict]) -> str:
    """[Pure] What the user sent (channel items) as the user turn the agent reads: their words as they are.
    Answers only get here when nothing is waiting for them any more (the runner restarted while the agent
    was asking), so they're spelled out as text instead."""
    texts = [item["text"] for item in items if item["type"] == "text"]
    late_answers = [item for item in items if item["type"] == "tool_result"]
    answer_text = (
        "[Answers to the questions you asked before this session restarted] "
        + json.dumps([item["content"] for item in late_answers], ensure_ascii=False, separators=(",", ":"))
        if late_answers
        else None
    )
    return "\n\n".join([*texts, *([answer_text] if answer_text else [])])
