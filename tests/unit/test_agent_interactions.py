# Interaction rules + signals + mock agent tests -- all pure, no fakes needed. What's under test: which
# tool calls count as pending, what a valid submission looks like (questions and actions), what the agent
# may send, which approvals a submission carries, reading signals off the log, and the mock's replies.


import pytest

from src.constants import ASK_USER_TOOL_NAME, REQUEST_ACTIONS_TOOL_NAME, AgentAction, ProjectStage
from src.services.agent import mock, signals
from src.services.agent.interactions import (
    InvalidSessionMessage,
    InvalidSubmission,
    find_pending_interactions,
    get_approved_actions,
    to_tool_result_block,
    validate_interaction_results,
    validate_session_content,
)

_SINGLE = {"question": "Style?", "options": ["Minimal", "Bold"], "multi_select": False, "allow_custom": True}
_SINGLE_NO_CUSTOM = {"question": "Signup?", "options": ["Yes", "No"], "multi_select": False, "allow_custom": False}
_MULTI = {"question": "Sections?", "options": ["Menu", "Map"], "multi_select": True, "allow_custom": True}


def _ask(tool_id="toolu_1", name=ASK_USER_TOOL_NAME):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {"questions": [_SINGLE]}}


def _approve(tool_id="toolu_a", *names):
    actions = [{"name": name, "params": {}} for name in names or [AgentAction.APPROVE_SPEC]]
    return {"type": "tool_use", "id": tool_id, "name": REQUEST_ACTIONS_TOOL_NAME, "input": {"actions": actions}}


def _msg(role, *blocks):
    return {"role": role, "content": list(blocks)}


def _signal(slug):
    return _msg("assistant", signals.to_signal_block(slug))


def _validate_questions(questions, results):
    validate_interaction_results({"name": ASK_USER_TOOL_NAME, "input": {"questions": questions}}, results)


def _decided(decision):
    return {"status": "submitted", "value": {"decision": decision}}


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


def test_only_what_came_after_the_latest_user_message_counts():
    # An old unanswered ask (shouldn't happen, but) is superseded once the user has said something since.
    messages = [
        _msg("assistant", _ask("toolu_old")),
        _msg("user", {"type": "text", "text": "x"}),
        _msg("assistant", {"type": "text", "text": "ok"}),
    ]
    assert find_pending_interactions(messages) == []


def test_signals_and_text_after_the_question_dont_hide_it():
    messages = [
        _msg("user", {"type": "text", "text": "hi"}),
        _msg("assistant", _approve()),
        _signal("BUILDING_DONE"),
        _msg("assistant", {"type": "text", "text": "take your time"}),
    ]
    assert [block["id"] for block in find_pending_interactions(messages)] == ["toolu_a"]


def test_every_ask_since_the_user_last_spoke_is_pending_in_order():
    messages = [
        _msg("user", {"type": "text", "text": "hi"}),
        _msg("assistant", _ask("toolu_1")),
        _msg("assistant", _approve()),
    ]
    assert [block["id"] for block in find_pending_interactions(messages)] == ["toolu_1", "toolu_a"]


def test_tools_the_server_runs_are_never_pending():
    assert find_pending_interactions([_msg("assistant", _ask(name="edit_file"))]) == []


def test_no_messages_nothing_pending():
    assert find_pending_interactions([]) == []


# --- validate_interaction_results ---


def test_accepts_every_valid_shape():
    questions = [_SINGLE, _SINGLE_NO_CUSTOM, _MULTI]
    _validate_questions(questions, [_submitted(custom="Earthy"), _submitted(["No"]), _submitted()])  # multi: none ok
    _validate_questions(questions, [_submitted(["Bold"]), {"status": "aborted"}, {"status": "untouched"}])
    _validate_questions(questions, [{"status": "aborted"}, {"status": "untouched"}, {"status": "untouched"}])
    _validate_questions(questions, [_submitted(["Minimal"]), _submitted(["Yes"]), _submitted(["Menu", "Map"], "Blog")])


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
        _validate_questions([_SINGLE, _SINGLE_NO_CUSTOM, _MULTI], results)


def test_action_answers_are_a_decision():
    two = _approve("toolu_a", AgentAction.APPROVE_SPEC, AgentAction.APPROVE_IMPLEMENTATION)
    validate_interaction_results(two, [_decided("approved"), _decided("iterate")])
    validate_interaction_results(two, [{"status": "aborted"}, {"status": "untouched"}])
    with pytest.raises(InvalidSubmission, match="decision"):
        validate_interaction_results(two, [_decided("maybe"), _decided("iterate")])
    with pytest.raises(InvalidSubmission, match="expected 2 results"):
        validate_interaction_results(two, [_decided("approved")])


# --- get_approved_actions ---


def test_only_submitted_approvals_count():
    answered = [
        (_ask(), [_submitted(["Bold"])]),
        (_approve("toolu_a", AgentAction.APPROVE_SPEC), [_decided("approved")]),
        (_approve("toolu_b", AgentAction.APPROVE_IMPLEMENTATION), [_decided("iterate")]),
    ]
    assert get_approved_actions(answered) == [AgentAction.APPROVE_SPEC]
    assert get_approved_actions([(_approve(), [{"status": "aborted"}])]) == []


# --- validate_session_content ---


def _channel_actions(*names):
    return {"type": "tool_use", "input": {"actions": [{"name": name, "params": {}} for name in names]}}


def test_the_agent_may_ask_for_approvals_that_fit_the_stage():
    validate_session_content(
        [{"type": "text", "text": "ready?"}, _channel_actions("APPROVE_SPEC")], ProjectStage.SPECING
    )
    validate_session_content([_channel_actions("APPROVE_IMPLEMENTATION")], ProjectStage.IMPLEMENTING)


@pytest.mark.parametrize(
    ("content", "stage", "reason"),
    [
        ([{"type": "signal", "slug": "IMPLEMENTATION_DONE"}], ProjectStage.IMPLEMENTING, "send_signal"),
        ([_channel_actions("DEPLOY_NOW")], ProjectStage.IMPLEMENTING, "unknown actions"),
        ([_channel_actions("APPROVE_IMPLEMENTATION")], ProjectStage.SPECING, "approve the spec first"),
    ],
)
def test_the_agent_may_not_send(content, stage, reason):
    with pytest.raises(InvalidSessionMessage, match=reason):
        validate_session_content(content, stage)


# --- signals ---


def test_signal_messages_are_told_apart_from_said_ones():
    assert signals.is_signal_message(_signal("BUILDING"))
    assert not signals.is_signal_message(_msg("assistant", {"type": "text", "text": "hi"}))


def test_signal_block_leaves_out_what_wasnt_given():
    assert signals.to_signal_block("BUILDING") == {"type": "signal", "slug": "BUILDING"}
    assert signals.to_signal_block("BUILDING", "Menu page", {"a": 1}) == {
        "type": "signal",
        "slug": "BUILDING",
        "description": "Menu page",
        "meta": {"a": 1},
    }


def test_a_build_is_open_until_its_done():
    text = _msg("assistant", {"type": "text", "text": "working on it"})
    assert signals.is_build_open([_signal("BUILDING"), text])
    assert signals.is_build_open([_signal("BUILDING_DONE"), _signal("BUILDING")])
    assert not signals.is_build_open([_signal("BUILDING"), text, _signal("BUILDING_DONE")])
    assert not signals.is_build_open([text, _signal("IMPLEMENTATION_DONE")])


def test_a_turn_is_open_from_the_users_message_until_the_agent_goes_idle():
    def _at(seq, message):
        return {**message, "seq": seq}

    said = _at(1, _msg("user", {"type": "text", "text": "add a menu page"}))
    reply = _at(2, _msg("assistant", {"type": "text", "text": "done"}))
    idle = _at(3, _signal("AGENT_IDLE"))
    answer = _at(4, _msg("user", {"type": "tool_result", "tool_use_id": "toolu_1", "content": []}))
    assert not signals.is_turn_open([])
    assert signals.is_turn_open([said])
    assert signals.is_turn_open([said, reply])  # the reply alone doesn't end it: the runner's idle does
    assert not signals.is_turn_open([said, reply, idle])
    assert signals.is_turn_open([said, reply, idle, answer])  # an answer wakes the agent too


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
    _validate_questions(questions, results)  # doesn't raise


def test_mock_echoes_answers_but_not_aborts():
    tool_use = mock.reply_to_text("question")[-1]
    other_tool_use = mock.reply_to_text("question")[-1]
    reply = mock.reply_to_submission(
        [(tool_use, [_submitted(custom="Earthy")]), (other_tool_use, [_submitted(["Minimal"])])]
    )
    assert '"Earthy"' in reply[0]["text"] and "Minimal" in reply[0]["text"]
    # one aborted interaction anywhere in the batch -> no reply
    assert (
        mock.reply_to_submission([(tool_use, [_submitted(["Minimal"])]), (other_tool_use, [{"status": "aborted"}])])
        is None
    )


def test_mock_lists_uploads_with_types_and_sizes():
    reply = mock.reply_to_uploads(
        [{"name": "logo.png", "mime": "image/png", "size": 2048}, {"name": "a.txt", "mime": "text/plain", "size": 5}]
    )
    assert reply[0]["text"].splitlines()[1:] == ["- logo.png (image/png, 2.0 KB)", "- a.txt (text/plain, 5 B)"]


def test_options_with_subtitles_are_answered_by_label():
    question = {
        "question": "Layout?",
        "options": ["Plain", {"label": "Grid", "description": "Cards in rows"}],
        "multi_select": False,
        "allow_custom": True,
    }
    _validate_questions([question], [_submitted(["Grid"])])
    with pytest.raises(InvalidSubmission, match="not among the options"):
        _validate_questions([question], [_submitted(["Cards in rows"])])
