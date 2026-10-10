"""Counted SDK callbacks retain one shared account and joined cleanup debt."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace
import unittest

from lib.agent_core.native_model_control import ModelControl, ModelRequest, ModelSettlement
from lib.agent_core.native_process import NativeProcessClient, NativeProcessError
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_task_terminal import principal


class Writer:
    def __init__(self): self.frames=[]
    def write(self,value): self.frames.append(json.loads(value))
    async def drain(self): pass


class ModelControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor=ThreadPoolExecutor(max_workers=1);self.addCleanup(self.executor.shutdown)
        self.root={'id':'parent','_userId':7,'config':{'executionLimits':dict(max_tokens=50,max_tool_calls=None,deadline=None)}}
        self.account=ExecutionBudget(self.root)
        self.task={'id':'task','_userId':7,'config':{},'abort_event':threading.Event(),'_executionBudget':self.account}
        self.account.attach(self.task)
        self.policy=NativeModelAccountPolicy(principal=principal(),task=self.task,executor=self.executor)
        self.client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1',budget=SimpleNamespace(frame_bytes=65536,shutdown_seconds=1),_encode=lambda value,limit:json.dumps(value).encode())
        self.control=ModelControl(self.client,self.policy,'task');self.process=SimpleNamespace(stdin=Writer())

    def frame(self,action,**fields):
        value=dict(version=self.client.protocol_version,type='model_request',request_id=action,owner_user_id=7,task_id='task',attempt_ordinal=1,
            action=action,input_tokens=20,max_output_tokens=100,minimum_output_tokens=1,expires_monotonic_ns=time.monotonic_ns()+1000000000)
        if action=='settle':value.update(usage=None,unsent=False)
        return dict(value,**fields)

    async def test_shared_parent_cap_and_reported_usage_settle_exactly_once(self):
        await self.control.handle(self.frame('reserve'),self.process)
        self.assertEqual(self.process.stdin.frames[-1]['output_tokens'],30)
        await self.control.handle(self.frame('dispatch'),self.process)
        usage={'prompt_tokens':20,'completion_tokens':10}
        await self.control.handle(self.frame('settle',usage=usage),self.process)
        self.assertIsNone(self.control.pending)
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(30,0,True))
        await self.control.abandon()
        self.assertEqual(self.account.continuity_snapshot()['pending_model_requests'],0)

    async def test_process_loss_after_dispatch_preserves_unknown_usage(self):
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.handle(self.frame('dispatch'),self.process)
        await self.control.abandon()
        self.assertEqual(self.account.consumption_snapshot(),(0,0,False))
        with self.assertRaises(ModelBudgetExceeded):self.account.reserve_model(self.task,input_tokens=1,max_output_tokens=1)

    async def test_before_dispatch_cleanup_is_proven_unsent(self):
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.abandon()
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(0,0,True))
        self.assertEqual(self.account.continuity_snapshot()['pending_model_requests'],0)

    async def test_closing_dispatch_never_marks_account_and_settlement_voids(self):
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.handle(self.frame('dispatch'),self.process,closing=True)
        self.assertFalse(self.process.stdin.frames[-1]['accepted'])
        await self.control.handle(self.frame('settle'),self.process,closing=True)
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(0,0,True))

    async def test_foreign_changed_and_duplicate_requests_refuse_before_effects(self):
        with self.assertRaises(NativeProcessError):await self.control.handle(self.frame('reserve',owner_user_id=True),self.process)
        await self.control.handle(self.frame('reserve'),self.process)
        with self.assertRaises(NativeProcessError):await self.control.handle(self.frame('reserve'),self.process)
        with self.assertRaises(NativeProcessError):await self.control.handle(self.frame('dispatch',input_tokens=21),self.process)
        await self.control.abandon()
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(0,0,True))

    async def test_settlement_retry_preserves_paid_candidate(self):
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.handle(self.frame('dispatch'),self.process)
        original=self.policy.settle;observed=[]
        async def debt(result):
            observed.append(result.usage_json)
            if len(observed)==1:raise OSError('settlement acknowledgement unavailable')
            return await original(result)
        self.policy.settle=debt
        await self.control.handle(self.frame('settle',usage={'prompt_tokens':20,'completion_tokens':10}),self.process)
        await self.control.abandon()
        self.assertEqual(observed[0],observed[1])
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(30,0,True))

    async def test_timeout_joins_callback_before_cleanup_and_sends_no_stale_ack(self):
        entered=asyncio.Event();finished=asyncio.Event();original=self.policy.reserve
        async def slow(request):
            entered.set();await asyncio.sleep(.03)
            try:return await original(request)
            finally:finished.set()
        self.policy.reserve=slow
        await self.control.handle(self.frame('reserve',expires_monotonic_ns=time.monotonic_ns()+1000000),self.process)
        self.assertTrue(entered.is_set() and finished.is_set())
        self.assertIsInstance(self.control.failure,TimeoutError)
        self.assertEqual(self.process.stdin.frames,[])
        await self.control.abandon()
        self.assertEqual(self.account.continuity_snapshot()['pending_model_requests'],0)

    async def test_dispatch_headers_are_frozen_redacted_and_only_joined_before_ack(self):
        from lib.agent_core.native_model_control import ModelDispatch
        original={'Authorization':'Bearer private-fixture-token','X-Session':'session'}
        prepared=ModelDispatch.headers(original)
        original['Authorization']='changed'
        self.assertNotIn('private-fixture-token',repr(prepared))
        self.policy.prepare_headers=lambda request: prepared.values()
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.handle(self.frame('dispatch'),self.process)
        self.assertEqual(self.process.stdin.frames[-1]['headers']['Authorization'],'Bearer private-fixture-token')
        await self.control.handle(self.frame('settle',usage={'prompt_tokens':3,'completion_tokens':2}),self.process)
        self.assertNotIn('headers',self.process.stdin.frames[-1])
        self.assertEqual(self.account.consumption_snapshot(),(5,0,False))
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(5,0,True))

    async def test_invalid_credential_headers_refuse_before_account_dispatch(self):
        for headers in [{'Host':'other'},{'Authorization':'token\r\nX-Injected: value'},
                        {'X-Token':'one','x-token':'two'},{'Content-Length':'0'}, {'X-Token':True}]:
            from lib.agent_core.native_model_control import ModelDispatch
            with self.assertRaises((ValueError,TypeError)):ModelDispatch.headers(headers)
        self.policy.prepare_headers=lambda request:{'Authorization':'token\nsecret'}
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.handle(self.frame('dispatch'),self.process)
        self.assertFalse(self.process.stdin.frames[-1]['accepted'])
        self.assertNotIn('headers',self.process.stdin.frames[-1])
        await self.control.abandon()
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(0,0,True))

    async def test_expired_credential_refresh_is_joined_without_dispatch_or_stale_ack(self):
        finished=threading.Event()
        def prepare(request):
            time.sleep(.03)
            finished.set()
            return {'Authorization':'Bearer expired-fixture'}
        self.policy.prepare_headers=prepare
        await self.control.handle(self.frame('reserve'),self.process)
        before=len(self.process.stdin.frames)
        await self.control.handle(self.frame('dispatch',expires_monotonic_ns=time.monotonic_ns()+1_000_000),self.process)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(self.process.stdin.frames),before)
        await self.control.abandon()
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(0,0,True))
