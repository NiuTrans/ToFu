#!/usr/bin/env python3
"""Epic C — sticky-affinity + cross-replica runtime state (board pt_96b80d88c8d54b71).

Covers Build Order step 4:
  * the `interrupted` false-positive fix (§4.1/§6.4): a running checkpoint
    absent from THIS replica reports running+reconnect under the sharded
    backend, NOT interrupted (which would strand a live task on another
    replica); inproc keeps the crash-recovery interrupted behaviour.
  * consistent-hash task→replica affinity (owner_replica/owns_task).
  * supersede index externalized onto the shared store (conv→latest task),
    fleet-authoritative across replicas.

Bare-CI-safe: no live redis/DB/node; the store's inproc backend + a fresh
reset stand in for the shared table.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════════════
#  The `interrupted` false-positive fix (the decisive Epic C test)
# ══════════════════════════════════════════════════════════════════════
def test_running_checkpoint_sharded_reports_running_reconnect_not_interrupted():
    """DECISIVE: under the sharded (redis) backend, a running checkpoint absent
    locally must report status='running' + reconnect=True — NOT 'interrupted'.
    A wrong-replica poll of a task that is ALIVE on another replica must not be
    told the task died."""
    from routes.chat import _running_checkpoint_verdict
    status, reconnect = _running_checkpoint_verdict(sharded=True)
    assert status == 'running', 'sharded wrong-replica poll must NOT be interrupted'
    assert reconnect is True, 'client must be told to reconnect (affinity re-route)'


def test_running_checkpoint_inproc_still_interrupted():
    """Single-process (inproc): absent genuinely means crashed → interrupted,
    byte-identical to the pre-Epic-C crash-recovery behaviour. No reconnect."""
    from routes.chat import _running_checkpoint_verdict
    status, reconnect = _running_checkpoint_verdict(sharded=False)
    assert status == 'interrupted'
    assert reconnect is False


def test_NC_verdict_must_distinguish_backends():
    """NEGATIVE CONTROL: the two backends MUST produce different verdicts for a
    running-absent checkpoint. If a refactor collapsed them (always
    interrupted), the sharded path would strand live cross-replica tasks — the
    exact false-positive. Assert they differ."""
    from routes.chat import _running_checkpoint_verdict
    sharded = _running_checkpoint_verdict(True)
    inproc = _running_checkpoint_verdict(False)
    assert sharded != inproc, (
        'sharded and inproc verdicts must differ — collapsing them reintroduces '
        'the interrupted false-positive for live cross-replica tasks')
    assert sharded[0] == 'running' and inproc[0] == 'interrupted'


# ══════════════════════════════════════════════════════════════════════
#  Consistent-hash affinity
# ══════════════════════════════════════════════════════════════════════
def test_affinity_is_deterministic_across_replicas():
    """owner_replica is a pure function of (task_id, ring) — every replica
    computes the SAME owner without coordination. That determinism is what the
    LB affinity relies on."""
    from lib.agent_core.affinity import owner_replica
    ring = ['r1', 'r2', 'r3']
    for tid in ('task-abc', 'task-def', 'psx-123', 'x'):
        owners = {owner_replica(tid, ring) for _ in range(5)}
        assert len(owners) == 1, 'owner must be stable for a given task+ring'
        assert owners.pop() in ring


def test_affinity_distributes_across_ring():
    """A spread of taskIds should map across the ring (not all to one replica) —
    otherwise affinity gives no load distribution."""
    from lib.agent_core.affinity import owner_replica
    ring = ['r1', 'r2', 'r3', 'r4']
    owners = {owner_replica('task-%d' % i, ring) for i in range(200)}
    assert len(owners) >= 2, 'consistent hash must spread tasks across the ring'


def test_affinity_single_replica_owns_everything():
    """Default single-replica ring → this replica owns every task (byte-identical
    single-box: no request is ever considered 'not mine')."""
    from lib.agent_core.affinity import owns_task
    # No TOFU_REPLICA_RING set → ring is just this replica.
    assert owns_task('any-task') is True
    assert owns_task('another') is True


def test_rendezvous_add_replica_only_moves_keys_to_new_replica():
    """A scale-up must not reshuffle keys between existing replicas."""
    from lib.agent_core.affinity import owner_replica
    old_ring = ['r1', 'r2', 'r3']
    new_ring = old_ring + ['r4']
    moved = 0
    for i in range(1000):
        task_id = 'task-%d' % i
        before = owner_replica(task_id, old_ring)
        after = owner_replica(task_id, new_ring)
        if before != after:
            moved += 1
            assert after == 'r4'
    assert 100 < moved < 400, 'distribution should move roughly 1/4 of keys'


def test_programmatic_server_rejects_fake_multiworker_flag():
    source = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'), encoding='utf-8').read()
    assert "if args.workers != 1:" in source
    assert '--workers must be 1' in source


# ══════════════════════════════════════════════════════════════════════
#  Supersede index externalized onto the shared store
# ══════════════════════════════════════════════════════════════════════
def _reset_state_store():
    import lib.runtime_state_store as rss
    rss.reset_for_test()


def test_supersede_index_written_and_read_via_store():
    """_record_latest_task mirrors conv→latest into the shared store, and
    _latest_task_for_conv reads it back — the cross-replica source of truth."""
    _reset_state_store()
    from lib.tasks_pkg import manager as m
    m._record_latest_task('conv-1', 'task-A')
    assert m._latest_task_for_conv('conv-1') == 'task-A'
    # A newer task supersedes.
    m._record_latest_task('conv-1', 'task-B')
    assert m._latest_task_for_conv('conv-1') == 'task-B'


def test_supersede_index_is_cross_replica_via_store():
    """The DECISIVE cross-replica property: a task recorded as latest by 'replica
    B' (writing straight to the shared store) is seen as latest by 'replica A'
    reading through _latest_task_for_conv — even though A's LOCAL dict never saw
    it. This is what lets a stale task on A recognise B's newer task."""
    _reset_state_store()
    from lib.tasks_pkg import manager as m
    import lib.runtime_state_store as rss
    # Replica B records the newest task directly in the shared store (as its
    # _record_latest_task would), WITHOUT touching replica A's local dict.
    rss.get_store().set_value(m._LATEST_KIND, 'conv-x', 'task-from-B', m._LATEST_TTL)
    # Replica A's local dict has an OLDER task for the same conv.
    with m._conv_latest_task_lock:
        m._conv_latest_task['conv-x'] = 'task-old-on-A'
    # A reads the fleet-authoritative latest → B's task, not its stale local one.
    assert m._latest_task_for_conv('conv-x') == 'task-from-B'


def test_store_set_get_value_roundtrip_and_expiry():
    """set_value/get_value roundtrip + TTL expiry (the primitive the supersede
    index rides on)."""
    import time
    from lib.runtime_state_store import InProcRuntimeStateStore
    s = InProcRuntimeStateStore()
    s.set_value('latest', 'c1', 'tA', ttl=100)
    assert s.get_value('latest', 'c1') == 'tA'
    s.set_value('latest', 'c2', 'tB', ttl=0.1)
    time.sleep(0.15)
    assert s.get_value('latest', 'c2') is None  # expired


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
