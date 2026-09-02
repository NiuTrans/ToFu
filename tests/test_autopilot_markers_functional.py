#!/usr/bin/env python3
"""Comprehensive functional tests for ``lib/tasks_pkg/autopilot_markers.py``.

Complements the wire-parity guard
(``test_autopilot_markers_extraction_wire_parity.py``) — which locks
structural identity — and the end-to-end arm/disarm suite
(``test_autopilot_arm.py``) — which covers the happy path.

Gaps this file closes:

  * ``_marker_exists`` — was previously only tested INDIRECTLY (via the
    ``armed`` flag in arm_autopilot's result). Direct coverage of both
    branches (present, absent, exception-swallowed).
  * ``arm_autopilot`` edge cases: empty conv_id return shape, task without
    a config dict (skipped), task in wrong status (skipped), marker persist
    failure swallowed, Flow-managed blocking returns the proper shape with no
    marker.
  * ``disarm_autopilot`` edge cases: no live tasks (marker clear alone),
    conclude_run exception swallowed, no run at all → concluded=None →
    result has NO ``runConcluded`` key, VU subtask skipped.

Every test targets ``autopilot_markers.X`` directly (the extraction home) so
a future rename/regression flips them; wire-parity coverage of the facade
mirror lives in the sister test file.

Pure unit — the in-memory task registry is real (tasks / tasks_lock) but
tests use synthetic tasks; message_queue markers are cleared to keep
suite runs hermetic.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

import pytest

import lib.tasks_pkg.autopilot_markers as ap_markers


@pytest.fixture(autouse=True)
def marker_authority(monkeypatch):
    """Owner-keyed marker fake; Sidecar queue semantics are tested elsewhere."""
    import lib.message_queue as message_queue

    markers: dict[tuple[int, str], dict] = {}

    def arm(conversation_id, config, *, user_id):
        key = (int(user_id), conversation_id)
        if key in markers:
            return {'armed': False, 'queueId': markers[key]['queueId']}
        record = {'queueId': f'marker:{user_id}:{conversation_id}',
                  'config': dict(config or {})}
        markers[key] = record
        return {'armed': True, **record}

    def clear(conversation_id, *, user_id):
        return markers.pop((int(user_id), conversation_id), None) is not None

    def has(conversation_id, *, user_id):
        return (int(user_id), conversation_id) in markers

    monkeypatch.setattr(message_queue, 'arm_autopilot_marker', arm)
    monkeypatch.setattr(message_queue, 'clear_autopilot_marker', clear)
    monkeypatch.setattr(message_queue, 'has_autopilot_marker', has)
    monkeypatch.setattr(
        ap_markers,
        'conclude_run',
        lambda conversation_id, *, user_id, reason='stopped', run_id='': None,
    )
    return markers


# ══════════════════════════════════════════════════════════
#  Fixture: in-memory task insertion + marker cleanup
# ══════════════════════════════════════════════════════════

@pytest.fixture()
def put_task():
    """Insert a synthetic task into the in-memory registry; auto-cleanup."""
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    added: list[str] = []
    convs: set[str] = set()

    def _put(task):
        task.setdefault('_userId', 1)
        with tasks_lock:
            tasks[task['id']] = task
        added.append(task['id'])
        if task.get('convId'):
            convs.add(task['convId'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)
    # Best-effort marker cleanup so DB state doesn't leak between tests.
    try:
        from lib.message_queue import clear_autopilot_marker
        for cid in convs:
            try:
                clear_autopilot_marker(cid, user_id=1)
            except Exception:
                pass
    except Exception:
        pass


def _running_task(tid, conv_id, **cfg_over):
    cfg = {'model': 'm', 'autopilot': False}
    cfg.update(cfg_over)
    return {
        'id': tid,
        'convId': conv_id,
        '_userId': 1,
        'status': 'running',
        'config': cfg,
    }


# ══════════════════════════════════════════════════════════
#  _marker_exists — direct coverage
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_marker_exists_returns_true_when_marker_present(monkeypatch):
    """has_autopilot_marker returns True → _marker_exists → True."""
    import lib.message_queue as mq
    monkeypatch.setattr(mq, 'has_autopilot_marker', lambda cid, *, user_id: True)
    assert ap_markers._marker_exists('cv-any', user_id=1) is True


@pytest.mark.unit
def test_marker_exists_returns_false_when_marker_absent(monkeypatch):
    import lib.message_queue as mq
    monkeypatch.setattr(mq, 'has_autopilot_marker', lambda cid, *, user_id: False)
    assert ap_markers._marker_exists('cv-any', user_id=1) is False


@pytest.mark.unit
def test_marker_exists_swallows_exception_returns_false(monkeypatch):
    """A probe failure must never propagate — treated as 'no marker'."""
    import lib.message_queue as mq
    def _boom(_cid, *, user_id):
        raise RuntimeError('queue table missing')
    monkeypatch.setattr(mq, 'has_autopilot_marker', _boom)
    assert ap_markers._marker_exists('cv-boom', user_id=1) is False


@pytest.mark.unit
def test_marker_exists_handles_empty_conv_id(monkeypatch):
    """Empty conv_id must not raise on the shortening logger call
    (``conv_id[:8]`` is guarded)."""
    import lib.message_queue as mq
    def _boom(_cid, *, user_id):
        raise RuntimeError('boom for empty conv')
    monkeypatch.setattr(mq, 'has_autopilot_marker', _boom)
    # No exception even though the swallow-log path runs.
    assert ap_markers._marker_exists('', user_id=1) is False


# ══════════════════════════════════════════════════════════
#  arm_autopilot — edge cases beyond test_autopilot_arm.py
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_arm_skips_task_with_non_dict_config(put_task, monkeypatch):
    """A live task whose ``config`` is None (or any non-dict) must be
    silently skipped — no flip, no crash. Verified by putting a healthy
    task alongside; only the healthy one flips."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-nondict', user_id=1)
    put_task({'id': 't-broken', 'convId': 'conv-nondict',
              'status': 'running', 'config': None})
    put_task(_running_task('t-good', 'conv-nondict'))
    result = ap_markers.arm_autopilot('conv-nondict', user_id=1)
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-broken']['config'] is None
    assert tasks['t-good']['config']['autopilot'] is True
    assert 't-good' in result['taskIds']
    assert 't-broken' not in result['taskIds']


@pytest.mark.unit
def test_arm_skips_task_in_wrong_status(put_task, monkeypatch):
    """A task in status='done' or 'aborted' must NOT be flipped."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-mixed-status', user_id=1)
    put_task({'id': 't-done', 'convId': 'conv-mixed-status',
              'status': 'done', 'config': {'model': 'm', 'autopilot': False}})
    put_task({'id': 't-aborted', 'convId': 'conv-mixed-status',
              'status': 'aborted', 'config': {'model': 'm', 'autopilot': False}})
    put_task(_running_task('t-running', 'conv-mixed-status'))
    result = ap_markers.arm_autopilot('conv-mixed-status', user_id=1)
    assert set(result['taskIds']) == {'t-running'}
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-done']['config']['autopilot'] is False
    assert tasks['t-aborted']['config']['autopilot'] is False
    assert tasks['t-running']['config']['autopilot'] is True


@pytest.mark.unit
def test_arm_flow_managed_flag_blocks(put_task):
    """A task owned by the Flow engine cannot also enter live Autopilot."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-ep-managed', user_id=1)
    t = _running_task('t-ep-m', 'conv-ep-managed')
    t['_flow_managed'] = True
    put_task(t)
    result = ap_markers.arm_autopilot('conv-ep-managed', user_id=1)
    assert result['armed'] is False
    assert result['markerAdded'] is False


@pytest.mark.unit
def test_arm_marker_persist_failure_swallowed(put_task, monkeypatch):
    """A failure to persist the queue-lane marker (e.g. DB unavailable) is
    swallowed at WARNING; the live-task flip STILL succeeded, so armed=True."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-marker-fail', user_id=1)
    put_task(_running_task('t-flip', 'conv-marker-fail'))
    import lib.message_queue as mq
    def _boom(_cid, _cfg, *, user_id):
        raise RuntimeError('queue table locked')
    monkeypatch.setattr(mq, 'arm_autopilot_marker', _boom)
    # And has_autopilot_marker (queried at the tail for the final armed flag)
    # returns False under the same failure regime.
    monkeypatch.setattr(mq, 'has_autopilot_marker', lambda _cid, *, user_id: False)
    result = ap_markers.arm_autopilot('conv-marker-fail', user_id=1)
    # Live flip succeeded → armed via the flip half, not the marker half.
    assert 't-flip' in result['taskIds']
    assert result['markerAdded'] is False
    assert result['armed'] is True  # bool(armed_ids) → True


@pytest.mark.unit
def test_arm_no_live_task_and_marker_fails_returns_not_armed(
        put_task, monkeypatch):
    """The corner case: no live task to flip AND marker persist also fails
    AND _marker_exists returns False → armed=False (nothing arms).

    Guards that the final ``armed`` computation genuinely combines all
    three signals (armed_ids OR marker_added OR _marker_exists).
    """
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-nothing', user_id=1)
    # No live task inserted.
    import lib.message_queue as mq
    monkeypatch.setattr(mq, 'arm_autopilot_marker',
                        lambda cid, cfg, *, user_id: (_ for _ in ()).throw(
                            RuntimeError('marker persist failed')))
    monkeypatch.setattr(mq, 'has_autopilot_marker', lambda _cid, *, user_id: False)
    result = ap_markers.arm_autopilot('conv-nothing', user_id=1)
    assert result == {'armed': False, 'taskIds': [], 'markerAdded': False}


@pytest.mark.unit
def test_arm_captures_marker_cfg_from_first_matching_task(put_task, monkeypatch):
    """The marker persists a COPY of the first live task's config so the
    kick-resume path has something to seed the follow-up with. If TWO
    running tasks exist, the FIRST-encountered wins the marker_cfg (map
    iteration is deterministic within a single run under CPython 3.7+)."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-two-live', user_id=1)
    put_task(_running_task('t-first', 'conv-two-live', model='M1'))
    put_task(_running_task('t-second', 'conv-two-live', model='M2'))
    captured = []
    import lib.message_queue as mq
    def _capture(cid, cfg, *, user_id):
        captured.append(dict(cfg))
        return {'armed': True}
    monkeypatch.setattr(mq, 'arm_autopilot_marker', _capture)
    ap_markers.arm_autopilot('conv-two-live', user_id=1)
    assert len(captured) == 1
    # The marker cfg is one of the two live-task configs — capturing exactly
    # ONE means we didn't double-persist. The MODEL is preserved.
    assert captured[0].get('model') in ('M1', 'M2')


@pytest.mark.unit
def test_arm_skips_task_for_other_conv(put_task):
    """A running task for a different conversation must not be flipped."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-target', user_id=1)
    clear_autopilot_marker('conv-other', user_id=1)
    put_task(_running_task('t-target', 'conv-target'))
    put_task(_running_task('t-other', 'conv-other'))
    result = ap_markers.arm_autopilot('conv-target', user_id=1)
    assert 't-target' in result['taskIds']
    assert 't-other' not in result['taskIds']
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-other']['config']['autopilot'] is False


# ══════════════════════════════════════════════════════════
#  disarm_autopilot — edge cases
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_disarm_returns_no_run_concluded_when_no_run(put_task, monkeypatch):
    """When no autopilot run is anchored on the conv, ``conclude_run`` returns
    None → the disarm result has NO ``runConcluded`` key at all."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-no-run', user_id=1)
    # Stub conclude_run → None (as though _resolve_recent_run_id returned '').
    # Post-slice-3: patch the origin binding on autopilot_markers, not
    # the facade — disarm reads the module-scope name.
    monkeypatch.setattr(ap_markers, 'conclude_run',
                        lambda cid, reason='stopped', *, user_id: None)
    result = ap_markers.disarm_autopilot('conv-no-run', user_id=1)
    assert 'runConcluded' not in result
    assert result['disarmed'] is False  # no marker, no tasks flipped
    assert result['markerCleared'] is False
    assert result['taskIds'] == []


@pytest.mark.unit
def test_disarm_returns_run_concluded_dict_when_run_exists(
        put_task, monkeypatch):
    """When conclude_run returns a record, disarm's result carries it under
    ``runConcluded`` — the calling client (idle-disarm case with no SSE)
    can fold instantly."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-with-run', user_id=1)
    fake_record = {'runId': 'ar-x', 'status': 'concluded',
                   'reason': 'stopped', 'ts': 123456}
    # Post-slice-3: disarm_autopilot resolves ``conclude_run`` from
    # autopilot_markers' OWN module namespace (top-level import from
    # autopilot_run_lifecycle). Patching the facade no longer steers the
    # call — patch the origin binding instead.
    monkeypatch.setattr(ap_markers, 'conclude_run',
                        lambda cid, reason='stopped', *, user_id: fake_record)
    result = ap_markers.disarm_autopilot('conv-with-run', user_id=1)
    assert result['runConcluded'] == fake_record


@pytest.mark.unit
def test_disarm_swallows_conclude_run_exception(put_task, monkeypatch):
    """A crash in ``conclude_run`` must NOT break disarm — the marker /
    live-config parts have already cleared, and the client depends on the
    ``disarmed`` flag."""
    from lib.message_queue import arm_autopilot_marker, clear_autopilot_marker
    clear_autopilot_marker('conv-conclude-fail', user_id=1)
    put_task(_running_task('t-c', 'conv-conclude-fail', autopilot=True))
    arm_autopilot_marker('conv-conclude-fail', {}, user_id=1)
    def _boom(_cid, *, user_id, reason='stopped'):
        raise RuntimeError('conclude_run raised')
    # Post-slice-3: patch the origin binding on autopilot_markers so the
    # module-scope call inside disarm_autopilot resolves to _boom.
    monkeypatch.setattr(ap_markers, 'conclude_run', _boom)
    # No raise; live-flip + marker-clear halves still landed.
    result = ap_markers.disarm_autopilot('conv-conclude-fail', user_id=1)
    assert result['markerCleared'] is True
    assert 't-c' in result['taskIds']
    assert 'runConcluded' not in result  # conclude never returned a record


@pytest.mark.unit
def test_disarm_skips_vu_subtask(put_task):
    """A live task with ``_vu_subtask=True`` (the internal VU sub-task) must
    be left alone — flipping it would break the running VU."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-vu', user_id=1)
    put_task(_running_task('t-parent', 'conv-vu', autopilot=True))
    vu = _running_task('t-vu-sub', 'conv-vu', autopilot=True)
    vu['_vu_subtask'] = True
    put_task(vu)
    result = ap_markers.disarm_autopilot('conv-vu', user_id=1)
    # Parent flipped; VU subtask untouched.
    assert 't-parent' in result['taskIds']
    assert 't-vu-sub' not in result['taskIds']
    from tests.support.chat_tasks import chat_task_registry as tasks
    assert tasks['t-parent']['config']['autopilot'] is False
    assert tasks['t-vu-sub']['config']['autopilot'] is True


@pytest.mark.unit
def test_disarm_flips_tasks_across_multiple_statuses(put_task, monkeypatch):
    """disarm DELIBERATELY does NOT gate on status='running' (only arm does):
    a task in 'done' with a still-armed config can be flipped too, because
    autopilot=True on a stale config could still be read by a follow-up.
    Verified by NOT filtering on status here."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-multi-status', user_id=1)
    put_task({'id': 't-done-armed', 'convId': 'conv-multi-status',
              'status': 'done',
              'config': {'model': 'm', 'autopilot': True}})
    put_task(_running_task('t-run-armed', 'conv-multi-status', autopilot=True))
    result = ap_markers.disarm_autopilot('conv-multi-status', user_id=1)
    assert set(result['taskIds']) == {'t-done-armed', 't-run-armed'}


@pytest.mark.unit
def test_disarm_only_flips_tasks_with_autopilot_true(put_task, monkeypatch):
    """A task whose config already has autopilot=False (or missing) must
    not appear in taskIds — the mutation is a no-op, no double-fire signal."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-mixed-flag', user_id=1)
    put_task(_running_task('t-off', 'conv-mixed-flag', autopilot=False))
    put_task(_running_task('t-on', 'conv-mixed-flag', autopilot=True))
    result = ap_markers.disarm_autopilot('conv-mixed-flag', user_id=1)
    assert result['taskIds'] == ['t-on']


@pytest.mark.unit
def test_disarm_swallows_marker_clear_exception(put_task, monkeypatch):
    """A failure to CLEAR the queue-lane marker (e.g. DB glitch) is
    swallowed at WARNING; the live-config flip still lands."""
    from lib.message_queue import clear_autopilot_marker as _real_clear
    _real_clear('conv-clear-fail', user_id=1)  # baseline
    put_task(_running_task('t-flip', 'conv-clear-fail', autopilot=True))
    import lib.message_queue as mq
    def _boom(_cid, *, user_id):
        raise RuntimeError('clear failed')
    monkeypatch.setattr(mq, 'clear_autopilot_marker', _boom)
    result = ap_markers.disarm_autopilot('conv-clear-fail', user_id=1)
    # markerCleared is the return of the failing call → False (best-effort).
    assert result['markerCleared'] is False
    # But the live-config flip still landed.
    assert 't-flip' in result['taskIds']


@pytest.mark.unit
def test_disarm_no_tasks_no_marker_reports_nothing(monkeypatch):
    """The truly-nothing case: no marker, no tasks, no run → all fields
    False/empty, no runConcluded key, no raise."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-empty', user_id=1)
    # Post-slice-3: patch the origin binding on autopilot_markers.
    monkeypatch.setattr(ap_markers, 'conclude_run',
                        lambda cid, reason='stopped', *, user_id: None)
    result = ap_markers.disarm_autopilot('conv-empty', user_id=1)
    assert result == {'disarmed': False, 'markerCleared': False, 'taskIds': []}


@pytest.mark.unit
def test_disarm_result_shape_stable(put_task, monkeypatch):
    """The result keys are stable — 'disarmed', 'markerCleared', 'taskIds'
    ALWAYS present; 'runConcluded' conditionally present."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-shape', user_id=1)
    # Post-slice-3: patch the origin binding on autopilot_markers.
    monkeypatch.setattr(ap_markers, 'conclude_run',
                        lambda cid, reason='stopped', *, user_id: None)
    result = ap_markers.disarm_autopilot('conv-shape', user_id=1)
    for k in ('disarmed', 'markerCleared', 'taskIds'):
        assert k in result, f'{k} missing from disarm result'
    assert isinstance(result['taskIds'], list)
    assert isinstance(result['disarmed'], bool)
    assert isinstance(result['markerCleared'], bool)


@pytest.mark.unit
def test_disarm_calls_conclude_run_via_module_scope_binding(
        put_task, monkeypatch):
    """★ Post-slice-3 (pt_00459503): ``disarm_autopilot`` resolves
    ``conclude_run`` from ``autopilot_markers``'s OWN module namespace.
    That name is bound at module import time to
    ``lib.tasks_pkg.autopilot_run_lifecycle.conclude_run`` (the leaf
    module). Slice 2's lazy-import posture is gone — the cycle it
    guarded is eliminated at the graph level (autopilot_markers has zero
    top-level dep on autopilot.py).

    Guard the wiring end-to-end: a monkeypatch of the module-scope name
    steers the call site. Together with the AST/subprocess guards in
    ``test_autopilot_markers_lazy_import_contract.py``, this locks the
    cycle-free binding chain.
    """
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-lazy', user_id=1)
    calls = []
    def _tracer(cid, *, user_id, reason='stopped'):
        calls.append((cid, user_id, reason))
        return {'runId': 'ar-lazy', 'status': 'concluded', 'reason': reason,
                'ts': 0}
    # Patch autopilot_markers.conclude_run — the module-scope binding
    # disarm_autopilot actually reads. The facade attr on autopilot.py
    # points at the SAME callable (identity re-export), but is a distinct
    # binding for monkeypatch purposes; only patching the origin steers.
    monkeypatch.setattr(ap_markers, 'conclude_run', _tracer)
    ap_markers.disarm_autopilot('conv-lazy', user_id=1)
    assert calls == [('conv-lazy', 1, 'stopped')]

    # Sanity: confirm the identity chain (facade attr IS leaf attr IS
    # markers's bound name PRE-patch — asserted after patch by re-import).
    import importlib
    importlib.reload(ap_markers)
    try:
        import lib.tasks_pkg.autopilot as ap
        import lib.tasks_pkg.autopilot_run_lifecycle as leaf
        assert ap_markers.conclude_run is leaf.conclude_run, (
            'autopilot_markers.conclude_run must be identity-bound to the '
            'leaf module after a fresh reload — otherwise the top-level '
            'import wiring is wrong.')
        assert ap.conclude_run is leaf.conclude_run, (
            'autopilot.conclude_run facade attr must be identity-bound to '
            'the leaf module.')
    finally:
        # reload() rebound ap_markers' OWN defs (arm_autopilot /
        # disarm_autopilot / _marker_exists) to NEW objects, but the
        # facade's from-imports (lib/tasks_pkg/autopilot.py) still point at
        # the PRE-reload ones — leaving the process with a broken
        # facade↔markers identity that the NEXT suite in a ring
        # (test_autopilot_markers_extraction_wire_parity) reads as a
        # contract violation. Re-sync the facade bindings so the state we
        # leave behind is coherent. Only re-sync when the facade is
        # already imported — never force-import it from here.
        _facade = sys.modules.get('lib.tasks_pkg.autopilot')
        if _facade is not None:
            for _n in ('arm_autopilot', 'disarm_autopilot', '_marker_exists'):
                setattr(_facade, _n, getattr(ap_markers, _n))


# ══════════════════════════════════════════════════════════
#  arm/disarm — result-shape stability
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_arm_result_shape_stable(put_task):
    """arm_autopilot result MUST always carry 'armed', 'taskIds',
    'markerAdded' — external callers (routes/chat_queue.py, frontend)
    depend on this."""
    from lib.message_queue import clear_autopilot_marker
    clear_autopilot_marker('conv-arm-shape', user_id=1)
    result = ap_markers.arm_autopilot('conv-arm-shape', user_id=1)
    for k in ('armed', 'taskIds', 'markerAdded'):
        assert k in result, f'{k} missing from arm result'
    assert isinstance(result['armed'], bool)
    assert isinstance(result['taskIds'], list)
    assert isinstance(result['markerAdded'], bool)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
