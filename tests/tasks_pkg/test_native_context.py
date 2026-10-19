"""Real prompt admission over native route bounds and joined caller execution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import patch

import pytest

from lib.agent_core.native_process import RoundRequest, RoundPreparation
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


@pytest.mark.parametrize('round_number',[0,7])
def test_wire_projection_reads_injected_admitted_history_on_fresh_or_restored_round(round_number):
    async def run():
        owner=task();events=[];raw_tools=[{'type':'function','function':{'name':'read','parameters':{}}}]
        def inject(task,messages,**kwargs):
            events.append('inject');messages.append({'role':'user','content':'injected context'})
            assert kwargs['round_tools']==raw_tools
        def admit(task,messages,tools,**kwargs):
            events.append('admit');assert messages[-1]['content']=='injected context';assert tools==raw_tools
        def project(route,template,catalog,messages,number):
            events.append('wire');assert number==round_number;assert messages[-1]['content']=='injected context'
            assert template['max_tokens']==64 and route['owner_user_id']==7
            return []
        with ThreadPoolExecutor(max_workers=1) as executor:
            policy=NativeContextPolicy(principal=principal(),task=owner,executor=executor)
            policy.bind_wire_projection(project,raw_tools)
            policy.bind_route(metadata(),{'max_tokens':64},{'read':{'name':'read'}})
            request=RoundRequest(principal(),owner['id'],'request',round_number,1,[{'role':'user','content':'before'}])
            with patch('lib.tasks_pkg.orchestrator._root_agent_loop.prepare_round_context',side_effect=inject),patch('lib.tasks_pkg.orchestrator._root_agent_loop.admit_round_context',side_effect=admit):
                result=await policy.prepare(request)
            assert isinstance(result,RoundPreparation) and result.wire_tools==[]
            assert result.messages[-1]['content']=='injected context' and events==['inject','admit','wire']
            assert policy.completion_binding()==('fixture',[])
            policy.close()
    asyncio.run(run())


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
    from lib.llm.llm_errors import PromptTooLongError
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


def test_execution_notice_is_counted_in_the_single_admission(monkeypatch):
    """The decided notice is committed BEFORE the one measured admission, so
    the appended notice is counted inside that measurement — no second
    full-prompt re-measure."""
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context
    seen=[]
    def admit(messages,*args,**kwargs):
        seen.append([dict(row) for row in messages]);return {'totalTokens':len(messages),'toolSchemaTokens':0}
    monkeypatch.setattr('lib.tasks_pkg.compaction._prompt_admission.enforce_dispatch_prompt_limit',admit)
    decision={'snapshot': {'tokens_remaining': None, 'usage_known': True},
              'level': 0, 'initial_seconds': None, 'text': 'budget notice'}
    monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.notice_decision',
                        lambda *a, **k: dict(decision))
    monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.render_allowance',
                        lambda *a, **k: 'budget notice')
    messages=[{'role':'user','content':'intent'}]
    result=admit_round_context(task(),messages,[],round_num=0,model='fixture')
    # The notice is decided + committed before the single measurement.
    assert [len(rows) for rows in seen]==[2] and result['totalTokens']==2
    assert any(m.get('_contextInjectionId')=='execution_allowance' for m in messages)


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


def test_native_context_preserves_explicit_price_consumer_and_exact_settlement():
    from lib.agent_core.native_process import RoundSettlement
    async def run(executor):
        owner_task=task()
        received=[]
        class Consumer(Accounting):
            accepts_native_cost=True
            async def settle(self,result): received.append(result)
        policy=NativeContextPolicy(principal=principal(),task=owner_task,executor=executor,round_policy=Consumer())
        assert policy.accepts_native_cost
        request=RoundRequest(principal(),owner_task['id'],'priced-round',0,1)
        settlement=RoundSettlement(request,{'kind':'returned','provider_state':'incomplete_stream','cost':{'credits':{'total':7593}}})
        await policy.settle(settlement)
        assert received==[settlement] and received[0] is settlement
        policy.close()
        assert not policy.accepts_native_cost
    with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))


def test_native_preparation_preserves_invocation_context_in_callback_worker():
    from contextvars import ContextVar
    invocation=ContextVar('native-prepare-invocation',default='unbound')
    async def run():
        owner=task()
        with ThreadPoolExecutor(max_workers=1) as executor:
            policy=NativeContextPolicy(principal=principal(),task=owner,executor=executor)
            policy.bind_route(metadata(),{'max_tokens':64},{})
            request=RoundRequest(principal(),owner['id'],'request',0,1,[{'role':'user','content':'question'}])
            def prepare(task,messages,**kwargs):assert invocation.get()=='owned-root'
            with patch('lib.tasks_pkg.orchestrator._root_agent_loop.prepare_round_context',side_effect=prepare),patch(
                    'lib.tasks_pkg.orchestrator._root_agent_loop.admit_round_context'):
                token=invocation.set('owned-root')
                try:assert await policy.prepare(request)==request.messages
                finally:invocation.reset(token)
            assert executor.submit(invocation.get).result()=='unbound'
    asyncio.run(run())


def test_native_context_confirmation_cancellation_joins_physical_callback():
    from types import SimpleNamespace
    async def run():
        owner=task();entered,release=threading.Event(),threading.Event()
        def confirm(*args,**kwargs):
            entered.set();assert release.wait(2)
        with ThreadPoolExecutor(max_workers=1) as executor:
            policy=NativeContextPolicy(principal=principal(),task=owner,executor=executor)
            result=SimpleNamespace(request=SimpleNamespace(principal=principal(),task_id=owner['id'],round=0),
                evidence={'kind':'returned','provider_state':'provider_finished'},cancelled=None)
            with patch('lib.tasks_pkg.orchestrator._deferred_inbox_flush.confirm_consumed_project_context',side_effect=confirm),patch(
                    'lib.tasks_pkg.orchestrator._deferred_inbox_flush.flush_deferred_peer_and_steer') as flush:
                pending=asyncio.create_task(policy.settle(result))
                assert await asyncio.to_thread(entered.wait,1)
                pending.cancel();await asyncio.sleep(.01)
                assert not pending.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):await pending
                flush.assert_called_once()
    asyncio.run(run())


def test_background_preparation_explicitly_borrows_token_host_without_app_context():
    from lib.tasks_pkg.native_host import NativeHost
    from tests.tasks_pkg.test_native_host import image
    from tests.token_counter.test_native_application_adapter import Counter
    from lib.token_counter.native import NativeTextEncoder
    from lib.token_counter import tiktoken_counter
    from quart import has_app_context
    async def run():
        host = NativeHost(image(), token_count=True)
        backend = Counter()
        host._token_counter = backend
        owner = task()
        evidence = []
        def observe(*args, **kwargs):
            assert not has_app_context()
            encoder = tiktoken_counter._get_encoder('cl100k_base')
            assert isinstance(encoder, NativeTextEncoder) and encoder.host is host
            evidence.append(encoder.count_text('background token scope'))
        def project(*args):
            observe()
            return []
        with ThreadPoolExecutor(max_workers=1) as executor:
            policy = NativeContextPolicy(principal=principal(), task=owner, executor=executor, token_count_host=host)
            policy.bind_wire_projection(project, [])
            policy.bind_route(metadata(), {'max_tokens':64}, {})
            request = RoundRequest(principal(), owner['id'], 'request', 0, 1, [{'role':'user', 'content':'before'}])
            with patch('lib.tasks_pkg.orchestrator._root_agent_loop.prepare_round_context', side_effect=observe), \
                 patch('lib.tasks_pkg.orchestrator._root_agent_loop.admit_round_context', side_effect=observe):
                await policy.prepare(request)
            policy.close()
        assert len(evidence) == 3 and len(set(evidence)) == 1
        assert not isinstance(tiktoken_counter._get_encoder('cl100k_base'), NativeTextEncoder)
        await host.close()
        assert backend.closed
    asyncio.run(run())
