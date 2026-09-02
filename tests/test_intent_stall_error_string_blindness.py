"""Criterion-A blindness to errors-returned-as-content (epic pt_5303eb3c7afb44a8).

THE SHAPE. ``_round_failed`` decides "did the previous tool round fail?" from
STRUCTURAL fields only — ``status``, per-result ``notRun`` / ``badge`` / ``type``
/ ``exitCode``. But a large family of project tools does not FAIL, it RETURNS a
failure: ``read_files`` with an inverted line range returns the string
``'Error: requested line range 6171-6162 is empty or out of bounds ...'`` as
ordinary content, and the round is finalized ``status='done'`` with a perfectly
normal ``badge='21961L'``. No structural field carries the failure, so criterion
A reads "previous tool was fine" and the intent-stall nudge is suppressed.

WHY THE FIX MUST NOT SNIFF THE PROSE (the trap this file pins). "content
contains 'Error:'" is NOT a usable criterion: ``read_files`` on a log file and
``grep_search`` matching lines that contain the word both return content full of
``Error:`` while having SUCCEEDED. A nudge fired on those would re-drive a model
that did nothing wrong, and the measurement (docs/modules/task_engine.md)
is explicit that a false positive costs more than a miss. ``run_command`` is the
sharpest case: a command may legitimately print ``Error: ...`` on stdout and
exit 0. So the failure must be STAMPED at the point the execution layer
generates it, and ``_round_failed`` must read that stamp.

GROUND TRUTH — conv ms5i5ydigs9j9w (2026-07-29 11:20:38–11:21:04, app.log):
    roundNum 35 / llmRound 25  read_files(start=6171, end=6162)  -> 'Error: ...'
    roundNum 36 / llmRound 26  read_files(6170-6180)             -> real content
    R28 terminal               finish_reason=stop content=784chars tool_calls=0
The turn settled normally and the user saw the conversation stop mid-thought.
"""

from __future__ import annotations

import json

import pytest

from lib.tasks_pkg.stream_handler._intent_stall import (
    _round_failed,
    _uncovered_failure,
    should_nudge_intent_stall,
)
from lib.tools.meta import build_project_tool_meta

pytestmark = pytest.mark.unit


def _round(rn, llm, tool, args, tool_content):
    """Build a tool round through the REAL production meta path.

    Deliberately NOT a hand-written ``results`` dict: the whole defect lives in
    what ``build_project_tool_meta`` does (or fails to do) with an
    execution-layer error returned as content, so a fixture that fabricates the
    meta would test a shape production never produces and would stay green
    through the bug. Everything below is therefore derived from the real
    builder given the real tool output string.
    """
    return {
        'roundNum': rn, 'llmRound': llm, 'status': 'done', 'toolName': tool,
        'toolArgs': json.dumps(args),
        'results': [build_project_tool_meta(tool, args, tool_content)],
        'toolContent': tool_content,
    }


# ── The real ms5i5ydigs9j9w rounds ─────────────────────────────────────

def _r35_error_as_content() -> dict:
    """read_files with an INVERTED range — failed, but finalized done."""
    return _round(
        35, 25, 'read_files',
        {'path': 'static/styles.css', 'start_line': 6171, 'end_line': 6162},
        'Error: requested line range 6171-6162 is empty or out of bounds '
        'for static/styles.css (21961 lines).')


def _r36_real_success() -> dict:
    """The model's own CORRECTED re-read — a genuine success."""
    return _round(
        36, 26, 'read_files',
        {'path': 'static/styles.css', 'start_line': 6170, 'end_line': 6180},
        'File: static/styles.css (lines 6170-6180 of 21961)\n'
        '────────\n.sw-a-failed .sw-a-objective{border-left-color:red}')


# ── FALSE-POSITIVE CORPUS: successes whose CONTENT is full of "Error:" ──

def _read_of_a_log_file() -> dict:
    """read_files on logs/error.log — content is nothing but 'Error:' lines."""
    return _round(
        10, 5, 'read_files', {'path': 'logs/error.log'},
        'File: logs/error.log (lines 1-3 of 38755)\n'
        '────────\n'
        'Error: tool "x" execution failed\n'
        'Error: requested line range is empty\n'
        'Error: unknown tool "y"\n')


def _grep_matching_the_word_error() -> dict:
    """grep_search for 'Error' — every match line starts with Error:."""
    return _round(
        11, 6, 'grep_search', {'pattern': 'Error'},
        'grep "Error" — 12 matches:\n\n'
        'Error: requested line range 6171-6162 is empty\n')


def _command_that_prints_error_and_exits_zero() -> dict:
    """run_command whose stdout says Error: but which SUCCEEDED (exit 0)."""
    return _round(
        12, 7, 'run_command', {'command': 'grep -c Error logs/app.log'},
        '$ grep -c Error logs/app.log\n'
        'Error: connection reset\nError: retrying\n\n[exit code: 0]')



_STALL_TEXT = (
    "I'm adding CSS rules right after the `.sw-a-preview` definition to "
    "override the markdown content styling within swarm panels. ... "
    "Let me now insert these rules with proper anchoring."
)


# ══════════════════════════════════════════════════════════════════════
#  A. The blindness itself — failing-first
# ══════════════════════════════════════════════════════════════════════

def test_error_returned_as_content_is_recognised_as_a_failed_round():
    """FAILING-FIRST: an errors-as-return-value round must read as FAILED.

    This is the whole defect. Today every structural field says "fine".
    """
    assert _round_failed(_r35_error_as_content()) is True, (
        'read_files returned an execution-layer error as its content and the '
        'round was stamped done/21961L — criterion A must still see a failure, '
        'otherwise the intent-stall nudge is suppressed on exactly the shape '
        'it exists to catch'
    )


def test_a_genuine_success_is_still_not_a_failure():
    """Complement: the corrected re-read must NOT read as failed."""
    assert _round_failed(_r36_real_success()) is False


# ══════════════════════════════════════════════════════════════════════
#  B. The false-positive corpus — the trap the fix must not fall into
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('round_factory,label', [
    (_read_of_a_log_file, 'read_files on logs/error.log'),
    (_grep_matching_the_word_error, 'grep_search matching the word Error'),
    (_command_that_prints_error_and_exits_zero,
     'run_command printing Error: but exiting 0'),
])
def test_successful_rounds_whose_content_mentions_error_are_not_failures(
        round_factory, label):
    """A success whose CONTENT contains 'Error:' must never read as failed.

    Pins the boundary against a prose-sniffing implementation: these three all
    SUCCEEDED. Classifying them as failures would fire a nudge at a model that
    did nothing wrong — the false-positive class the measurement rules out.
    """
    assert _round_failed(round_factory()) is False, (
        f'{label} SUCCEEDED — a content-substring criterion misreads it as a '
        f'failure and would fire a spurious nudge'
    )


# ══════════════════════════════════════════════════════════════════════
#  C. Round SELECTION — the second hole
# ══════════════════════════════════════════════════════════════════════

def test_recovered_failure_in_an_earlier_batch_is_covered():
    """R35 failed but R36 (a LATER batch) succeeded → not an uncovered failure.

    The model fixed its own arguments and moved on; re-driving it would talk
    about something it already handled.
    """
    task = {'toolRounds': [_r35_error_as_content(), _r36_real_success()]}
    assert _uncovered_failure(task) is None


def test_a_failure_in_the_FINAL_batch_is_uncovered_even_beside_a_success():
    """Batch-awareness: one llmRound is ONE model decision.

    A mixed final batch (read_files fails, sibling grep_search succeeds) must
    still expose the failure — the model saw both results together and then
    stopped. Sorting and taking [-1] made this a lottery.
    """
    fail = _r35_error_as_content()
    ok = _grep_matching_the_word_error()
    # SAME llmRound = one parallel batch = one model decision. The success is
    # given the HIGHER roundNum so the old sort-and-take-[-1] selector lands on
    # it and reports "prev tool ok" — that is exactly the lottery being pinned.
    fail['llmRound'] = ok['llmRound'] = 9
    fail['roundNum'], ok['roundNum'] = 40, 41   # success sorts LAST
    task = {'toolRounds': [fail, ok]}
    got = _uncovered_failure(task)
    assert got is not None and got['roundNum'] == 40, (
        'a failure in the final batch must be found even when a sibling in '
        'the same batch sorts after it'
    )
    # And the end-to-end verdict must follow: this IS a nudgeable stall.
    ok_nudge, reason = should_nudge_intent_stall(
        task, {'content': _STALL_TEXT}, _STALL_TEXT)
    assert (ok_nudge, reason) == (True, 'intent_stall'), (
        f'batch-aware criterion A must nudge here; got {(ok_nudge, reason)!r}'
    )


# ══════════════════════════════════════════════════════════════════════
#  D. ★ The load-bearing measurement: does fixing A make the nudge fire
#     for the REAL ms5i5ydigs9j9w timeline?
# ══════════════════════════════════════════════════════════════════════

def test_real_timeline_verdict_is_recorded_not_assumed():
    """Record what the classifier ACTUALLY decides on the real timeline.

    The failure that reached the user had its error round (35) FOLLOWED BY a
    successful corrected re-read (36). So even with criterion A seeing round 35
    correctly, the round ADJACENT to the terminal prose is a success. This test
    exists to make that fact measured rather than assumed — it asserts the
    verdict and names the reason, so whichever way the implementation lands the
    reason is visible in the suite instead of being argued about.
    """
    task = {'toolRounds': [_r35_error_as_content(), _r36_real_success()]}
    ok, reason = should_nudge_intent_stall(
        task, {'content': _STALL_TEXT}, _STALL_TEXT)
    # The corrected re-read is the adjacent round, so criterion A is satisfied
    # by a SUCCESS: this turn is not an "intent stall" under the current
    # four-criterion definition at all.
    assert (ok, reason) == (False, 'prev_tool_ok'), (
        f'expected the real timeline to be suppressed by criterion A via the '
        f'successful re-read; got {(ok, reason)!r}. If this changed, the '
        f'definition of an intent stall changed with it — update the ticket.'
    )


def test_a_stall_directly_after_the_error_round_does_fire():
    """The shape criterion A is FOR: prose-stop immediately after the error.

    Same error round, but nothing recovered after it. With the blindness fixed
    this must nudge; today it is suppressed as prev_tool_ok.
    """
    task = {'toolRounds': [_r35_error_as_content()]}
    ok, reason = should_nudge_intent_stall(
        task, {'content': _STALL_TEXT}, _STALL_TEXT)
    assert (ok, reason) == (True, 'intent_stall'), (
        f'a prose-only stop directly after an errors-as-return-value round is '
        f'the target shape and must be nudged; got {(ok, reason)!r}'
    )
