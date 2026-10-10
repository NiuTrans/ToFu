"""Approval authority, exact single-use host proofs and cancel-time joining."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from lib.agent_core.native_process import NativeProcessClient, NativeProcessError, NativeConfigurationError, _ApprovalControl
from tests.agent_core.test_native_process import principal, budget
from tests.agent_core.test_native_text import Sink


def frame():
    return dict(version='fixture/v1',type='approval_request',owner_user_id=7,task_id='task',
                epoch='epoch',request_id='task-approval-0',call_id='call',name='write',arguments={'text':'original'},caller=None,remaining_ms=1000)


class NativeApprovalTests(unittest.IsolatedAsyncioTestCase):
    def control(self, policy):
        client = NativeProcessClient(binary=Path('/does-not-exist'),sha256='0'*64,
            protocol_version='fixture/v1',principal=principal(),budget=budget())
        return _ApprovalControl(client,policy,'task','epoch',{'write':{'permission':'write'}},
            {'receipts':2,'identity_bytes':4096}), SimpleNamespace(stdin=Sink())

    async def test_exact_original_arguments_are_single_use_and_callback_views_detached(self):
        class Policy:
            async def decide(self, request):
                request.arguments['text']='rewritten'
                return 'auto_write_mode'
        control, process = self.control(Policy())
        await control.handle(frame(),process)
        tool=dict(frame(),type='tool_request',request_id='tool')
        control.consume(tool)
        self.assertEqual(control.bytes,0)
        with self.assertRaises(NativeProcessError):
            control.consume(tool)
        self.assertEqual(process.stdin.frames[0]['decision'],'auto_write_mode')

    async def test_changed_arguments_or_caller_cannot_execute_and_consumes_the_old_proof(self):
        class Policy:
            async def decide(self,_request): return 'human'
        for mutation in [dict(arguments={'text':'different'}),dict(caller={'role':'foreign'})]:
            control, process = self.control(Policy())
            await control.handle(frame(),process)
            with self.assertRaises(NativeProcessError):
                control.consume(dict(frame(),**mutation))
            self.assertEqual(control.pending,{})

    async def test_foreign_identity_and_duplicate_pending_call_refuse_before_decision(self):
        calls=[]
        class Policy:
            async def decide(self,request): calls.append(request);return 'human'
        for mutation in [dict(owner_user_id=8),dict(epoch='other'),dict(task_id='other'),dict(name='unknown')]:
            control,process=self.control(Policy())
            with self.assertRaises(NativeProcessError):await control.handle(dict(frame(),**mutation),process)
        self.assertEqual(calls,[])
        control,process=self.control(Policy());await control.handle(frame(),process)
        with self.assertRaises(NativeProcessError):await control.handle(dict(frame(),request_id='task-approval-1'),process)
        self.assertEqual(len(calls),1)

    async def test_denied_and_failed_policy_never_leave_execution_proofs(self):
        failure=OSError('fixture policy unavailable')
        for decision in ['denied','fault']:
            class Policy:
                async def decide(self,_request):
                    if decision=='fault':raise failure
                    return decision
            control,process=self.control(Policy())
            if decision=='fault':
                with self.assertRaises(NativeProcessError) as caught:await control.handle(frame(),process)
                self.assertIs(caught.exception.__cause__,failure)
                self.assertEqual(process.stdin.frames[-1]['decision'],'failed')
            else:await control.handle(frame(),process)
            with self.assertRaises(NativeProcessError):control.consume(frame())

    async def test_cancel_is_sent_before_joined_late_approval_and_denial(self):
        entered, release=asyncio.Event(),asyncio.Event()
        class Policy:
            async def decide(self,request):
                entered.set();await request.cancelled.wait();await release.wait();return 'human'
        control,process=self.control(Policy())
        run=asyncio.create_task(control.handle(frame(),process));await entered.wait();run.cancel()
        await asyncio.sleep(0);run.cancel();await asyncio.sleep(0)
        self.assertFalse(run.done());self.assertEqual(process.stdin.frames[0]['type'],'cancel')
        release.set()
        with self.assertRaises(asyncio.CancelledError):await run
        self.assertEqual(process.stdin.frames[-1]['decision'],'denied');self.assertEqual(control.pending,{})

    async def test_write_tools_without_policy_refuse_before_image_acquisition(self):
        class Policy:
            async def decide(self,_request):return 'human'
        control,_process=self.control(Policy())
        with self.assertRaises(NativeConfigurationError):
            await control.client.run({'version':'fixture/v1','type':'configure','owner_user_id':7,
                'task_id':'task','tools':{'write':{'permission':'write'}}},callbacks={'write':lambda _r:None},working_directory=Path('/tmp'))
        self.assertEqual(control.client.active_runs,0);await control.client.close()

    async def test_deadline_signals_and_joins_late_decision_without_granting_it(self):
        class Policy:
            async def decide(self,request):
                await request.cancelled.wait();return 'human'
        control,process=self.control(Policy())
        await control.handle(dict(frame(),remaining_ms=1),process)
        self.assertEqual(control.pending,{})
        self.assertEqual(process.stdin.frames[-1]['decision'],'denied')

    async def test_policy_timeout_error_is_a_failure_not_deadline_denial(self):
        failure=TimeoutError('upstream policy unavailable')
        class Policy:
            async def decide(self,_request):raise failure
        control,process=self.control(Policy())
        with self.assertRaises(NativeProcessError) as caught:await control.handle(frame(),process)
        self.assertIs(caught.exception.__cause__,failure)
        self.assertEqual(process.stdin.frames[-1]['decision'],'failed')

    async def test_invalid_or_self_cancelled_policy_fails_without_fabricating_user_stop(self):
        class Invalid:
            def decide(self,_request):return 'human'
        class Cancelled:
            async def decide(self,_request):raise asyncio.CancelledError
        for policy in [Invalid(),Cancelled()]:
            control,process=self.control(policy)
            with self.assertRaises(NativeProcessError):await control.handle(frame(),process)
            self.assertEqual([item['type'] for item in process.stdin.frames],['approval_decision'])
            self.assertEqual(process.stdin.frames[0]['decision'],'failed')

    def test_transport_authorities_match_the_shared_schema(self):
        schema=json.loads(Path('contracts/native_agent_stdio_v1.schema.json').read_text())
        values=set(schema['$defs']['approval_decision']['properties']['decision']['enum'])
        self.assertEqual(values,_ApprovalControl.AUTHORITIES|{'denied','failed'})


if __name__=='__main__':unittest.main()
