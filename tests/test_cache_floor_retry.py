#!/usr/bin/env python3
"""Floor-collapse identical-resend mitigation — production wiring guard.

WHY (real-gateway evidence, this effort):
  A fraction of rounds report ``cache_read`` pinned at the system+tools FLOOR
  (whole body re-billed) even though the wire bytes are byte-IDENTICAL to a
  cached request and the block geometry is in-window. Four identical-byte
  replays collapse DIFFERENT rounds at 13-40% → SERVER-SIDE stochastic
  cache-write-visibility (SDK #1451), not a client layout bug. Resending the
  IDENTICAL body re-rolls the dice and recovers (harness: mrsfs9d6 20%->0%).

THE FIX under test (``lib/tasks_pkg/floor_retry.py`` + the resend loop in
``lib/tasks_pkg/manager/_stream.py::stream_llm_response``):
  On a byte-STABLE floor-collapse, when ``TOFU_CACHE_FLOOR_RETRY`` is enabled,
  resend the identical body up to N times; adopt the first recovered response.

Failing-first / NEUTER discipline:
  * ``test_resend_fires_and_recovers_when_enabled`` is RED without the resend
    loop (dispatch called once, floored usage kept) and GREEN with it.
  * ``test_no_resend_when_disabled`` is the NEUTER control — the env gate off
    means exactly one dispatch, proving the loop is gated (not always-on).
  * ``test_no_resend_when_prefix_changed`` proves we never resend on a body the
    client actually mutated (would be a wasted call / could mask a real break).
  * ``test_resend_stops_on_throttle`` proves a 503/throttle on the resend stops
    the loop (don't pile retries on an already-throttled gateway).

Run directly (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_floor_retry.py
"""
from __future__ import annotations

import os
import sys
import threading as _thr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit
OWNER = 1


_FLOOR_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 28654,
                'cache_creation_input_tokens': 42000, '_wire_fp': [{'k': 'a'}]}
_HIT_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 150000,
              'cache_creation_input_tokens': 1200, '_wire_fp': [{'k': 'a'}]}


def _seed_wire_fp(conv_id, fp):
    """Seed the cache-tracking state's PREVIOUS-round wire fingerprint so
    wire_prefix_stable(conv_id, usage) can compare against it."""
    from lib.tasks_pkg.cache_tracking._state import (
        _cache_lock,
        _cache_states,
    )
    from lib.tasks_pkg.cache_tracking._state import CacheState, _state_key
    key = _state_key(conv_id, user_id=OWNER)
    with _cache_lock:
        st = _cache_states.get(key)
        if st is None:
            st = CacheState()
            _cache_states[key] = st
        st.wire_fp = list(fp)


def _task(conv_id='cfr1'):
    return {'id': 'task-fr-1', 'convId': conv_id, 'content': '',
            '_userId': OWNER,
            'thinking': '', 'config': {}, 'events': [],
            'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}


def _body():
    return {'model': 'aws.claude-opus-4.8',
            'messages': [{'role': 'system', 'content': 'S'},
                         {'role': 'user', 'content': 'go'}]}


# ── Pure predicate coverage ─────────────────────────────────────────────────

def test_is_floor_collapse_predicate():
    from lib.tasks_pkg import floor_retry as fr
    assert fr.is_floor_collapse(_FLOOR_USAGE) is True
    assert fr.is_floor_collapse(_HIT_USAGE) is False
    assert fr.is_floor_collapse({}) is False
    # big read + small write = healthy
    assert fr.is_floor_collapse(
        {'cache_read_tokens': 200000, 'cache_creation_input_tokens': 500}) is False


def test_floor_retry_max_clamped_and_gate(monkeypatch):
    from lib.tasks_pkg import floor_retry as fr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '0')
    assert fr.floor_retry_enabled() is False
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', 'on')
    assert fr.floor_retry_enabled() is True
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '99')
    assert fr.floor_retry_max() == 3   # hard-capped
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '-5')
    assert fr.floor_retry_max() == 0


def test_default_gate_is_OFF_2026_07_23(monkeypatch):
    """★ Failing-first guard for the 2026-07-23 REVERSAL: with NO env var set,
    the mitigation must be OFF. Reason: the "0.0% floor" acceptance metric was
    REPORT-KIND (adopted-usage-only) — the discarded first attempt was billed by
    the gateway but never counted into apiRounds/accumulated_usage/compute_cost,
    so the "cost minimisation" claim was manufactured by hiding the second
    request's cost. With honest accounting now in place (extra_billing_rounds
    propagated), a resend's expected marginal cost is strictly positive with no
    proved TCO reduction, so default-on violates the North-Star (cost
    minimisation). Set TOFU_CACHE_FLOOR_RETRY=1 to opt in for future controlled
    A/B tests.
    NEUTER: revert the module default to '1' and this test goes red."""
    from lib.tasks_pkg import floor_retry as fr
    monkeypatch.delenv('TOFU_CACHE_FLOOR_RETRY', raising=False)
    monkeypatch.delenv('TOFU_CACHE_FLOOR_RETRY_MAX', raising=False)
    assert fr.floor_retry_enabled() is False, (
        'floor-retry must be OFF by default — resend is a per-request cost '
        'loss until a real TCO A/B proves otherwise')
    assert fr.floor_retry_max() == 2, (
        'when opted-in, resend cap must be 2 to cover a 503-on-first-resend round')


def test_wire_prefix_stable_true_when_prefix_matches():
    from lib.tasks_pkg import floor_retry as fr
    _seed_wire_fp('cfr-stable', [{'k': 'a'}])
    assert fr.wire_prefix_stable(
        'cfr-stable', {'_wire_fp': [{'k': 'a'}, {'k': 'b'}]},
        user_id=OWNER) is True


def test_wire_prefix_stable_false_when_prefix_changed():
    from lib.tasks_pkg import floor_retry as fr
    _seed_wire_fp('cfr-changed', [{'k': 'a'}])
    assert fr.wire_prefix_stable(
        'cfr-changed', {'_wire_fp': [{'k': 'X'}, {'k': 'b'}]},
        user_id=OWNER) is False


# ── Integration: the resend loop in stream_llm_response ─────────────────────

def _run_stream(monkeypatch, *, enabled, dispatch_seq, conv_id='cfr-int',
                seed_fp=(({'k': 'a'}),)):
    """Drive stream_llm_response with a scripted dispatch sequence. Returns
    (call_count, final_usage)."""
    import lib.tasks_pkg.manager._stream as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1' if enabled else '0')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    _seed_wire_fp(conv_id, list(seed_fp))
    calls = {'n': 0}
    seq = list(dispatch_seq)

    def _fake_dispatch(body, **kwargs):
        i = calls['n']
        calls['n'] += 1
        item = seq[min(i, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', dict(item))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        _msg, _fin, usage = _mgr.stream_llm_response(_task(conv_id), _body(), tag='FR')
    finally:
        _mgr.dispatch_stream = _orig
    return calls['n'], usage


def test_resend_fires_and_recovers_when_enabled(monkeypatch):
    """RED without the resend loop (only 1 dispatch, floored usage kept);
    GREEN with it (2 dispatches, recovered usage adopted)."""
    n, usage = _run_stream(monkeypatch, enabled=True,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE])
    assert n == 2, f'a byte-stable floor-collapse must trigger ONE resend; dispatches={n}'
    assert usage.get('cache_read_tokens') == 150000, (
        'the recovered resend usage must be adopted (cache hit), not the floored one')


def test_no_resend_when_disabled(monkeypatch):
    """NEUTER control: env gate OFF → exactly one dispatch, floored usage kept."""
    n, usage = _run_stream(monkeypatch, enabled=False,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE])
    assert n == 1, f'gate OFF must not resend; dispatches={n}'
    assert usage.get('cache_read_tokens') == 28654


def test_no_resend_when_prefix_changed(monkeypatch):
    """A floor-collapse whose wire prefix DIFFERS from the previous round is
    NOT the server-stochastic class — never resend (would be wasted)."""
    # Seed a DIFFERENT previous fingerprint so wire_prefix_stable is False.
    n, usage = _run_stream(monkeypatch, enabled=True,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE],
                           conv_id='cfr-diff', seed_fp=({'k': 'DIFFERENT'},))
    assert n == 1, f'must not resend on a client-changed prefix; dispatches={n}'


def test_resend_stops_on_throttle(monkeypatch):
    """A throttle/503 on the resend stops the loop (don't pile retries on an
    already-throttled gateway) — exactly one resend attempt, no third call."""
    from lib.llm_errors import RateLimitError
    n, usage = _run_stream(
        monkeypatch, enabled=True,
        dispatch_seq=[_FLOOR_USAGE, RateLimitError('429 throttled'), _HIT_USAGE])
    assert n == 2, f'must attempt one resend then STOP on throttle; dispatches={n}'
    # original floored usage retained (resend raised before returning usage)
    assert usage.get('cache_read_tokens') == 28654


def test_floorretry_first_attempt_orphan_reconciled(monkeypatch):
    """Layer-3: reconcile sits ABOVE the FloorRetry loop, so a first-attempt
    announced round that is superseded when a RECOVERED resend response is
    adopted gets settled (not left spinning).

    Scenario through the real stream_llm_response + accumulator wiring:
      * primary dispatch floors AND announces an early tool round (tc_A) via the
        real on_tool_call_ready callback, returns a floored msg carrying tc_A;
      * the FloorRetry resend (Layer-1: on_tool_call_ready=None) recovers and its
        response carries a DIFFERENT tc_id (tc_B) — adopted as the final msg;
      * so tc_A is announced-but-orphaned (not in the final msg) and tc_B is in
        the final msg but was never announced.
    The orchestrator's reconcile_announced_rounds(final_msg) — the method the
    accumulator exposes and _run.py calls after _llm_call_with_fallback — must
    settle tc_A to a terminal state (spinner flipped) while tc_B is left for the
    normal dispatch pipeline. This proves the orphan-cleanup is at the RIGHT
    layer (above FloorRetry). NEUTER: skip the reconcile call and tc_A stays
    'searching' (the pre-fix spinning-forever state)."""
    import threading as _thr2
    import lib.tasks_pkg.manager._stream as _mgr
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    conv_id = 'cfr-l3'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    task = {'id': 'task-fr-l3', 'convId': conv_id, '_userId': OWNER,
            'content': '', 'thinking': '',
            'config': {}, 'events': [], 'toolRounds': [],
            'content_lock': _thr2.Lock(), 'events_lock': _thr2.Lock()}
    acc = StreamingToolAccumulator(task, project_path='/tmp',
                                   round_num=0, project_enabled=True)

    calls = {'n': 0}

    def _fake_dispatch(body, **kwargs):
        i = calls['n']
        calls['n'] += 1
        cb = kwargs.get('on_tool_call_ready')
        if i == 0:
            # Primary attempt: announce tc_A (early tool_start), floor-collapse.
            if cb:
                cb({'id': 'tc_A', 'type': 'function',
                    'function': {'name': 'read_files', 'arguments': '{}'}})
            return ({'role': 'assistant',
                     'tool_calls': [{'id': 'tc_A', 'type': 'function',
                                     'function': {'name': 'read_files', 'arguments': '{}'}}]},
                    'tool_calls', dict(_FLOOR_USAGE))
        # Resend (Layer-1: cb is None) — recovers, DIFFERENT tc_id tc_B.
        assert cb is None, 'Layer-1 broken: resend still carries the tool callback'
        return ({'role': 'assistant',
                 'tool_calls': [{'id': 'tc_B', 'type': 'function',
                                 'function': {'name': 'read_files', 'arguments': '{}'}}]},
                'tool_calls', dict(_HIT_USAGE))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        msg, _fin, _usage = _mgr.stream_llm_response(
            task, _body(), tag='R1', on_tool_call_ready=acc.on_tool_call_ready)
    finally:
        _mgr.dispatch_stream = _orig

    # Adopted msg = recovered resend (tc_B). tc_A announced, tc_B not announced.
    assert [tc['id'] for tc in msg['tool_calls']] == ['tc_B']
    announced = acc.announced_tc_map
    assert 'tc_A' in announced, 'primary attempt must have announced tc_A'
    assert 'tc_B' not in announced, 'Layer-1: resend must not have announced tc_B'
    # tc_A is spinning ('searching') until reconcile runs.
    assert announced['tc_A'][1]['status'] == 'searching'

    # The orchestrator layer's reconcile (above FloorRetry) settles the orphan.
    n = acc.reconcile_announced_rounds(msg)
    assert n == 1, f'reconcile must settle the FloorRetry first-attempt orphan; settled={n}'
    assert acc.announced_tc_map['tc_A'][1]['status'] == 'aborted'
    # And it carries NO toolContent, so Layer-2 drops it from model history.
    assert acc.announced_tc_map['tc_A'][1].get('toolContent') is None


def test_stream_stamps_floor_retry_adopted_marker(monkeypatch):
    """The TRUE-CAUSE marker: stream_llm_response must set
    task['_floor_retry_adopted']=True when it adopts a recovered resend, and
    reset it to False on a round that did NOT adopt (so a later non-adopting
    round can't read a stale True). This marker is what lets
    reconcile_announced_rounds attribute an orphan to FloorRetry vs a stream
    retry — the mis-attribution that made this symptom un-traceable for
    sessions (app.log proved stream retries=0 while FloorRetry drove 100%)."""
    import lib.tasks_pkg.manager._stream as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')

    # Case A: floor then recover → adopted True.
    conv_a = 'cfr-mark-a'
    _seed_wire_fp(conv_a, [{'k': 'a'}])
    seq_a = [_FLOOR_USAGE, _HIT_USAGE]
    calls_a = {'n': 0}

    def _disp_a(body, **kwargs):
        i = calls_a['n']; calls_a['n'] += 1
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', dict(seq_a[min(i, len(seq_a) - 1)]))

    task_a = _task(conv_a)
    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _disp_a
    try:
        _mgr.stream_llm_response(task_a, _body(), tag='FR')
    finally:
        _mgr.dispatch_stream = _orig
    assert task_a.get('_floor_retry_adopted') is True, (
        'adopting a recovered resend must stamp the true-cause marker')

    # Case B: a healthy round (no floor collapse) → marker reset to False.
    conv_b = 'cfr-mark-b'
    _seed_wire_fp(conv_b, [{'k': 'a'}])
    calls_b = {'n': 0}

    def _disp_b(body, **kwargs):
        calls_b['n'] += 1
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', dict(_HIT_USAGE))

    task_b = _task(conv_b)
    task_b['_floor_retry_adopted'] = True   # pretend a prior round left it True
    _mgr.dispatch_stream = _disp_b
    try:
        _mgr.stream_llm_response(task_b, _body(), tag='FR')
    finally:
        _mgr.dispatch_stream = _orig
    assert calls_b['n'] == 1, 'healthy round must not resend'
    assert task_b.get('_floor_retry_adopted') is False, (
        'a non-adopting round must RESET the marker (no stale True)')


def test_reconcile_logs_true_cause_floor_retry_vs_stream_retry(monkeypatch):
    """reconcile_announced_rounds must attribute the orphan to its TRUE cause,
    driven by task['_floor_retry_adopted']:
      * marker True  → audit cause='floor_retry_adoption' + FloorRetry snippet
      * marker absent → audit cause='stream_retry' (legacy default)
    This replaces the hardcoded — and, per app.log, FALSE — 'discarded
    stream-retry attempt' story that was emitted unconditionally.
    NEUTER-adjacent: the two branches assert DIFFERENT causes for the SAME
    orphan shape, so a regression that hardcodes one cause fails one branch."""
    import threading as _thr2
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    import lib.tasks_pkg.streaming_tool_executor as _ste

    captured = []

    def _fake_audit(event, **kw):
        captured.append((event, kw))

    monkeypatch.setattr(_ste, 'audit_log', _fake_audit, raising=False)
    # audit_log is imported lazily inside reconcile via `from lib.log import
    # audit_log`, so patch it on lib.log too.
    import lib.log as _log
    monkeypatch.setattr(_log, 'audit_log', _fake_audit, raising=False)

    def _mk_acc(marker):
        task = {'id': 'task-cause', 'convId': 'cfr-cause', 'content': '',
                'thinking': '', 'config': {}, 'events': [], 'toolRounds': [],
                'content_lock': _thr2.Lock(), 'events_lock': _thr2.Lock()}
        if marker is not None:
            task['_floor_retry_adopted'] = marker
        acc = StreamingToolAccumulator(task, project_path='/tmp',
                                       round_num=0, project_enabled=True)
        # Announce tc_A (an orphan — final msg carries a DIFFERENT id).
        acc.on_tool_call_ready({'id': 'tc_A', 'type': 'function',
                                'function': {'name': 'read_files', 'arguments': '{}'}})
        final_msg = {'role': 'assistant', 'tool_calls': [
            {'id': 'tc_B', 'type': 'function',
             'function': {'name': 'read_files', 'arguments': '{}'}}]}
        return acc, final_msg

    # Branch 1: FloorRetry adoption marker set.
    captured.clear()
    acc1, msg1 = _mk_acc(True)
    n1 = acc1.reconcile_announced_rounds(msg1)
    assert n1 == 1
    ev1 = [kw for (name, kw) in captured if name == 'tool_round_superseded']
    assert ev1 and ev1[0].get('cause') == 'floor_retry_adoption', (
        f'marker True must audit floor_retry_adoption; got {ev1}')
    # The husk snippet reflects the resend-adoption cause, not stream reconnect.
    husk1 = acc1.announced_tc_map['tc_A'][1]['results'][0]
    assert 'resend' in husk1['snippet'].lower(), (
        f'FloorRetry husk snippet must mention the resend; got {husk1["snippet"]!r}')

    # Branch 2: no marker → legacy stream-retry attribution.
    captured.clear()
    acc2, msg2 = _mk_acc(None)
    n2 = acc2.reconcile_announced_rounds(msg2)
    assert n2 == 1
    ev2 = [kw for (name, kw) in captured if name == 'tool_round_superseded']
    assert ev2 and ev2[0].get('cause') == 'stream_retry', (
        f'no marker must audit stream_retry; got {ev2}')


def test_resend_does_not_reuse_tool_callback(monkeypatch):
    """Layer-1 orphan fix: the FIRST dispatch carries the orchestrator's
    on_tool_call_ready (early tool_start announcements), but every FloorRetry
    RESEND must pass on_tool_call_ready=None — a discarded resend that announces
    a fresh 'searching' round leaves a result-less orphan (swept to aborted).

    RED before the fix (resend reused the callback → captured callback is the
    same non-None object on call #2); GREEN after (call #2's callback is None).
    NEUTER: restore on_tool_call_ready=on_tool_call_ready on the resend and
    this assertion flips red."""
    import lib.tasks_pkg.manager._stream as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    conv_id = 'cfr-cb'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    seq = [_FLOOR_USAGE, _HIT_USAGE]
    captured_cbs = []
    calls = {'n': 0}

    def _fake_dispatch(body, **kwargs):
        captured_cbs.append(kwargs.get('on_tool_call_ready'))
        i = calls['n']
        calls['n'] += 1
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', dict(seq[min(i, len(seq) - 1)]))

    _sentinel = lambda tc: None  # noqa: E731 — a distinct non-None callback
    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        _mgr.stream_llm_response(_task(conv_id), _body(), tag='FR',
                                 on_tool_call_ready=_sentinel)
    finally:
        _mgr.dispatch_stream = _orig

    assert len(captured_cbs) == 2, f'expected 1 resend; dispatches={len(captured_cbs)}'
    assert captured_cbs[0] is _sentinel, 'first dispatch must carry the real tool callback'
    assert captured_cbs[1] is None, (
        'FloorRetry resend must pass on_tool_call_ready=None so a discarded '
        'attempt never announces an orphan tool round')


# ── HONEST ACCOUNTING (2026-07-23 flip): discarded resends must be BILLED ──

def test_recovered_resend_preserves_discarded_first_attempt_billing(monkeypatch):
    """When a resend RECOVERS, the ADOPTED usage supersedes the primary. But
    the gateway BILLED the primary (a full cache_creation) too — the returned
    usage MUST carry it under `_extra_billing_rounds` so downstream cost
    accounting can add it into api_rounds and the wallet debit.

    RED before the honest-accounting fix (the primary's usage was dropped);
    GREEN with _fr_discarded_billing preservation.
    NEUTER: skip the primary-preservation `_fr_discarded_billing.append({...})`
    inside the loop and this test flips red."""
    n, usage = _run_stream(monkeypatch, enabled=True,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE])
    assert n == 2
    assert usage.get('cache_read_tokens') == 150000, 'adopted usage = recovered'
    billed = usage.get('_extra_billing_rounds') or []
    assert len(billed) == 1, (
        f'discarded primary MUST be preserved as _extra_billing_rounds; got {billed}')
    disc = billed[0]
    assert disc['usage'].get('cache_creation_input_tokens') == 42000, (
        f'discarded entry must carry the floored PRIMARY usage; got {disc}')
    assert 'FLOOR-DISCARDED-primary' in disc['tag'], f'tag={disc["tag"]!r}'


def test_exhausted_resends_preserve_every_billed_attempt(monkeypatch):
    """When ALL resends stay floored (max=2), the gateway billed 3 times:
    primary + 2 resends. The ADOPTED usage is the LAST resend's, and BOTH
    prior attempts must appear in `_extra_billing_rounds`. Anything less is
    silent under-billing.
    NEUTER: revert the loop-body preservation and only 0-1 items appear."""
    # Three consecutive floors: primary, resend1, resend2 (final adopted).
    _FLOOR_2 = dict(_FLOOR_USAGE, cache_creation_input_tokens=41000)
    _FLOOR_3 = dict(_FLOOR_USAGE, cache_creation_input_tokens=40500)
    n, usage = _run_stream(monkeypatch, enabled=True,
                           dispatch_seq=[_FLOOR_USAGE, _FLOOR_2, _FLOOR_3])
    assert n == 3, f'primary + 2 resends when nothing recovers; dispatches={n}'
    assert usage.get('cache_creation_input_tokens') == 40500, (
        'adopted usage is the last resend when the loop exhausts')
    billed = usage.get('_extra_billing_rounds') or []
    assert len(billed) == 2, (
        f'BOTH the primary AND the mid resend were gateway-billed and must '
        f'appear as discarded rounds; got {billed}')
    tags = [b['tag'] for b in billed]
    assert any('primary' in t for t in tags), f'missing primary in {tags}'
    assert any('resend1' in t for t in tags), f'missing resend1 in {tags}'


def test_unusable_resend_is_billed_but_never_adopted(monkeypatch):
    """A cache hit cannot launder a malformed provider stream into success."""
    from lib.llm.stream_result import (
        ProviderStreamResult,
        ProviderStreamState,
    )
    import lib.tasks_pkg.manager._stream as manager_stream

    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    conv_id = 'cfr-unusable-resend'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    calls = {'n': 0}

    def _fake_dispatch(body, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            if kwargs.get('on_content'):
                kwargs['on_content']('verified primary')
            return ({'role': 'assistant', 'content': 'verified primary'},
                    'stop', dict(_FLOOR_USAGE))
        return ProviderStreamResult(
            message={'role': 'assistant', 'content': 'corrupt resend'},
            compatibility_finish_reason='stop',
            usage=dict(_HIT_USAGE, _stream_state='malformed_stream',
                       _malformed_stream=True, _malformed_frames=1),
            state=ProviderStreamState.MALFORMED_STREAM,
            malformed_frame_count=1,
        )

    monkeypatch.setattr(manager_stream, 'dispatch_stream', _fake_dispatch)
    task = _task(conv_id)
    result = manager_stream.stream_llm_response(task, _body(), tag='FR')
    message, _finish, usage = result

    assert calls['n'] == 2
    assert result.is_verified_complete is True
    assert message['content'] == 'verified primary'
    assert task['content'] == 'verified primary'
    billed = usage.get('_extra_billing_rounds') or []
    assert len(billed) == 1
    assert billed[0]['usage']['cache_read_tokens'] == 150000
    assert 'resend1' in billed[0]['tag']


def test_llm_call_with_fallback_bills_discarded_into_api_rounds(monkeypatch):
    """End-to-end: _llm_call_with_fallback must consume `_extra_billing_rounds`
    from the returned usage and append each entry as its own api_rounds row +
    accumulate its numeric tokens. Without this, cost popover / accumulated
    /wallet debit only sees the ADOPTED usage — the reported "cost
    minimisation" is manufactured by hiding the second bill.

    Failing-first: without the consumer loop in _call.py, api_rounds has ONE
    entry (the adopted usage). With it, api_rounds has 1 primary + N discarded.
    NEUTER: remove the `for _bill in _extra` loop and only 1 row appears."""
    import lib.tasks_pkg.manager._stream as _mgr
    import lib.tasks_pkg.llm_fallback._call as _fb
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    conv_id = 'cfr-bill-e2e'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    seq = [_FLOOR_USAGE, _HIT_USAGE]
    calls = {'n': 0}

    def _fake_dispatch(body, **kwargs):
        i = calls['n']; calls['n'] += 1
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', dict(seq[min(i, len(seq) - 1)]))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        task = _task(conv_id)
        api_rounds = []
        accumulated = {}
        # _llm_call_with_fallback signature — see lib/tasks_pkg/llm_fallback/_call.py
        result = _fb._llm_call_with_fallback(
            task, _body(), 'aws.claude-opus-4.8', 0, 4096,
            tool_call_happened=False, tool_list=None,
            messages=_body()['messages'], preset='opus',
            thinking_enabled=False,
            accumulated_usage=accumulated, api_rounds=api_rounds,
            on_tool_call_ready=None,
        )
    finally:
        _mgr.dispatch_stream = _orig

    assert result['_loop_action'] is None, f'primary path expected; result={result}'
    # api_rounds MUST have: the adopted primary row + the discarded primary row.
    assert len(api_rounds) == 2, (
        f'api_rounds must include the DISCARDED first attempt so cost popover '
        f'matches the gateway bill; got {len(api_rounds)}: {api_rounds}')
    tags = [r.get('tag') for r in api_rounds]
    assert any('FLOOR-DISCARDED' in (t or '') for t in tags), (
        f'a DISCARDED-tagged row is required; tags={tags}')
    # accumulated_usage MUST double-count cache_creation because the gateway
    # charged for BOTH the primary write AND the recovered read.
    # primary cache_creation = 42000, recovered cache_creation = 1200 → 43200
    assert accumulated.get('cache_creation_input_tokens') == 43200, (
        f'accumulated_usage must sum BOTH billed attempts, not just adopted; '
        f'got cache_creation={accumulated.get("cache_creation_input_tokens")}')
    # cache_read is 28654 (primary) + 150000 (recovered) = 178654.
    assert accumulated.get('cache_read_tokens') == 178654, (
        f'accumulated cache_read must include BOTH billed attempts; '
        f'got {accumulated.get("cache_read_tokens")}')


def test_no_extra_billing_rounds_when_gate_off(monkeypatch):
    """When the mitigation is OFF (the 2026-07-23 default), there is no resend,
    so there is nothing extra to bill — `_extra_billing_rounds` must be absent.
    This is a NEUTER control for the honest-accounting path itself: the
    accounting-preservation code must not fire when no resend happened."""
    n, usage = _run_stream(monkeypatch, enabled=False,
                           dispatch_seq=[_FLOOR_USAGE, _HIT_USAGE])
    assert n == 1
    assert '_extra_billing_rounds' not in usage, (
        f'no resend = no extra billing rows; got {usage.get("_extra_billing_rounds")}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
