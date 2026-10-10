"""Application composition refuses before route consumption and binds commit."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from lib.tasks_pkg.native_execution import run_native_task
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy
from tests.agent_core.test_native_task_terminal import principal


class NativeTaskExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.executor.shutdown)
        self.client = SimpleNamespace(principal=principal(), budget=SimpleNamespace(frame_bytes=4096))
        self.task = {'id': 'task', '_userId': 7, 'status': 'running'}
        self.config = {'owner_user_id': 7, 'task_id': 'task', 'terminal_persistence': {
            'operation_id': 'commit', 'attempts': 2, 'ack_ms': 500, 'frame_bytes': 4096,
        }}

    async def run_task(self, config):
        return await run_native_task(
            self.client, object(), config, task=self.task, executor=self.executor,
            project_terminal=lambda *_args: None, prepare_template=lambda _meta: {},
            callbacks={}, working_directory=Path('/tmp'),
        )

    async def test_owner_profile_and_task_refuse_before_routed_execution(self):
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            for config in [{}, dict(self.config, task_id='foreign'),
                           dict(self.config, owner_user_id=8),
                           dict(self.config, terminal_persistence={}),
                           dict(self.config, terminal_persistence=dict(
                               self.config['terminal_persistence'], frame_bytes=4097))]:
                with self.assertRaises(ValueError):
                    await self.run_task(config)
            self.task['_userId'] = 8
            with self.assertRaises(ValueError):
                await self.run_task(self.config)
        run.assert_not_awaited()

    async def test_explicit_task_commit_is_bound_to_existing_native_route(self):
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            run.return_value = object()
            self.assertIs(await self.run_task(self.config), run.return_value)
        policy = run.call_args.kwargs['terminal_policy']
        self.assertIsInstance(policy, NativeTaskTerminalPolicy)
        self.assertIs(policy.task, self.task)
        self.assertIs(policy.executor, self.executor)
        self.assertEqual(policy.operation_id, 'commit')

    async def test_application_deadline_can_shorten_but_never_extend_task_expiry(self):
        self.task['config'] = {'executionLimits': {
            'max_tokens': None, 'max_tool_calls': None, 'deadline': 100.125}}
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as run:
            for supplied, expected in [(None, 100125), (200000, 100125), (90000, 90000), (0, 0)]:
                config = dict(self.config)
                if supplied is not None:
                    config['deadline_unix_ms'] = supplied
                await self.run_task(config)
                self.assertEqual(run.call_args.args[2]['deadline_unix_ms'], expected)
                self.assertEqual(config.get('deadline_unix_ms'), supplied)
            for supplied in [None, True, -1, 18446744073709551616]:
                with self.assertRaisesRegex(ValueError, 'absolute deadline'):
                    await self.run_task(dict(self.config, deadline_unix_ms=supplied))

    async def test_checkpoint_cleanup_preserves_primary_failure_and_terminal_ack(self):
        from tests.agent_core.test_native_checkpoint_policy import SelectedStorage
        from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
        checkpoint = NativeCheckpointPolicy(
            principal=principal(), task=self.task, storage=SelectedStorage(), executor=self.executor,
            epoch='epoch', binding='binding', token='lease', header_bytes=4096,
            snapshot_bytes=65536, identity_bytes=1024, io_timeout_ms=5000,
            restore=AsyncMock(),
        )
        config = dict(self.config, epoch='epoch')
        async def run(**kwargs):
            return await run_native_task(self.client, object(), config, task=self.task,
                executor=self.executor, project_terminal=lambda *_: None,
                prepare_template=lambda _: {}, callbacks={}, working_directory=Path('/tmp'),
                checkpoint_policy=checkpoint, **kwargs)
        checkpoint.close = AsyncMock(side_effect=OSError('cleanup unavailable'))
        primary = RuntimeError('native execution failed')
        with patch('lib.tasks_pkg.native_execution.run_native_route', new_callable=AsyncMock) as route:
            route.side_effect = primary
            with self.assertRaises(RuntimeError) as caught:
                await run()
        self.assertIs(caught.exception, primary)
        self.assertEqual(primary.__notes__, ['native checkpoint cleanup failed: OSError'])
        checkpoint.close.assert_awaited_once_with(terminal_saved=False)
        checkpoint.close = AsyncMock()
        async def accepted(*args, **kwargs):
            kwargs['terminal_policy']._accepted = True
            return 'committed'
        with patch('lib.tasks_pkg.native_execution.run_native_route', side_effect=accepted):
            self.assertEqual(await run(), 'committed')
        checkpoint.close.assert_awaited_once_with(terminal_saved=True)


if __name__ == '__main__':
    unittest.main()
