"""tests/test_serial_lane_heartbeat.py — pt_9f5a51ba45bd423c.

THE DEFECT
----------
``_start_tool_heartbeat`` was wired into ONE of the three dispatch lanes of
``execute_tool_pipeline``: the parallel read-only pool. The two SERIAL lanes
ran ``_execute_tool_one`` bare, so a tool blocking there emitted nothing at
all — and the stuck-task reaper's discriminator is exactly "both liveness
clocks silent past ``TOFU_STUCK_TASK_MAX_SILENT_SECS`` (1800s)".

Measured in production 2026-07-31 (logs/app.log), two tasks, same shape:

  task 38562f78 (conv ms8bx708)  run_command @10:46:54 → reaped 11:17:40
  task 31d08c82 (conv ms8c54p5)  run_command @        → reaped 11:18:41

both ``no event/dispatch progress for 1846s``, both ``finish=aborted`` with the
process group killed, ¥22.95 + ¥12.01 of completed rounds discarded. The user
saw a bubble reading "内部错误 / Internal error — 请查看服务器日志".

Three properties are pinned here, each independently NEUTER-able:

1. **Serial WRITE lane heartbeats** (``run_command``, ``write_file``, every
   non-readOnly MCP tool — ``_task_partitions`` puts them all in the write
   partition). This is the lane that produced both production casualties.

2. **Long-blocking serial lane heartbeats** (``_SERIAL_BLOCKING_TOOLS``).
   ``_heartbeat.py``'s own comment asserts these are reaper-immune because
   "the heartbeat below keeps refreshing ``_dispatch_heartbeat``" — MEASURED
   FALSE for ``await_task``: ``lib/scheduler/executor/_await.py`` contains
   zero ``_dispatch_heartbeat`` writes and zero ``append_event`` calls, and
   its own wait is capped at 3600s, i.e. twice the reap threshold. Only
   ``ask_human`` (self-bumps in ``lib/tasks_pkg/human_guidance.py``) and
   ``timer_create`` (emits ``append_event`` per poll) were genuinely covered.
   So the comment documented a protection that did not exist for one of its
   three members.

3. **The reaper's error envelope is ``worker_lost``, not ``internal``.**
   ``internal`` is ``retryable=False`` and its hint is "go read
   logs/error.log" — for an event the user can neither diagnose nor act on.
   ``worker_lost`` already exists in ``KINDS`` / ``_RETRYABLE_KINDS`` /
   ``_WARNING_KINDS`` / ``_TITLES`` with a frontend chip and both i18n keys,
   and its hint states the one true recovery: re-running is safe.

WHY A HEARTBEAT AND NOT A TIMEOUT
---------------------------------
Capping the wait was considered and is FORBIDDEN by a ratified decision
recorded at ``lib/tasks_pkg/tool_dispatch/_heartbeat.py`` (2026-07-25, epic
pt_1acd0bcdb2174566 F4, option A): "Do NOT 'fix' this by capping the
heartbeat; the cap question was decided as status-quo." ``run_command``
likewise resolves ``timeout=None`` by design and is pinned by
``tests/test_no_backend_timeouts.py``. Aliveness is proven by BEATING.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_serial_lane_heartbeat.py -v
"""

from __future__ import annotations

import ast
import inspect
import os
import textwrap
import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  Harness — drive the REAL execute_tool_pipeline with a slow fake tool
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    t = {
        'id': 'serial-hb-1',
        'convId': 'cv-serial-hb',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        # Both liveness clocks start STALE (epoch 0) — exactly the state the
        # reaper reads. A lane that never beats leaves them at 0.
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    t.update(over)
    return t


def _mk_tc(tc_id: str, fn_name: str, seq: int, args=None):
    """Build a parsed_tcs 7-tuple through the REAL round constructor."""
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, args or {}, tc_id, '{}', seq, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, dict(args or {}), round_entry['roundNum'],
            round_entry, None)


class _Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append(dict(event))
        # Mirror the REAL append_event side effect the reaper depends on:
        # every emitted event bumps liveness clock #1 — EXCEPT events marked
        # ``_selfTick`` (the tool-heartbeat pinging itself, pt_8524e0ec),
        # which lib/tasks_pkg/manager/_events.py deliberately skips: a
        # self-tick proves the dispatcher is alive, NOT that the tool is
        # producing. Mirroring the bump for self-ticks here (the pre-
        # 2026-08-01 behaviour) made silent ordinary tools look alive in
        # this harness while production would grade them reap-eligible.
        if not event.get('_selfTick'):
            task['_t_last_event'] = time.time()

    def of_type(self, etype: str):
        return [e for e in self.events if e.get('type') == etype]


@pytest.fixture()
def rec(monkeypatch):
    r = _Recorder()
    import lib.tasks_pkg.tool_dispatch._heartbeat as facade
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_pipeline, 'append_event', r, raising=False)
    monkeypatch.setattr(facade, 'append_event', r, raising=False)
    monkeypatch.setattr(_heartbeat, 'append_event', r, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', r, raising=False)
    return r


@pytest.fixture()
def slow_tools(monkeypatch):
    """Scripted executor: {fn_name: sleep_seconds}. Records the clock values
    observed by the tool WHILE it is still blocking, which is the only moment
    the reaper's verdict would be taken in production."""
    script: dict[str, float] = {}
    observed: dict[str, dict] = {}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        sleep_s = script.get(fn_name, 0.0)
        if sleep_s:
            time.sleep(sleep_s)
        # Snapshot the clocks at the LAST instant of the blocking window.
        # ★ The verdict must be taken HERE, not after the pipeline returns:
        #   once the tool completes, tool_result/tool_complete bump
        #   _t_last_event and every task looks alive. The reaper fires while
        #   the tool is STILL BLOCKING, so that is the only honest sample
        #   point. (A post-hoc check passes even against the unfixed code.)
        observed[fn_name] = {
            'dispatch': task.get('_dispatch_heartbeat', 0.0),
            'event': task.get('_t_last_event', 0.0),
            'reap_now': _reap_verdict(task),
        }
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': 'ok',
              'source': 'Test', 'fetched': True, 'fetchedChars': 2}])
        return tc_id, 'ok', False

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)
    return script, observed


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch):
    """Clamped floor is 2s; keep every test in the low seconds."""
    monkeypatch.setenv('TOOL_HEARTBEAT_INTERVAL', '2')


def _run(task, tcs, cfg=None):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    messages: list = []
    execute_tool_pipeline(
        task, tcs, cfg=cfg or {'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test-model')
    return messages


def _reap_verdict(task) -> bool:
    """Apply the REAL reaper predicate to this task.

    Imported from the production module rather than restated, so a change to
    the discriminator is reflected here instead of drifting.
    """
    from lib.tasks_pkg.manager._maintenance import _stuck_task_max_silent_secs
    max_silent = _stuck_task_max_silent_secs()
    now = time.time()
    created = task.get('created_at', now)
    last_event = task.get('_t_last_event', created)
    heartbeat = task.get('_dispatch_heartbeat', created)
    return (now - last_event) >= max_silent and (now - heartbeat) >= max_silent


# ═══════════════════════════════════════════════════════════════════
#  Face 1 — the serial WRITE lane (the production casualty)
# ═══════════════════════════════════════════════════════════════════

def test_serial_write_lane_beats_while_the_tool_blocks(rec, slow_tools):
    """``run_command`` blocking past a heartbeat interval MUST still beat on
    the WIRE — but the beat is GRADED and must never feed the reaper clocks.

    ★ SEMANTICS CORRECTION (2026-08-01, drift repair): the original version
    of this test asserted ``_dispatch_heartbeat`` refreshes while run_command
    blocks. That was the pt_9f5a51ba semantics, DELIBERATELY reversed the
    same day by pt_8524e0ec (evidence grading, owner-ratified): for ordinary
    tools the tick is marked ``_selfTick: True`` — transport keepalive only,
    NOT evidence the tool is producing — so ``_dispatch_heartbeat`` stays
    untouched and ``append_event`` skips the ``_t_last_event`` bump. A silent
    command IS reap-eligible; since pt_232244fb the reaper's answer is to
    INTERRUPT the command (task spared), pinned in
    tests/test_run_command_interrupt.py. Name kept as a historical handle.
    """
    script, observed = slow_tools
    script['run_command'] = 5.0          # > 2s interval → at least 2 ticks
    task = _mk_task()

    _run(task, [_mk_tc('tc-rc', 'run_command', 0)])

    seen = observed['run_command']
    # (1) The transport beat exists: tool_progress ticks reached the wire
    #     while the tool was still blocking.
    ticks = [e for e in rec.of_type('tool_progress')
             if e.get('toolCallId') == 'tc-rc']
    assert ticks, (
        'no tool_progress emitted for a 5s serial-write run_command — the '
        'SSE stream is silent for the whole command')
    # (2) The beat is GRADED: every tick for an ordinary tool carries the
    #     _selfTick marker ("dispatcher alive", not "tool producing").
    assert all(e.get('_selfTick') is True for e in ticks), (
        'ordinary-tool ticks lost their _selfTick grading: %r' % (ticks,))
    # (3) Evidence grading holds: the reaper's positive-liveness clock is
    #     NEVER fed by a self-tick.
    assert seen['dispatch'] == 0.0, (
        '_dispatch_heartbeat moved for an ordinary tool — a self-tick is '
        'impersonating real progress, the exact pt_8524e0ec defect class '
        '(a hung grep looked alive for 2.5h)')


def test_serial_write_lane_emits_tool_progress_while_blocking(rec, slow_tools):
    """The beat must also reach the WIRE, not just the in-memory clock.

    ``tool_progress`` is what (a) bumps ``_t_last_event`` through the real
    ``append_event``, (b) keeps the SSE stream non-silent so a buffering proxy
    does not idle-close, and (c) gives the UI its "run_command… (Ns)" ticker.
    A fix that only set the dict field would satisfy the reaper while leaving
    the user staring at a frozen spinner.
    """
    script, _obs = slow_tools
    script['run_command'] = 5.0
    task = _mk_task()

    _run(task, [_mk_tc('tc-rc', 'run_command', 0)])

    progress = [e for e in rec.of_type('tool_progress')
                if e.get('toolCallId') == 'tc-rc']
    assert progress, (
        'no tool_progress emitted for a 5s serial-write run_command — the SSE '
        'stream is silent for the whole command and _t_last_event never moves')
    assert any(e.get('elapsed', 0) >= 2 for e in progress), (
        'tool_progress carries no advancing elapsed — the UI cannot show how '
        'long the command has been running: %r' % (progress,))


def test_a_long_serial_write_is_not_reaped(rec, slow_tools):
    """★ NAME KEPT AS A HISTORICAL HANDLE — the contract was corrected twice.

    Original (pt_9f5a51ba): a beating tool is judged ALIVE by the reaper
    predicate. Reversed same-day (pt_8524e0ec, evidence grading): a SILENT
    ordinary tool is deliberately reap-ELIGIBLE — self-ticks are transport
    keepalive, not evidence of production. Current (pt_232244fb): when the
    predicate fires on a task blocked inside a real run_command, the
    reaper's ACTION is to INTERRUPT the command (partial output returns to
    the model, task spared) instead of force-failing the turn — that half
    is pinned end-to-end in tests/test_run_command_interrupt.py.

    What THIS test pins is the middle contract, sampled INSIDE the blocking
    window (a post-hoc sample passes even unfixed, because completion
    events refresh the clocks on the way out): a 5s silent run_command
    under a 3s threshold trips the REAL predicate. That is the input the
    interrupt path consumes.
    """
    script, observed = slow_tools
    script['run_command'] = 5.0
    task = _mk_task(created_at=time.time())
    # Squeeze the reap threshold to 3s: shorter than the 5s tool, so a lane
    # whose ticks are graded _selfTick (no real output) is unambiguously
    # reap-eligible mid-flight.
    os.environ['TOFU_STUCK_TASK_MAX_SILENT_SECS'] = '3'
    try:
        _run(task, [_mk_tc('tc-rc', 'run_command', 0)])
        assert observed['run_command']['reap_now'], (
            'evidence grading regressed: a 5s SILENT run_command under a 3s '
            'threshold is NOT reap-eligible — something is feeding a liveness '
            'clock without real output (the pt_8524e0ec defect class: a hung '
            'grep looked alive for 2.5h)')
    finally:
        os.environ.pop('TOFU_STUCK_TASK_MAX_SILENT_SECS', None)


def test_heartbeat_stops_when_the_serial_tool_finishes(rec, slow_tools):
    """The ticker must not outlive its tool.

    A leaked daemon ticker would keep a DEAD task's clocks warm forever,
    disabling the reaper for that task permanently — turning a false-kill bug
    into a never-kill bug.
    """
    script, _obs = slow_tools
    script['run_command'] = 3.0
    task = _mk_task()

    _run(task, [_mk_tc('tc-rc', 'run_command', 0)])

    # The stop event is set in a finally, but set() ≠ the thread has EXITED —
    # a loaded CI runner can schedule the ticker's exit past this assert
    # (3.10 leg, 2026-08-06). A REAL leak (stop never set) still fails: the
    # ticker would outlive a 2s grace by minutes.
    def _live_tickers():
        return [t for t in threading.enumerate()
                if t.name.startswith('tool-hb-') and t.is_alive()]

    deadline = time.time() + 2.0
    live = _live_tickers()
    while live and time.time() < deadline:
        time.sleep(0.05)
        live = _live_tickers()
    assert not live, (
        'heartbeat ticker thread(s) still alive after the pipeline returned: '
        '%r — the stop event is not set in a finally' % ([t.name for t in live],))

    settled = task['_dispatch_heartbeat']
    time.sleep(2.5)  # longer than one interval
    assert task['_dispatch_heartbeat'] == settled, (
        'the clock kept advancing after the tool finished — a leaked ticker '
        'makes this task permanently un-reapable')


# ═══════════════════════════════════════════════════════════════════
#  Face 2 — the long-blocking serial lane (_SERIAL_BLOCKING_TOOLS)
# ═══════════════════════════════════════════════════════════════════

def test_long_blocking_serial_lane_beats(rec, slow_tools, monkeypatch):
    """``await_task`` is in ``_SERIAL_BLOCKING_TOOLS`` and its handler
    (``lib/scheduler/executor/_await.py``) contains NO ``_dispatch_heartbeat``
    write and NO ``append_event`` — measured. Its own wait is capped at 3600s,
    i.e. double the reap threshold, so a legitimate long wait is reap-eligible
    while the module comment claims the opposite.
    """
    script, observed = slow_tools
    script['await_task'] = 5.0
    task = _mk_task()

    _run(task, [_mk_tc('tc-aw', 'await_task', 0, args={'action': 'wait'})])

    seen = observed['await_task']
    assert seen['dispatch'] > 0.0, (
        'the long-blocking serial lane never beat while await_task blocked — '
        "_heartbeat.py's comment asserts these tools are reaper-immune "
        'because "the heartbeat below keeps refreshing _dispatch_heartbeat", '
        'but no lane ever starts that ticker for them')


def test_await_task_handler_still_has_no_heartbeat_of_its_own():
    """Pin the PREMISE of the test above.

    If ``_await.py`` ever grows its own heartbeat, the lane-level guard could
    pass for the wrong reason (the handler beating, not the lane). This makes
    that change announce itself instead of silently hollowing out the sibling
    assertion.
    """
    import lib.scheduler.executor._await as await_mod
    src = inspect.getsource(await_mod)
    assert '_dispatch_heartbeat' not in src, (
        '_await.py now self-heartbeats — re-evaluate whether the lane-level '
        'ticker is still what protects await_task, and update the sibling '
        'test that relies on this premise')


# ═══════════════════════════════════════════════════════════════════
#  Face 3 — every serial lane is covered (enumerate, don't hand-list)
# ═══════════════════════════════════════════════════════════════════

def test_no_bare_execute_tool_one_call_survives_in_the_pipeline():
    """★ Enumerate rather than trust a list.

    The originating diagnosis named ONE uncovered lane (serial write); an AST
    walk found a second (long-blocking serial). This asserts the structural
    property directly: inside ``execute_tool_pipeline`` every
    ``_execute_tool_one`` call must sit under a ``try`` whose ``finally``
    stops a heartbeat — so a THIRD lane added later cannot quietly reintroduce
    the defect.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    src = textwrap.dedent(inspect.getsource(_pipeline.execute_tool_pipeline))
    tree = ast.parse(src)
    fn = tree.body[0]

    parent = {}
    for p in ast.walk(fn):
        for c in ast.iter_child_nodes(p):
            parent[c] = p

    def _is_exec_call(node):
        f = node.func
        return isinstance(f, ast.Name) and f.id == '_execute_tool_one'

    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and _is_exec_call(n)]
    assert calls, ('no _execute_tool_one call found — the pipeline was '
                   'restructured; re-target this guard')

    unguarded = []
    for call in calls:
        node, guarded = call, False
        while node in parent:
            node = parent[node]
            if isinstance(node, ast.Try) and node.finalbody:
                fin = ast.unparse(ast.Module(body=node.finalbody,
                                             type_ignores=[]))
                if '_hb_stop' in fin:
                    guarded = True
                    break
        if not guarded:
            unguarded.append(ast.unparse(call)[:80])

    assert not unguarded, (
        'these _execute_tool_one call sites run with NO heartbeat ticker '
        'around them, so a tool blocking there goes silent on BOTH reaper '
        'clocks and gets the whole task force-failed at '
        'TOFU_STUCK_TASK_MAX_SILENT_SECS: %r' % (unguarded,))


# ═══════════════════════════════════════════════════════════════════
#  Face 4 — the reaper tells the truth about what happened
# ═══════════════════════════════════════════════════════════════════

def test_reaper_envelope_is_worker_lost_and_retryable(monkeypatch):
    """A reaped task must carry ``worker_lost``, not ``internal``.

    ``internal`` is retryable=False with the hint "go read logs/error.log" —
    it tells the user nothing they can act on and actively suppresses the
    frontend's retry affordance. ``worker_lost`` is already fully registered
    (KINDS / _RETRYABLE_KINDS / _WARNING_KINDS / _TITLES + frontend chip +
    both i18n keys) and its hint names the one correct recovery.
    """
    import lib.tasks_pkg.manager._maintenance as _maintenance
    from tests.support.chat_tasks import chat_task_registry

    now = time.time()
    stale = now - 4000
    task = {
        'id': 'reap-me-1', 'convId': 'cv-reap-1', 'status': 'running',
        'aborted': False, 'content': 'partial output', 'thinking': '',
        'config': {'model': 'claude-opus-5'},
        'created_at': stale, '_t_last_event': stale,
        '_dispatch_heartbeat': stale,
        'events': [], 'events_lock': threading.Lock(),
    }

    finalized = []
    monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                        lambda t: finalized.append(t))
    monkeypatch.setitem(chat_task_registry, task['id'], task)

    n = _maintenance.reap_stuck_running_tasks()
    assert n == 1, 'the synthetic wedged task should have been reaped'

    env = task['error']
    assert env['kind'] == 'worker_lost', (
        "the reaper stamps kind=%r. 'internal' renders as a bare '内部错误' "
        'with the hint "go read logs/error.log" — for an event the user did '
        'not cause, cannot diagnose, and whose only correct recovery '
        '(re-run) the envelope actively hides by being retryable=False'
        % (env['kind'],))
    assert env['retryable'] is True, (
        'a reaped task is retryable by construction — the worker is gone, so '
        're-running is the designed recovery')
    # The measured fact must survive into the envelope, not just the log.
    assert 'no progress' in env['detail'].lower()
    assert env['context'] == 'stuck-task-reaper'


def test_worker_lost_hint_does_not_send_the_user_to_the_logs_only(monkeypatch):
    """The user-facing text must state the ACTION, not delegate to a logfile.

    Charter-adjacent: this project has already ruled that an error hint whose
    only content is "look somewhere else" is a non-hint (the Settings→Keys
    misdirection case). Pin that the shipped worker_lost hint leads with the
    recovery.
    """
    from lib.error_envelope import make_envelope
    env = make_envelope('worker_lost', detail='x', context='stuck-task-reaper')
    hint = env['hint']
    assert '重新发起任务是安全的' in hint or 'retrying the task is safe' in hint, (
        'the worker_lost hint no longer leads with the recovery action: %r'
        % (hint,))
