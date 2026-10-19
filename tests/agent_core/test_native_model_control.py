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

    async def test_native_wallet_admission_failure_never_enters_credentials_or_provider(self):
        from unittest.mock import patch
        self.task['_nativePricingRequired'] = True
        headers = []
        self.policy.prepare_headers = lambda request: headers.append(request)
        await self.control.handle(self.frame('reserve'), self.process)
        with patch('lib.billing.native_admission.protect_before_dispatch',
                   side_effect=OSError('wallet admission ACK lost')) as admission:
            await self.control.handle(self.frame('dispatch'), self.process)
            admission.assert_called_once_with(self.task, budget=self.account, owner=7)
        self.assertFalse(self.process.stdin.frames[-1]['accepted'])
        self.assertFalse(self.policy.dispatch_entered)
        self.assertFalse(headers)
        await self.control.abandon()
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(), (0, 0, True))

    async def test_cancel_after_native_wallet_ack_still_refuses_provider_entry(self):
        from unittest.mock import patch
        self.task['_nativePricingRequired'] = True
        await self.control.handle(self.frame('reserve'), self.process)
        with patch('lib.billing.native_admission.protect_before_dispatch',
                   side_effect=lambda *a, **k: self.task['abort_event'].set()):
            with self.assertRaises(asyncio.CancelledError):
                await self.control.handle(self.frame('dispatch'), self.process)
        self.assertEqual(len(self.process.stdin.frames), 1)
        self.assertFalse(self.policy.dispatch_entered)
        await self.control.abandon()
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(), (0, 0, True))

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

    async def test_null_expiry_requires_explicit_unbounded_callback_policy(self):
        with self.assertRaises(NativeProcessError):
            await self.control.handle(self.frame('reserve',expires_monotonic_ns=None),self.process)
        self.assertIsNone(self.control.pending)
        self.assertEqual(self.process.stdin.frames,[])
        self.assertEqual(self.account.continuity_snapshot()['pending_model_requests'],0)

    async def test_unbounded_callbacks_complete_and_settle_once_after_delay(self):
        self.control=ModelControl(self.client,self.policy,'task',unbounded_ack=True)
        entered=[]
        for action in ('reserve','dispatch','settle'):
            original=getattr(self.policy,action)
            async def delayed(request, *, original=original, action=action):
                entered.append(action)
                await asyncio.sleep(.03)
                return await original(request)
            setattr(self.policy,action,delayed)
        for action in ('reserve','dispatch','settle'):
            fields=dict(expires_monotonic_ns=None)
            if action=='settle':fields['usage']={'prompt_tokens':3,'completion_tokens':2}
            await asyncio.wait_for(self.control.handle(self.frame(action,**fields),self.process),1)
            self.assertTrue(self.process.stdin.frames[-1]['accepted'])
        self.assertEqual(entered,['reserve','dispatch','settle'])
        self.assertIsNone(self.control.failure)
        await self.control.abandon()
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(5,0,True))
        self.assertEqual(self.account.continuity_snapshot()['pending_model_requests'],0)

    async def test_unbounded_callback_cancel_still_joins_before_abandon(self):
        self.control=ModelControl(self.client,self.policy,'task',unbounded_ack=True)
        entered=asyncio.Event();release=asyncio.Event();finished=asyncio.Event()
        original=self.policy.reserve
        async def delayed(request):
            entered.set()
            await release.wait()
            try:return await original(request)
            finally:finished.set()
        self.policy.reserve=delayed
        operation=asyncio.create_task(self.control.handle(
            self.frame('reserve',expires_monotonic_ns=None),self.process))
        await asyncio.wait_for(entered.wait(),1)
        operation.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.control.pending.cancelled.is_set())
        self.assertFalse(operation.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(operation,1)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.process.stdin.frames,[])
        await self.control.abandon()
        self.assertEqual(self.account.continuity_snapshot()['pending_model_requests'],0)

    async def test_unbounded_policy_preserves_finite_attempt_deadline(self):
        self.control=ModelControl(self.client,self.policy,'task',unbounded_ack=True)
        original=self.policy.reserve
        async def delayed(request):
            await asyncio.sleep(.03)
            return await original(request)
        self.policy.reserve=delayed
        await self.control.handle(self.frame('reserve',
            expires_monotonic_ns=time.monotonic_ns()+1_000_000),self.process)
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


    async def test_oauth_binding_is_owned_immutable_and_released_after_joined_scope(self):
        metadata=dict(owner_user_id=7,credential_kind='oauth',protocol='openai_responses',responses_profile='codex',credential_id='credential-a',wire_model='fixture',provider_id='provider-a',offering_id='offering-a',connection_id='connection-a',deployment_id='deployment-a')
        observed=[]
        def resolve(request):
            observed.append(request)
            headers=request.extra_headers
            self.assertEqual(headers,{'X-Region':'fixture'})
            headers['X-Region']='mutated'
            self.assertEqual(request.extra_headers,{'X-Region':'fixture'})
            return {'Authorization':'Bearer owned-fixture','session-id':request.cache_affinity_key}
        options=dict(principal=principal(),task_id='task',metadata=metadata,subscription='codex',secret_reference='secret-a',endpoint='https://owned.example/responses',extra_headers={'X-Region':'fixture'},resolver=resolve)
        from tests.model_routing.test_native_route import principal as route_principal
        for changed in [dict(options,task_id='foreign'),dict(options,principal=route_principal(8)),dict(options,subscription='claude')]:
            with self.assertRaises(ValueError):
                with self.policy.bind_credentials(**changed):pass
        self.assertIsNone(self.policy.prepare_headers)
        with self.policy.bind_credentials(**options) as cache_key:
            await self.control.handle(self.frame('reserve'),self.process)
            await self.control.handle(self.frame('dispatch'),self.process)
            self.assertEqual(self.process.stdin.frames[-1]['headers']['session-id'],cache_key)
            await self.control.handle(self.frame('settle',usage={'prompt_tokens':3,'completion_tokens':2}),self.process)
        self.assertIsNone(self.policy.prepare_headers)
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0].credential_id,'credential-a')
        self.assertNotIn('secret-a',repr(observed[0]))
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(5,0,True))


    async def test_explicit_pricing_is_after_account_settlement_and_snapshot_is_frozen(self):
        from lib.agent_core.native_model_control import ModelPricing
        observed=[]
        def prepare(result):
            observed.append(self.account.continuity_snapshot()['pending_model_requests'])
            return ModelPricing.resolved(dict(prompt_tokens=4,input=2.5,output=10.,cache_write_multiplier=1.25,cache_read_multiplier=.1,usd_to_cny=7.24,currency='USD',margin=.2))
        self.policy.prepare_pricing=prepare
        self.control.pricing=True
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.handle(self.frame('dispatch'),self.process)
        await self.control.handle(self.frame('settle',usage={'prompt_tokens':4,'completion_tokens':2},unsent=False),self.process)
        self.assertEqual(observed,[0])
        self.assertEqual(self.process.stdin.frames[-1]['price']['prompt_tokens'],4)
        self.assertEqual(self.account.consumption_snapshot(),(6,0,False))
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(6,0,True))

    async def test_pricing_callback_failure_keeps_paid_settlement_and_never_repeats_preparation(self):
        count=[]
        def prepare(result):
            count.append(result)
            raise ValueError('price unavailable')
        self.policy.prepare_pricing=prepare
        self.control.pricing=True
        await self.control.handle(self.frame('reserve'),self.process)
        await self.control.handle(self.frame('dispatch'),self.process)
        await self.control.handle(self.frame('settle',usage={'prompt_tokens':4,'completion_tokens':2},unsent=False),self.process)
        await self.control.abandon()
        self.assertEqual(len(count),1)
        self.assertEqual(self.account.consumption_snapshot(),(6,0,False))
        self.account.close(self.task)
        self.assertEqual(self.account.consumption_snapshot(),(6,0,True))
        self.assertFalse(self.process.stdin.frames[-1]['accepted'])
        self.assertIsInstance(self.control.failure,ValueError)


class ResolvedPriceValidationTests(unittest.TestCase):
    def test_price_wire_has_one_closed_finite_numeric_shape(self):
        from lib.agent_core.native_model_control import ModelPricing
        valid=dict(prompt_tokens=10,input=2.5,output=10.,cache_write_multiplier=1.25,cache_read_multiplier=.1,usd_to_cny=7.24,currency='USD',margin=.2)
        for bad in [dict(valid,input=True),dict(valid,output=float('nan')),dict(valid,usd_to_cny=0),dict(valid,prompt_tokens=True),dict(valid,currency='EUR'),dict(valid,extra=1),dict(valid,margin=-1)]:
            with self.assertRaises(ValueError):ModelPricing.resolved(bad)
        frozen=ModelPricing.resolved(valid);valid['input']=100
        self.assertEqual(frozen.values()['input'],2.5)
        with self.assertRaises(ValueError):ModelPricing(b'x'*4097).values()
