# Interaction rules + mock agent tests -- all pure, no fakes needed. What's under test: which tool calls
# count as pending, what a valid submission looks like, and the mock's replies.


import pytest

from src.constants import ASK_USER_TOOL_NAME
from src.services.agent import mock
from src.services.agent.interactions import (
    InvalidSubmission,
    find_pending_interactions,
    to_tool_result_block,
    validate_question_results,
)

_SINGLE = {"question": "Style?", "options": ["Minimal", "Bold"], "multi_select": False, "allow_custom": True}
_SINGLE_NO_CUSTOM = {"question": "Signup?", "options": ["Yes", "No"], "multi_select": False, "allow_custom": False}
_MULTI = {"question": "Sections?", "options": ["Menu", "Map"], "multi_select": True, "allow_custom": True}


def _ask(tool_id="toolu_1", name=ASK_USER_TOOL_NAME):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {"questions": [_SINGLE]}}


def _msg(role, *blocks):
    return {"role": role, "content": list(blocks)}


def _submitted(selected=(), custom=None):
    return {"status": "submitted", "value": {"selected": list(selected), "custom": custom}}


# --- find_pending_interactions ---


def test_an_unanswered_question_is_pending():
    messages = [_msg("user", {"type": "text", "text": "hi"}), _msg("assistant", {"type": "text", "text": "q:"}, _ask())]
    assert [block["id"] for block in find_pending_interactions(messages)] == ["toolu_1"]


def test_an_answered_question_is_not_pending():
    messages = [
        _msg("assistant", _ask()),
        _msg("user", {"type": "tool_result", "tool_use_id": "toolu_1", "content": "[]"}),
    ]
    assert find_pending_interactions(messages) == []


def test_only_the_latest_assistant_message_counts():
    # An old unanswered ask (shouldn't happen, but) is superseded once the agent has spoken again.
    messages = [
        _msg("assistant", _ask("toolu_old")),
        _msg("user", {"type": "text", "text": "x"}),
        _msg("assistant", {"type": "text", "text": "ok"}),
    ]
    assert find_pending_interactions(messages) == []


def test_tools_the_server_runs_are_never_pending():
    assert find_pending_interactions([_msg("assistant", _ask(name="edit_file"))]) == []


def test_no_messages_nothing_pending():
    assert find_pending_interactions([]) == []


# --- validate_question_results ---


def test_accepts_every_valid_shape():
    questions = [_SINGLE, _SINGLE_NO_CUSTOM, _MULTI]
    validate_question_results(
        questions, [_submitted(custom="Earthy"), _submitted(["No"]), _submitted()]
    )  # multi: none ok
    validate_question_results(questions, [_submitted(["Bold"]), {"status": "aborted"}, {"status": "untouched"}])
    validate_question_results(questions, [{"status": "aborted"}, {"status": "untouched"}, {"status": "untouched"}])
    validate_question_results(
        questions, [_submitted(["Minimal"]), _submitted(["Yes"]), _submitted(["Menu", "Map"], "Blog")]
    )


@pytest.mark.parametrize(
    ("results", "reason"),
    [
        ([_submitted(["Minimal"])], "expected 3 results"),
        (
            [_submitted(["Minimal"]), {"status": "untouched"}, {"status": "untouched"}],
            "statuses",
        ),  # untouched without an abort
        ([{"status": "aborted"}, {"status": "aborted"}, {"status": "untouched"}], "statuses"),  # two aborts
        ([{"status": "aborted"}, _submitted(["Yes"]), {"status": "untouched"}], "statuses"),  # answer after the abort
        (
            [_submitted(["Minimal", "Bold"]), _submitted(["Yes"]), _submitted()],
            "exactly one",
        ),  # single-select, two picks
        ([_submitted(["Minimal"], "also this"), _submitted(["Yes"]), _submitted()], "exactly one"),  # option AND custom
        ([_submitted(), _submitted(["Yes"]), _submitted()], "exactly one"),  # single-select, nothing
        ([_submitted(["Minimal"]), _submitted(custom="Maybe"), _submitted()], "custom answer"),  # allow_custom false
        ([_submitted(["Neon"]), _submitted(["Yes"]), _submitted()], "not among the options"),
        ([_submitted(["Minimal"]), _submitted(["Yes"]), _submitted(["Map", "Map"])], "twice"),
        ([_submitted(custom="  "), _submitted(["Yes"]), _submitted()], "non-empty"),
        ([{"status": "submitted", "value": "Minimal"}, _submitted(["Yes"]), _submitted()], "value must be"),
    ],
)
def test_rejects_invalid_submissions(results, reason):
    with pytest.raises(InvalidSubmission, match=reason):
        validate_question_results([_SINGLE, _SINGLE_NO_CUSTOM, _MULTI], results)


# --- to_tool_result_block ---


def test_tool_result_carries_results_as_is_and_drops_values_of_unanswered_items():
    block = to_tool_result_block("toolu_1", [_submitted(["Bold"]), {"status": "aborted", "value": None}])
    assert block["type"] == "tool_result" and block["tool_use_id"] == "toolu_1"
    assert block["content"] == [_submitted(["Bold"]), {"status": "aborted"}]


# --- mock agent ---


def test_mock_echoes_text_with_a_preamble():
    assert mock.reply_to_text("make it blue") == [{"type": "text", "text": "(mock agent) You said: make it blue"}]


def test_mock_asks_one_question():
    tool_use = mock.reply_to_text(" Question ")[-1]
    assert tool_use["name"] == ASK_USER_TOOL_NAME and len(tool_use["input"]["questions"]) == 1


def test_mock_asks_every_question_combination():
    questions = mock.reply_to_text("questions")[-1]["input"]["questions"]
    combos = {(question["multi_select"], question["allow_custom"]) for question in questions}
    assert combos == {(False, False), (False, True), (True, False), (True, True)}


def test_mock_questions_are_valid_against_its_own_rules():
    questions = mock.reply_to_text("questions")[-1]["input"]["questions"]
    results = [_submitted([question["options"][0]]) for question in questions]
    validate_question_results(questions, results)  # doesn't raise


def test_mock_echoes_answers_but_not_aborts():
    tool_use = mock.reply_to_text("question")[-1]
    reply = mock.reply_to_submission(tool_use, [_submitted(custom="Earthy")])
    assert '"Earthy"' in reply[0]["text"]
    assert mock.reply_to_submission(tool_use, [{"status": "aborted"}]) is None


def test_mock_lists_uploads_with_types_and_sizes():
    reply = mock.reply_to_uploads(
        [{"name": "logo.png", "mime": "image/png", "size": 2048}, {"name": "a.txt", "mime": "text/plain", "size": 5}]
    )
    assert reply[0]["text"].splitlines()[1:] == ["- logo.png (image/png, 2.0 KB)", "- a.txt (text/plain, 5 B)"]
