"""Application callbacks preserve native lease debt and joined cancellation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import AsyncMock, patch

from lib.agent_core.native_process import CheckpointRequest, CheckpointTerminalRequest, TerminalRequest
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from tests.agent_core.test_native_task_terminal import principal


class SelectedStorage:
    def __init__(self):
        self.calls = []
        self.state = None
        self.create = None
        self.failure = None

    def native_checkpoint_available(self, *, user_id):
        assert user_id == 7
        return True

    def native_checkpoint_control(self, operation, payload, **authority):
        assert authority['user_id'] == 7
        self.calls.append((operation, payload, authority))
        if operation == 'create' and self.create:
            self.create()
        if operation == self.failure:
            raise OSError('unknown native delivery')
        return {'accepted': True, 'capability_id': 19, 'state': self.state if operation == 'load' else None}


class NativeCheckpointPolicyTests(unittest.IsolatedAsyncioTestCase):
    def pending_recovery(self):
        recovery = type('Recovery', (), {})()
        recovery.capture = AsyncMock(side_effect=lambda request: dict(
            request.state, tool_recovery={'operation_id': 'owned-operation'}))
        recovery.committed = AsyncMock()
        recovery.reconcile = AsyncMock(return_value=[dict(call_id='call', content='domain proof')])
        self.policy.tool_recovery = recovery
        self.policy.supports_pending_tools = True
        self.policy.capability = 19
        state = dict(tools_pending=True, next_round=1, usage=[{'tokens': 23}],
                     messages=[dict(role='assistant', tool_calls=[dict(id='call')])])
        return recovery, replace(self.request, action='pending', state=state)

    async def test_pending_commits_capture_before_arming_and_preserves_metadata_on_completion(self):
        recovery, request = self.pending_recovery()
        async def committed(callback):
            self.assertEqual([row[0] for row in self.storage.calls], ['pending'])
            self.assertEqual(callback.state['tool_recovery'], {'operation_id': 'owned-operation'})
            callback.state['tool_recovery']['operation_id'] = 'changed-after-commit'
        recovery.committed.side_effect = committed
        await self.policy.pending(request)
        await self.policy.complete(replace(request, action='complete', state={'next_round': 1}))
        self.assertEqual(self.storage.calls[-1][1]['state']['tool_recovery'],
                         {'operation_id': 'owned-operation'})
        self.assertIsNone(self.policy.pending_metadata)
        await self.policy.close()
        self.assertIsNone(self.policy.tool_recovery)

    async def test_pending_unknown_ack_never_arms_tools(self):
        recovery, request = self.pending_recovery()
        self.storage.failure = 'pending'
        with self.assertRaises(OSError):
            await self.policy.pending(request)
        recovery.committed.assert_not_awaited()
        self.assertIsNone(self.policy.pending_metadata)

    async def test_pending_rejects_domain_changes_to_paid_fields_even_in_place(self):
        recovery, request = self.pending_recovery()
        async def capture(callback):
            callback.state['usage'][0]['tokens'] = 0
            return dict(callback.state, tool_recovery={})
        recovery.capture.side_effect = capture
        with self.assertRaisesRegex(ValueError, 'paid checkpoint authority'):
            await self.policy.pending(request)
        self.assertEqual(self.storage.calls, [])
        recovery.committed.assert_not_awaited()

    async def test_cancel_after_pending_storage_ack_never_arms_tools(self):
        recovery, request = self.pending_recovery()
        original = self.storage.native_checkpoint_control
        def cancel(*args, **kwargs):
            result = original(*args, **kwargs)
            request.cancelled.set()
            return result
        self.storage.native_checkpoint_control = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.policy.pending(request)
        self.assertEqual([row[0] for row in self.storage.calls], ['pending'])
        recovery.committed.assert_not_awaited()

    async def test_pending_load_refuses_unsupported_before_financial_restore(self):
        self.storage.state = {'tools_pending': True}
        with self.assertRaisesRegex(ValueError, 'domain reconciler'):
            await self.policy.load(self.request)
        self.restore.assert_not_awaited()

    async def test_reconciler_cannot_change_its_restored_operation_reference(self):
        recovery, request = self.pending_recovery()
        self.storage.state = dict(request.state, tool_recovery={'operation_id': 'owned-operation'})
        saved = await self.policy.load(self.request)
        async def changed(callback):
            callback.state['tool_recovery']['operation_id'] = 'other-operation'
            return [dict(call_id='call', content='wrong domain proof')]
        recovery.reconcile.side_effect = changed
        with self.assertRaisesRegex(ValueError, 'changed its restored'):
            await self.policy.reconcile(replace(request, action='reconcile', state=saved))
        self.assertIsNotNone(self.policy.recovery_digest)

    async def test_reconciliation_requires_exact_loaded_state_and_ordered_complete_results(self):
        recovery, request = self.pending_recovery()
        self.storage.state = dict(request.state, tool_recovery={'operation_id': 'owned-operation'})
        saved = await self.policy.load(self.request)
        request = replace(request, action='reconcile', state=saved)
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            await self.policy.complete(replace(request, action='complete', state={}))
        with self.assertRaisesRegex(ValueError, 'exact restored'):
            await self.policy.reconcile(replace(request, state=dict(saved, next_round=2)))
        recovery.reconcile.assert_not_awaited()
        recovery.reconcile.return_value = [dict(call_id='different', content='not our result')]
        with self.assertRaisesRegex(ValueError, 'ordered exact'):
            await self.policy.reconcile(request)
        self.assertIsNotNone(self.policy.recovery_digest)
        recovery.reconcile.return_value = [dict(call_id='call', content='domain proof')]
        self.assertEqual(await self.policy.reconcile(request), recovery.reconcile.return_value)
        self.assertIsNone(self.policy.recovery_digest)
        with self.assertRaisesRegex(ValueError, 'exact restored'):
            await self.policy.reconcile(request)
        await self.policy.complete(replace(request, action='complete', state={'next_round': 1}))
        self.assertEqual(self.storage.calls[-1][1]['state']['tool_recovery'],
                         {'operation_id': 'owned-operation'})

    async def test_handoff_reads_owned_lineage_on_worker_and_never_falls_back(self):
        from unittest.mock import patch
        from lib.conversation_sync.dispatch_contract import AcceptedResumeLineage
        self.task['_billing_reservation_micro']=30
        self.policy.handoff_from_accepted=True
        loop_thread=threading.get_ident();seen=[]
        def lineage(task):
            self.assertIs(task,self.task);seen.append(threading.get_ident())
            return AcceptedResumeLineage(7,'conversation','turn','target-attempt','task','source-attempt','source-task')
        with patch('lib.conversation_sync.dispatch_contract.read_accepted_resume_lineage',side_effect=lineage):
            await self.policy.load(self.request)
        self.assertNotEqual(seen,[loop_thread])
        self.assertEqual([call[0] for call in self.storage.calls],['handoff','load'])
        payload=self.storage.calls[0][1]
        self.assertEqual(payload['resume'],dict(conversation_id='conversation',turn_id='turn',
            source_attempt_id='source-attempt',target_attempt_id='target-attempt',source_task_id='source-task',reserved_micro=30))
        self.assertEqual(payload['parameters']['task_id'],'task')

    async def test_unknown_handoff_delivery_retains_one_shot_fence(self):
        from unittest.mock import patch
        from lib.conversation_sync.dispatch_contract import AcceptedResumeLineage
        self.policy.handoff_from_accepted=True;self.storage.failure='handoff'
        relation=AcceptedResumeLineage(7,'conversation','turn','target-attempt','task','source-attempt','source-task')
        with patch('lib.conversation_sync.dispatch_contract.read_accepted_resume_lineage',return_value=relation):
            with self.assertRaises(OSError):await self.policy.load(self.request)
        self.assertEqual([call[0] for call in self.storage.calls],['handoff'])
        self.assertIsNone(self.policy.capability)
        with self.assertRaisesRegex(ValueError,'one-shot'):await self.policy.load(self.request)
        self.restore.assert_not_awaited()

    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.executor.shutdown)
        self.storage = SelectedStorage()
        self.task = {'id': 'task', '_userId': 7}
        self.restore = AsyncMock()
        self.policy = NativeCheckpointPolicy(
            principal=principal(), task=self.task, storage=self.storage, executor=self.executor,
            epoch='epoch', binding='binding', token='lease', header_bytes=4096,
            snapshot_bytes=65536, identity_bytes=1024, io_timeout_ms=5000,
            restore=self.restore, suspend=True,
        )
        self.request = CheckpointRequest(principal(), 'task', 'load', 0, 'epoch', 'binding', threading.Event())

    def terminal(self, status='aborted', suspended=True):
        terminal = TerminalRequest(principal(), 'task', 'commit', 1,
            json.dumps({'status': status}).encode(), b'{}')
        return CheckpointTerminalRequest(principal(), 'task', 'epoch', 'binding', terminal,
            suspended, self.request.cancelled)

    def test_oversized_document_bounds_refuse_before_storage_capability_query(self):
        from tofu_db.generated_checkpoint_control import CHECKPOINT_DOCUMENT_MAX_BYTES
        for field in ('header_bytes', 'snapshot_bytes'):
            bounds = dict(header_bytes=4096, snapshot_bytes=65536)
            bounds[field] = CHECKPOINT_DOCUMENT_MAX_BYTES + 1
            with patch.object(self.storage, 'native_checkpoint_available',
                              side_effect=AssertionError('must refuse before storage')):
                with self.assertRaisesRegex(ValueError, 'document capacity'):
                    NativeCheckpointPolicy(principal=principal(), task=self.task, storage=self.storage,
                        executor=self.executor, epoch='epoch', binding='binding', token='lease',
                        identity_bytes=1024, io_timeout_ms=5000, restore=self.restore, **bounds)

    async def test_restore_is_explicit_and_round_calls_delegate_to_same_authority(self):
        self.storage.state = {'financial': 'trusted recovery input'}
        self.assertEqual(await self.policy.load(self.request), self.storage.state)
        self.restore.assert_awaited_once_with(self.request, self.storage.state)
        await self.policy.begin(replace(self.request, action='begin', round=1))
        self.assertEqual(await self.policy.complete(replace(self.request, action='complete', state={})), 'suspend')
        with self.assertRaisesRegex(ValueError, 'one-shot'):
            await self.policy.load(self.request)
        self.assertEqual([c[0] for c in self.storage.calls], ['create', 'load', 'begin', 'complete'])
        await self.policy.close()

    async def test_abandoned_recovery_is_explicit_one_shot_without_create_fallback(self):
        self.policy.recover_abandoned = True
        self.storage.failure = 'recover'
        with self.assertRaisesRegex(OSError, 'unknown native delivery'):
            await self.policy.load(self.request)
        with self.assertRaisesRegex(ValueError, 'one-shot'):
            await self.policy.load(self.request)
        self.assertEqual([c[0] for c in self.storage.calls], ['recover'])
        await self.policy.close()

    async def test_completed_frontier_waits_for_accepted_projection_receipts(self):
        from unittest.mock import patch
        from lib.tasks_pkg.event_delivery import EventDeliveryError
        self.policy.capability = 19
        with patch('lib.tasks_pkg.event_delivery.flush_task_events',
                   side_effect=EventDeliveryError('projection not durable')):
            with self.assertRaisesRegex(EventDeliveryError, 'projection not durable'):
                await self.policy.complete(replace(self.request,action='complete',state={}))
        self.assertEqual(self.storage.calls, [])
        await self.policy.close()

    async def test_paused_lease_releases_only_after_accepted_canonical_terminal(self):
        for accepted in [False, True]:
            self.policy.closed = False
            self.policy.storage = self.storage
            self.policy.capability = 19
            self.storage.calls.clear()
            await self.policy.seal(self.terminal())
            await self.policy.close(terminal_saved=accepted)
            self.assertEqual([c[0] for c in self.storage.calls],
                ['finish', 'release', 'forget'] if accepted else ['finish', 'forget'])

    async def test_error_and_ordinary_abort_never_release_recovery_frontier(self):
        for status, suspended in [('error', True), ('aborted', False), ('done', False)]:
            self.policy.closed = False
            self.policy.storage = self.storage
            self.policy.capability = 19
            self.storage.calls.clear()
            await self.policy.seal(self.terminal(status, suspended))
            await self.policy.close(terminal_saved=True)
            self.assertEqual([c[0] for c in self.storage.calls], ['finish', 'forget'])

    async def test_cancellation_joins_acquisition_and_retains_capability_for_terminal(self):
        entered, release = threading.Event(), threading.Event()
        def create():
            entered.set()
            assert release.wait(5)
        self.storage.create = create
        load = asyncio.create_task(self.policy.load(self.request))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            load.cancel()
            await asyncio.sleep(0)
            self.assertFalse(load.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await load
        self.assertEqual(self.policy.capability, 19)
        self.request.cancelled.set()
        await self.policy.seal(self.terminal(suspended=False))
        await self.policy.close(terminal_saved=True)
        self.assertEqual([c[0] for c in self.storage.calls], ['create', 'finish', 'forget'])

    async def test_unknown_acquisition_and_restore_failure_cannot_claim_no_effect(self):
        self.storage.failure = 'create'
        with self.assertRaises(OSError):
            await self.policy.load(self.request)
        with self.assertRaisesRegex(ValueError, 'delivery unknown'):
            await self.policy.seal(self.terminal())
        await self.policy.close()

    async def test_restore_failure_keeps_owned_capability_for_error_seal(self):
        self.storage.state = {'state': 'durable'}
        self.restore.side_effect = RuntimeError('budget recovery refused')
        with self.assertRaisesRegex(RuntimeError, 'recovery refused'):
            await self.policy.load(self.request)
        await self.policy.seal(self.terminal('error', False))
        await self.policy.close(terminal_saved=True)
        self.assertEqual([c[0] for c in self.storage.calls], ['create', 'load', 'finish', 'forget'])

    async def test_owner_binding_and_cancel_refuse_before_storage_effects(self):
        for request in [replace(self.request, principal=principal(8)),
                        replace(self.request, task_id='foreign'), replace(self.request, binding='changed')]:
            with self.assertRaises(ValueError):
                await self.policy.load(request)
        self.request.cancelled.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.policy.load(self.request)
        self.assertEqual(self.storage.calls, [])
        self.assertEqual(await self.policy.seal(self.terminal(suspended=False)), 'accepted')
        await self.policy.close()
