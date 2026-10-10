"""Application commit ACK, frozen retries and physical writer quiescence."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

from lib.agent_core.native_process import TerminalRequest
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy


def principal(owner=7):
    return PrincipalContext.user(subject_id='native-task-test', owner_user_id=owner)


def request(attempt=1, *, owner=7, text='answer'):
    return TerminalRequest(
        principal(owner), 'task', 'commit', attempt,
        json.dumps({'type': 'done', 'status': 'done', 'result': text}).encode(),
        b'{"invariants_satisfied":true}',
    )


class NativeTaskTerminalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.executor.shutdown)
        self.task = {'id': 'task', '_userId': 7, 'status': 'running'}
        self.projections = []

        def project(task, candidate):
            self.projections.append(candidate)
            task.update(status=candidate.event['status'], content=candidate.event['result'],
                        thinking='', finishReason='stop', _contentEpoch=1)

        self.policy = NativeTaskTerminalPolicy(
            principal=principal(), task=self.task, operation_id='commit',
            executor=self.executor, project=project, candidate_bytes=4096,
        )

    async def test_commit_debt_retries_without_reprojection_and_duplicates_do_not_write(self):
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=[False, True]) as commit:
            self.assertEqual(await self.policy.persist(request()), 'retry')
            self.assertTrue(self.task['_terminalPersistencePending'])
            self.assertEqual(await self.policy.persist(request(2)), 'accepted')
            self.assertEqual(await self.policy.persist(request(3)), 'accepted')
        self.assertEqual(commit.call_count, 2)
        self.assertEqual(len(self.projections), 1)

    async def test_foreign_owner_operation_and_overflow_fail_before_projection(self):
        from dataclasses import replace
        cases = [request(owner=8), replace(request(), operation_id='other'),
                 replace(request(), task_id='other'), request(text='x' * 5000),
                 replace(request(), receipt_json=b'{"invariants_satisfied":false}')]
        with patch('lib.tasks_pkg.native_terminal._persist') as commit:
            for candidate in cases:
                with self.assertRaises(ValueError):
                    await self.policy.persist(candidate)
        commit.assert_not_called()
        self.assertEqual(self.projections, [])
        self.assertEqual(self.task['status'], 'running')

    async def test_changed_candidate_or_projected_result_cannot_retry_write(self):
        with patch('lib.tasks_pkg.native_terminal._persist', return_value=False) as commit:
            await self.policy.persist(request())
            with self.assertRaises(ValueError):
                await self.policy.persist(request(2, text='changed'))
            self.task['content'] = 'competing finalizer'
            with self.assertRaises(ValueError):
                await self.policy.persist(request(2))
        self.assertEqual(commit.call_count, 1)

    async def test_recovery_fence_is_rejected_and_never_retried(self):
        def commit(task):
            task['_workspaceDeliveryOwnershipRejected'] = True
            return False
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=commit) as writer:
            self.assertEqual(await self.policy.persist(request()), 'rejected')
            self.assertEqual(await self.policy.persist(request(2)), 'rejected')
        self.assertEqual(writer.call_count, 1)

    async def test_storage_error_retains_debt_and_cause_without_reprojecting(self):
        failure = OSError('fixture storage failure')
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=[failure, True]):
            with self.assertRaises(OSError) as caught:
                await self.policy.persist(request())
            self.assertIs(caught.exception, failure)
            self.assertTrue(self.task['_terminalPersistencePending'])
            self.assertTrue(self.task['_terminalPersistenceRetryReady'])
            self.assertEqual(await self.policy.persist(request(2)), 'accepted')
        self.assertEqual(len(self.projections), 1)

    async def test_projection_failure_never_repeats_partial_side_effects(self):
        def project(task, _request):
            task['content'] = 'partial projection'
            raise OSError('fixture projection failure')
        self.policy.project = project
        with patch('lib.tasks_pkg.native_terminal._persist') as writer:
            with self.assertRaises(OSError):
                await self.policy.persist(request())
            with self.assertRaises(RuntimeError) as caught:
                await self.policy.persist(request(2))
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(self.task['_terminalPersistencePending'])
        self.assertFalse(self.task['_terminalPersistenceRetryReady'])
        writer.assert_not_called()

    async def test_failed_settlement_can_persist_error_but_cannot_claim_success(self):
        from dataclasses import replace
        candidate = replace(request(),
            event_json=b'{"type":"error","status":"error","error":{"kind":"settlement"}}',
            receipt_json=b'{"invariants_satisfied":false}')
        def project(task, request):
            task.update(status=request.event['status'], error=request.event['error'])
        self.policy.project = project
        with patch('lib.tasks_pkg.native_terminal._persist', return_value=True):
            self.assertEqual(await self.policy.persist(candidate), 'accepted')
        self.assertEqual(self.task['status'], 'error')

    async def test_repeated_cancel_joins_physical_commit_and_keeps_serialization(self):
        entered, release = threading.Event(), threading.Event()
        def commit(_task):
            entered.set()
            release.wait(5)
            return True
        with patch('lib.tasks_pkg.native_terminal._persist', side_effect=commit) as writer:
            first = asyncio.create_task(self.policy.persist(request()))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            second = asyncio.create_task(self.policy.persist(request(2)))
            await asyncio.sleep(0.01)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(await second, 'accepted')
        self.assertEqual(writer.call_count, 1)


if __name__ == '__main__':
    unittest.main()
