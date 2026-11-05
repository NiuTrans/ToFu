"""Regression: live-but-unregistered tasks are re-adopted into the chat
runtime instead of flooding the legacy fallback (pt_624fcb98708446f1).

WHY (measured 2026-08-21): three long autopilot turns lost their registry
row mid-run (stall-reaper false terminal-flip → TTL/capacity eviction).
Every subsequent ``manager.append_event`` took the legacy fallback, which
minted ``seq = len(task['events'])`` from a TRIMMED in-memory list — each
mint collided with the original run's durable storage_events rows
('Event sequence has a conflicting payload'), so every authoritative frame
was withheld (3 tasks x 800+ frames) and the client froze until refresh.

THE FIX (path B): the fallback seam first tries ``_try_readopt_task`` —
seed the next seq from the durable log, re-register via
``TaskRuntime.adopt``, and retry through the runtime's monotonic path.
Terminal tasks, discard-tombstoned dicts (the autopilot VU carrier's
retirement is BY DESIGN) and durable-probe failures keep the legacy
fallback unchanged.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def _chat_task(task_id: str, **overrides):
    task = {
        'id': task_id,
        'kind': 'chat',
        '_userId': TEST_OWNER_USER_ID,
        'status': 'running',
        'events': [],
        '_eventBaseSeq': 0,
        '_eventNextSeq': 0,
        'events_lock': threading.Lock(),
        'abort_event': threading.Event(),
        'content_lock': threading.Lock(),
        'config': {},
        'phase': None,
        'aborted': False,
    }
    task.update(overrides)
    return task


@pytest.fixture()
def runtime():
    from lib.agent_core.task_runtime import TaskRuntime
    return TaskRuntime('chat', push_channel=None)


@pytest.fixture()
def chat_runtime_cleanup():
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    before = set(chat_task_runtime.task_ids())
    yield chat_task_runtime
    for task_id in set(chat_task_runtime.task_ids()) - before:
        chat_task_runtime.discard(task_id)


# ── TaskRuntime.adopt ─────────────────────────────────────────


def test_adopt_registers_live_task_and_fills_missing_fields(runtime):
    bare = {
        'id': 't-live', 'status': 'running',
        '_userId': TEST_OWNER_USER_ID,
    }
    assert runtime.adopt(bare) is True
    assert runtime.get('t-live') is bare
    assert 'events_lock' in bare and 'abort_event' in bare
    assert bare['kind'] == 'chat'
    assert bare['events'] == []


def test_adopt_refuses_ownerless_and_principal_owner_mismatch(runtime):
    from lib.identity import PrincipalContext

    ownerless = {'id': 't-ownerless', 'status': 'running'}
    assert runtime.adopt(ownerless) is False
    assert runtime.get('t-ownerless') is None

    mismatched = {
        'id': 't-owner-mismatch',
        'status': 'running',
        '_userId': TEST_OWNER_USER_ID,
        '_principalContext': PrincipalContext.user(
            subject_id='test-user:2', owner_user_id=2,
        ).to_payload(),
    }
    assert runtime.adopt(mismatched) is False
    assert runtime.get('t-owner-mismatch') is None


def test_adopt_refuses_terminal_tombstoned_and_foreign(runtime):
    assert runtime.adopt({'id': 't-done', 'status': 'done'}) is False
    assert runtime.adopt({'id': 't-err', 'status': 'error'}) is False
    assert runtime.get('t-done') is None
    assert runtime.adopt(
        {'id': 't-tomb', 'status': 'running', '_discarded_at': 1.0}) is False
    assert runtime.get('t-tomb') is None
    assert runtime.adopt(
        {'id': 't-foreign', 'status': 'running', 'kind': 'paper-report'}) is False
    assert runtime.get('t-foreign') is None


def test_adopt_idempotent_and_registry_copy_wins(runtime):
    first = {
        'id': 't-race', 'status': 'running',
        '_userId': TEST_OWNER_USER_ID,
    }
    second = {
        'id': 't-race', 'status': 'running',
        '_userId': TEST_OWNER_USER_ID,
    }
    assert runtime.adopt(first) is True
    assert runtime.adopt(second) is True
    assert runtime.get('t-race') is first


# ── manager.append_event readopt seam ─────────────────────────


def _wire_persist_and_push(monkeypatch):
    captured = {'persist': [], 'push': []}

    import lib.tasks_pkg.event_log as event_log
    monkeypatch.setattr(
        event_log, 'append_persistent_event',
        lambda task_id, seq, event, **_kwargs: captured['persist'].append(
            (task_id, seq)))
    import lib.agent_core.push as agent_push
    monkeypatch.setattr(
        agent_push, 'push_event',
        lambda channel, task_id, event, **_kwargs:
        captured['push'].append(task_id))
    return captured


def test_readopt_seam_seeds_from_durable_and_uses_runtime_path(
        monkeypatch, chat_runtime_cleanup):
    import lib.tasks_pkg.manager._events as _events

    monkeypatch.setattr(_events, '_probe_durable_next_seq', lambda _tid: 500)
    captured = _wire_persist_and_push(monkeypatch)
    task = _chat_task('t-readopt-main')

    _events.append_event(task, {'type': 'delta', 'content': 'hello'})

    runtime = chat_runtime_cleanup
    assert runtime.get('t-readopt-main') is task
    assert captured['persist'] == [('t-readopt-main', 500)]
    assert captured['push'] == ['t-readopt-main']
    assert task['events'][-1]['seq'] == 500
    # The NEXT frame stays on the monotonic runtime path.
    _events.append_event(task, {'type': 'delta', 'content': 'world'})
    assert captured['persist'][-1] == ('t-readopt-main', 501)
    assert captured['push'] == ['t-readopt-main', 't-readopt-main']


def test_readopt_seam_clears_diverged_retained_window(
        monkeypatch, chat_runtime_cleanup):
    import lib.tasks_pkg.manager._events as _events

    monkeypatch.setattr(_events, '_probe_durable_next_seq', lambda _tid: 500)
    _wire_persist_and_push(monkeypatch)
    # A retained tail minted by the legacy fallback from a trimmed list —
    # its next seq (4) sits deep inside the durable-owned range.
    task = _chat_task('t-readopt-window',
                      events=[{'type': 'delta', 'seq': 3}])

    _events.append_event(task, {'type': 'delta', 'content': 'fresh'})

    assert task['events'] == [
        {'type': 'delta', 'content': 'fresh', 'taskId': 't-readopt-window',
         'seq': 500}]
    assert task['_eventBaseSeq'] == 500


def test_readopt_keeps_consistent_retained_window(
        monkeypatch, chat_runtime_cleanup):
    import lib.tasks_pkg.manager._events as _events

    # Durable max seq 3 → seed 4; retained tail already ends at 3 — the two
    # agree, so the replay window must survive intact.
    monkeypatch.setattr(_events, '_probe_durable_next_seq', lambda _tid: 4)
    _wire_persist_and_push(monkeypatch)
    task = _chat_task('t-readopt-consistent',
                      events=[{'type': 'delta', 'seq': 3}])

    _events.append_event(task, {'type': 'delta', 'content': 'next'})

    assert [e['seq'] for e in task['events']] == [3, 4]


def test_readopt_probe_failure_withholds_without_second_sequence_authority(
        monkeypatch, chat_runtime_cleanup):
    import lib.tasks_pkg.manager._events as _events

    monkeypatch.setattr(_events, '_probe_durable_next_seq', lambda _tid: None)
    captured = _wire_persist_and_push(monkeypatch)
    task = _chat_task('t-readopt-noprobe')

    _events.append_event(task, {'type': 'delta', 'content': 'x'})

    runtime = chat_runtime_cleanup
    assert runtime.get('t-readopt-noprobe') is None
    assert task['events'] == []
    assert captured['push'] == []
    assert task['_registryWithheldCount'] == 1


def test_readopt_refuses_terminal_and_tombstoned(
        monkeypatch, chat_runtime_cleanup):
    import lib.tasks_pkg.manager._events as _events

    probe_calls = []
    monkeypatch.setattr(
        _events, '_probe_durable_next_seq',
        lambda tid: probe_calls.append(tid) or 500)
    captured = _wire_persist_and_push(monkeypatch)

    terminal = _chat_task('t-readopt-done', status='done')
    _events.append_event(terminal, {'type': 'delta', 'content': 'x'})
    assert chat_runtime_cleanup.get('t-readopt-done') is None
    assert captured['push'] == []

    tombstoned = _chat_task('t-readopt-tomb', _discarded_at=1.0)
    _events.append_event(tombstoned, {'type': 'delta', 'content': 'x'})
    assert chat_runtime_cleanup.get('t-readopt-tomb') is None
    assert captured['push'] == []

    # Neither path even probed the durable log.
    assert probe_calls == []


def test_readopt_seeds_stale_attempt_fence_still_unwinds(
        monkeypatch, chat_runtime_cleanup):
    """After adoption a genuinely stale attempt must still hit the
    existing stale fence (record_task_event falsy → cooperative abort)."""
    import lib.tasks_pkg.manager._events as _events

    monkeypatch.setattr(_events, '_probe_durable_next_seq', lambda _tid: 500)
    _wire_persist_and_push(monkeypatch)
    import lib.turn_lifecycle as turn_lifecycle
    monkeypatch.setattr(turn_lifecycle, 'record_task_event',
                        lambda *a, **k: False)
    monkeypatch.setattr(turn_lifecycle, 'get_attempt',
                        lambda *a, **k: {'status': 'superseded', 'taskId': 'old-task'})
    task = _chat_task(
        't-readopt-stale',
        _turnId='turn-1',
        _attemptId='att-1',
        convId='conv-1',
    )

    _events.append_event(task, {'type': 'delta', 'content': 'x'})

    assert chat_runtime_cleanup.get('t-readopt-stale') is task
    assert task['aborted'] is True
    assert task['_abort_reason'] == 'conversation_attempt_stale_fence'
    assert task['abort_event'].is_set()


def _admission_wait_event():
    return {'type': 'workspace_admission_wait', 'roundNum': 3,
            'toolCallId': 'call-1', 'toolName': 'run_command',
            'waitStartedAtMs': 1, 'waitEndedAtMs': 2, 'waitedMs': 1,
            'mode': 'write', 'lockDomain': 'workspace', 'lockRoot': 'repo',
            'outcome': 'admitted'}


@pytest.mark.parametrize('retirement', [
    {'status': 'done'},          # settled, then evicted after retention
    {'_discarded_at': 1.0},      # deliberately unregistered while running
])
def test_detached_command_view_never_resurrects_or_aborts_its_retired_turn(
        monkeypatch, chat_runtime_cleanup, retirement):
    """A handed-off command outlives its turn. Its late admission evidence
    must not re-adopt the retired task or plant the stale-attempt abort that
    the command itself observes."""
    import lib.tasks_pkg.manager._events as _events
    import lib.turn_lifecycle as turn_lifecycle
    from lib.tasks_pkg.handlers._background_command import DetachableCommandTask

    monkeypatch.setattr(_events, '_probe_durable_next_seq', lambda _tid: 3188)
    captured = _wire_persist_and_push(monkeypatch)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event',
                        lambda *a, **k: False)
    monkeypatch.setattr(turn_lifecycle, 'get_attempt',
                        lambda *a, **k: {'status': 'completed', 'taskId': 't-handoff'})
    parent = _chat_task('t-handoff', _turnId='turn-1', _attemptId='att-1',
                        convId='conv-1')
    command_view = DetachableCommandTask(parent)
    command_view.detach()
    parent.update(retirement)

    _events.append_event(command_view, _admission_wait_event())

    assert chat_runtime_cleanup.get('t-handoff') is None
    assert captured == {'persist': [], 'push': []}
    assert command_view.get('aborted') is False
    assert not parent['abort_event'].is_set()


def test_settled_turn_ignores_its_handed_off_commands_late_admission_wait(
        monkeypatch, chat_runtime_cleanup):
    """Within the retention window the settled row is still registered; the
    evidence of a wait that ended after settlement has no live turn to join."""
    import time

    import lib.turn_lifecycle as turn_lifecycle
    from lib.agent_core.execution_clock import ExecutionClock
    from lib.project_mod.workspace_lease import AdmissionWait
    from lib.tasks_pkg.executor._admission_wait import observe_admission_wait
    from lib.tasks_pkg.handlers._background_command import DetachableCommandTask

    captured = _wire_persist_and_push(monkeypatch)
    recorded = []
    monkeypatch.setattr(turn_lifecycle, 'record_task_event',
                        lambda *a, **k: recorded.append(a) or False)
    monkeypatch.setattr(turn_lifecycle, 'get_attempt',
                        lambda *a, **k: {'status': 'completed', 'taskId': 't-retained'})
    parent = _chat_task('t-retained', _turnId='turn-1', _attemptId='att-1',
                        convId='conv-1', _executionClock=ExecutionClock.start())
    assert chat_runtime_cleanup.adopt(parent) is True
    command_view = DetachableCommandTask(parent)
    command_view.detach()
    parent['status'] = 'done'
    now = time.monotonic()

    observe_admission_wait(command_view, AdmissionWait(
        started_monotonic=now - 5044, ended_monotonic=now, mode='write',
        lock_domain='workspace', lock_root='repo', outcome='admitted',
        waiter_tool='run_command', tool_call_id='call-1', round_num=3))

    assert parent['events'] == []
    assert recorded == []
    assert captured == {'persist': [], 'push': []}
    assert command_view.get('aborted') is False
    assert not parent['abort_event'].is_set()


@pytest.mark.parametrize('detached', [False, True])
def test_command_view_still_explains_its_live_turn(
        monkeypatch, chat_runtime_cleanup, detached):
    """While the turn runs, a handed-off command's wait still reaches its
    Turn Trace (clamped to the tool span there)."""
    import lib.tasks_pkg.manager._events as _events
    from lib.tasks_pkg.handlers._background_command import DetachableCommandTask

    captured = _wire_persist_and_push(monkeypatch)
    parent = _chat_task('t-live-turn')
    assert chat_runtime_cleanup.adopt(parent) is True
    command_view = DetachableCommandTask(parent)
    if detached:
        command_view.detach()

    _events.append_event(command_view, _admission_wait_event())

    assert captured['push'] == ['t-live-turn']
    assert [event['type'] for event in parent['events']] == ['workspace_admission_wait']


# ── discard_task tombstone ────────────────────────────────────


def test_discard_task_stamps_tombstone(chat_runtime_cleanup):
    import lib.tasks_pkg.manager._registry as _registry

    task = _chat_task('t-discard-stamp')
    assert chat_runtime_cleanup.adopt(task)
    _registry.discard_task(task['id'])
    assert chat_runtime_cleanup.get(task['id']) is None
    assert task.get('_discarded_at')
