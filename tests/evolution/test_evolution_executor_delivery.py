"""Explicit executor answers preserve independent acceptance and delivery fences."""
import json
from types import SimpleNamespace
import pytest
from lib.evolution import executor_delivery as delivery
from lib.evolution.contracts import EvolutionStateError
from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.tasks_pkg.tool_delivery import consume_delivery
from tests.evolution.test_evolution_runs import runtime as task_runtime, OWNER, finish

runtime = task_runtime
pytestmark = pytest.mark.unit


def request(runtime, **options):
    runtime[0].submit_run({'objective': 'Deliver paired arithmetic with honest evidence.',
                           'max_attempts': 1, **options}, user_id=OWNER)
    task = runtime[2][-1]
    arguments = {'action': 'submit_result', 'content': 'Pairs are (4,3), (6,5), (8,7); mean 1 and SE 0. Execution remains unproven.'}
    message = {'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': 'deliver', 'type': 'function', 'function': {
            'name': delivery.NAME, 'arguments': json.dumps(arguments)}}]}
    task['_compact_messages'] = [message]
    return task, arguments, message


def consume(task, message, result, *, verified=True, status='done'):
    task['_compact_messages'].append({'role': 'tool', 'tool_call_id': 'deliver', 'content': json.dumps(result)})
    task['_tool_call_id_receipts'] = {'deliver': {'name': delivery.NAME, 'status': status}}
    stream = ProviderStreamResult(message, 'tool_calls', {'input_tokens': 10, 'output_tokens': 5},
        ProviderStreamState.PROVIDER_FINISHED if verified else ProviderStreamState.PREMATURE_CLOSE,
        provider_finish_reason='tool_calls' if verified else None, saw_finish_reason=verified)
    state = SimpleNamespace(last_stream_result=stream, last_finish_reason='tool_calls', assistant_msg=message)
    return consume_delivery(task, state, round_num=0, message=message,
                            messages=task['_compact_messages'], note=None)


def test_answer_delivery_enters_independent_review_without_accepting_executor_claim(runtime):
    task, arguments, message = request(runtime)
    result = delivery.prepare(task, arguments, call_id='deliver', round_num=0)
    assert result['acceptance'] == 'pending independent review'
    assert runtime[0].get_run(result['run_id'], user_id=OWNER)['status'] == 'executing'
    assert len(runtime[2]) == 1
    assert consume(task, message, result)
    assert task['content'] == arguments['content']
    run = finish(runtime, task, task['content'])
    assert run['status'] == 'reviewing' and run['review'] is None
    assert run['result'] == arguments['content'] and len(runtime[2]) == 2


@pytest.mark.parametrize('fault', ['sibling', 'foreign_owner', 'wrong_role'])
def test_invalid_executor_cannot_bind_delivery(runtime, fault):
    task, arguments, message = request(runtime)
    if fault == 'sibling': message['tool_calls'].append(dict(message['tool_calls'][0], id='sibling'))
    elif fault == 'foreign_owner': task['_userId'] = OWNER + 1
    else: task['config']['evolution']['stage'] = 'reviewing'
    with pytest.raises((EvolutionStateError, ValueError)):
        delivery.prepare(task, arguments, call_id='deliver', round_num=0)
    assert '_explicitToolDeliveryRequest' not in task
    assert len(runtime[2]) == 1


@pytest.mark.parametrize('fault', ['cancel', 'unverified_stream', 'failed_receipt', 'changed_binding'])
def test_terminal_delivery_rechecks_authority_and_physical_receipt(runtime, fault):
    task, arguments, message = request(runtime)
    result = delivery.prepare(task, arguments, call_id='deliver', round_num=0)
    if fault == 'cancel': task['aborted'] = True
    elif fault == 'changed_binding':
        run, version = runtime[0]._read(result['run_id'], OWNER)
        run['objective'] += ' Changed objective.'
        runtime[0]._save(run, version, OWNER)
    assert not consume(task, message, result, verified=fault != 'unverified_stream',
                       status='error' if fault == 'failed_receipt' else 'done')
    assert task.get('content') != arguments['content']
    assert len(runtime[2]) == 1


def test_explicit_reply_never_grants_required_command_evidence(runtime):
    task, arguments, message = request(runtime, require_command_execution=True)
    result = delivery.prepare(task, arguments, call_id='deliver', round_num=0)
    assert consume(task, message, result)
    run = finish(runtime, task, task['content'])
    assert run['status'] == 'failed' and run['review'] is None
    assert len(runtime[2]) == 1


@pytest.mark.parametrize('fault', ['intent', 'task', 'unadmitted'])
@pytest.mark.parametrize('after_prepare', [False, True])
def test_delivery_requires_the_same_admitted_dispatch(runtime, fault, after_prepare):
    task, arguments, message = request(runtime)
    prepared = delivery.prepare(task, arguments, call_id='deliver', round_num=0) if after_prepare else None
    run_id = task['config']['evolution']['run_id']
    run, version = runtime[0]._read(run_id, OWNER)
    marker = run['stage_dispatch']
    if fault == 'intent': marker['intent_id'] = 'stage-foreign'
    elif fault == 'task': marker['task_id'] = 'other-task'
    else: marker['state'] = 'bound'
    runtime[0]._save(run, version, OWNER)
    if after_prepare:
        assert not consume(task, message, prepared)
    else:
        with pytest.raises(EvolutionStateError, match='admitted dispatch'):
            delivery.prepare(task, arguments, call_id='deliver', round_num=0)
        assert '_explicitToolDeliveryRequest' not in task
    assert len(runtime[2]) == 1
