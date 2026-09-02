"""tests/test_carrier_task_lifecycle.py — the "carrier task" predicate is the
SINGLE SOURCE OF TRUTH that keeps the restart-guard count and the reconnect
view from diverging.

Root cause this guards against (the "sidebar shows nothing running, but the
restart dialog says N conversations are running" report):

  * The autopilot virtual-user (VU) sub-task is created with ``create_task('')``
    and runs synchronously under ``_flow_managed=True``, which SUPPRESSES
    the orchestrator's terminal-status flip + ``persist_task_result``. So it
    never reaches a terminal status on its own and, before this fix, lingered
    in the registry as ``status='running'`` until the 30-min stuck-task reaper.
  * ``GET /api/chat/active`` (the reconnect view the frontend trusts) hid such
    carriers via an inline ``_inline_messages``/``_vu_subtask`` check, but
    ``list_running_tasks`` (the self-update restart guard) did NOT — so the two
    backends disagreed about whether a carrier counts as a running conversation.

The fix:
  1. ``is_carrier_task(task)`` — one predicate, consulted by BOTH backends.
  2. ``list_running_tasks`` skips carriers (parity with ``/api/chat/active``).
  3. ``run_virtual_user`` discards the VU carrier in a ``finally`` the moment
     its synchronous turn returns (mirrors the reporter-carrier contract).

These tests drive ``list_running_tasks()`` directly against synthetic in-memory
tasks and include NEGATIVE CONTROLS (documented NEUTER toggles): flip the
carrier filter off and the guard counts the invisible carrier again.
"""

import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _mk(tid, conv, *, created, last_event=None, heartbeat=None,
        status='running', aborted=False, **flags):
    """Build a synthetic task dict shaped like the registry's live tasks."""
    t = {
        'id': tid,
        'convId': conv,
        '_userId': 1,
        'status': status,
        'aborted': aborted,
        'created_at': created,
        '_t_last_event': created if last_event is None else last_event,
        '_dispatch_heartbeat': created if heartbeat is None else heartbeat,
        'events': [],
        'events_lock': threading.Lock(),
    }
    t.update(flags)
    return t


def _install(monkeypatch, task_list, *, max_silent=300):
    """Install a public-runtime snapshot and pin the liveness threshold."""
    import lib.tasks_pkg.manager._maintenance as _maintenance
    import lib.tasks_pkg.manager._registry as _registry
    runtime = SimpleNamespace(snapshot=lambda: list(task_list))
    monkeypatch.setattr(_registry, 'chat_task_runtime', runtime, raising=True)
    monkeypatch.setattr(_maintenance, '_stuck_task_max_silent_secs',
                        lambda: max_silent, raising=True)


# ─────────────────────────────────────────────────────────────────────────
# is_carrier_task — the predicate itself.
# ─────────────────────────────────────────────────────────────────────────
def test_predicate_classifies_carriers():
    from lib.tasks_pkg.manager import is_carrier_task
    now = time.time()
    assert is_carrier_task(_mk('v', 'c', created=now, _vu_subtask=True)) is True
    assert is_carrier_task(_mk('i', 'c', created=now, _inline_messages=True)) is True
    # A real streaming task is NOT a carrier.
    assert is_carrier_task(_mk('r', 'c', created=now)) is False
    # The autopilot-KICK carrier is a REAL streaming task — must stay reconnectable.
    assert is_carrier_task(_mk('k', 'c', created=now, _autopilot_kick=True)) is False


# ─────────────────────────────────────────────────────────────────────────
# list_running_tasks — carriers are not counted as running conversations.
# ─────────────────────────────────────────────────────────────────────────
def test_vu_carrier_not_counted(monkeypatch):
    """A live VU sub-task (fresh clocks, convId='') must NOT be counted — it is
    invisible to the frontend, so it must not block/gate a restart."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    _install(monkeypatch, [
        _mk('vu1', '', created=now - 2, last_event=now - 1, _vu_subtask=True),
    ])
    assert list_running_tasks() == [], \
        'a fresh VU carrier must not count as a running conversation'


def test_inline_carrier_not_counted(monkeypatch):
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    _install(monkeypatch, [
        _mk('inl1', '', created=now - 2, last_event=now - 1, _inline_messages=True),
    ])
    assert list_running_tasks() == []


def test_real_task_beside_carrier_still_counted(monkeypatch):
    """REVERSE assertion: a real streaming task in the SAME sweep is still
    counted — the carrier filter must not silence genuine work."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    _install(monkeypatch, [
        _mk('vu1', '', created=now - 2, last_event=now - 1, _vu_subtask=True),
        _mk('real', 'convReal', created=now - 3, last_event=now - 1),
    ])
    out = list_running_tasks()
    assert [e['convId'] for e in out] == ['convReal'], \
        'only the real conversation is counted; the VU carrier is skipped'


def test_kick_carrier_still_counted(monkeypatch):
    """The autopilot-KICK carrier is a real streaming task → still counted."""
    from lib.tasks_pkg.manager import list_running_tasks
    now = time.time()
    _install(monkeypatch, [
        _mk('kick', 'convKick', created=now - 2, last_event=now - 1,
            _autopilot_kick=True),
    ])
    out = list_running_tasks()
    assert [e['convId'] for e in out] == ['convKick']


# ─────────────────────────────────────────────────────────────────────────
# NEGATIVE CONTROL (NEUTER): if the carrier filter is bypassed, the invisible
# VU carrier is counted again — proving the filter is load-bearing.
# ─────────────────────────────────────────────────────────────────────────
def test_neuter_carrier_filter_recounts_carrier(monkeypatch):
    import lib.tasks_pkg.manager._registry as _registry
    now = time.time()
    # Neuter: make the predicate always say "not a carrier".
    monkeypatch.setattr(_registry, 'is_carrier_task', lambda t: False, raising=True)
    _install(monkeypatch, [
        _mk('vu1', '', created=now - 2, last_event=now - 1, _vu_subtask=True),
    ])
    out = _registry.list_running_tasks()
    assert len(out) == 1 and out[0]['taskId'] == 'vu1', \
        ('with the carrier filter neutered the invisible VU carrier is counted '
         'again — the real filter is load-bearing')


# ─────────────────────────────────────────────────────────────────────────
# run_virtual_user discards the VU carrier from the registry in a finally,
# even when _run_single_turn raises (leak-proof lifecycle).
# ─────────────────────────────────────────────────────────────────────────
def _base_vu_task():
    now = time.time()
    return {
        'id': 'parent-vu', 'convId': 'convParent', '_userId': 1,
        'status': 'running', 'aborted': False, 'created_at': now,
        'messages': [{'role': 'user', 'content': 'hi'},
                     {'role': 'assistant', 'content': 'done'}],
        'config': {'model': 'm'},
        'events': [], 'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
    }


def test_vu_carrier_discarded_on_normal_return(monkeypatch):
    import lib.tasks_pkg.autopilot as ap
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks

    seen = {}
    with tasks_lock:
        baseline_carriers = {
            tid for tid, item in tasks.items() if item.get('_vu_subtask')
        }

    def _fake_single_turn(sub_task, messages_override=None):
        # The carrier must be registered WHILE it runs.
        with tasks_lock:
            seen['present_during'] = sub_task['id'] in tasks
        sub_task['content'] = 'keep going'
        return {'content': 'keep going', 'error': None, 'thinking': '',
                'usage': {}, 'finishReason': 'stop',
                'messages': list(sub_task.get('messages') or [])}

    import lib.tasks_pkg.orchestrator._turn as orch
    monkeypatch.setattr(orch, '_run_single_turn', _fake_single_turn, raising=True)
    # Neutralize the objective-persist DB hop + segment assembly noise.
    monkeypatch.setattr(ap, '_get_or_persist_objective', lambda c, m, *, user_id: '', raising=True)

    task = _base_vu_task()
    ap.run_virtual_user(task)

    assert seen.get('present_during') is True, \
        'carrier must be registered while _run_single_turn runs'
    with tasks_lock:
        leaked = [
            tid for tid, item in tasks.items()
            if item.get('_vu_subtask') and tid not in baseline_carriers
        ]
    assert leaked == [], \
        'the VU carrier must be discarded from the registry after the turn'


def test_vu_carrier_discarded_even_when_turn_raises(monkeypatch):
    import lib.tasks_pkg.autopilot as ap
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks

    def _boom(sub_task, messages_override=None):
        raise RuntimeError('turn blew up')

    import lib.tasks_pkg.orchestrator._turn as orch
    monkeypatch.setattr(orch, '_run_single_turn', _boom, raising=True)
    monkeypatch.setattr(ap, '_get_or_persist_objective', lambda c, m, *, user_id: '', raising=True)

    with tasks_lock:
        baseline_carriers = {
            tid for tid, item in tasks.items() if item.get('_vu_subtask')
        }
    task = _base_vu_task()
    result = ap.run_virtual_user(task)

    assert result is None, 'a raising VU turn stops the loop (returns None)'
    with tasks_lock:
        leaked = [
            tid for tid, item in tasks.items()
            if item.get('_vu_subtask') and tid not in baseline_carriers
        ]
    assert leaked == [], \
        'the VU carrier must be discarded even when the turn raises'
