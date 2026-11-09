"""tests/tasks_pkg/test_stuck_task_reaper.py — the generalized stuck/wedged-task reaper.

Regression coverage for the "sidebar dot outlives the work" incident:
a task that emitted a first token then had its thread WEDGE (content=2 chars,
events>0, then silence forever) used to stay ``status='running'`` in the
in-memory registry indefinitely. ``/api/chat/active`` kept reporting it running
→ the frontend reconnected an SSE that never delivered a byte → the sidebar
"busy" dot never cleared, and the task never wrote a terminal ``task_results``
row (so a post-restart poll 404'd and the turn was lost).

The ORIGINAL ``reap_stuck_running_tasks`` gated on
``content=='' AND thinking=='' AND n_events==0`` — it could ONLY see the
never-produced-anything case, so the produced-then-wedged zombie fell straight
through both ``continue`` guards. The fix generalizes the discriminator to two
liveness clocks that must BOTH be stale to reap:

  • ``_t_last_event``     — bumped by every emitted event (deltas / keepalive /
                            retry / waiting_model phase). A rate-limited-but-alive
                            turn keeps emitting retry phases → stays fresh.
  • ``_dispatch_heartbeat`` — refreshed while a dispatch / cooldown-wait / tool
                            call is genuinely in-flight (and while blocking on
                            human input). A turn stuck in a live socket read
                            emits no event but IS heartbeating → never reaped.

Requiring BOTH stale is exactly the signal a client-side poll CANNOT recover
(poll only sees ``status='running'``, indistinguishable from a slow turn), so
the reap must be server-side.

These tests drive ``reap_stuck_running_tasks()`` directly against synthetic
tasks in the in-memory registry, controlling the two clocks + the env
threshold, with no live LLM. The terminal-floor DB write is best-effort and
guarded, so it is exercised but not asserted here (a separate DB-backed poll
test would cover the 404→terminal transition).
"""

import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def reaper_env(monkeypatch):
    """Pin the silence threshold to a small, deterministic value (N=300s)."""
    monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '300')
    return 300


@pytest.fixture()
def put_task(monkeypatch):
    """Insert synthetic tasks into the in-memory registry; auto-cleanup.

    Also stubs the terminal-floor DB writer to a no-op so the unit test needs
    no DB — we assert the in-memory transition (status/finishReason/aborted),
    which is what ``/api/chat/active`` and ``/poll`` read from memory.
    """
    from tests.support.chat_tasks import (
        chat_task_fixture_guard as tasks_lock,
        chat_task_registry as tasks,
        isolated_reaper_registry,
    )
    from lib.tasks_pkg.manager import _maintenance

    reaper_tasks = isolated_reaper_registry(monkeypatch)
    # Stub the full finalizer (terminal-floor DB write + conv sync + terminal
    # SSE + queue drain) to a no-op so these unit tests need no DB — they
    # assert the in-memory transition (status/finishReason/aborted), which is
    # set BEFORE the finalizer runs and is what /api/chat/active + /poll read.
    monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                        lambda t: None, raising=True)

    added = []

    def _put(task):
        with tasks_lock:
            tasks[task['id']] = task
            reaper_tasks[task['id']] = task
        added.append(task['id'])
        return task['id']

    yield _put

    with tasks_lock:
        for tid in added:
            tasks.pop(tid, None)


def _mk_task(task_id, **fields):
    """A minimal running task with a real events_lock (like create_task)."""
    import threading
    t = {
        'id': task_id,
        'convId': 'cv-' + task_id,
        'status': 'running',
        'aborted': False,
        'content': '',
        'thinking': '',
        'events': [],
        'events_lock': threading.Lock(),
        'config': {'model': 'aws.claude-opus-4.8'},
        'created_at': time.time(),
    }
    t.update(fields)
    return t


def _reap():
    from lib.tasks_pkg.manager import reap_stuck_running_tasks
    return reap_stuck_running_tasks()


def _get(task_id):
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        return dict(tasks.get(task_id) or {})


# ─────────────────────────────────────────────────────────────────────────
# (i) The incident: produced-then-wedged. content=2 chars, one event, BOTH
#     clocks stale past N → REAPED to terminal.
# ─────────────────────────────────────────────────────────────────────────
def test_reap_partial_output_then_wedged(reaper_env, put_task):
    now = time.time()
    stale = now - 400  # > N (300)
    put_task(_mk_task(
        'wedged-partial-1',
        content='ok',                 # 2 chars — the exact zombie shape
        events=[{'type': 'delta', 'seq': 0}],  # >0 events (defeats old guard)
        _t_last_event=stale,          # event-silence 400s
        _dispatch_heartbeat=stale,    # dispatch-silence 400s
        created_at=stale,
    ))

    n = _reap()
    assert n == 1, 'the produced-then-wedged zombie must be reaped'
    t = _get('wedged-partial-1')
    assert t['status'] == 'error'
    assert t['aborted'] is True
    assert t['_abort_reason'] == 'stuck_no_progress'
    assert t['finishReason'] == 'error'
    assert t['error'] is not None
    assert t.get('_reap_had_output') is True  # it HAD produced output


def test_reaped_root_settles_its_turn_failed_never_as_a_user_stop(reaper_env, put_task):
    """The reaper force-fails a wedged root; its Turn must record that failure.

    2026-11-09 (task 990b7f97): read as ``interrupted/user_abort``, the Turn showed a
    phantom Stop while the task result said ``error``, and durable worker recovery,
    which requires the two to agree, retried ``settlement_unconfirmed`` for hours.
    """
    from lib.turn_lifecycle import _settlement
    from lib.turn_verdict import TurnStatus, derive_task_verdict
    stale = time.time() - 400
    put_task(_mk_task('wedged-root-verdict', content='ok', events=[{'type': 'delta', 'seq': 0}],
                      _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale))
    assert _reap() == 1
    task = _get('wedged-root-verdict')
    # The frame _finalize_reaped_stuck_task appends to settle the Turn.
    done = {'type': 'done', 'error': task['error'], 'finishReason': 'error'}
    verdict = derive_task_verdict(task, done)
    assert verdict.status is TurnStatus.FAILED and verdict.cause == 'worker_lost'
    status, settlement = _settlement(task, done, {'content': 'ok', 'toolRounds': []})
    assert status == 'failed' and settlement['cause'] == 'worker_lost'
    assert settlement['error']['kind'] == 'worker_lost'
    assert 'terminated as wedged' in settlement['error']['detail']


# ─────────────────────────────────────────────────────────────────────────
# (ii) NEUTER — the load-bearing gate. Same wedged-looking task, but the
#      dispatch heartbeat is FRESH (a live slow / rate-limited dispatch). It
#      MUST NOT be reaped. Proves "no active dispatch" protects a slow turn.
# ─────────────────────────────────────────────────────────────────────────
def test_fresh_dispatch_heartbeat_exempts_slow_turn(reaper_env, put_task):
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'slow-alive-1',
        content='ok',
        events=[{'type': 'delta', 'seq': 0}],
        _t_last_event=stale,          # no events for 400s (silent)…
        _dispatch_heartbeat=now,      # …BUT a dispatch is in-flight right now
        created_at=stale,
    ))

    n = _reap()
    assert n == 0, 'a heartbeating (slow/rate-limited) turn must NOT be reaped'
    t = _get('slow-alive-1')
    assert t['status'] == 'running'
    assert t['aborted'] is False


def test_reaper_diagnostic_reports_silence_instead_of_task_age(reaper_env, put_task, monkeypatch, caplog):
    from lib.tasks_pkg.manager import _maintenance
    from types import SimpleNamespace

    now = time.time()
    monkeypatch.setattr(_maintenance, 'time', SimpleNamespace(time=lambda: now))
    put_task(_mk_task('wedged-diagnostic', created_at=now - 7200,
                      _t_last_event=now - 600, _dispatch_heartbeat=now - 400))

    assert _reap() == 1
    warnings = [record.getMessage() for record in caplog.records if 'WEDGED —' in record.getMessage()]
    assert len(warnings) == 1
    assert 'no event/dispatch progress for 400s' in warnings[0]


def test_fresh_event_clock_exempts_retrying_turn(reaper_env, put_task):
    """Symmetric neuter: retry phases keep _t_last_event fresh → not reaped."""
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'retrying-alive-1',
        _t_last_event=now,            # emitting retry/keepalive phases now
        _dispatch_heartbeat=stale,    # (heartbeat happens to be old)
        created_at=stale,
    ))
    assert _reap() == 0
    assert _get('retrying-alive-1')['status'] == 'running'


# ─────────────────────────────────────────────────────────────────────────
# (iii) Legacy subset: never produced anything, never dispatched. Both clocks
#       fall back to created_at → still reaped (the original behaviour).
# ─────────────────────────────────────────────────────────────────────────
def test_legacy_zero_output_still_reaped(reaper_env, put_task):
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'zero-output-1',
        content='', thinking='', events=[],
        created_at=stale,             # no _t_last_event / _dispatch_heartbeat set
    ))
    n = _reap()
    assert n == 1, 'the classic zero-output wedged task is still caught (subset)'
    t = _get('zero-output-1')
    assert t['status'] == 'error'
    assert t.get('_reap_had_output') is False


# ─────────────────────────────────────────────────────────────────────────
# (iv) Human-waiting task: ask_user / approval keeps the heartbeat fresh via
#      the human_guidance poll loop → never reaped even though it's silent.
# ─────────────────────────────────────────────────────────────────────────
def test_human_waiting_task_not_reaped(reaper_env, put_task):
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'human-wait-1',
        events=[{'type': 'phase', 'seq': 0}],  # emitted the ask_user prompt
        _t_last_event=stale,          # no new events while waiting on the user
        _dispatch_heartbeat=now,      # poll loop refreshes this every 2s
        created_at=stale,
    ))
    assert _reap() == 0
    assert _get('human-wait-1')['status'] == 'running'


# ─────────────────────────────────────────────────────────────────────────
# (iv-b) LONG WAITS (owner decision 2026-10-28). A blocking poll and the
#        run_command background handoff stay silent for their whole wait,
#        which starts with fresh clocks. reaper_safe_wait_s keeps that wait
#        short of the reaper at the default and at an operator threshold.
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('threshold', [None, '600', '3600'])
def test_longest_silent_tool_wait_is_never_reaped(monkeypatch, put_task, threshold):
    from lib.tasks_pkg.handlers._background_command import AUTO_BACKGROUND_AFTER_S
    from lib.tasks_pkg.manager import reaper_safe_wait_s
    from lib.tools.poll import POLL_WAIT_CAP_S
    if threshold is None:
        monkeypatch.delenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', raising=False)
    else:
        monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', threshold)
    longest = max(reaper_safe_wait_s(POLL_WAIT_CAP_S),
                  reaper_safe_wait_s(AUTO_BACKGROUND_AFTER_S))
    assert longest > 60
    started = time.time() - longest
    put_task(_mk_task('long-wait-1', events=[{'type': 'tool_start', 'seq': 0}],
                      _t_last_event=started, _dispatch_heartbeat=started,
                      created_at=started))
    assert _reap() == 0, 'a wait inside its declared bound is not a wedge'

    # NEUTER: the same silence one reaper threshold long IS reaped.
    threshold_s = int(threshold or 1800)
    expired = time.time() - threshold_s - 1
    put_task(_mk_task('long-wait-2', events=[{'type': 'tool_start', 'seq': 0}],
                      _t_last_event=expired, _dispatch_heartbeat=expired,
                      created_at=expired))
    assert _reap() == 1
    assert _get('long-wait-1')['status'] == 'running'


@pytest.mark.parametrize('threshold, expected', [
    (None, 1500.0), ('3600', 1500.0), ('1200', 900.0), ('200', 60.0), ('0', 1500.0)])
def test_reaper_safe_wait_bound(monkeypatch, threshold, expected):
    from lib.tasks_pkg.manager import reaper_safe_wait_s
    if threshold is None:
        monkeypatch.delenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', raising=False)
    else:
        monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', threshold)
    assert reaper_safe_wait_s(1500) == expected
    assert reaper_safe_wait_s(0.05) == 0.05


# ─────────────────────────────────────────────────────────────────────────
# Guards: not-yet-stale, already-terminal, already-aborted, disabled.
# ─────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────
# (v) EXPLAINED WAIT — the 2026-10-20 incident. A writer-admission wait has no
#     process and no output by design, so BOTH clocks are stale from the start.
#     Waiting is progress owned by another writer, never a wedge.
# ─────────────────────────────────────────────────────────────────────────
def test_task_waiting_for_writer_admission_not_reaped(reaper_env, put_task):
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'admission-wait-1',
        content='', thinking='', events=[],
        _t_last_event=stale,
        _dispatch_heartbeat=stale,
        created_at=stale,
        _workspaceAdmission={'since': stale, 'depth': 1, 'claimCount': 1},
    ))

    assert _reap() == 0, 'a queued writer is waiting, not wedged'
    t = _get('admission-wait-1')
    assert t['status'] == 'running'
    assert t['aborted'] is False


def test_task_waiting_for_storage_ack_not_reaped(reaper_env, put_task):
    """Parity: the pre-existing storage wait keeps the same exemption."""
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'storage-wait-1',
        _t_last_event=stale,
        _dispatch_heartbeat=stale,
        created_at=stale,
        _storageWaits={'append': 1},
    ))

    assert _reap() == 0
    assert _get('storage-wait-1')['status'] == 'running'


# ─────────────────────────────────────────────────────────────────────────
# (vi) FLOW PARENT BLOCKED ON A CHILD — the 2026-11-01 incident
#      (conv mvxv0vfzaimb4j). The child sat in one silent 31-minute command;
#      child events are the only thing that advance the parent's clocks, so
#      the parent was force-failed on the same tick the reaper correctly
#      interrupted the child's command, killing the whole turn.
# ─────────────────────────────────────────────────────────────────────────
def _flow_parent_and_child(stale, **child_fields):
    from lib.tasks_pkg.explained_wait import CHILD_INVOCATION_WAIT_KEY
    child = _mk_task('flow-child-1', _parentTaskId='flow-parent-1',
                     events=[{'type': 'tool_start', 'seq': 0}],
                     _t_last_event=stale, _dispatch_heartbeat=stale,
                     created_at=stale, **child_fields)
    parent = _mk_task('flow-parent-1', events=[{'type': 'phase', 'seq': 0}],
                      _t_last_event=stale, _dispatch_heartbeat=stale,
                      created_at=stale)
    parent[CHILD_INVOCATION_WAIT_KEY] = {child['id']: child}
    return parent, child


def test_flow_parent_waiting_on_silent_child_command_not_reaped(
        reaper_env, put_task, monkeypatch):
    from lib.project_mod import foreground_command_slots
    stale = time.time() - 400
    parent, child = _flow_parent_and_child(stale)
    put_task(parent)
    put_task(child)
    monkeypatch.setattr(
        foreground_command_slots, 'foreground_command_silences',
        lambda task, now: [(64635, 400.0)] if task is child else [])

    assert _reap() == 0
    assert _get('flow-parent-1')['status'] == 'running'
    # The child is still judged on its own: its silent command is interrupted
    # and the turn continues with partial output.
    assert child['_cmd_interrupt']['pid'] == 64635
    assert child['status'] == 'running'


def test_flow_parent_of_settled_child_is_judged_by_its_own_clocks(
        reaper_env, put_task):
    """NEUTER: once the child is reaped/aborted, a still-blocked parent is a wedge."""
    stale = time.time() - 400
    parent, child = _flow_parent_and_child(stale, status='error', aborted=True)
    put_task(parent)

    assert _reap() == 1
    assert _get('flow-parent-1')['_abort_reason'] == 'stuck_no_progress'

def test_young_task_not_reaped(reaper_env, put_task):
    now = time.time()
    put_task(_mk_task(
        'young-1',
        content='ok', events=[{'type': 'delta', 'seq': 0}],
        _t_last_event=now - 100,      # < N
        _dispatch_heartbeat=now - 100,
        created_at=now - 100,
    ))
    assert _reap() == 0
    assert _get('young-1')['status'] == 'running'


def test_already_terminal_not_touched(reaper_env, put_task):
    now = time.time()
    stale = now - 400
    put_task(_mk_task(
        'done-1', status='done', content='answer',
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale,
    ))
    assert _reap() == 0
    assert _get('done-1')['status'] == 'done'


def test_disabled_when_threshold_zero(monkeypatch, put_task):
    monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '0')
    now = time.time()
    stale = now - 100000
    put_task(_mk_task(
        'wedged-but-disabled-1',
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale,
    ))
    assert _reap() == 0
    assert _get('wedged-but-disabled-1')['status'] == 'running'


# ─────────────────────────────────────────────────────────────────────────
# (vii) ONE RULE FOR ROOT AND CHILD — the 2026-11-02 incident (conv
#       mvxih2silbup7r). Sub-agents were judged by a private 900s beacon rule
#       that killed the whole agent, while the root rule waited 1800s and
#       interrupted a silent command instead; a 27-minute filesystem freeze
#       then killed five healthy workers the moment their I/O returned.
# ─────────────────────────────────────────────────────────────────────────
def _child_task(task_id, stale, **fields):
    import threading
    from lib.tasks_pkg.invocation import InvocationPolicy
    return _mk_task(
        task_id, abort_event=threading.Event(), _parentTaskId='swarm-parent',
        _invocationPolicy=InvocationPolicy.child('swarm-parent'),
        events=[{'type': 'tool_start', 'seq': 0}],
        _t_last_event=stale, _dispatch_heartbeat=stale, created_at=stale, **fields)


def test_wedged_child_gets_its_own_typed_stall_not_root_finalization(
        reaper_env, put_task, monkeypatch):
    from lib.tasks_pkg.manager import _maintenance
    finalized = []
    monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task', finalized.append)
    child = _child_task('wedged-child-1', time.time() - 400)
    put_task(child)

    assert _reap() == 1
    assert child['_invocationStopReason'] == 'stalled'
    assert child['aborted'] is True and child['abort_event'].is_set()
    assert 'stall threshold 300s' in child['error']['detail']
    assert child['_invocationStopEvidence'] == {
        'silentSeconds': pytest.approx(400, abs=5), 'stallTimeoutSeconds': 300.0}
    # Its invocation owner settles it and reports to the parent; a child owns
    # no conversation Turn to finalize from outside.
    assert finalized == []
    assert child['status'] == 'running'
    assert child.get('_abort_reason') != 'stuck_no_progress'


def test_child_silent_command_is_interrupted_like_root(reaper_env, put_task, monkeypatch):
    from lib.project_mod import foreground_command_slots
    child = _child_task('child-cmd-1', time.time() - 400)
    put_task(child)
    monkeypatch.setattr(
        foreground_command_slots, 'foreground_command_silences',
        lambda task, now: [(4242, 400.0)] if task is child else [])

    assert _reap() == 0
    assert child['_cmd_interrupt']['pid'] == 4242
    assert child['aborted'] is False and '_invocationStopReason' not in child


def test_owner_stop_wins_over_a_concurrent_stall_verdict(reaper_env, put_task):
    child = _child_task('child-stop-1', time.time() - 400,
                        _invocationAbortCheck=lambda: True)
    put_task(child)

    assert _reap() == 0
    assert child['aborted'] is True
    assert '_invocationStopReason' not in child


def test_stale_filesystem_mount_reaps_nothing(reaper_env, put_task, monkeypatch):
    import lib.fs_keepalive as fs_keepalive
    from lib.tasks_pkg.liveness import EXPLAINED_AT_KEY
    stale = time.time() - 4000
    root = _mk_task('frozen-root', _t_last_event=stale, _dispatch_heartbeat=stale,
                    created_at=stale)
    child = _child_task('frozen-child', stale)
    put_task(root)
    put_task(child)
    monkeypatch.setattr(fs_keepalive, 'filesystem_unresponsive', lambda: True)

    assert _reap() == 0
    assert root['status'] == child['status'] == 'running'
    assert not root['aborted'] and not child['aborted']
    assert root[EXPLAINED_AT_KEY] > stale and child[EXPLAINED_AT_KEY] > stale


def test_silence_counts_only_from_filesystem_recovery(reaper_env, put_task, monkeypatch):
    """The 2026-11-02 kills landed the moment I/O returned: silence spanning
    the freeze must not count once the mount answers again."""
    import lib.fs_keepalive as fs_keepalive
    now = time.time()
    stale = now - 4000
    root = _mk_task('thawed-root', _t_last_event=stale, _dispatch_heartbeat=stale,
                    created_at=stale)
    child = _child_task('thawed-child', stale)
    put_task(root)
    put_task(child)
    monkeypatch.setattr(fs_keepalive, 'filesystem_resumed_at',
                        lambda: (time.monotonic() - 60, now - 60))
    assert _reap() == 0
    assert not root['aborted'] and not child['aborted']

    # NEUTER: a recovery long ago forgives nothing since.
    monkeypatch.setattr(fs_keepalive, 'filesystem_resumed_at',
                        lambda: (time.monotonic() - 3000, now - 3000))
    assert _reap() == 2
    assert root['_abort_reason'] == 'stuck_no_progress'
    assert child['_invocationStopReason'] == 'stalled'


def test_silence_restarts_after_an_explained_wait_ends(reaper_env, put_task):
    """A task that waited 40 minutes on storage is not wedged at the ACK."""
    stale = time.time() - 2400
    task = _mk_task('waited-1', _t_last_event=stale, _dispatch_heartbeat=stale,
                    created_at=stale, _storageWaits={'append': 1})
    put_task(task)

    assert _reap() == 0
    task['_storageWaits'] = {}
    assert _reap() == 0, 'a just-acknowledged wait is not a wedge'
    assert task['status'] == 'running' and task['aborted'] is False


def test_coordinator_status_read_wedge_is_reaped_within_one_silence_threshold(
        reaper_env, put_task):
    """A delegated wait explains nothing: the completed observation is the clock.

    N11 (2026-11-04/05): a Coordinator parked in await_project_workers was
    force-failed as worker_lost. Its per-poll heartbeat
    (fdffeb72a4, live since 2026-11-05 09:59) owns that silence, exactly like a
    ratified human wait. This spec binds the other half: the moment a status
    read stops completing, the wait is an ordinary silence and the reaper owns
    it — it must not be extended by a declared-wait marker.
    """
    from lib.tasks_pkg.liveness import judge
    now = time.time()
    stale = now - 400
    task = _mk_task('coordinator-await-1',
                    content='Delegating stage 2', thinking='',
                    events=[{'type': 'tool_start', 'seq': 0}],
                    _t_last_event=stale, _dispatch_heartbeat=stale,
                    created_at=stale)
    put_task(task)
    # While the wait keeps observing Workers, its own clock is fresh: not reaped.
    task['_dispatch_heartbeat'] = time.time()
    assert judge(task, time.time(), reaper_env, commands=[]).state == 'alive'
    assert _reap() == 0
    assert _get('coordinator-await-1')['status'] == 'running'
    # The status read then wedges: one threshold after the last completed
    # observation, and no later, the Turn is force-failed.
    task['_dispatch_heartbeat'] = time.time() - reaper_env - 1
    task['_t_last_event'] = task['created_at'] = time.time() - reaper_env - 1
    assert _reap() == 1, 'a blocked status read is a wedge, not a wait'
    assert _get('coordinator-await-1')['aborted'] is True


def test_task_waiting_on_a_claimed_device_command_not_reaped(reaper_env, put_task):
    from lib.desktop import bridge
    from lib.tasks_pkg.explained_wait import device_wait
    now = time.time()
    stale = now - 400
    task = _mk_task('device-wait-1', _t_last_event=stale, _dispatch_heartbeat=stale,
                    created_at=stale)
    cmd = {'id': 'cmd-reaper-1', 'user_id': '1', 'claimed_agent_id': 'dev-1'}
    bridge.command_queue['cmd-reaper-1'] = cmd
    bridge._agents['dev-1'] = {'agent_id': 'dev-1', 'user_id': '1', 'last_seen': now,
                               'active_command_ids': {'cmd-reaper-1'}}
    try:
        with device_wait(task, cmd):
            put_task(task)
            assert _reap() == 0, 'a claimed command on a connected device is not a wedge'
            assert _get('device-wait-1')['status'] == 'running'
    finally:
        bridge.command_queue.pop('cmd-reaper-1', None)
        bridge._agents.pop('dev-1', None)


def test_unclaimed_device_command_stays_reapable(reaper_env, put_task):
    """No device owns an unclaimed command, so its silence is not explained."""
    from lib.tasks_pkg.explained_wait import device_wait
    now = time.time()
    stale = now - 400
    task = _mk_task('device-unclaimed-1', _t_last_event=stale, _dispatch_heartbeat=stale,
                    created_at=stale)
    with device_wait(task, {'id': 'cmd-reaper-2', 'user_id': '1', 'claimed_agent_id': None}):
        put_task(task)
        assert _reap() == 1
        assert _get('device-unclaimed-1')['aborted'] is True
