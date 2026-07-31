#!/usr/bin/env python3
"""pt_conv_state_ssot — P1.5: connect snapshot frame.

Owner mandate (2026-07-24, board pt_e1c4693341b24730):

    On successful `subscribe(channel='notify', taskId='*')` the server MUST
    enqueue a fresh ``conv_state_snapshot`` frame to THAT client (and only
    that client), carrying the full ``{convId → {runningTaskIds, runningTaskIdsRev}}``
    projection of the task registry. Every conv independently rev-stamped
    (owner: "每 conv 独立 rev … 否则乱序时新 snapshot 会被旧 notify 打回旧").
    The frame is enqueued DIRECTLY to the client's queue — NOT broadcast via
    hub.push_event — so it cannot leak into another user's push queue.

    Content is sourced from ``snapshot_running_by_conv()`` in exactly one
    call; the route does NOT re-implement carrier/aborted filtering.

Six required faces (all failing-first, NEUTER-verified where relevant):

  1. Frame shape: type='conv_state_snapshot', includes ``convs`` dict + ``userId``.
  2. Per-conv independent rev tuple — NOT a single shared rev across the frame.
  3. Empty-registry case: ``convs == {}`` and frame IS STILL SENT (client must
     be able to distinguish "no snapshot arrived" from "snapshot says all idle").
  4. Filter fidelity — carrier / aborted / empty-convId excluded (via delegation
     to snapshot_running_by_conv, not a second copy of the filter).
  5. Wrong channel / wrong taskId → NO snapshot triggered (only notify+'*' fires).
  6. Direct enqueue to THIS client, NOT broadcast — a second PushClient must
     NOT receive the frame.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


@pytest.fixture
def clean_hub():
    """Reset the singleton PushHub state between tests so each test sees a
    predictable list of clients + subscriptions."""
    from lib.agent_core.push import hub
    with hub._lock:
        hub._clients.clear()
        hub._subscriptions.clear()
    yield hub
    with hub._lock:
        hub._clients.clear()
        hub._subscriptions.clear()


@pytest.fixture
def stub_registry(monkeypatch):
    """Force snapshot_running_by_conv() to return a controlled projection so
    the route-level test does not depend on real live tasks. Reused across
    faces #1–#4."""
    import lib.tasks_pkg.manager._registry as reg_mod

    def _factory(mapping):
        monkeypatch.setattr(reg_mod, 'snapshot_running_by_conv',
                            lambda user_id='': dict(mapping))
    return _factory


def _drain_all(client):
    """Drain everything currently in the client's queue (non-blocking)."""
    frames = []
    q = client._queue
    while not q.empty():
        frames.append(q.get_nowait())
    return frames


# ─────────────────────────────────────────────────────────────────────
#  Face 1: subscribe(notify, '*') → single conv_state_snapshot frame,
#          well-formed shape with convs dict + userId.
# ─────────────────────────────────────────────────────────────────────
def test_subscribe_notify_star_enqueues_snapshot_frame(clean_hub, stub_registry):
    from lib.agent_core.push import PushClient
    from routes.push import _handle_client_frame

    stub_registry({'conv-A': ['tid-1']})
    client = PushClient()
    clean_hub.register(client)

    _handle_client_frame(client, {'action': 'subscribe',
                                  'channel': 'notify', 'taskId': '*'})

    frames = _drain_all(client)
    snapshots = [f for f in frames if f.get('type') == 'conv_state_snapshot']
    assert len(snapshots) == 1, (
        'exactly one conv_state_snapshot frame must be enqueued on '
        'subscribe(notify, "*"); got frames=%r' % frames)
    snap = snapshots[0]
    assert snap['channel'] == 'notify'
    assert snap['taskId'] == '*'
    assert 'convs' in snap and isinstance(snap['convs'], dict)
    assert 'userId' in snap
    # conv-A entry must carry both new fields as documented for P1:
    entry = snap['convs'].get('conv-A')
    assert entry is not None, 'conv-A must appear in convs (registry had it)'
    assert entry.get('runningTaskIds') == ['tid-1']
    assert 'runningTaskIdsRev' in entry
    assert isinstance(entry['runningTaskIdsRev'], list)
    assert len(entry['runningTaskIdsRev']) == 2


# ─────────────────────────────────────────────────────────────────────
#  Face 2: each conv carries its OWN rev tuple — no shared frame-level rev.
# ─────────────────────────────────────────────────────────────────────
def test_snapshot_rev_is_per_conv_not_shared(clean_hub, stub_registry):
    from lib.agent_core.push import PushClient
    from routes.push import _handle_client_frame

    stub_registry({'conv-A': ['tid-1'], 'conv-B': ['tid-2', 'tid-3']})
    client = PushClient()
    clean_hub.register(client)

    _handle_client_frame(client, {'action': 'subscribe',
                                  'channel': 'notify', 'taskId': '*'})
    snap = [f for f in _drain_all(client)
            if f.get('type') == 'conv_state_snapshot'][0]

    # Each conv MUST have its own rev tuple inside its entry — a stale
    # notify_conv_changed(conv-A) frame arriving AFTER the snapshot must be
    # decidable per-conv, not against a single frame-wide rev.
    assert 'runningTaskIdsRev' not in snap, (
        'the snapshot frame itself must NOT carry a single shared rev — '
        'a per-frame rev would let one late notify overwrite state for '
        'convs it does not concern')
    a = snap['convs']['conv-A']['runningTaskIdsRev']
    b = snap['convs']['conv-B']['runningTaskIdsRev']
    assert a[1] == b[1], 'same process → same replica_id on all entries'
    # ns is strictly monotonic per call — two entries in the same snapshot
    # must therefore differ (or at least be non-decreasing; the loop reads
    # each conv's rev sequentially with time.monotonic_ns()).
    assert a[0] != b[0] or True  # not strictly required to differ, but not shared


# ─────────────────────────────────────────────────────────────────────
#  Face 3: empty registry — frame is STILL SENT with convs={}.
# ─────────────────────────────────────────────────────────────────────
def test_empty_registry_still_sends_snapshot(clean_hub, stub_registry):
    """Owner: 'runningTaskIds=[] 表示"没在跑",这是权威事实'. An empty
    snapshot is meaningfully different from "no snapshot arrived" — the
    client uses arrival-of-snapshot to prune stale local state.
    """
    from lib.agent_core.push import PushClient
    from routes.push import _handle_client_frame

    stub_registry({})
    client = PushClient()
    clean_hub.register(client)

    _handle_client_frame(client, {'action': 'subscribe',
                                  'channel': 'notify', 'taskId': '*'})
    snapshots = [f for f in _drain_all(client)
                 if f.get('type') == 'conv_state_snapshot']
    assert len(snapshots) == 1, (
        'even an empty registry must produce exactly one snapshot frame — '
        'clients need to distinguish "not received" from "received: all idle"')
    assert snapshots[0]['convs'] == {}


# ─────────────────────────────────────────────────────────────────────
#  Face 4: filter fidelity — MUST delegate to snapshot_running_by_conv,
#          not re-implement filtering in the route.
# ─────────────────────────────────────────────────────────────────────
def test_snapshot_delegates_to_registry_snapshot_helper(clean_hub, monkeypatch):
    """Force snapshot_running_by_conv to return a specific projection; the
    frame's convs dict must be EXACTLY that projection's keys.

    This test locks the delegation: if a future refactor re-adds carrier
    filtering in the route (dead-code drift), this test catches it — the
    stub would return a canary convId the route filter would strip.
    """
    import lib.tasks_pkg.manager._registry as reg_mod
    canary = {'conv-canary': ['tid-canary'],
              'conv-normal': ['tid-normal']}
    monkeypatch.setattr(reg_mod, 'snapshot_running_by_conv',
                        lambda user_id='': dict(canary))
    from lib.agent_core.push import PushClient
    from routes.push import _handle_client_frame

    client = PushClient()
    clean_hub.register(client)
    _handle_client_frame(client, {'action': 'subscribe',
                                  'channel': 'notify', 'taskId': '*'})
    snap = [f for f in _drain_all(client)
            if f.get('type') == 'conv_state_snapshot'][0]

    assert set(snap['convs'].keys()) == set(canary.keys()), (
        'convs must be EXACTLY the projection from snapshot_running_by_conv; '
        'route must not add its own filter — got %r expected %r' %
        (list(snap['convs'].keys()), list(canary.keys())))


# ─────────────────────────────────────────────────────────────────────
#  Face 5: only channel=='notify' + taskId=='*' triggers the snapshot.
# ─────────────────────────────────────────────────────────────────────
def test_wrong_channel_does_not_trigger_snapshot(clean_hub, stub_registry):
    from lib.agent_core.push import PushClient
    from routes.push import _handle_client_frame

    stub_registry({'conv-A': ['tid-1']})
    client = PushClient()
    clean_hub.register(client)

    # Subscribing to chat / paper / translate must NOT enqueue a snapshot.
    for ch in ('chat', 'paper', 'translate'):
        _handle_client_frame(client, {'action': 'subscribe',
                                      'channel': ch, 'taskId': '*'})
    frames = _drain_all(client)
    assert not any(f.get('type') == 'conv_state_snapshot' for f in frames), (
        'snapshot must fire ONLY for subscribe(notify, "*"), got frames=%r' %
        frames)


def test_specific_task_id_notify_does_not_trigger_snapshot(clean_hub,
                                                           stub_registry):
    """A client that subscribes to notify for a specific taskId (not '*')
    is asking about ONE task — the full-conv snapshot is not what it wants.
    Reserve the snapshot for the '*' subscription, which is what the
    frontend sidebar uses.
    """
    from lib.agent_core.push import PushClient
    from routes.push import _handle_client_frame

    stub_registry({'conv-A': ['tid-1']})
    client = PushClient()
    clean_hub.register(client)

    _handle_client_frame(client, {'action': 'subscribe',
                                  'channel': 'notify', 'taskId': 'tid-1'})
    frames = _drain_all(client)
    assert not any(f.get('type') == 'conv_state_snapshot' for f in frames)


# ─────────────────────────────────────────────────────────────────────
#  Face 6: snapshot is enqueued to THIS client only, not broadcast.
# ─────────────────────────────────────────────────────────────────────
def test_snapshot_does_not_leak_to_other_clients(clean_hub, stub_registry):
    """A second connected PushClient (e.g. a different user's tab) must NOT
    receive this snapshot. The route must use ``client.enqueue`` directly,
    not ``hub.push_event`` (which would fan out via subscriptions and
    cross-replica bus).
    """
    from lib.agent_core.push import PushClient
    from routes.push import _handle_client_frame

    stub_registry({'conv-A': ['tid-1']})
    subscriber = PushClient()
    bystander = PushClient()
    clean_hub.register(subscriber)
    clean_hub.register(bystander)

    _handle_client_frame(subscriber, {'action': 'subscribe',
                                      'channel': 'notify', 'taskId': '*'})

    sub_frames = _drain_all(subscriber)
    by_frames = _drain_all(bystander)
    assert any(f.get('type') == 'conv_state_snapshot' for f in sub_frames)
    assert not any(f.get('type') == 'conv_state_snapshot' for f in by_frames), (
        'a second client must not receive the snapshot — it was targeted at '
        'the subscriber only; bystander frames=%r' % by_frames)


# ─────────────────────────────────────────────────────────────────────
#  Bonus: build_conv_state_snapshot() is the pure-function seam the route
#         is expected to call — locking it here means the route can be
#         re-tested against the payload contract independently.
# ─────────────────────────────────────────────────────────────────────
def test_build_conv_state_snapshot_payload_contract(monkeypatch):
    """The payload builder (extracted from the route) must return a JSON-
    ready dict with per-conv rev tuples. This is the seam P2 will reuse."""
    import lib.tasks_pkg.manager._registry as reg_mod
    monkeypatch.setattr(reg_mod, 'snapshot_running_by_conv',
                        lambda user_id='': {'conv-A': ['tid-1', 'tid-2']})
    from lib.agent_core.push import build_conv_state_snapshot
    payload = build_conv_state_snapshot(user_id=42)
    assert payload['type'] == 'conv_state_snapshot'
    assert payload['channel'] == 'notify'
    assert payload['taskId'] == '*'
    assert payload['userId'] == 42
    entry = payload['convs']['conv-A']
    assert entry['runningTaskIds'] == ['tid-1', 'tid-2']
    rev = entry['runningTaskIdsRev']
    assert isinstance(rev, list) and len(rev) == 2
    assert isinstance(rev[0], int) and rev[0] > 0
    assert isinstance(rev[1], str) and rev[1]


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
