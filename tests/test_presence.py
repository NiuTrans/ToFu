"""tests/test_presence.py — cross-conversation live-presence registry.

Covers lib/presence (slice 1, backend-only):

  • announce / heartbeat / record_files / mark_idle / depart mutate the
    in-memory authoritative state AND write through to
    <root>/.tofu/presence/registry.json atomically.
  • The backend computes status (active|idle) from the heartbeat age — the
    frontend never derives liveness from mere presence.
  • Overlap detection is notify-only and forms the full advisory string.
  • The sweep transitions active→idle→reaped (ghost cleanup).
  • Startup reconciliation reaps ghost peers a crashed server left "active".

The push hub is stubbed so we can assert the exact presence frames broadcast
without a live WebSocket. The disk mirror uses a tmp project root.
"""

from __future__ import annotations

import importlib
import json
import os
import time

import pytest

import lib.presence.registry as reg

pytestmark = pytest.mark.unit


@pytest.fixture
def captured_broadcasts(monkeypatch):
    """Capture every presence frame the registry broadcasts."""
    frames: list[dict] = []

    def _fake_push_event(channel, task_id, payload):
        frames.append({'channel': channel, 'taskId': task_id, **payload})

    # The registry imports push_event lazily inside _broadcast; patch the
    # source module so the lazy import resolves to our stub.
    import lib.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _fake_push_event)
    return frames


@pytest.fixture
def fresh_state(monkeypatch):
    """Reset the module-global registry state between tests."""
    monkeypatch.setattr(reg, '_state', {})
    monkeypatch.setattr(reg, '_sweeper_started', True)  # don't spawn a thread
    return reg


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / 'proj')


# ── status computation (backend-owned) ──

def test_announce_creates_active_peer_and_persists(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='task-1', title='Fix the parser',
                 objective='make it ship', phase='working')
    snap = reg.snapshot(root)
    assert len(snap['peers']) == 1
    peer = snap['peers'][0]
    assert peer['convId'] == 'conv-a'
    assert peer['status'] == 'active'          # backend-computed
    assert peer['statusLabel'] == 'working'
    assert peer['title'] == 'Fix the parser'

    # Disk mirror written under .tofu/presence/.
    path = os.path.join(os.path.abspath(root), '.tofu', 'presence', 'registry.json')
    assert os.path.exists(path)
    disk = json.loads(open(path).read())
    assert disk['peers'][0]['convId'] == 'conv-a'

    # Broadcast an 'update' frame to all clients.
    update = [f for f in captured_broadcasts if f.get('kind') == 'update']
    assert update and update[-1]['taskId'] == '*'
    assert update[-1]['peer']['status'] == 'active'


def test_peer_goes_idle_after_active_ttl(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1')
    # Backdate the heartbeat beyond ACTIVE_TTL.
    reg._state[os.path.abspath(root)]['conv-a']['lastBeatTs'] = int(
        (time.time() - reg.ACTIVE_TTL_SEC - 5) * 1000)
    # snapshot only returns ACTIVE peers → the idle one is filtered out.
    assert reg.snapshot(root)['peers'] == []


def test_announce_idempotent_per_conv_preserves_files(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1')
    reg.record_files(root, 'conv-a', [{'path': 'a.py', 'action': 'written'}])
    # A follow-up turn re-announces the SAME conv — must keep the file set and
    # the original startedTs (no flicker, no new peer).
    started = reg._state[os.path.abspath(root)]['conv-a']['startedTs']
    reg.announce(root, 'conv-a', task_id='t2', phase='working')
    peer = reg._state[os.path.abspath(root)]['conv-a']
    assert peer['files'] == ['a.py']
    assert peer['startedTs'] == started
    assert peer['taskId'] == 't2'


# ── record_files + currentFile + label ──

def test_record_files_unions_and_sets_current(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1')
    reg.record_files(root, 'conv-a', [{'path': 'a.py', 'action': 'written'},
                                      {'path': 'b.py', 'action': 'patched'}])
    reg.record_files(root, 'conv-a', [{'path': 'b.py', 'action': 'patched'},
                                      {'path': 'c.py', 'action': 'created'}])
    peer = reg.snapshot(root)['peers'][0]
    assert peer['files'] == ['a.py', 'b.py', 'c.py']   # unioned, no dup
    assert peer['currentFile'] == 'c.py'
    assert peer['statusLabel'] == 'editing c.py'       # backend-formed label


# ── overlap detection (notify-only) ──

def test_overlap_emits_conflict_advisory(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1', title='Alpha')
    reg.announce(root, 'conv-b', task_id='t2', title='Beta')
    reg.record_files(root, 'conv-a', [{'path': 'lib/llm/stream.py', 'action': 'patched'}])
    captured_broadcasts.clear()
    # conv-b touches the same file → conflict advisory broadcast.
    reg.record_files(root, 'conv-b', [{'path': 'lib/llm/stream.py', 'action': 'patched'}])
    conflicts = [f for f in captured_broadcasts if f.get('kind') == 'conflict']
    assert conflicts, 'expected a conflict advisory frame'
    adv = conflicts[-1]['conflict']
    assert adv['path'] == 'lib/llm/stream.py'
    assert set(adv['peers']) == {'conv-a', 'conv-b'}
    # Fully-formed message string (frontend renders verbatim).
    assert 'lib/llm/stream.py' in adv['message']
    assert 'Alpha' in adv['message'] and 'Beta' in adv['message']


def test_no_conflict_for_single_peer_or_distinct_files(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1')
    reg.announce(root, 'conv-b', task_id='t2')
    reg.record_files(root, 'conv-a', [{'path': 'a.py', 'action': 'written'}])
    captured_broadcasts.clear()
    reg.record_files(root, 'conv-b', [{'path': 'b.py', 'action': 'written'}])
    assert [f for f in captured_broadcasts if f.get('kind') == 'conflict'] == []


# ── idle vs depart ──

def test_mark_idle_keeps_peer_depart_removes(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1')
    reg.mark_idle(root, 'conv-a')
    # Peer still present in memory (lingers), but computed status is idle.
    assert 'conv-a' in reg._state[os.path.abspath(root)]
    assert reg.snapshot(root)['peers'] == []   # idle filtered from active view
    reg.depart(root, 'conv-a')
    assert os.path.abspath(root) not in reg._state
    departs = [f for f in captured_broadcasts if f.get('kind') == 'depart']
    assert departs and departs[-1]['peer']['convId'] == 'conv-a'


# ── sweep (active→idle→reaped) ──

def test_sweep_reaps_peer_past_idle_ttl(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1')
    reg._state[os.path.abspath(root)]['conv-a']['lastBeatTs'] = int(
        (time.time() - reg.IDLE_TTL_SEC - 10) * 1000)
    reaped = reg.sweep()
    assert reaped == 1
    assert os.path.abspath(root) not in reg._state
    assert [f for f in captured_broadcasts if f.get('kind') == 'depart']


def test_sweep_emits_idle_transition_once(fresh_state, captured_broadcasts, root):
    reg.announce(root, 'conv-a', task_id='t1')
    reg._state[os.path.abspath(root)]['conv-a']['lastBeatTs'] = int(
        (time.time() - reg.ACTIVE_TTL_SEC - 2) * 1000)
    captured_broadcasts.clear()
    reg.sweep()
    reg.sweep()  # second pass must NOT re-emit the idle transition
    idle_updates = [f for f in captured_broadcasts
                    if f.get('kind') == 'update' and f['peer']['status'] == 'idle']
    assert len(idle_updates) == 1


# ── startup reconciliation (ghost cleanup) ──

def test_reconcile_reaps_ghost_peers_on_fresh_process(fresh_state, captured_broadcasts, root):
    """A crashed server's persisted 'active' peer is a ghost on restart.

    With no live tasks in this fresh process, every disk peer is a ghost and
    must be reaped — otherwise the strip lies after every restart.
    """
    # Simulate a pre-crash registry on disk with a fresh heartbeat (the peer
    # *looked* active when the server died), but no live task backs it now.
    reg.announce(root, 'conv-ghost', task_id='dead-task')
    # Wipe in-memory state to mimic a fresh process that only has the disk file.
    reg._state.clear()

    # No live tasks → reconcile should reap the ghost.
    import lib.tasks_pkg.manager as mgr
    saved = dict(mgr.tasks)
    mgr.tasks.clear()
    try:
        reaped = reg.reconcile_on_startup([root])
    finally:
        mgr.tasks.update(saved)
    assert reaped == 1
    # Disk mirror rewritten to an empty peer list (no stale ghost).
    path = os.path.join(os.path.abspath(root), '.tofu', 'presence', 'registry.json')
    disk = json.loads(open(path).read())
    assert disk['peers'] == []
    assert reg.snapshot(root)['peers'] == []


def test_reconcile_keeps_peer_backed_by_live_task(fresh_state, captured_broadcasts, root, monkeypatch):
    """A peer whose task is genuinely live (fresh heartbeat) survives reconcile."""
    reg.announce(root, 'conv-live', task_id='live-task')
    reg._state.clear()  # fresh process reads only from disk

    import lib.tasks_pkg.manager as mgr
    saved = dict(mgr.tasks)
    mgr.tasks.clear()
    mgr.tasks['live-task'] = {'id': 'live-task', 'status': 'running'}
    try:
        reaped = reg.reconcile_on_startup([root])
    finally:
        mgr.tasks.clear()
        mgr.tasks.update(saved)
    assert reaped == 0
    assert reg.snapshot(root)['peers'] and \
        reg.snapshot(root)['peers'][0]['convId'] == 'conv-live'


# ── sub-agent peers (composite key convId#agentId) ──

def test_subagents_are_distinct_peers_under_one_conv(fresh_state, captured_broadcasts, root):
    """Two sub-agents of ONE conversation are two distinct peers, not one."""
    reg.announce(root, 'conv-a', task_id='t1', title='Parent conv')
    reg.announce(root, 'conv-a', agent_id='agent-coder-1', task_id='t1',
                 title='coder', parent_title='Parent conv')
    reg.announce(root, 'conv-a', agent_id='agent-coder-2', task_id='t1',
                 title='coder', parent_title='Parent conv')
    peers = reg.snapshot(root)['peers']
    # 1 conversation peer + 2 distinct sub-agent peers.
    assert len(peers) == 3
    agent_ids = sorted(p.get('agentId', '') for p in peers)
    assert agent_ids == ['', 'agent-coder-1', 'agent-coder-2']
    # All carry the SAME convId (grouping key).
    assert all(p['convId'] == 'conv-a' for p in peers)


def test_two_subagents_same_file_conflict_within_one_conv(fresh_state, captured_broadcasts, root):
    """The within-conversation worst case: two sub-agents clobber one file.

    This is the core slice-3 acceptance criterion — sub-agent-vs-sub-agent
    overlap inside ONE conversation must produce a conflict advisory exactly
    like cross-conversation overlap.
    """
    reg.announce(root, 'conv-a', agent_id='agent-coder-1', task_id='t1',
                 title='coder', parent_title='Refactor session')
    reg.announce(root, 'conv-a', agent_id='agent-coder-2', task_id='t1',
                 title='coder', parent_title='Refactor session')
    reg.record_files(root, 'conv-a', [{'path': 'lib/llm/stream.py', 'action': 'patched'}],
                     agent_id='agent-coder-1')
    captured_broadcasts.clear()
    reg.record_files(root, 'conv-a', [{'path': 'lib/llm/stream.py', 'action': 'patched'}],
                     agent_id='agent-coder-2')
    conflicts = [f for f in captured_broadcasts if f.get('kind') == 'conflict']
    assert conflicts, 'two sub-agents on one file must produce a conflict advisory'
    adv = conflicts[-1]['conflict']
    assert adv['path'] == 'lib/llm/stream.py'
    # Two DISTINCT sub-agent peer keys (conv#agent), not one collapsed convId.
    assert set(adv['peers']) == {'conv-a#agent-coder-1', 'conv-a#agent-coder-2'}
    # The advisory names BOTH sub-agents + the parent conversation, verbatim.
    assert 'agent-coder-1' in adv['message'] and 'agent-coder-2' in adv['message']
    assert 'Refactor session' in adv['message']


def test_single_subagent_touching_file_twice_is_no_conflict(fresh_state, captured_broadcasts, root):
    """One sub-agent editing a file across rounds is NOT a conflict."""
    reg.announce(root, 'conv-a', agent_id='agent-coder-1', task_id='t1', title='coder')
    reg.record_files(root, 'conv-a', [{'path': 'a.py', 'action': 'written'}],
                     agent_id='agent-coder-1')
    captured_broadcasts.clear()
    reg.record_files(root, 'conv-a', [{'path': 'a.py', 'action': 'patched'}],
                     agent_id='agent-coder-1')
    assert [f for f in captured_broadcasts if f.get('kind') == 'conflict'] == []


def test_subagent_idle_and_depart_target_the_right_peer(fresh_state, captured_broadcasts, root):
    """mark_idle / depart of a sub-agent must not touch the conversation peer."""
    reg.announce(root, 'conv-a', task_id='t1', title='Parent')
    reg.announce(root, 'conv-a', agent_id='agent-r-1', task_id='t1', title='researcher')
    reg.depart(root, 'conv-a', agent_id='agent-r-1')
    peers = reg.snapshot(root)['peers']
    # The conversation peer survives; only the sub-agent departed.
    assert len(peers) == 1 and peers[0].get('agentId', '') == ''
    departs = [f for f in captured_broadcasts if f.get('kind') == 'depart']
    assert departs and departs[-1]['peer'].get('agentId') == 'agent-r-1'


def test_cross_conv_conflict_still_works_with_composite_keys(fresh_state, captured_broadcasts, root):
    """Regression: the original cross-conversation overlap still fires."""
    reg.announce(root, 'conv-a', task_id='t1', title='Alpha')
    reg.announce(root, 'conv-b', task_id='t2', title='Beta')
    reg.record_files(root, 'conv-a', [{'path': 'shared.py', 'action': 'patched'}])
    captured_broadcasts.clear()
    reg.record_files(root, 'conv-b', [{'path': 'shared.py', 'action': 'patched'}])
    conflicts = [f for f in captured_broadcasts if f.get('kind') == 'conflict']
    assert conflicts
    adv = conflicts[-1]['conflict']
    assert set(adv['peers']) == {'conv-a', 'conv-b'}
    assert 'Alpha' in adv['message'] and 'Beta' in adv['message']


# ── lock discipline: broadcasts must NOT hold the global presence lock ──

def test_mutators_broadcast_outside_lock(fresh_state, monkeypatch, root):
    """push_event must never be called while _lock is held.

    Holding the global presence RLock across push-hub I/O is a latency /
    lock-ordering hazard under real concurrency. This asserts every mutator
    that broadcasts has already RELEASED the lock by the time push_event runs
    (the broadcast captures a decorated copy under the lock, then emits after).
    """
    seen_locked: list[str] = []

    def _probe_push(channel, task_id, payload):
        # Try to acquire the lock non-blocking; if we CAN'T, it's still held by
        # the calling mutator → defect. RLock is re-entrant on the same thread,
        # so we must check via a separate thread to detect a held lock.
        import threading as _t
        acquired = {'ok': False}

        def _try():
            acquired['ok'] = reg._lock.acquire(blocking=False)
            if acquired['ok']:
                reg._lock.release()

        th = _t.Thread(target=_try)
        th.start()
        th.join()
        if not acquired['ok']:
            seen_locked.append(payload.get('kind', '?'))

    import lib.push as push_mod
    monkeypatch.setattr(push_mod, 'push_event', _probe_push)

    reg.announce(root, 'conv-a', task_id='t1', title='A')
    reg.announce(root, 'conv-b', task_id='t2', title='B')
    reg.record_files(root, 'conv-a', [{'path': 'x.py', 'action': 'written'}])
    reg.record_files(root, 'conv-b', [{'path': 'x.py', 'action': 'written'}])  # conflict
    reg.heartbeat(root, 'conv-a', phase='generating')
    reg.mark_idle(root, 'conv-a')
    reg.depart(root, 'conv-b')

    assert seen_locked == [], (
        f'push_event fired while _lock was held for: {seen_locked}')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
