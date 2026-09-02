"""tests/test_stuck_task_reaper.py — the generalized stuck/wedged-task reaper.

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
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    from lib.tasks_pkg.manager import _maintenance

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
# Guards: not-yet-stale, already-terminal, already-aborted, disabled.
# ─────────────────────────────────────────────────────────────────────────
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
