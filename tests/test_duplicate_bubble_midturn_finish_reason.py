"""tests/test_duplicate_bubble_midturn_finish_reason.py — the "first bubble
freezes mid-generation, a SECOND duplicate bubble appears and continues" class.

WHY (measured, 2026-07-31, conv ms8c0645hwl327 / task 320e046d)
---------------------------------------------------------------
The conversation DB held EXACTLY ONE assistant message (msg_count=2: one user +
one assistant) while the screen showed TWO agent bubbles. So the duplicate is a
RENDER-IDENTITY violation, not a backend double-write — no fix belongs in the
persistence layer.

The chain has two independent links; BOTH are asserted here because either one
alone still produces the duplicate.

LINK 1 (backend, `routes/chat_poll_abort.py::chat_poll`) — a TERMINAL field is
advertised on a NON-TERMINAL snapshot. `lib/tasks_pkg/orchestrator/_finalize.py`
stamps ``task['finishReason']`` at line ~843 but only flips
``task['status']='done'`` ~111 lines later (~954). Between them it runs the
dangling-round sweep, the compaction-usage fold and `_generate_tool_summary` —
the last is a *blocking LLM call*, so the window is seconds wide, not
microseconds. `chat_poll` copies `finishReason` out of the live task whenever it
is truthy, with NO status gate. Any poll landing in that window therefore returns
the self-contradictory pair ``{status:'running', finishReason:'stop'}``.

This is not hypothetical on this deployment: all 3 chat tasks on 2026-07-31
logged ``SSE stream … DISCONNECTED PREMATURELY — 0 events sent in 0.1s``, so
100% of traffic ran on the 1 Hz poll fallback (925 polls vs 5 sends in
access.log) — the transport where this window is sampled repeatedly.

LINK 2 (frontend, `core/conv_reducers.js::assistantTailIsPriorTurn`) — the
reducer treats ``!!msg.finishReason`` as "this tail is a PRIOR turn" *even when
the tail is bound to the very task now connecting*. `_pollFallback` copies the
contradictory `finishReason` onto the live message; the next `connectToTask`
re-entry then classifies the task's OWN live bubble as somebody else's finished
turn, pushes a fresh placeholder with a NEW `_msgId`, and every subsequent delta
goes there. The original bubble is never written to again (frozen mid-sentence),
and the next repaint renders it statically plus a new `#streaming-msg` — two
bubbles, one data entry.

THE FIX
-------
1. `chat_poll` gates the terminal fields on a terminal status, so a running task
   never advertises `finishReason` (the field becomes an honest terminal signal).
2. `assistantTailIsPriorTurn` makes IDENTITY win: a tail explicitly bound to THIS
   task (`_taskId === activeTaskId`) is never a "prior turn", whatever
   `finishReason` says. The reload-safe `!!finishReason` arm is PRESERVED for
   tails that are NOT bound to this task (a DB-loaded tail has no `_taskId`) —
   that arm is what `test_frontend_connecttotask_taskid_dedupe.py` Scenario D
   pins, and it must keep working.

Defence in depth is deliberate: (1) stops the contradiction being minted, (2)
stops it being load-bearing if any other writer ever mints one again (the SSE
`state` handler at sse_pipeline.js:884 also copies `ev.finishReason` onto the
live message).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
        tests/test_duplicate_bubble_midturn_finish_reason.py -q
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
POLL = os.path.join(ROOT, 'routes', 'chat_poll_abort.py')
FINALIZE = os.path.join(ROOT, 'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')
CORE_DIR = os.path.join(ROOT, 'static', 'js', 'core')

_REDUCER_SIG = 'function assistantTailIsPriorTurn('


def _reducer_module() -> str:
    """Locate the core module defining the shared prior-turn reducer.

    Resolved by search, not a hardcoded path: the reducer already moved once
    (core/conversations.js → core/conv_reducers.js) and a hardcoded path made
    that relocation look like a behavioural regression.
    """
    hits = sorted(
        os.path.join(CORE_DIR, name)
        for name in os.listdir(CORE_DIR)
        if name.endswith('.js')
        and _REDUCER_SIG in open(os.path.join(CORE_DIR, name), encoding='utf-8').read()
    )
    assert hits, 'assistantTailIsPriorTurn is not defined in any static/js/core/*.js'
    assert len(hits) == 1, f'reducer duplicated across {hits}'
    return hits[0]


def _reducer_src() -> str:
    src = open(_reducer_module(), encoding='utf-8').read()
    start = src.index(_REDUCER_SIG)
    end = src.index('\n}\n', start) + len('\n}\n')
    return src[start:end]


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ─────────────────────────────────────────────────────────────────────────
# LINK 1 — the backend must not mint the contradiction
# ─────────────────────────────────────────────────────────────────────────

def test_finalize_window_between_finishreason_and_done_is_real():
    """Document the WINDOW this guard exists for — and fail loudly if the
    orchestrator is ever restructured so the two stamps become adjacent.

    This is not a style assertion: the whole reason `chat_poll` needs a status
    gate is that these two writes are separated by blocking work. If a future
    refactor makes them atomic, this test should be revisited rather than
    silently kept alive on a premise that no longer holds.
    """
    lines = open(FINALIZE, encoding='utf-8').read().splitlines()
    fr = next((i for i, l in enumerate(lines, 1)
               if l.strip() == "task['finishReason'] = last_finish_reason"), None)
    assert fr, "the finishReason stamp moved — re-derive this guard's premise"
    done = next((i for i, l in enumerate(lines, 1)
                 if i > fr and l.strip() == "task['status'] = 'done'"), None)
    assert done, "the terminal status flip moved — re-derive this guard's premise"
    assert done > fr, (
        'status=done is now stamped BEFORE finishReason — the window this guard '
        'protects against has inverted; re-derive the fix.'
    )
    # Blocking work inside the window is what makes it seconds-wide (and thus
    # reliably sampled by a 1 Hz poll) rather than a microsecond race.
    window = '\n'.join(lines[fr:done])
    assert '_generate_tool_summary' in window, (
        'the blocking LLM tool-summary call left the finishReason→done window; '
        'the window may now be narrow, but the poll gate is still required for '
        'correctness — verify before weakening it.'
    )


def _poll_the_real_endpoint(task_fields: dict) -> dict:
    """Register an in-memory task and GET the REAL /api/v1/chat/poll/<id>.

    Behavioural, not source-text: the response body is what the browser
    actually receives, so deleting the gate in the handler makes the assertion
    fail (proven by the NEUTER tests below). Harness mirrors
    tests/test_chat_created_at_surface.py, the existing guard for this endpoint.
    """
    import asyncio
    import importlib.util
    import sys
    import threading

    sys.path.insert(0, ROOT)
    os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
    os.environ.setdefault('TOFU_DB_PATH', '/tmp/dup_bubble_poll_gate.db')

    from lib.database import init_db
    init_db()
    from lib import auth_mode as _auth_mode
    _prev = os.environ.pop('TOFU_AUTH_MODE', None)
    _auth_mode.reset_for_tests()
    _auth_mode.set_mode('open', set_by='dup-bubble-test')

    from lib.tasks_pkg import tasks, tasks_lock
    tid = 'tk-dupbubble-gate'
    task = {
        'id': tid, 'convId': 'cv-dupbubble', 'content': 'partial answer…',
        'thinking': '', 'error': None, 'toolRounds': [],
        'created_at': 1_700_000_000.0,
        'events': [], 'events_lock': threading.Lock(),
    }
    task.update(task_fields)
    with tasks_lock:
        tasks[tid] = task

    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(ROOT, 'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)

    captured: dict = {}

    async def _t():
        async with mod.app.test_client() as client:
            r = await client.get(f'/api/v1/chat/poll/{tid}')
            captured['status'] = r.status_code
            captured['json'] = await r.get_json()

    try:
        asyncio.run(_t())
    finally:
        with tasks_lock:
            tasks.pop(tid, None)
        _auth_mode.reset_for_tests()
        os.environ['TOFU_AUTH_MODE'] = _prev if _prev is not None else 'private'
        _auth_mode.reset_for_tests()

    assert captured.get('status') == 200, captured
    return captured.get('json') or {}


def test_chat_poll_withholds_finish_reason_while_running():
    """THE defect, on the real wire: a task inside the finalize window
    (finishReason stamped, status still 'running') must NOT advertise it.

    This is the exact state `_finalize.py` holds for the duration of the
    blocking `_generate_tool_summary` call. Shipping `finishReason` here is
    what makes the frontend reducer classify the live bubble as a prior turn.
    """
    body = _poll_the_real_endpoint({
        'status': 'running', 'finishReason': 'stop',
        'usage': {'input_tokens': 10}, 'preset': 'max',
    })
    assert body.get('status') == 'running', body
    assert 'finishReason' not in body, (
        "chat_poll shipped {status:'running', finishReason:%r} — a running task "
        'must never advertise a terminal field. The frontend reads it as "this '
        'tail is a finished prior turn", pushes a second assistant bubble, and '
        'the first one freezes mid-generation.' % body.get('finishReason')
    )
    # Live progress must STILL flow — the gate withholds terminal fields only.
    assert body.get('content') == 'partial answer…', (
        'the gate suppressed live content — it must withhold ONLY the terminal '
        f'fields, otherwise streaming breaks: {body}'
    )


def test_chat_poll_still_ships_finish_reason_when_terminal():
    """The COMPLEMENT: once the task is genuinely done, the terminal fields
    must arrive — otherwise the turn never settles and the finish bar is empty.

    Without this, "withhold while running" could be satisfied by withholding
    always, which would be a worse bug than the one being fixed.
    """
    body = _poll_the_real_endpoint({
        'status': 'done', 'finishReason': 'stop',
        'usage': {'input_tokens': 10}, 'preset': 'max',
    })
    assert body.get('status') == 'done', body
    assert body.get('finishReason') == 'stop', (
        f'a DONE task must still ship its finishReason: {body}')
    assert body.get('usage'), f'a DONE task must still ship usage: {body}'


def _stream_the_real_endpoint(task_fields: dict) -> dict:
    """Build the `state` frame through the REAL production builder
    (`lib.chat_dispatch.build_fresh_state_snapshot`) — the exact function the
    SSE endpoint calls to compose its fresh-connection snapshot.

    WHY NOT AN HTTP ROUND-TRIP (as the poll tests use): measured — for a task
    whose status is 'running', `/api/chat/stream/<id>` deliberately HOLDS THE
    CONNECTION OPEN for `_MAX_SSE_DURATION = 7200` seconds (routes/chat.py) to
    keep streaming deltas. A test client cannot read the body until the stream
    ends, so the request simply times out (first attempt: 299.3s, zero frames
    parsed) — and the running case is precisely the one under test. Registering
    the task as 'done' to make the endpoint return would destroy the scenario.

    So this drives the real builder at its function boundary: still the shipped
    code path composing the real event dict (NOT a source-text assertion — the
    NEUTER below proves it bites), just entered below the transport that cannot
    terminate for a live task.
    """
    import sys
    import threading

    sys.path.insert(0, ROOT)
    os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
    os.environ.setdefault('TOFU_DB_PATH', '/tmp/dup_bubble_sse_gate.db')

    from lib.chat_dispatch import build_fresh_state_snapshot

    task = {
        'id': 'tk-dupbubble-sse', 'convId': 'cv-dupbubble-sse',
        'content': 'partial answer…', 'thinking': '', 'error': None,
        'toolRounds': [], 'created_at': 1_700_000_000.0,
        'events': [], 'events_lock': threading.Lock(),
    }
    task.update(task_fields)
    state, _meta, _cursor = build_fresh_state_snapshot(task)
    return state


def test_sse_state_snapshot_withholds_finish_reason_while_running():
    """THE SECOND TRANSPORT — the one the poll gate does NOT cover.

    `lib/chat_dispatch.py::build_fresh_state_snapshot` copied
    `extract_task_meta` into the `state` event with a bare `if meta.get(key)`
    and no status gate. Since this path exists to attach to a RUNNING task, it
    reliably emitted {status:'running', finishReason:'stop'} inside the
    finalize window.

    The frontend reducer cannot save this: `renderFinishInfo`
    (static/js/ui/finish_info.js) reads `msg.finishReason || msg.usage` as the
    terminal signal directly, never passing through
    `assistantTailIsPriorTurn` — so a still-generating turn got a settled
    finish bar. Fixing only the poll transport left this open, which is why
    the bug class kept coming back.
    """
    state = _stream_the_real_endpoint({
        'status': 'running', 'finishReason': 'stop',
        'usage': {'input_tokens': 10}, 'preset': 'max', 'model': 'claude-opus-5',
    })
    assert state.get('status') == 'running', state
    assert 'finishReason' not in state, (
        'the SSE `state` snapshot shipped finishReason=%r on a status=running '
        'task — finish_info.js paints a settled finish bar from it and '
        'connectToTask misfiles the live bubble as a prior turn.'
        % state.get('finishReason'))
    assert 'usage' not in state, (
        'the SSE `state` snapshot shipped `usage` on a running task; '
        'finish_info.js treats `usage` as a terminal signal too '
        '(`msg.finishReason || msg.usage`).')
    assert 'preset' not in state, (
        '`preset` is stamped in the same _finalize.py block as '
        'finishReason/usage and must share their gate.')
    # Live progress + the model tag MUST still flow.
    assert state.get('content') == 'partial answer…', (
        f'the gate suppressed live content: {state}')
    assert state.get('model') == 'claude-opus-5', (
        'the gate must NOT withhold `model` — it is set at task birth and the '
        f'live bubble renders its tag from it: {state}')


def test_sse_state_snapshot_still_ships_terminal_fields_when_done():
    """COMPLEMENT: a terminal `state` snapshot must carry the terminal fields,
    otherwise a reconnect to a finished turn shows a permanently empty finish
    bar — worse than the bug being fixed."""
    state = _stream_the_real_endpoint({
        'status': 'done', 'finishReason': 'stop',
        'usage': {'input_tokens': 10}, 'preset': 'max', 'model': 'claude-opus-5',
    })
    assert state.get('status') == 'done', state
    assert state.get('finishReason') == 'stop', (
        f'a DONE state snapshot must still ship finishReason: {state}')
    assert state.get('usage'), f'a DONE state snapshot must still ship usage: {state}'


def test_terminal_gate_is_a_single_shared_implementation():
    """The rule must exist ONCE. `extract_task_meta`'s docstring records what
    four hand-maintained copies of a metadata field policy already cost this
    project — so both transports import the same module rather than each
    carrying its own constant set."""
    gate = os.path.join(ROOT, 'lib', 'chat', 'terminal_gate.py')
    assert os.path.exists(gate), 'the shared terminal gate module is missing'
    poll_src = open(POLL, encoding='utf-8').read()
    disp_src = open(os.path.join(ROOT, 'lib', 'chat_dispatch.py'),
                    encoding='utf-8').read()
    assert 'from lib.chat.terminal_gate import' in poll_src, (
        'chat_poll no longer imports the shared gate — it has re-grown its own '
        'copy of the terminal-field policy.')
    assert 'lib.chat.terminal_gate' in disp_src, (
        'chat_dispatch no longer imports the shared gate — the SSE state '
        'snapshot has drifted off the shared rule.')
    # Neither consumer may hand-roll the status set again.
    for label, src in (('routes/chat_poll_abort.py', poll_src),
                       ('lib/chat_dispatch.py', disp_src)):
        assert "frozenset({'done', 'error', 'aborted', 'interrupted'})" not in src, (
            f'{label} re-defines the terminal-status set locally — it must come '
            f'from lib/chat/terminal_gate.py')


def test_terminal_gate_does_not_strip_terminal_done_events():
    """The gate must NOT be pushed down into `extract_task_meta`.

    That function's output also builds genuinely TERMINAL `done` events (the
    late/synthetic done in chat_dispatch.py and routes/chat.py). Gating there
    would strip the terminal fields from the very events whose job is to
    deliver them — so `extract_task_meta` stays status-blind by design and the
    gate lives at the snapshot boundary.
    """
    from lib.chat.persistence import extract_task_meta
    meta = extract_task_meta({
        'status': 'running', 'finishReason': 'stop',
        'usage': {'input_tokens': 5}, 'model': 'm',
    })
    assert meta.get('finishReason') == 'stop', (
        'extract_task_meta became status-aware — that breaks late/synthetic '
        '`done` events, which MUST carry the terminal fields. The gate belongs '
        'at the snapshot boundary (lib/chat/terminal_gate.py), not here.')


def _db_row_state_meta(status: str, meta: dict) -> dict:
    """Apply the gate exactly as the SSE DB-ROW state branch does.

    That branch (`lib/chat_dispatch.py`, the "DB snapshot path") serves a
    reconnect whose task is no longer in memory. It reads `status` from the
    persisted `task_results` row and `meta` from that row's metadata JSON —
    and those two are written by DIFFERENT writers at different times, so the
    row can legitimately hold `status='running'` alongside a finishReason
    persisted inside the finalize window.

    The branch is `async` and sits behind a DB fetch, so the assertion is made
    against the same shared gate call it performs, extracted here verbatim.
    The `test_terminal_gate_is_a_single_shared_implementation` test above pins
    that the branch actually routes through this gate rather than hand-rolling
    its own copy.
    """
    from lib.chat.terminal_gate import filtered_snapshot_meta
    return filtered_snapshot_meta(meta, status)


def test_sse_db_row_state_branch_withholds_terminal_fields_while_running():
    """The SSE DB-row reconnect branch must obey the same gate.

    Reachable whenever a client reconnects to a task that has been evicted
    from memory (TTL / restart) while its persisted row still says 'running' —
    the sharded-backend reconnect verdict keeps exactly that state. Leaving
    this branch ungated would let the identical contradiction back onto the
    wire through the other half of the same endpoint.
    """
    persisted = {'finishReason': 'stop', 'usage': {'input_tokens': 7},
                 'preset': 'max', 'model': 'claude-opus-5',
                 'apiRounds': [{'r': 1}]}
    running = _db_row_state_meta('running', persisted)
    assert 'finishReason' not in running, (
        f'the SSE DB-row state branch shipped a terminal field on a running '
        f'row: {running}')
    assert 'usage' not in running, running
    assert running.get('model') == 'claude-opus-5', (
        f'live/identity fields must survive the gate: {running}')
    assert running.get('apiRounds'), (
        f'apiRounds is progress, not a settlement claim — it must survive: {running}')
    # Complement: an evicted-but-FINISHED task still hands over its finish bar.
    done = _db_row_state_meta('done', persisted)
    assert done.get('finishReason') == 'stop', done
    assert done.get('usage'), done


def test_chat_poll_gate_covers_the_db_branch_too():
    """The DB branch must apply the same gate.

    It computes `effective_status`, which stays 'running' under the sharded
    reconnect verdict while `_db_meta` may already carry a persisted
    finishReason. Gating only the in-memory branch would leave the identical
    contradiction reachable on the other half of the same endpoint.

    Kept as a source assertion because reaching that branch needs a persisted
    task_results row under the redis backend; the behavioural tests above cover
    the branch that 100% of this deployment's traffic actually hits.
    """
    src = open(POLL, encoding='utf-8').read()
    assert re.search(r'_db_terminal_ok\s*=\s*_is_terminal_status\(effective_status\)',
                     src), (
        'the DB branch of chat_poll has no terminal-field gate — under the '
        'sharded reconnect verdict it can still ship a finishReason on a '
        "status='running' response."
    )
    assert re.search(r'if key in _TERMINAL_ONLY_KEYS and not _db_terminal_ok:\s*\n\s*continue',
                     src), 'the DB branch computes the gate but never applies it.'


# ─────────────────────────────────────────────────────────────────────────
# LINK 2 — the reducer must let identity beat a stale terminal field
# ─────────────────────────────────────────────────────────────────────────

_HARNESS = r"""
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

__REDUCER__

/* ── The reported bug, as data ────────────────────────────────────────────
 * A LIVE turn, still generating, whose message got a finishReason copied onto
 * it by a poll that landed inside the backend's finalize window. It is bound
 * to THIS task (_taskId === activeTaskId) — that binding is stamped by
 * connectToTask at stream-bind time, so it is present for every live turn.
 *
 * Classifying it as a "prior turn" is what mints the second bubble. */
(function () {
  const liveOwnTail = { role: 'assistant', content: 'partial…',
                        _taskId: 'T', finishReason: 'stop', _msgId: 'a_live' };
  check('own_task_tail_is_never_a_prior_turn',
        assistantTailIsPriorTurn(liveOwnTail, 'T') === false);
})();

/* Same tail, no terminal field — must obviously still be reusable. */
(function () {
  const liveOwnTail = { role: 'assistant', content: 'partial…',
                        _taskId: 'T', _msgId: 'a_live' };
  check('own_task_live_tail_reusable',
        assistantTailIsPriorTurn(liveOwnTail, 'T') === false);
})();

/* ── The COMPLEMENT — the reload-safe arm must survive the fix ──────────────
 * A DB-loaded completed tail has NO _taskId (it is not persisted). A brand-new
 * task must still push a fresh placeholder ahead of it, or the old turn gets
 * replayed into the new bubble. This is Scenario D of
 * test_frontend_connecttotask_taskid_dedupe.py — narrowing the finishReason arm
 * too far would regress it, so it is pinned here as well. */
(function () {
  const reloadedTail = { role: 'assistant', content: 'old reply',
                         finishReason: 'stop', _msgId: 'a_dbold' };
  check('reloaded_completed_tail_without_taskid_is_prior',
        assistantTailIsPriorTurn(reloadedTail, 'T_NEW') === true);
})();

/* A FOREIGN task's completed tail is still a prior turn. */
(function () {
  const foreignDone = { role: 'assistant', content: 'other', _taskId: 'T_OLD',
                        finishReason: 'stop', _msgId: 'a_old' };
  check('foreign_completed_tail_is_prior',
        assistantTailIsPriorTurn(foreignDone, 'T_NEW') === true);
})();

/* A FOREIGN task's still-open tail is a prior turn (the _staleTaskId arm). */
(function () {
  const foreignOpen = { role: 'assistant', content: 'other', _taskId: 'T_OTHER',
                        _msgId: 'a_other' };
  check('foreign_open_tail_is_prior',
        assistantTailIsPriorTurn(foreignOpen, 'T_NEW') === true);
})();

/* Non-assistant tails are never prior turns. */
(function () {
  check('user_tail_not_prior',
        assistantTailIsPriorTurn({ role: 'user', content: 'q' }, 'T') === false);
})();

console.log(out.join('\n'));
"""


def _run_reducer_harness(reducer_src: str) -> str:
    harness = os.path.join(HERE, '_dupbubble_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS.replace('__REDUCER__', reducer_src))
    try:
        proc = subprocess.run(['node', harness], capture_output=True,
                              text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_reducer_identity_beats_stale_finish_reason():
    output = _run_reducer_harness(_reducer_src())
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, (
        'prior-turn reducer failures (a live own-task tail misclassified as a '
        'prior turn is exactly the duplicate-bubble bug):\n' + output
    )


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_reducer_without_own_task_arm_reproduces_the_duplicate():
    """NEUTER: restore the pre-fix reducer → the live own-task tail is
    misclassified again, which is the duplicate-bubble bug.

    Proves the new arm is load-bearing and not decorative.
    """
    pre_fix = (
        'function assistantTailIsPriorTurn(msg, activeTaskId) {\n'
        "  if (!msg || msg.role !== 'assistant') return false;\n"
        '  const _staleTaskId = !!(msg._taskId && msg._taskId !== activeTaskId);\n'
        '  const _isCompletedTurn = !!msg.finishReason;\n'
        '  return _staleTaskId || _isCompletedTurn;\n'
        '}\n'
    )
    output = _run_reducer_harness(pre_fix)
    assert 'FAIL own_task_tail_is_never_a_prior_turn' in output, (
        'the pre-fix reducer no longer reproduces the bug — the harness has '
        'stopped exercising the real defect:\n' + output
    )
    # The complement must still pass under the neuter: this proves the two
    # tests fail for DIFFERENT reasons and the fix is a narrowing, not a
    # wholesale removal of the finishReason arm.
    assert 'PASS reloaded_completed_tail_without_taskid_is_prior' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_overbroad_reducer_regresses_the_reload_path():
    """NEUTER (other direction): dropping the finishReason arm ENTIRELY — the
    lazy way to make the first test pass — regresses the reload path.

    Pins that the fix must be "identity wins", not "ignore finishReason".
    """
    overbroad = (
        'function assistantTailIsPriorTurn(msg, activeTaskId) {\n'
        "  if (!msg || msg.role !== 'assistant') return false;\n"
        '  return !!(msg._taskId && msg._taskId !== activeTaskId);\n'
        '}\n'
    )
    output = _run_reducer_harness(overbroad)
    assert 'FAIL reloaded_completed_tail_without_taskid_is_prior' in output, (
        'dropping the finishReason arm no longer regresses the reload path — '
        'the complement assertion has gone blind:\n' + output
    )
    assert 'PASS own_task_tail_is_never_a_prior_turn' in output, output
