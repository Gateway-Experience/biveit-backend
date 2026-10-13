# Intentions: a stand-in agent so the FE can be built and tested before the real session is wired up.
# It replies predictably, after a short pause (REPLY_DELAY_SECONDS):
#   - a message with attachments -> a listing of everything in the conversation's uploads folder
#   - "artifact"  -> writes a sample spec (timestamped, so each run is a new version) and shows it
#   - "artifacts" -> writes and shows two (a markdown spec and an SVG palette), in one message
#   - "question"  -> one multiple-choice question
#   - "questions" -> four questions, one per combination of multi_select x allow_custom
#   - any other text -> the text echoed back with a preamble
#   - answers to its questions -> echoed back; an aborted submission gets no reply (same as the real
#     loop: aborting closes the interaction without prompting the agent)
# Delete this module once services/agent/conversations forwards to the real session.

import uuid

from src.constants import ASK_USER_TOOL_NAME, SHOW_ARTIFACT_TOOL_NAME, InteractionStatus
from src.utils.common import utc_now

# How long the mock "thinks" before its reply lands. Kept here rather than in constants.py since it goes
# away with this module.
REPLY_DELAY_SECONDS = 2

# Every multi_select x allow_custom combination, so the FE can exercise each form variant.
_QUESTIONS = [
    {
        "question": "Which visual style fits your site best?",
        "options": ["Minimal", "Bold & colorful", "Warm & cozy"],
        "multi_select": False,
        "allow_custom": True,
    },
    {
        "question": "Should visitors be able to sign up?",
        "options": ["Yes", "No"],
        "multi_select": False,
        "allow_custom": False,
    },
    {
        "question": "Which sections should the homepage have?",
        "options": ["Menu", "Opening hours", "Map", "Gallery"],
        "multi_select": True,
        "allow_custom": True,
    },
    {
        "question": "Which extra pages do you need?",
        "options": ["About", "Contact", "Blog"],
        "multi_select": True,
        "allow_custom": False,
    },
]


_SAMPLE_SPEC = {
    "name": "website-spec.md",
    "title": "Website spec",
    "content": b"""# Bean & Brew -- website spec

## What it's for
A landing page for a neighborhood coffee roaster: show the menu, the opening hours and how to get there.

## Pages
- **Home** -- hero photo, today's specials, opening hours
- **Menu** -- drinks and pastries, with prices
- **Visit us** -- map, address, parking tips

## Decisions so far
- Light theme, warm & cozy style
- No user accounts
""",
}
_SAMPLE_PALETTE = {
    "name": "color-palette.svg",
    "title": "Color palette",
    "content": b"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100">
  <rect x="0" width="100" height="100" fill="#6f4e37"/><rect x="100" width="100" height="100" fill="#c8a27a"/>
  <rect x="200" width="100" height="100" fill="#f3e9dc"/><rect x="300" width="100" height="100" fill="#2e2e2e"/>
</svg>
""",
}
# What each artifact command writes.
_SAMPLE_ARTIFACTS_BY_COMMAND = {"artifact": [_SAMPLE_SPEC], "artifacts": [_SAMPLE_SPEC, _SAMPLE_PALETTE]}


def get_sample_artifacts(text: str) -> list[dict]:
    """[Pure-ish] The artifacts an artifact command writes, as [{name, title, content (bytes)}] -- empty
    for any other text. The spec gets a "last updated" line with the current time, so running the
    command again produces a visibly new version (handy for testing that clients don't show a stale one)."""
    stamp = f"\n_Last updated {utc_now():%H:%M:%S} UTC_\n".encode()
    return [
        {**sample, "content": sample["content"] + stamp} if sample["name"].endswith(".md") else sample
        for sample in _SAMPLE_ARTIFACTS_BY_COMMAND.get(text.strip().lower(), [])
    ]


def reply_with_artifacts(shown: list[dict]) -> list[dict]:
    """[Pure-ish] The mock's reply showing artifacts it just wrote -- `shown` is [{path, title}] -- as one
    show_artifact tool call each, the same way the real agent will show files."""
    return [
        _to_text_block("(mock agent) Here's what I've put together so far:"),
        *[_to_show_artifact_block(artifact["path"], artifact["title"]) for artifact in shown],
    ]


def reply_to_text(text: str) -> list[dict]:
    """[Pure-ish] The mock's reply to a user's text message, as content blocks."""
    command = text.strip().lower()
    if command == "question":
        return [_to_text_block("(mock agent) Quick question before I go on:"), _to_ask_user_block(_QUESTIONS[:1])]
    if command == "questions":
        return [_to_text_block("(mock agent) A few questions so I get this right:"), _to_ask_user_block(_QUESTIONS)]
    return [_to_text_block(f"(mock agent) You said: {text}")]


def reply_to_uploads(uploaded_files: list[dict]) -> list[dict]:
    """[Pure] The mock's reply to a message with attachments: everything in the conversation's uploads
    folder (`uploaded_files` as storage.list_files describes them), not just this message's files."""
    file_lines = [f"- {file['name']} ({file['mime']}, {_format_size(file['size'])})" for file in uploaded_files]
    return [
        _to_text_block(
            "(mock agent) Here's everything in this conversation's uploads folder:\n" + "\n".join(file_lines)
        )
    ]


def _format_size(size: int) -> str:
    return (
        f"{size} B"
        if size < 1024
        else f"{size / 1024:.1f} KB"
        if size < 1024 * 1024
        else f"{size / 1024 / 1024:.1f} MB"
    )


def reply_to_submission(tool_use: dict, results: list[dict]) -> list[dict] | None:
    """[Pure] The mock's reply to the user's answers, or None when they aborted (no reply then)."""
    if any(result["status"] != InteractionStatus.SUBMITTED for result in results):
        return None
    answer_lines = [
        f"- {question['question']} -> {_describe_answer(result['value'])}"
        for question, result in zip(tool_use["input"]["questions"], results, strict=True)
    ]
    return [_to_text_block("(mock agent) Got your answers:\n" + "\n".join(answer_lines))]


def _describe_answer(value: dict) -> str:
    parts = [*value["selected"], *([f'"{value["custom"]}"'] if value.get("custom") else [])]
    return ", ".join(parts) or "(nothing selected)"


def _to_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _to_ask_user_block(questions: list[dict]) -> dict:
    # Same id shape as the API's (toolu_...), so the FE treats mock and real tool calls alike.
    return {
        "type": "tool_use",
        "id": f"toolu_mock_{uuid.uuid4().hex[:16]}",
        "name": ASK_USER_TOOL_NAME,
        "input": {"questions": questions},
    }


def _to_show_artifact_block(path: str, title: str) -> dict:
    return {
        "type": "tool_use",
        "id": f"toolu_mock_{uuid.uuid4().hex[:16]}",
        "name": SHOW_ARTIFACT_TOOL_NAME,
        "input": {"path": path, "title": title},
    }
