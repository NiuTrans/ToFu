"""tests/test_tool_exec_failure_verdict.py — 2026-08-06 silent-timeout incident.

THE INCIDENT
------------
A ``get_conversation`` call exceeded the parallel-pool ceiling
(``TOOL_PARALLEL_TIMEOUT``). The pipeline recorded
``'Tool execution timed out: get_conversation'`` as the tool message — and
settled the round with NO terminal verdict. The wire ``tool_complete``
carried no ``status``, so the client reducer promoted the round to
``'done'`` and the chat timeline rendered a perfectly successful tool card
(token badge and all); the failure was visible only in the raw debug panel.
Owner verdict: "后端执行失败了,前端却显示的好像成功了——这里有显示逻辑 bug".

ROOT CAUSE
----------
``tool_results[tc_id] = (content, is_search)`` encodes failure ONLY in the
content string; the second tuple slot is ``is_search``, not a success flag.
The rejected/aborted lanes learned to ship a terminal verdict with the settle
(pt_ac380e3d), but the FAILURE lanes (raise / pool-timeout / abort-during-
pool / unknown-tool fallback) never did.

THE FIX
-------
A per-round verdict map ``tool_verdicts[tc_id] -> 'error' | 'aborted'`` is
populated at every failure lane, and the post-phase settle passes it as
``terminal_status`` — stamped on the round AND shipped on the wire, where the
reducer's terminal-verdict contract (pinned by
tests/test_tool_settle_all_lanes.py::test_client_never_overwrites_a_terminal_verdict)
keeps it from ever being promoted to 'done'.

This suite pins the BACKEND half: every failure lane must stamp + ship the
verdict, and the success hot path must stay silent (no status key at all).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_tool_exec_failure_verdict.py -v
"""

from __future__ import annotations

import inspect
import os
import re
import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  Harness — REAL pipeline + real round constructor, scripted executor.
#  Mirrors tests/test_tool_settle_all_lanes.py (kept self-contained per
#  repo convention: each suite carries its own harness).
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    t = {
        'id': 'verdict-task-1',
        'convId': 'cv-verdict-1',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    t.update(over)
    return t


def _mk_tc(tc_id: str, fn_name: str, seq: int, *, args=None):
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

    def find(self, tc_id: str, etype: str):
        for e in self.events:
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return e
        return None


@pytest.fixture()
def rec(monkeypatch):
    r = _Recorder()
    import lib.tasks_pkg.tool_dispatch._heartbeat as facade
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_pipeline, 'append_event', r, raising=False)
    monkeypatch.setattr(facade, 'append_event', r, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', r, raising=False)
    return r


@pytest.fixture()
def scripted_tools(monkeypatch):
    """Scripted executor: {fn_name: ('ok', sleep_s, text) | ('raise', exc)
    | ('abort_flip', sleep_s, text)}."""
    script: dict[str, tuple] = {}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        spec = script.get(fn_name, ('ok', 0.0, 'ok'))
        mode = spec[0]
        if mode == 'raise':
            raise spec[1]
        _sleep, text = spec[1], spec[2]
        if _sleep:
            time.sleep(_sleep)
        if mode == 'abort_flip':
            task['aborted'] = True
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': text[:60],
              'source': 'Test', 'fetched': True, 'fetchedChars': len(text)}])
        return tc_id, text, False

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)
    return script


def _run(task, tcs, cfg=None, messages=None):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    messages = messages if messages is not None else []
    timed_out = execute_tool_pipeline(
        task, tcs, cfg=cfg or {'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test-model')
    return messages, timed_out


# ═══════════════════════════════════════════════════════════════════
#  Face 1 — the reported incident: pool-timeout lane ships 'error'
# ═══════════════════════════════════════════════════════════════════

def test_timeout_lane_stamps_and_ships_error(rec, scripted_tools, monkeypatch):
    """★ THE INCIDENT FACE. A tool cancelled by the pool ceiling must render
    as FAILED — never as the clean 'done' card the owner screenshotted."""
    monkeypatch.setenv('TOOL_PARALLEL_TIMEOUT', '1')
    scripted_tools['get_conversation'] = ('ok', 2.5, 'SLOW BODY')
    scripted_tools['grep_search'] = ('ok', 0.0, 'FAST BODY')

    task = _mk_task()
    slow = _mk_tc('tc-slow', 'get_conversation', 1)
    fast = _mk_tc('tc-fast', 'grep_search', 2)
    messages, timed_out = _run(task, [slow, fast])

    assert timed_out is True, 'the pipeline must report the timeout upward'

    # The verdict is stamped on the round (cold projection ships rounds whole)
    # and SHIPPED on the tool_complete wire event (live lane).
    assert slow[5]['status'] == 'error', (
        'a pool-timeout round must be stamped status=error; got %r — without '
        'it the poll/cold lane renders the failure as a success card'
        % (slow[5]['status'],))
    ev = rec.find('tc-slow', 'tool_complete')
    assert ev is not None, 'the timed-out tool must still settle (spinner off)'
    assert ev.get('status') == 'error', (
        "tool_complete for a timed-out tool must carry status='error'; "
        'without it the client reducer promotes the round to done. Event: %r'
        % (ev,))
    assert 'timed out' in (ev.get('toolContent') or ''), (
        'the failure reason must reach the card verbatim; got %r'
        % (ev.get('toolContent'),))

    # The model receives the failure string (never a fabricated success).
    tool_msgs = [m for m in messages if m.get('role') == 'tool']
    slow_msg = [m for m in tool_msgs if m.get('tool_call_id') == 'tc-slow']
    assert slow_msg and slow_msg[0]['content'].startswith(
        'Tool execution timed out:'), slow_msg

    # The FAST sibling is untouched: settles done, and its wire frame stays
    # SILENT (no status key at all — the hot path carries zero verdict noise).
    assert fast[5]['status'] == 'done'
    fast_ev = rec.find('tc-fast', 'tool_complete')
    assert fast_ev is not None and 'status' not in fast_ev, (
        'a successful tool_complete must NOT grow a status field — verdicts '
        'are failure-only noise on the wire. Event: %r' % (fast_ev,))


# ═══════════════════════════════════════════════════════════════════
#  Face 2 — the raise lane ships 'error'
# ═══════════════════════════════════════════════════════════════════

def test_exception_lane_stamps_and_ships_error(rec, scripted_tools):
    scripted_tools['fetch_url'] = ('raise', RuntimeError('boom'))
    scripted_tools['grep_search'] = ('ok', 0.0, 'FAST BODY')

    task = _mk_task()
    bad = _mk_tc('tc-bad', 'fetch_url', 1)
    fast = _mk_tc('tc-fast', 'grep_search', 2)
    messages, timed_out = _run(task, [bad, fast])

    assert timed_out is False
    assert bad[5]['status'] == 'error', (
        'a raised tool must be stamped status=error; got %r'
        % (bad[5]['status'],))
    ev = rec.find('tc-bad', 'tool_complete')
    assert ev is not None and ev.get('status') == 'error', (
        "tool_complete for a raised tool must carry status='error'. Event: %r"
        % (ev,))
    tool_msgs = [m for m in messages if m.get('role') == 'tool']
    bad_msg = [m for m in tool_msgs if m.get('tool_call_id') == 'tc-bad']
    assert bad_msg and bad_msg[0]['content'].startswith(
        'Tool execution error:'), bad_msg

    assert fast[5]['status'] == 'done'
    assert 'status' not in rec.find('tc-fast', 'tool_complete')


# ═══════════════════════════════════════════════════════════════════
#  Face 3 — abort DURING the pool ships 'aborted' (same hole, other lane)
# ═══════════════════════════════════════════════════════════════════

def test_in_pool_abort_lane_ships_aborted(rec, scripted_tools):
    """A sibling's completion flips task['aborted'] mid-pool; the remaining
    pending futures are cancelled with 'Task aborted by user.' — a lane that
    ALSO settled silently before this fix."""
    scripted_tools['read_files'] = ('abort_flip', 0.0, 'FAST BODY')
    scripted_tools['web_search'] = ('ok', 1.5, 'SLOW BODY')

    task = _mk_task()
    fast = _mk_tc('tc-fast', 'read_files', 1)
    slow = _mk_tc('tc-slow', 'web_search', 2)
    _run(task, [fast, slow])

    assert slow[5]['status'] == 'aborted', (
        'a pool-cancelled-by-abort round must be stamped aborted; got %r'
        % (slow[5]['status'],))
    ev = rec.find('tc-slow', 'tool_complete')
    assert ev is not None and ev.get('status') == 'aborted', (
        "tool_complete for an abort-cancelled tool must carry "
        "status='aborted'. Event: %r" % (ev,))


def test_pre_pool_abort_lane_ships_aborted(rec, scripted_tools):
    """Abort flipped by a SERIAL WRITE tool (which runs before the pool) is
    caught by the pre-pool abort check — the third failure lane."""
    scripted_tools['write_file'] = ('abort_flip', 0.0, 'WROTE')
    scripted_tools['read_files'] = ('ok', 0.0, 'READ')

    task = _mk_task()
    wr = _mk_tc('tc-wr', 'write_file', 1)
    rd = _mk_tc('tc-rd', 'read_files', 2)
    _run(task, [wr, rd])

    assert wr[5]['status'] == 'done', (
        'the serial write itself completed before flipping abort; got %r'
        % (wr[5]['status'],))
    assert rd[5]['status'] == 'aborted', (
        'the parallel tool skipped by the pre-pool abort check must be '
        'stamped aborted; got %r' % (rd[5]['status'],))
    ev = rec.find('tc-rd', 'tool_complete')
    assert ev is not None and ev.get('status') == 'aborted', (
        "the pre-pool abort lane must ship status='aborted'. Event: %r"
        % (ev,))


# ═══════════════════════════════════════════════════════════════════
#  Face 4 — enumerate, don't trust a hand-written list (drift guard)
# ═══════════════════════════════════════════════════════════════════

def test_timeout_except_catches_the_futures_class_too():
    """concurrent.futures.TimeoutError is a DISTINCT class from builtin
    TimeoutError on Python ≤ 3.10 (an alias only since 3.11) — a bare
    ``except TimeoutError`` after ``as_completed(timeout=…)`` /
    ``future.result(timeout=…)`` lets the pool-timeout lane ESCAPE uncaught
    on the 3.10 CI leg (2026-08-06, rounds 3-5). Pin the dual-catch so the
    lane is version-proof; the behavioral face above proves it on 3.10."""
    from lib.tasks_pkg import streaming_tool_executor as ste
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    for mod in (_pipeline, ste):
        src = inspect.getsource(mod)
        assert '_FuturesTimeoutError' in src, (
            f'{mod.__name__} lost the futures-TimeoutError dual-catch — on '
            '3.10 the pool-timeout lane escapes again')


def test_every_failure_sentinel_has_a_verdict():
    """Guard the guard: every ``tool_results[…] = (failure sentinel, …)``
    write inside ``execute_tool_pipeline`` MUST be paired with a
    ``tool_verdicts[…]`` write within the next couple of lines. A future lane
    that records a failure string without a verdict fails this test — the
    exact defect class of the incident, caught at review time instead of in
    the owner's screenshot."""
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    src = inspect.getsource(_pipeline.execute_tool_pipeline)
    lines = src.splitlines()
    sentinel = re.compile(
        r"tool_results\[.+\]\s*=\s*\(\s*(?:f?'Tool execution (?:error|timed out)"
        r"|'Task aborted by user\.'|f?'Unknown tool:)")
    # A failure sentinel reaches its verdict through ONE of two channels:
    # the verdict map (lanes settled in the post-phase) or an immediate
    # ``_settle_tool_result(… terminal_status=…)`` (early-settle lanes).
    # Either satisfies the contract; neither is the incident.
    missing = []
    for i, ln in enumerate(lines):
        if sentinel.search(ln):
            window = '\n'.join(lines[i:i + 14])
            if 'tool_verdicts[' not in window and 'terminal_status=' not in window:
                missing.append((i + 1, ln.strip()))
    assert not missing, (
        'failure-sentinel writes without a paired verdict (map or immediate '
        'terminal_status settle):\n  '
        + '\n  '.join('L%d %s' % (n, s) for n, s in missing)
        + '\nEvery failure lane must record a terminal verdict — that is the '
          'whole fix. See suite docstring.')


# ═══════════════════════════════════════════════════════════════════
#  Face 5 — the safety-net crash lane: handler raises INSIDE the real
#  _execute_tool_one (the pool's except lane NEVER fires in production —
#  the executor's universal catch returns normally, so no tool_verdicts
#  entry is recorded; the finalize seam itself must stamp + ship 'error')
# ═══════════════════════════════════════════════════════════════════

def test_safety_net_crash_lane_stamps_and_ships_error(rec):
    """★ THE PRODUCTION CRASH LANE. Face 2 fakes ``_execute_tool_one`` so its
    raise escapes to the pool; real handlers raise INSIDE the executor's
    universal safety net, which converts the exception into an error
    tool-result and RETURNS NORMALLY — the pool records no verdict, and
    before this fix the round was finalized as 'done': the model read
    'Error: tool execution failed…' while the human saw a ✓ success card,
    and the persisted round said 'done' forever.
    """
    from lib.tasks_pkg.executor import tool_registry

    _CRASH_TOOL = 'zz_verdict_crash_tool'

    def _crashing(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                  cfg, project_path, project_enabled, all_tools=None):
        raise RuntimeError('handler exploded')

    tool_registry.register(_CRASH_TOOL, _crashing)
    try:
        task = _mk_task()
        bad = _mk_tc('tc-crash', _CRASH_TOOL, 1)
        ok = _mk_tc('tc-ok', 'grep_search', 2)
        # NOTE: no scripted_tools patch — the REAL _execute_tool_one runs.
        messages, timed_out = _run(task, [bad, ok])
    finally:
        tool_registry._exact.pop(_CRASH_TOOL, None)
        tool_registry._metadata.pop(_CRASH_TOOL, None)

    assert timed_out is False
    assert bad[5]['status'] == 'error', (
        "a handler that crashed inside the executor safety net must be "
        "stamped status='error' — the finalize seam is the ONLY failure "
        "signal on this lane; got %r" % (bad[5]['status'],))

    # The tool_result frame is SELF-DESCRIBING: the verdict rides the event,
    # so no client ever settles this round by inference.
    res_ev = rec.find('tc-crash', 'tool_result')
    assert res_ev is not None and res_ev.get('status') == 'error', (
        "tool_result for a crashed tool must carry status='error'; got %r"
        % (res_ev,))

    cmp_ev = rec.find('tc-crash', 'tool_complete')
    assert cmp_ev is not None and cmp_ev.get('status') == 'error', (
        "tool_complete must not re-settle the crashed round as done; got %r"
        % (cmp_ev,))

    # The model still receives the recoverable error string (unchanged).
    crash_msgs = [m for m in messages if m.get('role') == 'tool'
                  and m.get('tool_call_id') == 'tc-crash']
    assert crash_msgs and 'execution failed' in crash_msgs[0]['content'], (
        crash_msgs)


def test_tool_result_frames_are_self_describing(rec, scripted_tools):
    """Every tool_result frame carries an explicit terminal status — success
    included. A client must never have to GUESS the verdict from the frame's
    arrival: guessing is how a crashed tool rendered as a ✓ success card."""
    scripted_tools['grep_search'] = ('ok', 0.0, 'FAST BODY')
    task = _mk_task()
    ok = _mk_tc('tc-ok', 'grep_search', 1)
    _run(task, [ok])

    res_ev = rec.find('tc-ok', 'tool_result')
    assert res_ev is not None, 'the success lane must emit tool_result'
    assert res_ev.get('status') == 'done', (
        "a successful tool_result must carry an explicit status='done' — "
        'the frame is the single source of truth, so the truth must be ON '
        'the frame; got %r' % (res_ev,))


# ═══════════════════════════════════════════════════════════════════
#  Face 6 — the finalize seam itself: status param, whitelist, and
#  verdict protection (no pipeline needed)
# ═══════════════════════════════════════════════════════════════════

def test_finalize_seam_verdict_rules(rec):
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round

    task = _mk_task()

    # 1. Default success: stamped + shipped as 'done'.
    r1 = {'query': 'q1', 'toolCallId': 'tc-f1', 'status': 'searching'}
    _finalize_tool_round(task, 1, r1, [{'toolName': 't'}])
    assert r1['status'] == 'done'
    assert rec.find('tc-f1', 'tool_result').get('status') == 'done'

    # 2. Explicit failure verdict: stamped + shipped.
    r2 = {'query': 'q2', 'toolCallId': 'tc-f2', 'status': 'searching'}
    _finalize_tool_round(task, 2, r2, [{'toolName': 't'}], status='error')
    assert r2['status'] == 'error'
    assert rec.find('tc-f2', 'tool_result').get('status') == 'error'

    # 3. Verdict protection: a late 'done' finalize never demotes a failure
    #    verdict the round already holds (pool-timeout lane whose cancelled
    #    thread finishes late).
    r3 = {'query': 'q3', 'toolCallId': 'tc-f3', 'status': 'error'}
    _finalize_tool_round(task, 3, r3, [{'toolName': 't'}])
    assert r3['status'] == 'error', (
        "a 'done' finalize must not demote a held failure verdict; got %r"
        % (r3['status'],))
    assert rec.find('tc-f3', 'tool_result').get('status') == 'error'

    # 4. Unknown statuses normalize to 'done' (a finalize SETTLES).
    r4 = {'query': 'q4', 'toolCallId': 'tc-f4', 'status': 'searching'}
    _finalize_tool_round(task, 4, r4, [{'toolName': 't'}],
                         status='whatever-future')
    assert r4['status'] == 'done'


def test_finalize_preserves_settled_tEnd(rec):
    """A late finalize must not clobber the completion time a settle already
    stamped — ``_settle_tool_result`` preserves an existing truthy ``tEnd``,
    and ``_finalize_tool_round`` must follow the same rule."""
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round

    task = _mk_task()
    original_end = int(time.time() * 1000) - 7000
    round_entry = {
        'query': 'q-tEnd', 'toolCallId': 'tc-tEnd', 'status': 'searching',
        'tStart': original_end - 1500, 'tEnd': original_end,
    }
    _finalize_tool_round(task, 1, round_entry, [{'toolName': 't'}])

    assert round_entry['tEnd'] == original_end, (
        'late finalize overwrote the settled tEnd; got %r'
        % (round_entry['tEnd'],))
    assert rec.find('tc-tEnd', 'tool_result')['tEnd'] == original_end, (
        'the wire frame must carry the preserved clock')
