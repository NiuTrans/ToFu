"""pt_a21cd6eb ③-1 — registry eviction observability guards.

A live chat task evaporated from the in-memory registry twice on 2026-08-01
(fb6d1f8d, 7ddbc751) while its worker thread kept running — abort 404'd,
the busy projection went blind, and NOTHING logged because the only two
eviction paths (``discard_task`` / ``TaskRuntime.cleanup_stale``) were
silent / debug-level. These guards pin the INFO fingerprint both paths
must now leave.
"""

import logging
import time

import pytest

from lib.agent_core.task_runtime import TaskRuntime
import lib.tasks_pkg.manager._registry as reg
from tests.support.chat_tasks import (
    chat_task_fixture_guard,
    chat_task_registry,
)


@pytest.mark.unit
class TestDiscardTaskLeavesAFingerprint:

    def test_discard_logs_task_id_and_caller(self, caplog):
        from lib.observability import prometheus_lines, reset_for_tests

        reset_for_tests()
        tid = 'obs-evict-test-0001'
        with chat_task_fixture_guard:
            chat_task_registry[tid] = {'id': tid, 'convId': 'convObs1',
                              'status': 'done', 'created_at': time.time()}
        try:
            with caplog.at_level(logging.INFO, logger=reg.logger.name):
                reg.discard_task(tid, conv_id='convObs1')
            hits = [r for r in caplog.records
                    if 'discard_task' in r.getMessage()
                    and tid[:8] in r.getMessage()]
            assert hits, f'discard_task emitted no INFO fingerprint: ' \
                         f'{[r.getMessage() for r in caplog.records]}'
            msg = hits[0].getMessage()
            assert 'popped=True' in msg
            assert 'test_discard_logs_task_id_and_caller' in msg, (
                f'caller frame missing from fingerprint: {msg}')
            metrics = '\n'.join(prometheus_lines())
            assert 'tofu_task_registry_evictions_total' in metrics
            assert 'kind="chat",reason="discard"' in metrics
        finally:
            with chat_task_fixture_guard:
                chat_task_registry.pop(tid, None)


@pytest.mark.unit
class TestCleanupStaleLeavesAFingerprint:

    def test_cleanup_stale_logs_evicted_ids(self, caplog):
        from lib.observability import prometheus_lines, reset_for_tests

        reset_for_tests()
        rt = TaskRuntime('obs-kind', ttl=1)
        tid = rt.create(user_id=1, task_id='obs-evict-test-0002')['id']
        with rt._lock:
            rt._tasks[tid]['status'] = 'done'
            rt._tasks[tid]['finished_at'] = time.time() - 10
        with caplog.at_level(logging.INFO, logger=rt.logger.name
                             if hasattr(rt, 'logger') else
                             'lib.agent_core.task_runtime'):
            n = rt.cleanup_stale()
        assert n == 1
        hits = [r for r in caplog.records
                if 'cleaned' in r.getMessage() and tid[:8] in r.getMessage()]
        assert hits, (
            'cleanup_stale evicted a task without an INFO fingerprint '
            'naming the evicted id: '
            f'{[r.getMessage() for r in caplog.records]}')
        metrics = '\n'.join(prometheus_lines())
        assert 'tofu_task_registry_evictions_total' in metrics
        assert 'kind="obs-kind",reason="ttl"' in metrics
