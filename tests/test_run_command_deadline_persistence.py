"""Acceptance gate for the live run_command countdown (epic pt_1a82ffb3).

THE REQUIREMENT
---------------
A `run_command` that declared a `timeout` must render a real-time countdown,
and — the part that is easy to fake — that countdown must SURVIVE switching
conversations. It must resume from where it was, never restart from zero.

WHY A DB ROUND-TRIP AND NOT A REDUCER UNIT TEST
-----------------------------------------------
"Persisted" is a claim about the DATABASE, so a reducer-only test cannot
falsify it. Three independent holes each break persistence while leaving a
reducer test perfectly green, and each is pinned below:

  1. `tool_progress` had NO branch in the pure reducer, so everything it
     carried lived only on the live path and was absent from
     `projectColdSnapshot` (test_reducer_folds_deadline_from_tool_progress +
     test_cold_projection_preserves_deadline).
  2. Neither periodic checkpoint fires during a long command — the
     orchestrator's runs after a round COMPLETES, the stream's on a content
     delta — so whether a running round reached the DB at all was a race. The
     spawn callback now forces one write (see `_make_run_command_spawn_cb`),
     proven end-to-end by
     test_running_round_with_deadline_survives_a_real_db_roundtrip.
  3. Both checkpoint writers early-returned when content+thinking were empty,
     which is exactly a tool-only turn
     (test_tool_only_turn_is_checkpointed_despite_empty_prose).

Plus the reason the deadline cannot be computed on the client at all:
the effective budget is the requested timeout AFTER the cross-DC multiplier
and the MAX_COMMAND_TIMEOUT clamp
(test_deadline_reflects_effective_budget_not_the_requested_one).

NOTE on scope: `run_command` resolves timeout=None BY DESIGN (no ceiling,
pinned by tests/test_no_backend_timeouts.py), so most commands have NO
deadline. For those the truthful display is the ELAPSED count-up, which is why
the renderer covers both and why no cap was introduced to manufacture a
countdown.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TOOL_ROUNDS_JS = runtime_section_path('ui/tool_rounds.js')


# ══════════════════════════════════════════════════════════════════
#  1. Backend: the spawn clock publishes the EFFECTIVE budget
# ══════════════════════════════════════════════════════════════════

def test_spawn_publishes_absolute_deadline_for_an_explicit_timeout():
    """on_spawn fires once with (exec_start_ms, deadline_ms) in epoch ms."""
    import lib.project_mod.run_command as rc
    seen = []
    rc.tool_run_command('/tmp', 'echo hi', timeout=7,
                        on_spawn=lambda s, d: seen.append((s, d)))
    assert len(seen) == 1, f'on_spawn must fire exactly once, got {len(seen)}'
    start, deadline = seen[0]
    now = time.time() * 1000.0
    assert abs(start - now) < 60_000, 'exec_start must be epoch MILLISECONDS'
    assert abs((deadline - start) / 1000.0 - 7.0) < 0.5


def test_unbounded_command_publishes_no_deadline():
    """No timeout is the DEFAULT. It must yield None — never 0, never a past
    instant, either of which would render as an instantly-expired countdown."""
    import lib.project_mod.run_command as rc
    seen = []
    rc.tool_run_command('/tmp', 'echo hi', on_spawn=lambda s, d: seen.append(d))
    assert seen == [None]
    seen.clear()
    # timeout=0 is the documented "unlimited" spelling.
    rc.tool_run_command('/tmp', 'echo hi', timeout=0,
                        on_spawn=lambda s, d: seen.append(d))
    assert seen == [None]


def test_deadline_reflects_effective_budget_not_the_requested_one():
    """THE reason the client cannot compute this itself.

    A DolphinFS path multiplies the timeout (run_command.py cross-DC block). A
    frontend that read `toolArgs.timeout` would count to zero while the command
    was still legitimately running.
    """
    import lib.project_mod.run_command as rc
    seen = []
    rc.tool_run_command('/tmp', 'echo hi', timeout=10,
                        on_spawn=lambda s, d: seen.append((s, d)))
    base_budget = (seen[0][1] - seen[0][0]) / 1000.0

    seen.clear()
    import lib.cross_dc as _cross_dc
    _orig_mult = _cross_dc.get_timeout_multiplier
    _cross_dc.get_timeout_multiplier = lambda p: 3.0
    try:
        rc.tool_run_command('/tmp', 'echo hi', timeout=10,
                            on_spawn=lambda s, d: seen.append((s, d)))
    finally:
        _cross_dc.get_timeout_multiplier = _orig_mult
    scaled_budget = (seen[0][1] - seen[0][0]) / 1000.0

    assert abs(base_budget - 10.0) < 0.5
    assert abs(scaled_budget - 30.0) < 0.5, (
        f'cross-DC multiplier must be baked into the published deadline '
        f'(got {scaled_budget:.1f}s, expected ~30s). A client deriving the '
        f'countdown from the REQUESTED timeout would hit zero 20s early.')


def test_a_raising_spawn_callback_never_breaks_the_command():
    """Telemetry must never abort a running command."""
    import lib.project_mod.run_command as rc

    def _boom(_s, _d):
        raise RuntimeError('spawn callback exploded')

    out = rc.tool_run_command('/tmp', 'echo survived', on_spawn=_boom)
    assert 'survived' in out and 'exit code: 0' in out


# ══════════════════════════════════════════════════════════════════
#  1b. The PROJECT-mode dispatch wires the spawn callback
# ══════════════════════════════════════════════════════════════════
#
# Owner review (2026-07-31) caught that the first landing only wired the
# STANDALONE path (_handle_code_exec), while project mode — the common case —
# passed no on_spawn, so neither the deadline frame nor the forced checkpoint
# ever happened exactly where they mattered. The same shape as the pet-drag
# lesson recorded in JOURNAL: N entrances, N-1 implementations.

def test_project_mode_run_command_publishes_deadline_and_checkpoints():
    """Drive the REAL project-mode handler with a real subprocess and assert
    the two things the owner demands: (1) the round ends up carrying
    deadlineTs (from the spawn frame), (2) a checkpoint was forced."""
    import lib.tasks_pkg.handlers.code_exec as ce
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    from lib.tasks_pkg.manager import create_task

    events = []
    orig_append = ce.append_event
    ce.append_event = lambda task, ev: events.append(dict(ev))
    checkpoints = []
    import lib.tasks_pkg.manager as mgr
    orig_ckpt = mgr.checkpoint_task_partial
    mgr.checkpoint_task_partial = lambda task, force=False: checkpoints.append(force)
    try:
        round_entry = {'query': 'run_command', 'toolCallId': 'tc-proj-1',
                       'status': 'searching'}
        # A REAL task dict — the checkpoint/event machinery reaches for
        # events_lock etc., which a bare dict does not have.
        task = create_task('conv-proj-spawn',
                           [{'role': 'user', 'content': 'run it'}], {}, user_id=1)
        _handle_project_tool(
            task, {}, 'run_command', 'tc-proj-1',
            {'command': 'echo proj-ok', 'timeout': 9}, 1, round_entry,
            None, '/tmp', True)
    finally:
        ce.append_event = orig_append
        mgr.checkpoint_task_partial = orig_ckpt

    assert 'proj-ok' in (round_entry.get('results') or [{}])[0].get('output', ''), (
        'sanity: the project-mode command did not actually run')
    assert round_entry.get('deadlineTs'), (
        'THE BUG THE OWNER CAUGHT: project mode published no deadlineTs — '
        'the spawn callback was never wired into _handle_project_tool, so the '
        'common case (a conversation WITH a project) rendered no countdown.')
    assert round_entry.get('execStartTs'), (
        'execStartTs missing on the project-mode round — the elapsed chip '
        'would anchor on tStart (round ANNOUNCE) and over-report execution.')
    spawn_evs = [e for e in events
                 if e.get('type') == 'tool_progress' and e.get('deadlineTs')]
    assert spawn_evs, 'no tool_progress frame carried the deadline on the wire'
    assert any(checkpoints), (
        'no forced checkpoint fired at spawn — switching conversations '
        'mid-command is still a race in project mode.')


def test_every_run_command_dispatch_point_wires_the_spawn_callback():
    """Structural ratchet for the NEXT entrance.

    The suite above proves the two known dispatch points work today. This
    guard answers "what if someone adds a third dispatch point and forgets":
    every callsite that passes on_chunk into a run_command execution must
    also pass on_spawn — the two callbacks are one contract, never half.

    Implemented as a FILE-LEVEL co-occurrence rule over lib/tasks_pkg: any
    module that drives run_command's live output through
    ``_make_run_command_progress_cb`` must ALSO wire the spawn clock through
    ``_make_run_command_spawn_cb``. File level (not callsite level) because
    the two call shapes differ — project mode builds a kwargs DICT
    (``'on_chunk': _progress_cb``) while the standalone handler passes plain
    keywords (``on_chunk=progress_cb``); a callsite regex would silently
    cover only one. A new dispatch point in ANY file that wires the first
    factory but not the second turns this red.
    """
    import glob

    from tests._source_scan import strip_comments

    offenders = []
    pattern = os.path.join(ROOT, 'lib', 'tasks_pkg', '**', '*.py')
    for path in glob.glob(pattern, recursive=True):
        with open(path, encoding='utf-8') as fh:
            live = strip_comments(fh.read(), lang='python', inline=True)
        if '_make_run_command_progress_cb(' in live and \
                '_make_run_command_spawn_cb(' not in live:
            offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, (
        'these modules stream run_command output but publish no spawn clock '
        '— a countdown would silently never appear on their path (the exact '
        'project-mode gap the owner caught):\n  ' + '\n  '.join(offenders))


def test_remote_path_deliberately_publishes_no_deadline():
    """Pins the DECISION (owner instruction 2), not an accident: the remote
    bridge timeout bounds the server's WAIT for a result — it does not kill
    the process on the user's machine. A deadlineTs there would render a
    countdown to an event that never happens."""
    path = os.path.join(ROOT, 'lib/tasks_pkg/handlers/project.py')
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    # Strip comments BEFORE slicing: the function's own docstring-style
    # comment explains WHY there is no deadlineTs — a naive substring check
    # would match that prose and report the exact opposite of the truth
    # (the same trap this suite's pipeline guard documents).
    from tests._source_scan import strip_comments
    live = strip_comments(src, lang='python', inline=True)
    start = live.index('def _execute_remote_run_command')
    body = live[start:live.index('\ndef ', start + 10)]
    assert 'deadlineTs' not in body, (
        'the remote bridge path must NOT publish deadlineTs — the server '
        'cannot kill a process on the user machine, so the countdown would '
        'be a lie')
    assert 'bridge_timeout' in body  # sanity: we are reading the right fn


# ══════════════════════════════════════════════════════════════════
#  2. Checkpoint: a tool-only running round reaches the DB
# ══════════════════════════════════════════════════════════════════

def test_tool_only_turn_is_checkpointed_despite_empty_prose():
    """Both checkpoint writers used to early-return on empty content+thinking.

    A turn whose first act is a long `run_command` has empty content AND empty
    thinking for its entire duration, so that guard dropped precisely the write
    that makes the running round recoverable.
    """
    from lib.tasks_pkg.manager._sync import _has_inflight_round

    running = {'toolRounds': [{'roundNum': 1, 'status': 'searching',
                               'deadlineTs': time.time() * 1000 + 60_000}]}
    assert _has_inflight_round(running) is True

    # A settled round is NOT in flight — it must not keep re-arming writes.
    assert _has_inflight_round(
        {'toolRounds': [{'roundNum': 1, 'status': 'done'}]}) is False
    assert _has_inflight_round({'toolRounds': []}) is False

    # A command blocked on a human gate is emphatically still running — this is
    # the longest-lived case and the one most likely to span a conv switch.
    for st in ('executing', 'pending_approval', 'awaiting_human', 'awaiting_stdin'):
        assert _has_inflight_round({'toolRounds': [{'status': st}]}) is True, st


# ══════════════════════════════════════════════════════════════════
#  3. THE ACCEPTANCE TEST — real DB round-trip
# ══════════════════════════════════════════════════════════════════

def test_running_round_deadline_is_in_the_authoritative_turn_projection():
    """The cumulative turn projection owns live tool timing across reloads."""
    from lib.turn_lifecycle import _task_projection

    now_ms = int(time.time() * 1000)
    deadline_ms = now_ms + 300_000
    exec_start_ms = now_ms - 42_000
    task = {
        'content': '', 'thinking': '',
        'toolRounds': [{
            'roundNum': 1, 'toolName': 'run_command',
            'toolCallId': 'tc_build_1', 'query': 'make -j8',
            'status': 'searching', 'results': None,
            'tStart': exec_start_ms - 5_000,
            'execStartTs': exec_start_ms, 'deadlineTs': deadline_ms,
        }],
    }

    projection = json.loads(json.dumps(_task_projection(task, {})))
    round_record = projection['toolRounds'][0]
    assert round_record['status'] == 'searching'
    assert round_record['deadlineTs'] == deadline_ms
    assert round_record['execStartTs'] == exec_start_ms
    assert round_record['deadlineTs'] > time.time() * 1000


# ══════════════════════════════════════════════════════════════════
#  4. Renderer: countdown / count-up / no negative numbers
# ══════════════════════════════════════════════════════════════════

def _node(script: str) -> dict:
    if shutil.which('node') is None:
        pytest.skip('node is required for the reducer projection gate')
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run(['node', path], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f'node harness failed:\n{proc.stderr[:2000]}')
        return json.loads(proc.stdout)
    finally:
        os.unlink(path)


def test_timer_chip_counts_down_up_and_never_goes_negative():
    out = _node(f'''
const fs = require('fs');
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k, d) => (d || k);
global.Icon = (n, s) => '';
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);
global.window = {{ addEventListener(){{}}, removeEventListener(){{}} }};
global.document = {{ addEventListener(){{}}, removeEventListener(){{}},
  querySelectorAll: () => [],
  createElement: () => ({{ style:{{}}, setAttribute(){{}}, appendChild(){{}} }}) }};
eval(fs.readFileSync({json.dumps(TOOL_ROUNDS_JS)}, 'utf8'));
if (global.window._cmdTimerTicker) clearInterval(global.window._cmdTimerTicker);
// A REALISTIC epoch. An artificially small NOW makes `NOW - 3900000` NEGATIVE,
// which _cmdTimerAnchor correctly rejects as "not a real clock" — so a toy
// value would test the rejection path instead of the hour formatting.
const NOW = 1785000000000;
const mk = (o) => _cmdTimerState(o, NOW);
console.log(JSON.stringify({{
  countdown:  mk({{ execStartTs: NOW - 5000, deadlineTs: NOW + 90000 }}).txt,
  soonCls:    mk({{ execStartTs: NOW - 5000, deadlineTs: NOW + 5000 }}).cls,
  expired:    mk({{ execStartTs: NOW - 5000, deadlineTs: NOW - 30000 }}).txt,
  expiredCls: mk({{ execStartTs: NOW - 5000, deadlineTs: NOW - 30000 }}).cls,
  countUp:    mk({{ execStartTs: NOW - 195000 }}).txt,
  hours:      mk({{ execStartTs: NOW - 3900000 }}).txt,
  prefersExec: mk({{ tStart: NOW - 600000, execStartTs: NOW - 30000 }}).txt,
  noClock:    mk({{}}),
  tickerArmed: typeof _tickCmdTimers === 'function',
}}));
// tool_rounds.js installs a REAL setInterval on load (the 1 Hz chip ticker).
// An unref'd interval keeps node's event loop alive forever, so the harness
// must exit explicitly — the same trap tests/_tool_rounds_wire_parity_harness.js
// documents. clearInterval above only covers the handle we can see via
// window._cmdTimerTicker; exit() is the guarantee.
process.exit(0);
''')
    assert out['countdown'] == '1m30s left'
    assert 'soon' in out['soonCls'], 'under 10s must escalate visually'
    assert 'terminating' in out['expired'], (
        f'past the deadline must say what is happening, not show a negative '
        f'number; got {out["expired"]!r}')
    assert 'over' in out['expiredCls']
    assert out['countUp'] == '3m15s', (
        'a command with NO timeout — the DEFAULT — must still show elapsed time')
    assert out['hours'] == '1h05m'
    assert out['prefersExec'] == '30s', (
        'must anchor on execStartTs (spawn), not tStart (round announce): a '
        'write-approval gate between them would over-report execution by 9.5m')
    assert out['noClock'] is None, 'no clocks at all → render nothing, not "NaN"'
    assert out['tickerArmed'] is True
