"""Retention sweeps prune only the expired task they observed as latest.

The conversation latest-task index answers "which task owns this conversation
now" for successor hand-off, delivery-wedge probes and live workspace-root
pinning. A retention sweep snapshots the index under its lock and clears the
entries afterwards; a turn created for the same conversation in between must
keep its pointer.
"""

import time

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('sweep', ['cleanup_old_tasks', 'shed_memory_under_pressure'])
def test_retention_sweep_keeps_successor_registered_during_the_sweep(monkeypatch, sweep):
    import lib.tasks_pkg.manager._maintenance as maintenance
    from lib.tasks_pkg.manager import runtime as manager_runtime
    from tests.support.chat_tasks import chat_task_registry

    conv_id = 'conv-latest-index-sweep'
    expired_id, successor_id = 'task-expired-latest', 'task-live-successor'
    long_ago = time.time() - 10 * manager_runtime.chat_task_runtime.ttl - 60
    chat_task_registry[expired_id] = {
        'status': 'done', 'convId': conv_id,
        'created_at': long_ago, 'finished_at': long_ago,
    }
    chat_task_registry[successor_id] = {'status': 'running', 'convId': conv_id}
    manager_runtime._record_latest_task(conv_id, expired_id)
    monkeypatch.setattr(maintenance, 'reap_stuck_running_tasks', lambda: 0)

    clear_latest_task = maintenance._clear_latest_task

    def successor_created_before_clear(cid, **kwargs):
        # A new turn for this conversation registers between the sweep's
        # index snapshot and its clear.
        manager_runtime._record_latest_task(conv_id, successor_id)
        return clear_latest_task(cid, **kwargs)

    monkeypatch.setattr(maintenance, '_clear_latest_task', successor_created_before_clear)
    try:
        getattr(maintenance, sweep)()
        assert manager_runtime._latest_task_for_conv(conv_id) == successor_id
    finally:
        chat_task_registry.pop(expired_id, None)
        chat_task_registry.pop(successor_id, None)
        clear_latest_task(conv_id)
