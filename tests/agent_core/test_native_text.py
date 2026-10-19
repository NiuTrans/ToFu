"""Owned text consumers, explicit ACKs and cancel-time callback quiescence."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from lib.agent_core.native_process import NativeProcessClient, NativeProcessError, _TextControl
from lib.agent_core.native_process import NativeConfigurationError
from tests.agent_core.test_native_process import principal, budget


class Sink:
    def __init__(self):
        self.frames = []
        self.closed = False

    def write(self, raw):
        self.frames.append(json.loads(raw))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def frame(sequence=1, text='hello'):
    return dict(version='fixture/v1', type='text_delta', owner_user_id=7,
                task_id='task', sequence=sequence, request_id=f'task-text-{sequence}',
                channel='content', text=text)


class NativeTextTests(unittest.IsolatedAsyncioTestCase):
    def control(self, callback, limit=64):
        client = NativeProcessClient(binary=Path('/does-not-exist'), sha256='0'*64,
                                     protocol_version='fixture/v1', principal=principal(), budget=budget())
        return _TextControl(client, callback, 'task', limit), SimpleNamespace(stdin=Sink())

    async def test_ordered_owner_bound_text_acknowledges_only_after_consumer(self):
        seen = []
        async def callback(delta):
            seen.append(delta)
        control, process = self.control(callback)
        await control.handle(frame(), process)
        await control.handle(frame(2, '中文'), process)
        self.assertEqual([item.sequence for item in seen], [1, 2])
        self.assertEqual(control.bytes, 11)
        self.assertTrue(all(item['accepted'] for item in process.stdin.frames))
        self.assertEqual(process.stdin.frames[1]['request_id'], 'task-text-2')

    async def test_unbounded_rounds_keep_ordered_delivery_without_total_text_quota(self):
        seen=[]
        async def callback(delta):seen.append(delta.text)
        control,process=self.control(callback,limit=None)
        for number in range(1,5):
            await control.handle(frame(number,'hello'),process)
        self.assertEqual(seen,['hello']*4)
        self.assertEqual(control.bytes,20)
        self.assertTrue(all(value['accepted'] for value in process.stdin.frames))

    async def test_foreign_identity_sequence_channel_and_empty_text_never_reach_consumer(self):
        calls = []
        async def callback(delta):
            calls.append(delta)
        for changes in [dict(owner_user_id=8), dict(task_id='other'), dict(sequence=2),
                        dict(request_id='unissued'), dict(channel='signature'), dict(text='')]:
            control, process = self.control(callback)
            with self.assertRaises(NativeProcessError):
                await control.handle(dict(frame(), **changes), process)
        self.assertEqual(calls, [])

    async def test_utf8_budget_rejects_before_callback(self):
        async def callback(_delta):
            self.fail('over-limit text observed')
        control, process = self.control(callback, limit=5)
        with self.assertRaises(NativeProcessError):
            await control.handle(frame(text='中文'), process)
        self.assertEqual(process.stdin.frames, [])

    async def test_consumer_failure_is_rejected_with_original_cause(self):
        failure = OSError('fixture projection failure')
        async def callback(_delta):
            raise failure
        control, process = self.control(callback)
        with self.assertRaises(NativeProcessError) as caught:
            await control.handle(frame(), process)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertTrue(control.failed)
        self.assertFalse(process.stdin.frames[0]['accepted'])
        await control.handle(frame(2), process)
        self.assertFalse(process.stdin.frames[1]['accepted'])

    async def test_repeated_cancel_waits_for_consumer_then_ack_before_release(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def callback(_delta):
            entered.set()
            await release.wait()
        control, process = self.control(callback)
        task = asyncio.create_task(control.handle(frame(), process))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(process.stdin.frames, [])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(process.stdin.frames[0]['accepted'])

    async def test_missing_consumer_refuses_before_binary_acquisition(self):
        async def callback(_delta):
            pass
        control, _process = self.control(callback)
        with self.assertRaises(NativeConfigurationError):
            await control.client.run({'owner_user_id':7,'version':'fixture/v1','type':'configure',
                'task_id':'task','tools':{},'text_progress':True},callbacks={},working_directory=Path('/tmp'))
        self.assertEqual(control.client.active_runs,0)
        await control.client.close()

    async def test_ack_io_failure_preserves_consumer_failure_cause(self):
        failure=OSError('original receiver failure')
        async def callback(_delta):
            raise failure
        control,process=self.control(callback)
        def broken(_raw):
            raise BrokenPipeError('fixture closed pipe')
        process.stdin.write=broken
        with self.assertRaises(NativeProcessError) as caught:
            await control.handle(frame(),process)
        self.assertIs(caught.exception.__cause__,failure)


    async def test_unlimited_round_text_shutdown_still_delivers_settlement_and_terminal_ack(self):
        seen=[]
        completed=asyncio.Event()
        class Process:
            def __init__(self):
                self.stdin=Sink()
                self.stdout=asyncio.StreamReader()
                self.returncode=None
                self.killed=False
            async def wait(self):
                await completed.wait()
                self.returncode=-9 if self.killed else 0
                return self.returncode
            def kill(self):
                self.killed=True
                completed.set()
        process=Process()
        for kind in ('round_settled','terminal_request'):
            process.stdout.feed_data(json.dumps({'type':kind}).encode()+b'\n')
        process.stdout.feed_eof()
        class Rounds:
            async def handle(self,value,child,*,closing):
                seen.append((value['type'],closing))
        class Terminal:
            async def handle(self,value,child):
                seen.append((value['type'],True))
                completed.set()
        control,_=self.control(lambda _delta:None,limit=None)
        await control.client._shutdown(process,Rounds(),Terminal(),control)
        self.assertFalse(process.killed)
        self.assertTrue(process.stdin.closed)
        self.assertEqual(process.returncode,0)
        self.assertEqual(seen,[('round_settled',True),('terminal_request',True)])


if __name__ == '__main__':
    # Use the shared pytest storage and lifecycle isolation for direct runs.
    import sys as _runner_sys
    import pytest
    raise SystemExit(pytest.main([__file__, *_runner_sys.argv[1:]]))
