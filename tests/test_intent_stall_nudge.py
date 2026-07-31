"""Guards for the intent-stall nudge (epic pt_33ba079f5cea4841).

The measured shape: a tool round is rejected/errors, and the model's NEXT
round is prose-only with ``finish_reason=stop`` and zero tool calls. The task
settles normally and the user sees the conversation stop mid-thought.

DISCIPLINE THESE TESTS ENCODE
-----------------------------
The ticket authorized a two-part criterion (prev round failed ∧ prose-only
stop). A 7-day scan measured that pair at 60% false positives, so the
classifier ships four criteria. **Each false-positive class gets its own
test**, because a fail-safe suppressor that is only tested in aggregate can
be deleted one branch at a time while the suite stays green (charter: the
NEUTER discipline). The four suppression tests below are therefore the
load-bearing half of this file — not the happy path.

Assertions read RESULTS (does it nudge? does the message get appended?), not
implementation constants, so the criteria can be reorganised without going
falsely red (charter: 断言结果而非实现).
"""

import pytest

from lib.tasks_pkg.stream_handler._intent_stall import (
    END_TURN_MARKER,
    NUDGE_TEXT,
    parse_end_turn_reason,
    should_nudge_intent_stall,
)

pytestmark = pytest.mark.unit


# ── Fixtures modelled on the ground-truth sample ──────────────────────
# conv ms34yw0k74o2lq R17/R18: run_command blocked by a pre-execution hook,
# then "Let me use explicit paths only." with no tool call.

def _blocked_round():
    """R17: run_command rejected by the pre-execution hook."""
    return {
        'tool': 'run_command',
        'llmRound': 17,
        'status': 'blocked',
        'results': [{
            'badge': 'blocked',
            'notRun': True,
            'reason': ('Tool blocked by pre-execution hook: Blocked '
                       'catastrophic delete of $(git'),
        }],
    }


def _ok_round():
    return {
        'tool': 'read_files',
        'llmRound': 3,
        'status': 'ok',
        'results': [{'badge': 'ok', 'exitCode': 0}],
    }


def _task(rounds, **extra):
    import threading
    task = {
        'id': 'ms34yw0k74o2lq-task',
        'toolRounds': list(rounds),
        # Part of the real task shape — append_event takes it. Omitting it
        # made the analyser die on KeyError instead of on an assertion, which
        # would have hidden whether the nudge fires at all.
        'events': [],
        'events_lock': threading.Lock(),
    }
    task.update(extra)
    return task


_STALL_TEXT = '该钩子阻止了我的回退方案。让我仅使用显式路径。'


def test_the_ground_truth_sample_is_nudged():
    """R18 of the real conversation must be detected as a stall."""
    ok, reason = should_nudge_intent_stall(
        _task([_blocked_round()]), {'content': _STALL_TEXT}, _STALL_TEXT)
    assert ok is True, f'ground-truth sample not detected (reason={reason})'
    assert reason == 'intent_stall'


def test_an_errored_round_also_counts_not_just_a_blocked_one():
    """Criterion A covers errors and non-zero exits, not only hook blocks."""
    errored = {
        'tool': 'run_command',
        'llmRound': 5,
        'status': 'ok',
        'results': [{'badge': 'error', 'exitCode': 1}],
    }
    ok, _ = should_nudge_intent_stall(
        _task([errored]), {'content': _STALL_TEXT}, _STALL_TEXT)
    assert ok is True


# ── The four false-positive classes, one test each ────────────────────

def test_a_successful_previous_round_is_not_a_stall():
    """Criterion A: a plain final answer after successful work must stop."""
    ok, reason = should_nudge_intent_stall(
        _task([_ok_round()]), {'content': 'Done. All 27 tests pass.'},
        'Done. All 27 tests pass.')
    assert ok is False
    assert reason == 'prev_tool_ok'


def test_a_round_that_still_called_a_tool_is_not_a_stall():
    """Criterion B: the loop continues on its own — nothing to nudge."""
    msg = {'content': 'Retrying with explicit paths.',
           'tool_calls': [{'function': {'name': 'run_command'}}]}
    ok, reason = should_nudge_intent_stall(
        _task([_blocked_round()]), msg, msg['content'])
    assert ok is False
    assert reason == 'has_tool_calls'


def test_an_absent_tool_is_never_nudged():
    """Criterion C: nudging a tool that is not in the toolset burns money.

    3 of the 20 measured A∧B hits were this class. Retrying can only make the
    model reach for the same unavailable tool; the correct handling is the
    ``tool_not_available`` envelope (epic pt_88791cb08cb2495c).
    """
    absent = {
        'tool': 'project_board_complete',
        'llmRound': 9,
        'status': 'rejected',
        '_rejected': {'attempted': 'project_board_complete'},
        'results': [{
            'badge': 'error',
            'reason': ('Error: `project_board_complete` is not a real tool '
                       'and was NOT executed. It is not in the list of tools '
                       'available to you this turn.'),
        }],
    }
    ok, reason = should_nudge_intent_stall(
        _task([absent]), {'content': 'Let me mark it done another way.'},
        'Let me mark it done another way.')
    assert ok is False
    assert reason == 'non_retryable'


def test_a_hand_back_to_the_user_is_never_nudged():
    """Criterion D1: 5 of 20 measured hits were the model asking the USER.

    Nudging here answers on the user's behalf — worse than stopping.
    """
    handoff = dict(_blocked_round())
    task = _task([handoff, {'tool': 'ask_user', 'llmRound': 18,
                            'status': 'ok', 'results': []}])
    ok, reason = should_nudge_intent_stall(
        task, {'content': '要我按 A 还是 B 走?'}, '要我按 A 还是 B 走?')
    assert ok is False
    assert reason == 'awaiting_human'


def test_a_prose_hand_back_is_suppressed_by_the_self_declaration():
    """Criterion D2: the axis D1 structurally cannot see.

    A question asked in prose calls no tool, so no state probe reveals it.
    The model declaring ``[END_TURN: awaiting_human]`` closes that gap.
    """
    text = f'两条路你定。{END_TURN_MARKER} awaiting_human]'
    ok, reason = should_nudge_intent_stall(
        _task([_blocked_round()]), {'content': text}, text)
    assert ok is False
    assert reason == 'awaiting_human'


@pytest.mark.parametrize('declared', ['done', 'blocked'])
def test_an_explicit_done_or_blocked_declaration_also_stops(declared):
    """A model that says its turn is finished must not be re-driven."""
    text = f'收工。{END_TURN_MARKER} {declared}]'
    ok, reason = should_nudge_intent_stall(
        _task([_blocked_round()]), {'content': text}, text)
    assert ok is False
    assert reason == 'awaiting_human'


def test_an_invented_end_reason_does_not_suppress_the_nudge():
    """D2 is a closed set — an unknown reason reads as no declaration.

    Otherwise a model could mute the safety net with arbitrary text.
    """
    text = f'继续。{END_TURN_MARKER} whatever_i_want]'
    assert parse_end_turn_reason(text) is None
    ok, reason = should_nudge_intent_stall(
        _task([_blocked_round()]), {'content': text}, text)
    assert ok is True, f'invented reason wrongly suppressed (reason={reason})'


# ── Wording must never be a criterion ─────────────────────────────────

def test_wording_is_not_a_criterion_in_either_direction():
    """Phrase matching measured 45-49% FP on two independent scans.

    Both halves matter: prose WITHOUT an action verb still nudges when the
    structure says stall, and prose WITH one does not when it says otherwise.
    """
    terse = '嗯。'
    ok, _ = should_nudge_intent_stall(
        _task([_blocked_round()]), {'content': terse}, terse)
    assert ok is True, 'a stall without an action verb must still be caught'

    eager = '让我立刻用显式路径重跑一遍。'
    ok, reason = should_nudge_intent_stall(
        _task([_ok_round()]), {'content': eager}, eager)
    assert ok is False, 'wording must not create a stall where structure says none'
    assert reason == 'prev_tool_ok'


# ── The bounded-nudge contract, driven through the real analyser ───────

def _analyse(task, content, messages):
    from lib.tasks_pkg.stream_handler._analyse import analyse_stream_result
    return analyse_stream_result(
        assistant_msg={'content': content},
        last_finish_reason='stop',
        task=task,
        tid='ms34yw0k',
        model='yuju-claude-opus-5-evaDaily',
        round_num=18,
        _premature_retry_count=0,
        messages=messages,
        usage={},
    )


def test_the_analyser_re_drives_the_loop_and_appends_the_nudge():
    """End-to-end: the stop becomes a continue with a nudge message."""
    task = _task([_blocked_round()], aborted=False, content=_STALL_TEXT)
    messages = [{'role': 'user', 'content': 'clean up the worktrees'}]
    decision = _analyse(task, _STALL_TEXT, messages)

    assert decision['action'] == 'continue', (
        'the intent stall must re-drive the loop, not settle as a normal stop')
    assert messages[-1]['role'] == 'user'
    assert messages[-1]['content'] == NUDGE_TEXT
    assert task['_intent_stall_nudge_count'] == 1


def test_the_nudge_is_bounded_to_one_per_task():
    """Runaway guard: a second stall is allowed to stop.

    Same discipline as the retry caps — a model that will not act must be
    able to end, or the loop bills forever.
    """
    task = _task([_blocked_round()], aborted=False, content=_STALL_TEXT,
                 _intent_stall_nudge_count=1)
    messages = [{'role': 'user', 'content': 'clean up the worktrees'}]
    decision = _analyse(task, _STALL_TEXT, messages)

    assert decision['action'] == 'break'
    assert decision['loop_exit_reason'] == 'no_tool_calls_round_18'
    assert len(messages) == 1, 'no second nudge may be appended'


def test_a_normal_stop_is_untouched():
    """The common path must be byte-for-byte what it was before."""
    task = _task([_ok_round()], aborted=False, content='All done.')
    messages = [{'role': 'user', 'content': 'run the tests'}]
    decision = _analyse(task, 'All done.', messages)

    assert decision['action'] == 'break'
    assert decision['loop_exit_reason'] == 'no_tool_calls_round_18'
    assert len(messages) == 1
    assert '_intent_stall_nudge_count' not in task


def test_the_nudge_names_the_structural_fact_and_teaches_the_contract():
    """The nudge must be actionable, and must tell the model how to opt out.

    Without the opt-out sentence D2 is undiscoverable — the model can only
    declare an end reason it has been told about.
    """
    assert 'never executed' in NUDGE_TEXT or 'did not run' in NUDGE_TEXT.lower()
    for reason in ('awaiting_human', 'done', 'blocked'):
        assert f'{END_TURN_MARKER} {reason}]' in NUDGE_TEXT
