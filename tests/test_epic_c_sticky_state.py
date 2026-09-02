#!/usr/bin/env python3
"""Cross-replica runtime-state transition contracts.

Covers Build Order step 4:
  * the `interrupted` false-positive fix (§4.1/§6.4): a running checkpoint
    absent from THIS replica reports running+reconnect under the sharded
    backend, NOT interrupted (which would strand a live task on another
    replica); inproc keeps the crash-recovery interrupted behaviour.
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
    from lib.chat.task_wire import _running_checkpoint_verdict
    status, reconnect = _running_checkpoint_verdict(sharded=True)
    assert status == 'running', 'sharded wrong-replica poll must NOT be interrupted'
    assert reconnect is True, 'client must be told to reconnect (affinity re-route)'


def test_running_checkpoint_inproc_still_interrupted():
    """Single-process (inproc): absent genuinely means crashed → interrupted,
    byte-identical to the pre-Epic-C crash-recovery behaviour. No reconnect."""
    from lib.chat.task_wire import _running_checkpoint_verdict
    status, reconnect = _running_checkpoint_verdict(sharded=False)
    assert status == 'interrupted'
    assert reconnect is False


def test_NC_verdict_must_distinguish_backends():
    """NEGATIVE CONTROL: the two backends MUST produce different verdicts for a
    running-absent checkpoint. If a refactor collapsed them (always
    interrupted), the sharded path would strand live cross-replica tasks — the
    exact false-positive. Assert they differ."""
    from lib.chat.task_wire import _running_checkpoint_verdict
    sharded = _running_checkpoint_verdict(True)
    inproc = _running_checkpoint_verdict(False)
    assert sharded != inproc, (
        'sharded and inproc verdicts must differ — collapsing them reintroduces '
        'the interrupted false-positive for live cross-replica tasks')
    assert sharded[0] == 'running' and inproc[0] == 'interrupted'


def test_programmatic_server_rejects_fake_multiworker_flag():
    source = open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               'server.py'), encoding='utf-8').read()
    assert "if args.workers != 1:" in source
    assert '--workers must be 1' in source
    assert 'Horizontal scaling is not enabled in the ' in source
    assert "'current distributed preview; see '" in source
    assert 'for stateless horizontal scaling' not in source


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
    import lib.tasks_pkg.manager.runtime as m
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
    import lib.tasks_pkg.manager.runtime as m
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
