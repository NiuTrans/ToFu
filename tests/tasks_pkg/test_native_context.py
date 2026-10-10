"""Real prompt admission over native route bounds and joined caller execution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

import pytest

from lib.agent_core.native_process import RoundRequest
from lib.identity import PrincipalContext
from lib.llm_dispatch.context_policy import task_context_profile
from lib.llm_dispatch.native_context_scope import native_context_scope
from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
from lib.tasks_pkg.native_context import NativeContextPolicy

pytestmark = pytest.mark.unit


def principal():
    return PrincipalContext.user(subject_id='native-context-test', owner_user_id=7)


def task():
    return {'id': 'native-context', '_userId': 7, 'status': 'running',
            'config': {'model': 'fixture'}, 'abort_event': threading.Event()}


def metadata():
    return {'owner_user_id': 7, 'logical_model': 'fixture', 'wire_model': 'fixture-wire',
            'context_window': 131072, 'provider_id': 'fixture-provider'}


class Accounting:
    def __init__(self):
        self.events = []

    async def reserve(self, request):
        self.events.append(('reserve', request.round))
        return True

    async def settle(self, result):
        self.events.append(('settle', result.request.round))


def test_scoped_route_window_output_and_owner_reset_without_dispatcher():
    owner_task = task()
    with patch('lib.llm_dispatch.context_policy.pinned_context_profile', return_value=None):
        assert task_context_profile(owner_task) is None
        with native_context_scope(owner_task, metadata(), 90000):
            profile = task_context_profile(owner_task)
            assert profile['window'] == 131072
            assert profile['output_tokens'] == 90000
            assert resolve_admission_budget(owner_task)[0] <= 41072
            assert task_context_profile(dict(owner_task, _userId=8)) is None
            assert task_context_profile(dict(owner_task, id='another')) is None
        assert task_context_profile(owner_task) is None


def test_learned_route_shrink_never_expands_declared_window_or_admits_excess_output():
    owner_task = task()
    with patch('lib.context_limits.resolve_learned_context_limit', return_value=999999):
        with native_context_scope(owner_task, metadata(), 1024):
            assert task_context_profile(owner_task)['window'] == 131072
    with patch('lib.context_limits.resolve_learned_context_limit', return_value=512):
        with pytest.raises(ValueError):
            with native_context_scope(owner_task, metadata(), 1024):
                pass


def test_summary_pin_requires_exact_owner_and_primary_route_identity():
    from types import SimpleNamespace
    from lib.llm_dispatch.native_context_scope import native_compaction_route_ready
    owner_task=dict(task(),_pinned_provider_id='owned')
    route=dict(metadata(),offering_id='offer',deployment_id='deploy',connection_id='connection',credential_id='credential')
    slot=SimpleNamespace(provider_id='owned',model='fixture-wire',logical_model='fixture',
        routing_owner_user_id=7,routing_provider_id='fixture-provider',route_offering_id='offer',
        route_deployment_id='deploy',route_connection_id='connection',route_credential_id='credential')
    with patch('lib.llm_dispatch.factory.get_dispatcher',return_value=SimpleNamespace(slots=[slot])):
        with native_context_scope(owner_task,route,1024):
            assert native_compaction_route_ready(owner_task)
            slot.route_credential_id='foreign'
            assert not native_compaction_route_ready(owner_task)
            slot.route_credential_id='credential';slot.routing_owner_user_id=8
            assert not native_compaction_route_ready(owner_task)


def test_real_admission_preserves_tool_and_encrypted_reasoning_evidence():
    async def run(executor):
        owner_task = task(); account = Accounting()
        policy = NativeContextPolicy(principal=principal(), task=owner_task,
                                     executor=executor, round_policy=account)
        policy.bind_route(metadata(), {'max_tokens': 1024}, {})
        messages = [{'role': 'user', 'content': 'question'},
                    {'role': 'assistant', 'content': '', '_responses_items': [
                        {'type': 'reasoning', 'encrypted_content': 'opaque'}],
                     'tool_calls': [{'type': 'function', 'id': 'call', 'function': {
                         'name': 'read', 'arguments': '{}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call', 'content': 'evidence'}]
        request = RoundRequest(principal(), owner_task['id'], 'request', 1, 3, messages)
        prepared = await policy.prepare(request)
        assert prepared == messages
        assert owner_task['_lastPromptAdmission']['action'] == 'admit'
        assert owner_task['_contextBudgetDecision']['contextWindowTokens'] == 131072
        assert not account.events
        assert await policy.reserve(request) is True
        assert account.events == [('reserve', 1)]
        policy.close()
        with pytest.raises(ValueError):
            await policy.prepare(request)
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_native_pressure_without_owned_summary_route_never_buys_a_summary():
    from lib.llm_errors import PromptTooLongError
    async def run(executor):
        owner_task=task(); account=Accounting()
        policy=NativeContextPolicy(principal=principal(),task=owner_task,executor=executor,round_policy=account)
        route=dict(metadata(),context_window=2048)
        policy.bind_route(route,{'max_tokens':32},{})
        request=RoundRequest(principal(),owner_task['id'],'request',0,1,
                             [{'role':'user','content':'required user intent '*2000}])
        with patch('lib.tasks_pkg.compaction._prompt_admission.force_compact_if_needed') as summary:
            with pytest.raises(PromptTooLongError):
                await policy.prepare(request)
            summary.assert_not_called()
        assert not account.events
        assert owner_task['_lastPromptAdmission']['action']=='refuse_summary_route'
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_cancellation_signals_and_joins_worker_before_return():
    async def run(executor):
        owner_task = task(); entered = threading.Event(); release = threading.Event()
        account = Accounting()
        policy = NativeContextPolicy(principal=principal(), task=owner_task,
                                     executor=executor, round_policy=account)
        policy.bind_route(metadata(), {'max_tokens': 1024}, {})
        def prepare(*args, **kwargs):
            entered.set()
            assert release.wait(5)
        request = RoundRequest(principal(), owner_task['id'], 'request', 0, 1,
                               [{'role': 'user', 'content': 'question'}])
        with patch('lib.tasks_pkg.compaction._prompt_admission.enforce_dispatch_prompt_limit', prepare):
            pending = asyncio.create_task(policy.prepare(request))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            pending.cancel(); await asyncio.sleep(0)
            assert owner_task['abort_event'].is_set()
            assert not pending.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert not account.events
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


def test_running_preparation_timeout_signals_and_joins_before_return():
    async def run(executor):
        owner_task=task(); account=Accounting(); entered=threading.Event(); release=threading.Event()
        policy=NativeContextPolicy(principal=principal(),task=owner_task,executor=executor,round_policy=account)
        policy.bind_route(metadata(),{'max_tokens':1024},{})
        def slow(*args,**kwargs):
            entered.set();assert owner_task['abort_event'].wait(5);assert release.wait(5)
        request=RoundRequest(principal(),owner_task['id'],'request',0,1,[{'role':'user','content':'question'}],time.monotonic_ns()+50_000_000)
        with patch('lib.tasks_pkg.compaction._prompt_admission.enforce_dispatch_prompt_limit',slow):
            pending=asyncio.create_task(policy.prepare(request))
            while not owner_task['abort_event'].is_set():await asyncio.sleep(0.001)
            assert entered.is_set() and not pending.done()
            release.set()
            with pytest.raises(TimeoutError):await pending
        assert not account.events
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))


def test_queued_preparation_timeout_does_not_wait_for_unrelated_worker():
    async def run(executor):
        owner_task=task();account=Accounting();occupied=threading.Event();release=threading.Event()
        def blocker():occupied.set();assert release.wait(5)
        blocking=executor.submit(blocker);assert occupied.wait(5)
        policy=NativeContextPolicy(principal=principal(),task=owner_task,executor=executor,round_policy=account)
        policy.bind_route(metadata(),{'max_tokens':1024},{})
        request=RoundRequest(principal(),owner_task['id'],'request',0,1,[{'role':'user','content':'question'}],time.monotonic_ns()+20_000_000)
        try:
            with patch('lib.tasks_pkg.compaction._prompt_admission.enforce_dispatch_prompt_limit') as prepare:
                with pytest.raises(TimeoutError):await asyncio.wait_for(policy.prepare(request),1)
                prepare.assert_not_called()
                assert not blocking.done()
        finally:release.set();blocking.result(5)
        assert owner_task['abort_event'].is_set() and not account.events
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))


def test_shared_preparation_order_and_child_delivery_scope(monkeypatch):
    import lib.tasks_pkg.orchestrator._root_agent_loop as root
    for child in [False, True]:
        events=[]; owner_task=dict(task(),config={'projectPath':'/project','evolution':{'run_id':'run'}},_swarmMessageBinding=True)
        messages=[{'role':'user','content':'intent'}]
        monkeypatch.setattr(root,'run_round_message_hygiene',lambda *a,**k:events.append('hygiene'))
        monkeypatch.setattr(root,'drain_and_inject_inbox',lambda **k:events.append('inbox'))
        monkeypatch.setattr('lib.tasks_pkg.invocation.is_child_invocation',lambda task:child)
        monkeypatch.setattr('lib.tasks_pkg.message_input_receipts.remember',lambda *a:events.append('remember'))
        monkeypatch.setattr('lib.conversations.project_messages.inject',lambda *a:events.append('project'))
        monkeypatch.setattr('lib.swarm.run_messages.inject_messages',lambda *a:events.append('swarm'))
        monkeypatch.setattr('lib.evolution.coordination.inject_updates',lambda *a:events.append('evolution'))
        monkeypatch.setattr('lib.tasks_pkg.context_composer.refresh_programmatic_inputs',lambda *a,**k:events.append('programmatic'))
        root.prepare_round_context(owner_task,messages,round_num=1,tid='test',model='fixture',project_path='/project',project_enabled=True,round_tools=[])
        assert events==(['hygiene','remember','swarm','evolution','programmatic'] if child else ['hygiene','inbox','remember','project','swarm','evolution','programmatic'])


def test_execution_notice_is_admitted_after_its_actual_append(monkeypatch):
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context
    seen=[]
    def admit(messages,*args,**kwargs):
        seen.append([dict(row) for row in messages]);return {'totalTokens':len(messages),'toolSchemaTokens':0}
    def notice(task,messages,*args,**kwargs):messages.append({'role':'user','content':'budget notice'})
    monkeypatch.setattr('lib.tasks_pkg.compaction._prompt_admission.enforce_dispatch_prompt_limit',admit)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.inject_execution_allowance',notice)
    messages=[{'role':'user','content':'intent'}]
    result=admit_round_context(task(),messages,[],round_num=0,model='fixture')
    assert [len(rows) for rows in seen]==[1,2] and result['totalTokens']==2


def test_inbox_confirmation_requires_verified_return_and_live_settlement():
    from lib.agent_core.native_process import RoundSettlement
    async def run(executor):
        for kind,state,cancelled in [('returned','provider_finished',False),('returned','premature_eof',False),('failed',None,False),('returned','provider_finished',True)]:
            owner_task=task();account=Accounting()
            owner_task['_projectNarrativeDelivery']={'deliveryToken':'page'}
            narrative=[]
            def consume(value):
                assert value is owner_task and account.events==[('settle',0)]
                narrative.append(value.pop('_projectNarrativeDelivery'))
                return True
            from types import SimpleNamespace
            policy=NativeContextPolicy(principal=principal(),task=owner_task,executor=executor,round_policy=account)
            request=RoundRequest(principal(),owner_task['id'],'request',0,1)
            stop=threading.Event()
            if cancelled:stop.set()
            with patch('lib.agent_core.store.get_conversation_store',return_value=SimpleNamespace(confirm_project_context_consumption=consume)), patch('lib.tasks_pkg.orchestrator._deferred_inbox_flush.flush_deferred_peer_and_steer') as confirm:
                await policy.settle(RoundSettlement(request,{'kind':kind,'provider_state':state},stop))
                expected=int(kind=='returned' and state=='provider_finished' and not cancelled)
                assert confirm.call_count==expected and len(narrative)==expected
                assert ('_projectNarrativeDelivery' in owner_task)==(not expected)
            assert account.events==[('settle',0)]
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
