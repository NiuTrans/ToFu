"""Application callbacks preserve native lease debt and joined cancellation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import AsyncMock

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
