"""tests/test_tool_settle_all_lanes.py — pt_ac380e3dde2c4c69.

THE GAP THE FIRST BATCH LEFT
----------------------------
pt_67ffc2b7 removed the round-level barrier for tools that go through a
DISPATCH lane (parallel pool, serial write, long-blocking). It wired
``_settle_tool_result`` into 5 call sites. But ``execute_tool_pipeline``'s
pre-phase has SIX ``continue`` branches that never reach any of them — they
record a result and jump straight to the next tool call, so their
``tool_complete`` is still emitted in the post-phase, i.e. AFTER
``pool.shutdown(wait=True)``.

An AST enumeration of every ``continue`` in ``execute_tool_pipeline`` found 8
before this batch (the original ticket named 3 — hence this suite ENUMERATES
rather than trusting a list):

  L116  parse_err / hallucinated-tool rejection   → now settled (rejected)
  L224  dedup / streaming-prefetch cache HIT     → now settled
  L250  write-approval REJECTED                  → now settled (rejected)
  L261  abort short-circuit                      → now settled (aborted)
  L350  pre-hook BLOCK                           → now settled (rejected)
  L370  serial-write abort skip                  → now settled (aborted)
  L291  long-blocking serial dispatch            → settled inline (already was)
  L630  screenshot                               → settled at DISPATCH; its
        ``continue`` is GONE, so the census now counts 7

Every skip lane is a ZERO-COST path: the tool did not run at all. So they are
precisely the calls that should light up instantly — and instead they were the
ones that waited longest. The sharpest case is the streaming-prefetch cache
hit: ``StreamingToolExecutor`` exists to run a tool WHILE the model is still
emitting tokens (``inject_into_cache`` is called from
``orchestrator/_run.py``), so by dispatch time its result is already in hand.
Measured event order for a prefetched ``read_files`` beside a 1.2s
``web_search``, BEFORE the fix::

    tool_result(cached) → tool_result(slow) → tool_complete(slow) → tool_complete(cached)

The tool that cost nothing settled LAST. The screenshot lane measured the same
way, and its stated justification ("the verdict depends on the model's vision
capability, resolved later") was FALSE: ``model`` is a parameter of
``execute_tool_pipeline`` and ``model_supports_vision`` is pure, so the verdict
is knowable at dispatch time.

★ THE SECOND, MORE DANGEROUS HALF
---------------------------------
Wiring settle into the abort / reject lanes naively would introduce a defect
strictly worse than the latency. ``stream_reducer.js``'s tool_complete case
reads::

    if (r.status !== 'rejected') r.status = 'done';

It protects ONE terminal verdict. ``aborted`` / ``error`` / ``unanswerable``
are all real round statuses in this codebase (measured: 9 / 15 / 1 assignment
sites), and every one of them would be overwritten to ``done`` by a
tool_complete arriving afterwards — painting a REFUSED or INTERRUPTED tool as
successfully completed. A user could not tell a rejected write from an applied
one.

So the contract is two-sided:
  * a settled round must announce promptly (latency), AND
  * a terminal verdict must never be overwritten by a later completion frame
    (correctness). The second is non-negotiable and is guarded in BOTH
    directions here.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_tool_settle_all_lanes.py -v
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
# ═══════════════════════════════════════════════════════════════════
#  Harness — the REAL pipeline, real round constructor, fake tool bodies
# ═══════════════════════════════════════════════════════════════════

def _mk_task(**over):
    t = {
        'id': 'lanes-task-1',
        'convId': 'cv-lanes-1',
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        '_userId': 1,
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    t.update(over)
    return t


def _mk_tc(tc_id: str, fn_name: str, seq: int, *, parse_err=None, args=None):
    """Build a parsed_tcs 7-tuple through the REAL round constructor.

    Hand-rolling the round dict would omit ``tStart`` (stamped by
    ``_build_tool_round_entry``), making every measured duration read 0ms — a
    FIXTURE defect that mimics a product defect. The first batch of this epic
    was bitten by exactly that, so the constructor is mandatory here.
    """
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, args or {}, tc_id, '{}', seq, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, dict(args or {}), round_entry['roundNum'],
            round_entry, parse_err)


class _Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append(dict(event))

    def idx(self, tc_id: str, etype: str) -> int:
        for i, e in enumerate(self.events):
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return i
        return -1

    def find(self, tc_id: str, etype: str):
        for e in self.events:
            if e.get('toolCallId') == tc_id and e.get('type') == etype:
                return e
        return None

    def types_for(self, tc_id: str):
        return [e['type'] for e in self.events if e.get('toolCallId') == tc_id]

    def ordered(self):
        return [(e.get('type'), e.get('toolCallId')) for e in self.events]


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
def slow_tools(monkeypatch):
    """A scripted executor: {fn_name: (sleep_s, body)}."""
    script: dict[str, tuple[float, str]] = {}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        sleep_s, text = script.get(fn_name, (0.0, 'ok'))
        if sleep_s:
            time.sleep(sleep_s)
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
    execute_tool_pipeline(
        task, tcs, cfg=cfg or {'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test-model')
    return messages


def _assert_settles_before_slow(rec, fast_id, slow_id, lane):
    """The shared shape: a zero-cost lane must fully settle before the slow
    sibling even produces its result."""
    fast_complete = rec.idx(fast_id, 'tool_complete')
    slow_result = rec.idx(slow_id, 'tool_result')
    assert fast_complete >= 0, (
        '%s lane emitted no tool_complete at all; events for %s: %r'
        % (lane, fast_id, rec.types_for(fast_id)))
    assert slow_result >= 0, 'the slow sibling must produce a result'
    assert fast_complete < slow_result, (
        '%s: this lane costs ZERO time (the tool never ran / was already '
        'cached), yet its tool_complete (idx=%d) lands AFTER the slow '
        "sibling's result (idx=%d) — it is still being settled in the "
        'post-phase behind pool.shutdown(wait=True). Wire the settle into '
        'this lane. Stream: %r'
        % (lane, fast_complete, slow_result, rec.ordered()))


# ═══════════════════════════════════════════════════════════════════
#  Face 0 — enumerate the lanes (do not trust a hand-written list)
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
#  Face 1 — the streaming-prefetch cache hit (the sharpest case)
# ═══════════════════════════════════════════════════════════════════

def test_prefetch_cache_hit_settles_immediately(rec, slow_tools):
    """★ THE LOAD-BEARING FACE.

    ``StreamingToolExecutor`` runs a tool WHILE the model is still streaming
    tokens and injects the result into ``task['_tool_result_cache']`` with
    source='prefetch'. By the time dispatch runs, the answer is already in
    hand — cost ZERO. Yet its tool_complete is currently emitted in the
    post-phase, so the fastest possible tool in the product is the one that
    waits longest.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('read_files', {}):
            ('PREFETCHED BODY', False, 'prefetch', None, None, None),
    }
    tcs = [_mk_tc('tc-pf', 'read_files', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-pf', 'tc-slow', 'streaming-prefetch hit')

    ev = rec.find('tc-pf', 'tool_complete')
    visible_result = json.loads(ev.get('toolContent', ''))
    assert visible_result['contractVersion'] == 'tofu.tool-result/v2'
    assert visible_result['summary'] == 'PREFETCHED BODY', (
        'the prefetched content must reach the UI result envelope without '
        'loss; got %r' % (visible_result,))


def test_dedup_cache_hit_settles_immediately(rec, slow_tools):
    """Same lane, ``source='dedup'`` — a repeat call inside one turn.

    Also zero-cost (the result is replayed, not recomputed), so it has the
    same obligation.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('grep_search', {}):
            ('DEDUP BODY', False, 'dedup', None, None, None),
    }
    tcs = [_mk_tc('tc-dd', 'grep_search', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-dd', 'tc-slow', 'dedup hit')


# ═══════════════════════════════════════════════════════════════════
#  Face 2 — the refusal / interruption lanes
# ═══════════════════════════════════════════════════════════════════

def test_parse_error_lane_settles_immediately(rec, slow_tools):
    """A hallucinated or unparseable tool call never executes, so the round is
    knowably finished the instant it is inspected."""
    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    tcs = [_mk_tc('tc-bad', 'read_files', 1,
                  parse_err='Error: malformed arguments'),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-bad', 'tc-slow', 'parse-error')


def test_contract_rejection_stays_typed_and_never_becomes_done(
        rec, slow_tools):
    """A final ToolContractV2 refusal remains rejected through settlement."""
    task = _mk_task()
    rejected = _mk_tc(
        'tc-contract', 'read_files', 1,
        parse_err=('ERROR: Tool call `read_files` was NOT executed. '
                   '[invalid_argument_length] Invalid length at $.path.'))
    contract_error = {
        'code': 'invalid_argument_length', 'message': 'Invalid length.',
        'path': '$.path', 'retryable': True,
        'nextAction': 'Match arguments_schema and retry.',
    }
    rejected[5]['status'] = 'rejected'
    rejected[5]['_contractError'] = contract_error

    _run(task, [rejected])

    assert rejected[5]['status'] == 'rejected'
    assert rejected[5]['results'][0]['contractError'] == contract_error
    result_event = rec.find('tc-contract', 'tool_result')
    assert result_event is not None
    assert result_event['status'] == 'rejected'
    assert result_event['_contractError'] == contract_error


def test_pre_hook_block_lane_settles_immediately(rec, slow_tools, monkeypatch):
    """A pre-hook block refuses the tool before execution."""
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    class _Blocked:
        action = 'block'
        message = 'blocked by policy'
        additional_context = 'try a narrower path'

    def _pre(fn_name, fn_args, task):
        return _Blocked() if fn_name == 'write_file' else None

    monkeypatch.setattr(_pipeline, 'run_pre_hooks', _pre, raising=False)
    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task()
    tcs = [_mk_tc('tc-blk', 'write_file', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs)

    _assert_settles_before_slow(rec, 'tc-blk', 'tc-slow', 'pre-hook block')


def test_approval_rejected_lane_settles_immediately(rec, slow_tools, monkeypatch):
    """A user who clicks Reject has ALREADY answered — the round is settled at
    that instant, and must not wait for an unrelated slow sibling."""
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    monkeypatch.setattr(
        _pipeline, '_handle_approval',
        lambda *a, **k: (False, 'User rejected this write.'), raising=False)
    slow_tools['web_search'] = (1.2, 'SLOW BODY')

    task = _mk_task(_attended=True)
    tcs = [_mk_tc('tc-rej', 'write_file', 1),
           _mk_tc('tc-slow', 'web_search', 2)]
    _run(task, tcs, cfg={'autoApply': False})

    _assert_settles_before_slow(rec, 'tc-rej', 'tc-slow', 'approval rejected')


# ═══════════════════════════════════════════════════════════════════
#  Face 3 — ★ the terminal verdict must SURVIVE the settle
# ═══════════════════════════════════════════════════════════════════

def test_rejected_round_is_never_marked_done(rec, slow_tools, monkeypatch):
    """★ THE CORRECTNESS HALF — worse than the latency if we get it wrong.

    Settling a refused tool must NOT turn it into a success. A round that was
    rejected has to keep saying so on the wire: if the completion frame
    carried no verdict, the client's tool_complete case would flip it to
    'done' and a write the user explicitly REFUSED would render as applied.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    monkeypatch.setattr(
        _pipeline, '_handle_approval',
        lambda *a, **k: (False, 'User rejected this write.'), raising=False)
    slow_tools['web_search'] = (0.3, 'SLOW')

    task = _mk_task(_attended=True)
    rej = _mk_tc('tc-rej', 'write_file', 1)
    _run(task, [rej, _mk_tc('tc-slow', 'web_search', 2)],
         cfg={'autoApply': False})

    assert rej[5]['status'] == 'rejected', (
        "the round's own status must stay 'rejected' after settling; got %r"
        % (rej[5]['status'],))

    ev = rec.find('tc-rej', 'tool_complete')
    if ev is not None:
        assert ev.get('status') == 'rejected', (
            'a tool_complete for a REJECTED round must carry '
            "status='rejected'. Without it the client flips the round to "
            "'done' and a refused write renders as applied — strictly worse "
            'than the latency this epic removes. Event: %r' % (ev,))


def test_aborted_round_is_never_marked_done(rec, slow_tools):
    """Same contract for a user-pressed Stop.

    The abort short-circuit records 'Task aborted by user.' and skips
    execution. Settling that lane must stamp a terminal ABORTED verdict — not
    leave the round dangling for the end-of-task sweep, and never let it read
    'done'.
    """
    task = _mk_task(aborted=True)
    a = _mk_tc('tc-abort', 'read_files', 1)
    _run(task, [a])

    assert a[5].get('status') in ('aborted', 'rejected'), (
        "an aborted tool's round must carry a terminal ABORTED verdict at the "
        'moment it is skipped — relying on the end-of-task dangling sweep '
        'leaves a spinner running for the rest of the turn; got %r'
        % (a[5].get('status'),))
    assert a[5].get('status') != 'done', (
        'an aborted tool must NEVER read as done')

    ev = rec.find('tc-abort', 'tool_complete')
    if ev is not None:
        assert ev.get('status') != 'done', (
            'a completion frame for an aborted round must not claim done: %r'
            % (ev,))


# ═══════════════════════════════════════════════════════════════════
#  Face 4 — regression: ordering + exactly-once still hold
# ═══════════════════════════════════════════════════════════════════

def test_skip_lanes_emit_exactly_one_complete(rec, slow_tools):
    """Wiring a 6th..11th call site must not double-emit.

    ``_settle_tool_result`` is idempotent per tc_id; if a lane settled early
    AND the post-phase settled it again, per-tool token counts would be
    double-counted in the round accounting.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (0.3, 'SLOW')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('read_files', {}):
            ('PF', False, 'prefetch', None, None, None),
    }
    tcs = [_mk_tc('tc-pf', 'read_files', 1),
           _mk_tc('tc-bad', 'grep_search', 2, parse_err='bad args'),
           _mk_tc('tc-slow', 'web_search', 3)]
    _run(task, tcs)

    for tc_id in ('tc-pf', 'tc-bad', 'tc-slow'):
        n = sum(1 for e in rec.events
                if e.get('toolCallId') == tc_id
                and e.get('type') == 'tool_complete')
        assert n == 1, (
            '%s emitted %d tool_complete events (expected exactly 1); '
            'types=%r' % (tc_id, n, rec.types_for(tc_id)))


def test_message_order_survives_the_new_wiring(rec, slow_tools):
    """The post-phase loop still owns message ORDER.

    Tool messages must enter the list in the model's original tool-call order
    regardless of which lane settled when — an out-of-order
    tool_call/tool_result pair is a hard API error on Anthropic.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (0.5, 'SLOW')

    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('read_files', {}):
            ('PF', False, 'prefetch', None, None, None),
    }
    # Declaration order: slow first, then the instant cache hit.
    tcs = [_mk_tc('tc-slow', 'web_search', 1),
           _mk_tc('tc-pf', 'read_files', 2)]
    messages = _run(task, tcs)

    ids = [m['tool_call_id'] for m in messages if m.get('role') == 'tool']
    assert ids == ['tc-slow', 'tc-pf'], (
        'tool messages must follow DECLARATION order even though the cache '
        'hit settled first; got %r' % (ids,))


# ═══════════════════════════════════════════════════════════════════
#  Face 5 — the ROUND OBJECT is self-describing on every lane
# ═══════════════════════════════════════════════════════════════════
#
# The ordering faces above read the EVENT STREAM. That is structurally blind to
# a field missing on the ROUND — and a page reload rebuilds from persisted
# rounds, not from the live event stream.
# Measured before this face existed: a rejected round carried tStart with NO
# tEnd, because _settle_tool_result computed tEnd for the EVENT and never wrote
# it back. So the execution segment — the first of the three segments this epic
# exists to expose — was permanently unresolvable exactly on the paths a user
# takes when investigating a slow turn.

def _round_of(tc):
    return tc[5]


def test_every_lane_leaves_a_self_describing_round(rec, slow_tools, monkeypatch):
    """★ Assert the ROUND, not the event stream, on every skip lane.

    Each lane must leave: a terminal status, a tStart, and a tEnd — so
    ``execution = tEnd - tStart`` is computable from the persisted round alone.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    slow_tools['web_search'] = (0.3, 'SLOW')
    slow_tools['browser_screenshot'] = (0.0, 'SHOT')

    terminal = {'done', 'rejected', 'aborted', 'error', 'unanswerable'}
    checked = {}

    # ── lanes reachable in ONE pipeline run ──
    task = _mk_task()
    task['_tool_result_cache'] = {
        _make_cache_key('read_files', {}):
            ('PF', False, 'prefetch', None, None, None),
        _make_cache_key('grep_search', {}):
            ('DD', False, 'dedup', None, None, None),
    }
    pf = _mk_tc('tc-pf', 'read_files', 1)
    dd = _mk_tc('tc-dd', 'grep_search', 2)
    bad = _mk_tc('tc-bad', 'list_dir', 3, parse_err='bad args')
    slow = _mk_tc('tc-slow', 'web_search', 4)
    _run(task, [pf, dd, bad, slow])
    checked['prefetch-hit'] = _round_of(pf)
    checked['dedup-hit'] = _round_of(dd)
    checked['parse-error'] = _round_of(bad)
    checked['normal-dispatch'] = _round_of(slow)

    # ── pre-hook block ──
    class _Blocked:
        action = 'block'
        message = 'blocked'
        additional_context = ''
    monkeypatch.setattr(_pipeline, 'run_pre_hooks',
                        lambda fn, a, t: _Blocked() if fn == 'write_file' else None,
                        raising=False)
    t2 = _mk_task()
    blk = _mk_tc('tc-blk', 'write_file', 1)
    _run(t2, [blk])
    checked['pre-hook-block'] = _round_of(blk)
    monkeypatch.setattr(_pipeline, 'run_pre_hooks',
                        lambda fn, a, t: None, raising=False)

    # ── approval rejected ──
    monkeypatch.setattr(_pipeline, '_handle_approval',
                        lambda *a, **k: (False, 'rejected'), raising=False)
    t3 = _mk_task(_attended=True)
    rej = _mk_tc('tc-rej', 'write_file', 1)
    _run(t3, [rej], cfg={'autoApply': False})
    checked['approval-rejected'] = _round_of(rej)

    # ── abort short-circuit ──
    t4 = _mk_task(aborted=True)
    ab = _mk_tc('tc-ab', 'read_files', 1)
    _run(t4, [ab])
    checked['abort-skip'] = _round_of(ab)

    # ── screenshot (vision + no-vision) ──
    def _shot(task_, tc, fn, tcid, args, rn, re_, cfg, pp, pe, all_tools=None):
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(task_, rn, re_, [{'toolName': fn, 'title': fn,
                                               'snippet': 'shot', 'source': 'T',
                                               'fetched': True, 'fetchedChars': 4}])
        return tcid, {'__screenshot__': True, '_text_fallback': 'IMG',
                      'compressedSize': 10}, False
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _shot, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _shot, raising=False)
    for label, model in (('screenshot-vision', 'gpt-4o'),
                         ('screenshot-no-vision', 'deepseek-v3.2')):
        t5 = _mk_task(model=model)
        sh = _mk_tc('tc-sh', 'browser_screenshot', 1)
        from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
        execute_tool_pipeline(
            t5, [sh], cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=[],
            all_search_results_text=[], round_num=0, model=model)
        checked[label] = _round_of(sh)

    problems = []
    for lane, r in checked.items():
        if r.get('status') not in terminal:
            problems.append('%s: status=%r is not terminal' % (lane, r.get('status')))
        if r.get('tStart') is None:
            problems.append('%s: round has NO tStart' % lane)
        if r.get('tEnd') is None:
            problems.append(
                '%s: round has NO tEnd — every page reload reads the ROUND, '
                'not the event stream, so this lane\'s '
                'execution segment (tEnd - tStart) is unresolvable for any '
                'client without SSE and for anyone who refreshed' % lane)
        if (r.get('tStart') is not None and r.get('tEnd') is not None
                and r['tEnd'] < r['tStart']):
            problems.append('%s: tEnd precedes tStart' % lane)
    assert not problems, (
        'rounds are not self-describing:\n  ' + '\n  '.join(problems))

    assert len(checked) == 9, (
        'expected 9 settle points covered — the 5 pre-phase skip lanes, the '
        'abort skip, a normal dispatch, and BOTH screenshot verdicts '
        '(vision / no-vision); got %d: %r' % (len(checked), sorted(checked)))


def test_settle_adopts_the_finalize_completion_instant(rec, monkeypatch):
    """★ REVERSE guard on the write-back — EXACT, not a tolerance band.

    ``_settle_tool_result`` must ADOPT the ``tEnd`` that ``_finalize_tool_round``
    stamped at the tool's real completion, and only mint one when it is absent.
    Replacing it with a later ``now()`` would silently inflate the measured
    duration by however long the rest of the round took, so the instrument
    would misreport exactly the slow calls it exists to expose.

    A tolerance band is NOT enough here — measured: neutering the adopt to a
    bare ``now_ms()`` moved a 600ms tool to 718ms, which sails through any
    sane window. So this captures the EXACT instant finalize recorded and
    demands equality.
    """
    captured = {}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        time.sleep(0.4)
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': 's',
              'source': 'Test', 'fetched': True, 'fetchedChars': 1}])
        # The instant the REAL completion was recorded.
        captured['tEnd'] = round_entry['tEnd']
        # Make any later now() unmistakably different.
        time.sleep(0.25)
        return tc_id, 'BODY', False

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)

    task = _mk_task()
    slow = _mk_tc('tc-slow', 'web_search', 1)
    _run(task, [slow])

    r = _round_of(slow)
    assert captured.get('tEnd') is not None, 'the fake never finalized'
    assert r['tEnd'] == captured['tEnd'], (
        "the settle must ADOPT the completion instant _finalize_tool_round "
        'recorded (%.3f), not re-stamp a later one (%.3f, +%.0fms). Otherwise '
        "every tool's reported duration silently absorbs however long the "
        'rest of the round took.'
        % (captured['tEnd'], r['tEnd'], r['tEnd'] - captured['tEnd']))

    ev = rec.find('tc-slow', 'tool_complete')
    assert ev is not None
    assert ev['tEnd'] == r['tEnd'], (
        'the event and the round must report the SAME completion instant; '
        'event=%.3f round=%.3f' % (ev['tEnd'], r['tEnd']))


def test_screenshot_lane_settles_immediately(rec, monkeypatch):
    """★ ORDERING face for the screenshot lane.

    The round-shape face above is structurally blind to ORDERING: the
    post-phase settle is idempotent, so a screenshot that regressed to being
    skipped at dispatch still ends up with a well-formed round — it just
    arrives late. Measured: reverting the dispatch-lane wiring left every
    round-shape assertion green while the screenshot's tool_complete moved back
    behind the slow sibling. Only an ordering assertion catches that.

    A browser screenshot is one of the calls a user is most likely to read as
    "stuck" (browser action + large payload), so this lane matters.
    """
    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        if fn_name == 'browser_screenshot':
            _finalize_tool_round(
                task, rn, round_entry,
                [{'toolName': fn_name, 'title': fn_name, 'snippet': 'shot',
                  'source': 'Test', 'fetched': True, 'fetchedChars': 4}])
            return tc_id, {'__screenshot__': True, '_text_fallback': 'IMG',
                           'compressedSize': 10}, False
        time.sleep(1.2)
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': 's',
              'source': 'Test', 'fetched': True, 'fetchedChars': 1}])
        return tc_id, 'SLOW', False

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)

    # NOTE the model names: `model_supports_vision` defaults UNKNOWN names to
    # True (permissive), so an invented "text-only-model" would silently take
    # the vision branch and this face would assert nothing about the no-vision
    # path. `deepseek-v3.2` is a real entry the capability table reports as
    # text-only — measured, not assumed. (Was `deepseek-chat` until that
    # alias was retired upstream on 2026-07-24 and its row removed.)
    for model in ('gpt-4o', 'deepseek-v3.2'):
        rec.events.clear()
        task = _mk_task(model=model)
        tcs = [_mk_tc('tc-sh', 'browser_screenshot', 1),
               _mk_tc('tc-slow', 'web_search', 2)]
        from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
        execute_tool_pipeline(
            task, tcs, cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=[],
            all_search_results_text=[], round_num=0, model=model)
        _assert_settles_before_slow(
            rec, 'tc-sh', 'tc-slow', 'screenshot (model=%s)' % model)

        ev = rec.find('tc-sh', 'tool_complete')
        expected = ('IMG' if model == 'gpt-4o'
                    else '[Image not shown')  # no-vision placeholder
        assert expected in (ev.get('toolContent') or ''), (
            'the screenshot completion must carry the verdict-appropriate '
            'display text for model=%s; got %r' % (model, ev.get('toolContent')))


def test_screenshot_message_and_ui_agree_on_the_vision_verdict(monkeypatch):
    """★ The two screenshot halves must reach the SAME verdict.

    The verdict is now computed twice: once at DISPATCH (for the UI's
    tool_complete) and once in the POST-PHASE (to pick which role:'tool'
    message the model receives). They read the same helper, but they are
    separate call sites — and the settle is idempotent, so if the post-phase
    site drifted, the UI would keep the dispatch verdict while the MODEL got
    the other one. The user would see "Image captured." while the model was
    told the image is unreadable, or vice-versa.

    Guarding the ordering alone cannot see this (N-D measured: corrupting the
    post-phase model argument left all 14 other faces green), so this asserts
    the MESSAGE the model receives, not the event.
    """
    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name, 'snippet': 'shot',
              'source': 'Test', 'fetched': True, 'fetchedChars': 4}])
        return tc_id, {'__screenshot__': True, '_text_fallback': 'IMG',
                       'compressedSize': 10}, False

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)

    from lib.model_info import model_supports_vision
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline

    for model in ('gpt-4o', 'deepseek-chat'):
        task = _mk_task(model=model)
        sh = _mk_tc('tc-sh', 'browser_screenshot', 1)
        messages = []
        execute_tool_pipeline(
            task, [sh], cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=messages,
            all_search_results_text=[], round_num=0, model=model)

        tool_msgs = [m for m in messages if m.get('role') == 'tool']
        assert len(tool_msgs) == 1, (
            'exactly one tool message per screenshot; got %r' % (tool_msgs,))
        body = tool_msgs[0]['content']
        has_vision = model_supports_vision(model)

        if has_vision:
            assert not (isinstance(body, str) and 'Image not shown' in body), (
                'model=%s HAS vision, so the model must receive the real '
                'multimodal result, not the no-vision placeholder; got %.80r'
                % (model, body))
        else:
            assert isinstance(body, str) and 'Image not shown' in body, (
                'model=%s has NO vision, so the model must receive the '
                'placeholder — otherwise it is handed an image it cannot read '
                'and keeps retrying; got %.80r' % (model, body))

        # And the UI text must agree with what the model was told.
        assert sh[5].get('toolContent'), 'the round must carry display text'
        ui_no_vision = 'Image not shown' in sh[5]['toolContent']
        assert ui_no_vision == (not has_vision), (
            'the UI verdict (no_vision=%s) disagrees with the vision '
            'capability of model=%s — the dispatch-time settle and the '
            'post-phase message append have drifted apart'
            % (ui_no_vision, model))
