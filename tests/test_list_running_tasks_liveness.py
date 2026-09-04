"""tests/test_list_running_tasks_liveness.py — the restart guard counts only
CONVERSATIONS with genuinely-live work.

Root cause this guards against: ``list_running_tasks`` (the self-update restart
guard's source) filtered on ``status=='running' and not aborted`` ONLY — no
liveness check, no per-conversation dedup. Two consequences:

  * A task whose worker thread died stays ``status='running'`` in memory until
    the reaper flips it terminal. With the reaper's 30-min threshold, a
    just-died zombie was counted as "running" for that whole window, so the
    guard blocked a restart for work that was not actually running (the "63
    other conversations have running tasks" phantom, even with an idle server).
  * Counting per-task, not per-conversation: one autopilot conversation spawns
    dozens of tasks, so "3 busy convs" was reported as "63".

The fix adds (a) an activity filter using the SAME dual-clock predicate the
reaper uses (``_t_last_event`` AND ``_dispatch_heartbeat`` both stale past
``_stuck_task_max_silent_secs()`` → wedged → excluded) and (b) per-convId dedup.

These tests drive ``list_running_tasks()`` directly against synthetic in-memory
tasks. They include the REVERSE assertions the fix must never break: a task
with EITHER clock fresh is still counted (we did not silence real work), and
same-conv tasks collapse to one entry.
"""

import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _mk(tid, conv, *, created, last_event=None, heartbeat=None,
        status='running', aborted=False):
    """Build a synthetic task dict shaped like the registry's live tasks."""
    return {
        'id': tid,
        'convId': conv,
        'status': status,
        'aborted': aborted,
        'created_at': created,
        '_t_last_event': created if last_event is None else last_event,
        '_dispatch_heartbeat': created if heartbeat is None else heartbeat,
        'events': [],
        'events_lock': threading.Lock(),
    }


def _install(monkeypatch, task_list, *, max_silent=300):
    """Install a public-runtime snapshot and pin the liveness threshold."""
    import lib.tasks_pkg.manager._maintenance as _maintenance
    import lib.tasks_pkg.manager._registry as _registry
    runtime = SimpleNamespace(snapshot=lambda: list(task_list))
    monkeypatch.setattr(_registry, 'chat_task_runtime', runtime, raising=True)
    monkeypatch.setattr(_maintenance, '_stuck_task_max_silent_secs',
                        lambda: max_silent, raising=True)


# ─────────────────────────────────────────────────────────────────────────
# Activity filter — the direct cause of the "63 while idle" false positive.
# ─────────────────────────────────────────────────────────────────────────
def test_wedged_task_excluded(monkeypatch):
    """Both clocks stale past threshold → wedged → NOT counted."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    stale = now - 400  # > 300s threshold on both clocks
    _install(monkeypatch, [_mk('z1', 'convDead', created=stale)])
    assert list_running_tasks() == [], \
        'a wedged running task must not block a restart'


def test_old_pending_queue_resident_is_still_live(monkeypatch):
    """Queue residence is not worker silence; pending work blocks restart."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    stale = now - 400
    _install(monkeypatch, [
        _mk('queued', 'convQueued', created=stale, status='pending'),
    ])
    assert [item['convId'] for item in list_running_tasks()] == ['convQueued']


def test_fresh_event_clock_still_counted(monkeypatch):
    """REVERSE assertion: heartbeat stale but events fresh → ALIVE → counted.

    A rate-limited-but-alive turn keeps emitting retry/waiting phases → its
    _t_last_event stays fresh even if dispatch is quiet. Must be protected.
    """
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    t = _mk('a1', 'convAlive', created=now - 500,
            last_event=now - 5,      # fresh
            heartbeat=now - 400)     # stale
    _install(monkeypatch, [t])
    out = list_running_tasks()
    assert [e['convId'] for e in out] == ['convAlive'], \
        'either clock fresh must keep the task counted'


def test_fresh_heartbeat_clock_still_counted(monkeypatch):
    """REVERSE assertion: events stale but heartbeat fresh → ALIVE → counted.

    A turn stuck in a live socket read / long tool call emits no event but IS
    heartbeating (also covers human-input waits). Must be protected.
    """
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    t = _mk('b1', 'convBusy', created=now - 500,
            last_event=now - 400,    # stale
            heartbeat=now - 5)       # fresh
    _install(monkeypatch, [t])
    out = list_running_tasks()
    assert [e['convId'] for e in out] == ['convBusy'], \
        'a live-but-silent (heartbeating) task must stay counted'


def test_reaper_disabled_skips_activity_filter(monkeypatch):
    """max_silent<=0 disables the reaper → nothing treated as wedged (mirrors
    the reaper), so even a very old task is still counted."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    t = _mk('c1', 'convOld', created=now - 100000)
    _install(monkeypatch, [t], max_silent=0)
    out = list_running_tasks()
    assert [e['convId'] for e in out] == ['convOld'], \
        'with the reaper disabled no task is wedged; all live rows counted'


# ─────────────────────────────────────────────────────────────────────────
# Per-conversation dedup — "63 tasks" was really a handful of convs.
# ─────────────────────────────────────────────────────────────────────────
def test_same_conv_tasks_deduped(monkeypatch):
    """Many live tasks under ONE conv → a single counted entry."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    tasks = [_mk(f'ap{i}', 'convAuto', created=now - i, last_event=now - 1)
             for i in range(30)]
    _install(monkeypatch, tasks)
    out = list_running_tasks()
    assert len(out) == 1, 'a conversation with 30 live tasks counts once'
    assert out[0]['convId'] == 'convAuto'


def test_distinct_convs_not_deduped(monkeypatch):
    """Different convs stay distinct."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    tasks = [_mk('x1', 'convA', created=now - 1, last_event=now - 1),
             _mk('x2', 'convB', created=now - 1, last_event=now - 1)]
    _install(monkeypatch, tasks)
    out = list_running_tasks()
    assert {e['convId'] for e in out} == {'convA', 'convB'}


def test_convid_less_tasks_each_distinct(monkeypatch):
    """Headless/external tasks (no convId) must NOT collapse into one another."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    tasks = [_mk('h1', '', created=now - 1, last_event=now - 1),
             _mk('h2', '', created=now - 1, last_event=now - 1)]
    _install(monkeypatch, tasks)
    out = list_running_tasks()
    assert len(out) == 2, 'convId-less tasks are keyed by task id, not merged'
    assert {e['taskId'] for e in out} == {'h1', 'h2'}


def test_dedup_representative_is_earliest(monkeypatch):
    """The kept entry for a conv is its earliest-created live task."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    tasks = [_mk('late', 'convR', created=now - 10, last_event=now - 1),
             _mk('early', 'convR', created=now - 900, last_event=now - 1)]
    _install(monkeypatch, tasks)
    out = list_running_tasks()
    assert len(out) == 1
    assert out[0]['taskId'] == 'early', \
        'representative must be the oldest live task of the conversation'


# ─────────────────────────────────────────────────────────────────────────
# Interaction: exclude_conv_id (self) + dedup + activity, together.
# ─────────────────────────────────────────────────────────────────────────
def test_exclude_own_conv_with_dedup_and_wedged(monkeypatch):
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    tasks = [
        _mk('self1', 'me', created=now - 2, last_event=now - 1),      # excluded (self)
        _mk('self2', 'me', created=now - 3, last_event=now - 1),      # excluded (self)
        _mk('sibA1', 'sibA', created=now - 5, last_event=now - 1),    # live sibling
        _mk('sibA2', 'sibA', created=now - 6, last_event=now - 1),    # same conv → dedup
        _mk('deadB', 'sibB', created=now - 500),                      # wedged → excluded
    ]
    _install(monkeypatch, tasks)
    out = list_running_tasks(exclude_conv_id='me')
    assert [e['convId'] for e in out] == ['sibA'], \
        'own conv excluded, sibA deduped to one, wedged sibB dropped'
