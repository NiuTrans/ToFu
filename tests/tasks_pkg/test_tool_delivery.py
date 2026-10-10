"""Explicit delivery cannot bypass typed provider, receipt or consumption fences."""
from copy import deepcopy
from types import SimpleNamespace
import threading
import time

import pytest

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.tasks_pkg import tool_delivery as port

pytestmark = pytest.mark.unit


def setup_delivery(monkeypatch):
    task = {'id':'delivery-task', '_userId':123, 'aborted':False,
        'abort_event':threading.Event(), 'config':{}, 'content':'pretool narration'}
    arguments = {'path':'answer.json', 'delivery':'Exact final answer 🧪'}
    message = {'role':'assistant', 'content':'I will deliver.', 'tool_calls':[
        {'id':'final-call', 'type':'function', 'function':{
            'name':'deliver', 'arguments':arguments}}]}
    messages = [message]
    task['_compact_messages'] = messages
    authorized = [True]
    port.bind_delivery(task, call_id='final-call', name='deliver', round_num=0,
        arguments=arguments, content=arguments['delivery'], acknowledgement='sealed-intent:123',
        verify=lambda: authorized[0])
    messages.append({'role':'tool', 'tool_call_id':'final-call', 'content':'sealed-intent:123'})
    task['_tool_call_id_receipts'] = {'final-call':{'name':'deliver', 'status':'done'}}
    result = ProviderStreamResult(message, 'tool_calls', {'input_tokens':20,'output_tokens':5},
        ProviderStreamState.PROVIDER_FINISHED, provider_finish_reason='tool_calls',
        saw_finish_reason=True, saw_done=True)
    state = SimpleNamespace(last_stream_result=result, last_finish_reason='tool_calls',
        assistant_msg=message, exit_reason='running')
    events = []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event', lambda t,e:events.append(e))
    return task, state, message, messages, authorized, events


def consume(values, **kw):
    task, state, message, messages, _, _ = values
    return port.consume_delivery(task, state, round_num=0, message=message,
        messages=messages, note=kw.get('note'))


def test_supplied_reply_uses_text_generation_and_preserves_original_stream(monkeypatch):
    values = setup_delivery(monkeypatch)
    task, state, message, _, _, events = values
    original = state.last_stream_result
    assert consume(values)
    assert task['content'] == state.assistant_msg['content'] == 'Exact final answer 🧪'
    assert state.last_stream_result is original and original.message is message
    assert state.last_finish_reason == 'tool_calls' and state.exit_reason == port.EXIT_REASON
    assert [e['type'] for e in events] == ['delta_reset','delta']
    assert task['_contentEpoch'] == 1
    assert port.has_committed_delivery(task, 'tool_calls', port.EXIT_REASON)
    assert not consume(values)


@pytest.mark.parametrize('fault', ['unverified', 'truncated', 'different_provider_call',
    'different_arguments','sibling', 'missing_ack', 'missing_receipt', 'failed_receipt',
    'foreign_owner', 'different_task', 'cancelled', 'abort_event', 'error',
    'authority_lost','unknown_actuals','deadline','tokens','timeout'])
def test_unproven_delivery_keeps_the_task_running(monkeypatch, fault):
    values = setup_delivery(monkeypatch)
    task, state, message, messages, authorized, events = values
    if fault == 'unverified':
        state.last_stream_result = ProviderStreamResult(message, 'tool_calls', None, ProviderStreamState.UNKNOWN)
    elif fault == 'truncated':state.last_finish_reason = 'length'
    elif fault == 'different_provider_call':
        copied = deepcopy(message);copied['tool_calls'][0]['id'] = 'foreign'
        state.last_stream_result = ProviderStreamResult(copied,'tool_calls',None,
            ProviderStreamState.PROVIDER_FINISHED,provider_finish_reason='tool_calls',saw_finish_reason=True)
    elif fault == 'different_arguments':message['tool_calls'][0]['function']['arguments']['delivery']='changed'
    elif fault == 'sibling':message['tool_calls'].append(deepcopy(message['tool_calls'][0]))
    elif fault == 'missing_ack':messages[-1]['content']='lost acknowledgement'
    elif fault == 'missing_receipt':task['_tool_call_id_receipts']={}
    elif fault == 'failed_receipt':task['_tool_call_id_receipts']['final-call']['status']='error'
    elif fault == 'foreign_owner':task['_userId']=124
    elif fault == 'different_task':task['id']='another-task'
    elif fault == 'cancelled':task['aborted']=True
    elif fault == 'abort_event':task['abort_event'].set()
    elif fault == 'error':task['error']={'code':'internal'}
    elif fault == 'authority_lost':authorized[0]=False
    elif fault == 'unknown_actuals':task['_executionActualsComplete']=False
    elif fault in ('deadline','tokens'):
        task['config']['executionLimits']={'max_tokens':0 if fault=='tokens' else None,
            'max_tool_calls':None,'deadline':time.time()-1 if fault=='deadline' else None}
    assert not consume(values, note={'timed_out':fault=='timeout'})
    assert task['content']=='pretool narration' and not events
    assert state.exit_reason=='running' and not port.has_committed_delivery(task,'tool_calls',port.EXIT_REASON)


@pytest.mark.parametrize('kind', ['live_model','unknown_model','live_child'])
def test_original_consumption_owner_must_have_drained(monkeypatch, kind):
    from lib.agent_core.execution_budget import ExecutionBudget
    values=setup_delivery(monkeypatch);task=values[0]
    budget=task['_executionBudget']=ExecutionBudget(task)
    if kind=='live_child':
        child={'id':'child','config':{}};budget.attach(child)
    else:
        lease=budget.reserve_model(task,input_tokens=10,max_output_tokens=20)
        if kind=='unknown_model':lease.mark_dispatched();lease.settle(None)
    assert not consume(values)
    if kind=='live_child':budget.close(child)
    elif kind=='live_model':lease.settle(unsent=True)


def test_cancel_and_late_budget_take_precedence_in_the_existing_finalizer(monkeypatch):
    from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason
    values=setup_delivery(monkeypatch);task=values[0];assert consume(values)
    def settle():
        return _settle_post_loop_finish_reason(task,'tool_calls',loop_exit_reason=port.EXIT_REASON,
            abort_detected_phase=None,model='controlled',tid='controlled')
    assert settle()=='tool_calls'
    task['aborted']=True
    assert settle()=='aborted'
    task['aborted']=False;task['config']['executionLimits']={'max_tokens':0,'max_tool_calls':None,'deadline':None}
    assert settle()=='error'


def test_serialized_intent_is_insufficient_for_completion():
    task={'id':'cold','_userId':123,'_explicitToolDeliveryCommit':{'content':'looks done'}}
    assert not port.has_committed_delivery(task,'tool_calls',port.EXIT_REASON)


@pytest.mark.parametrize('message',[None,{'tool_calls':{'id':'x'}},
    {'tool_calls':['malformed']},{'tool_calls':[{'function':'malformed'}]}])
def test_malformed_carriers_never_authorize_delivery(message):
    assert not port.sole_call(message,'final-call','deliver','0'*64)


def test_explicit_child_delivery_checkpoint_retains_final_reply_without_restart_dispatch(monkeypatch):
    from unittest.mock import Mock
    from lib.tasks_pkg.orchestrator._root_agent_loop import _RootLoopHooks
    from lib.swarm.agent import SubAgent
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.protocol import SubTaskSpec
    task, state, message, messages, _, _ = setup_delivery(monkeypatch)
    task['config']['memoryEnabled'] = False
    task['usage'] = {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25}
    spec = SubTaskSpec(id='delivered', role='general', objective='deliver')
    agent = SubAgent(spec, parent_task=task, all_tools=[], model='fixture-model')
    snapshots = []
    agent._checkpoint = lambda **kw: snapshots.append(deepcopy(agent.result.to_dict()))
    task['_invocationCheckpointSink'] = agent._save_checkpoint
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._root_agent_loop.run_round_checkpoint_and_close', Mock())
    hooks = _RootLoopHooks(SimpleNamespace(task=task, state=state, messages=messages, tid='delivery'))
    directive = hooks.after_tools(0, message, {})
    assert directive is not None and state.exit_reason == port.EXIT_REASON
    assert snapshots[-1]['status'] == 'completed'
    assert snapshots[-1]['checkpoint_phase'] == 'terminal_delivery'
    assert snapshots[-1]['final_answer'] == 'Exact final answer 🧪'
    master = MasterOrchestrator(task_id='parent', conv_id='conv', user_id=123, specs=[spec])
    scheduler = SimpleNamespace(_completed={}, add_specs=Mock())
    monkeypatch.setattr(master, '_build_scheduler', lambda: scheduler)
    monkeypatch.setattr(master, '_start_driver', Mock())
    monkeypatch.setattr(master, '_reenqueue_swarm_update', Mock())
    master.rehydrate_in_background([{'agent_id': spec.id, 'status': 'completed',
        'result': snapshots[-1], 'messages': messages, 'rounds_used': 1}])
    scheduler.add_specs.assert_not_called()
    assert master._results_by_id[spec.id][1].final_answer == 'Exact final answer 🧪'
