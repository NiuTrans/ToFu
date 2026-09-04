"""Liveness contract: a live project task keeps its presence peer ACTIVE.

Pins the 2026-08-29 collaboration-view wedge: presence heartbeats only rode
the text stream, so a conversation inside a >ACTIVE_TTL_SEC tool-execution
window flipped idle and disappeared from ``project_peer_status`` while its
task was genuinely running (sidebar still showed it responding). The
keepalive refreshes liveness straight from the task registry.
"""

from __future__ import annotations

import threading
import time

import pytest

import lib.presence.registry as registry
import lib.tasks_pkg.manager._presence_keepalive as keepalive

pytestmark = pytest.mark.unit

OWNER = 11


@pytest.fixture
def fresh_presence(monkeypatch):
    monkeypatch.setattr(registry, '_state', {})
    monkeypatch.setattr(registry, '_sweeper_started', True)
    monkeypatch.setattr(registry, '_broadcast', lambda *_a, **_k: None)
    return registry


@pytest.fixture
def keepalive_stopped():
    yield
    keepalive.stop_keepalive(timeout=1.0)


def _task(*, status='running', aborted=False, conv_id='conv-live',
          project_path='', user_id=OWNER):
    return {
        'id': 'task-keepalive-1',
        'status': status,
        'aborted': aborted,
        'convId': conv_id,
        '_userId': user_id,
        'config': {'projectPath': project_path},
    }


def test_interval_stays_below_presence_active_ttl():
    # The whole mechanism rests on this inequality: with ticks at least this
    # often, a peer's lastBeatTs can never age past the TTL mid-tool-call.
    assert 0 < keepalive.KEEPALIVE_INTERVAL_SEC < registry.ACTIVE_TTL_SEC


def test_live_task_refreshes_liveness_beyond_active_ttl(
        fresh_presence, tmp_path):
    root = str(tmp_path / 'project')
    registry.announce(root, 'conv-live', user_id=OWNER, phase='working')
    peer = registry._state[(OWNER, root)]['conv-live']
    # Reproduce the wedge: a long tool call streamed nothing, so the last
    # heartbeat aged past the TTL and the peer reads idle (snapshot drops it).
    peer['lastBeatTs'] = int(
        (time.time() - registry.ACTIVE_TTL_SEC - 5) * 1000)
    assert registry.snapshot(root, user_id=OWNER)['peers'] == []

    eligible, refreshed = keepalive._tick_once(
        tasks=[_task(project_path=root)])

    assert (eligible, refreshed) == (1, 1)
    peers = registry.snapshot(root, user_id=OWNER)['peers']
    assert [p['convId'] for p in peers] == ['conv-live']
    assert peers[0]['status'] == 'active'
    # Liveness-only refresh: the phase the last genuine signal set survives.
    assert peers[0]['phase'] == 'working'


def test_tick_skips_non_live_or_projectless_tasks(fresh_presence, tmp_path):
    root = str(tmp_path / 'project')
    registry.announce(root, 'conv-live', user_id=OWNER)
    peer = registry._state[(OWNER, root)]['conv-live']
    peer['lastBeatTs'] = int(
        (time.time() - registry.ACTIVE_TTL_SEC - 5) * 1000)
    stale_before = peer['lastBeatTs']

    tasks = [
        _task(status='succeeded', project_path=root),   # terminal
        _task(aborted=True, project_path=root),          # aborted
        _task(project_path=''),                          # no project attached
        _task(project_path=root, conv_id=''),            # no conversation
        'not-a-task-dict',
    ]
    eligible, refreshed = keepalive._tick_once(tasks=tasks)

    assert (eligible, refreshed) == (0, 0)
    assert peer['lastBeatTs'] == stale_before
    assert registry.snapshot(root, user_id=OWNER)['peers'] == []


def test_tick_heartbeat_failure_isolates_one_task(fresh_presence, tmp_path):
    root = str(tmp_path / 'project')
    registry.announce(root, 'conv-good', user_id=OWNER)
    good = _task(project_path=root, conv_id='conv-good')
    bad = _task(project_path=root, conv_id='conv-bad', user_id=0)

    eligible, refreshed = keepalive._tick_once(tasks=[bad, good])

    # The invalid-owner task fails its heartbeat without stopping the pass.
    assert (eligible, refreshed) == (2, 1)


def test_ensure_started_is_idempotent_and_stop_joins(keepalive_stopped):
    assert keepalive.ensure_started(interval=60.0) is True
    first = keepalive._thread
    assert first is not None and first.is_alive()
    assert keepalive.ensure_started(interval=60.0) is False
    assert keepalive._thread is first

    assert keepalive.stop_keepalive(timeout=2.0) is True
    assert keepalive._thread is None
    assert keepalive.stop_keepalive(timeout=2.0) is True


def test_loop_retires_when_no_live_project_tasks(monkeypatch):
    monkeypatch.setattr(keepalive, '_stop', threading.Event())
    monkeypatch.setattr(keepalive, '_tick_once', lambda tasks=None: (0, 0))
    monkeypatch.setattr(keepalive, '_live_project_task_count', lambda: 0)
    current = threading.current_thread()
    monkeypatch.setattr(keepalive, '_thread', current)

    keepalive._loop(0)

    assert keepalive._thread is None


def test_retirement_cannot_detach_a_newer_owner(monkeypatch):
    newer_owner = object()
    monkeypatch.setattr(keepalive, '_thread', newer_owner)
    monkeypatch.setattr(keepalive, '_live_project_task_count', lambda: 0)

    assert keepalive._retire_if_idle(threading.current_thread()) is False
    assert keepalive._thread is newer_owner


def test_retirement_waits_while_live_tasks_remain(monkeypatch):
    current = threading.current_thread()
    monkeypatch.setattr(keepalive, '_thread', current)
    monkeypatch.setattr(keepalive, '_live_project_task_count', lambda: 2)

    assert keepalive._retire_if_idle(current) is False
    assert keepalive._thread is current


def test_failed_live_count_probe_blocks_retirement(monkeypatch):
    current = threading.current_thread()
    monkeypatch.setattr(keepalive, '_thread', current)
    monkeypatch.setattr(keepalive, '_live_project_task_count', lambda: -1)

    assert keepalive._retire_if_idle(current) is False
    assert keepalive._thread is current
