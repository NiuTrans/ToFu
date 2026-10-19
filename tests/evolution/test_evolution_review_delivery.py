"""Structured judgments use the registered root terminal port and native verdicts.

Real task/run ownership and durable terminal result CAS; controlled provider
completion. Coverage receipts come from successful provider inputs, never flags.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.evolution import review_delivery as delivery, stage_outcomes
from lib.evolution.contracts import EvolutionContractError, EvolutionStateError
from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.tasks_pkg.tool_delivery import consume_delivery
from tests.evolution.test_evolution_runs import runtime as task_runtime, OWNER, finish, review
from tests.evolution.test_evolution_artifact_delivery import persisted_runtime as durable_runtime
from tests.evolution.test_evolution_source_tools import CONTENT, delivered
from tests.evolution.test_evolution_hypothesis_review import assessment
from tests.evolution.test_evolution_exploration_tools import candidate
from tests.evolution.test_evolution_background_execution import command as _background_command_fixture, complete
background_command = _background_command_fixture

runtime = task_runtime
persisted_runtime = durable_runtime
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kw: [])


def request(runtime, *, outcome='pass', hypothesis=None, requirements=False, read_sources=True, short_history=False):
    payload = {'objective': 'Inspect the causal evidence and its future mechanism.', 'max_attempts': 1}
    if requirements:
        payload['acceptance_requirements'] = ['Distinguish observed demand from conditional future demand.']
    context = ''
    if short_history:
        from lib.evolution import programs
        program = programs.create_program({'objective':'Inspect earlier validation feedback.'},user_id=OWNER,idempotency_key='history-submit')
        context = json.dumps({'program_id':program['program_id'], 'settled_through':1})
    runtime[0].submit_run(payload, user_id=OWNER, exploration_hypothesis=hypothesis, context_seed=context)
    run = finish(runtime, runtime[2][-1], 'Short current submission.' if short_history else CONTENT)
    task = runtime[2][-1]
    value = json.loads(review(outcome))
    if hypothesis:
        value['hypothesis_assessment'] = assessment(hypothesis, feasibility='pass')
    if requirements:
        value['requirement_results'] = [{'requirement_id': key, 'outcome': outcome,
            'evidence_ref': 'Observed demand remains unknown; future conditions are explicit.'}
            for key in runtime[0]._requirements(run)]
    args = {'review': value}
    message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'review-call',
        'type': 'function', 'function': {'name': delivery.NAME, 'arguments': json.dumps(args)}}]}
    task['_compact_messages'] = [message]
    if read_sources: read_originals(run, task)
    return run, task, args, message


def read_originals(run, task):
    for key, kind in delivery._SOURCE_KINDS.items():
        source = run.get(key)
        if not source:
            continue
        offset = 0
        while True:
            page = delivered(task, {'action': 'read', 'run_id': run['run_id'],
                'task_id': source['task_id'], 'source_kind': kind,
                'source_sha256': source['source_sha256'], 'offset': offset},
                call_id=kind + str(offset))
            if page['next_offset'] is None:
                break
            offset = page['next_offset']


def acknowledge(task, message, result, *, verified=True, status='done', ack=True):
    task['_compact_messages'].append({'role': 'tool', 'tool_call_id': 'review-call',
        'content': json.dumps(result) if ack else 'Receipt unavailable'})
    task['_tool_call_id_receipts'] = {'review-call': {'name': delivery.NAME, 'status': status}}
    provider = ProviderStreamResult(message, 'tool_calls', {'input_tokens': 10, 'output_tokens': 5},
        ProviderStreamState.PROVIDER_FINISHED if verified else ProviderStreamState.PREMATURE_CLOSE,
        provider_finish_reason='tool_calls' if verified else None, saw_finish_reason=verified)
    state = SimpleNamespace(last_stream_result=provider, last_finish_reason='tool_calls', assistant_msg=message)
    return consume_delivery(task, state, round_num=0, message=message,
        messages=task['_compact_messages'], note=None)


@pytest.mark.parametrize('outcome', ['pass', 'fail', 'unknown'])
def test_submission_preserves_authored_judgment_and_native_coverage(runtime, outcome):
    run, task, args, message = request(runtime, outcome=outcome, read_sources=False)
    before = deepcopy(args)
    if outcome != 'fail':
        with pytest.raises(EvolutionStateError, match='inspection is unfinished'):
            delivery.prepare(task, args, call_id='review-call', round_num=0)
        assert args == before and '_explicitToolDeliveryRequest' not in task
        assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
        assert len(runtime[2]) == 2
        return
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert result['acceptance'] == 'pending native review adjudication'
    assert args == before and acknowledge(task, message, result)
    final = finish(runtime, task, task['content'])
    assert final['status'] == 'needs_attention'
    objective = next(row for row in final['review']['criterion_results'] if row['criterion_id'] == 'objective')
    assert objective['outcome'] == 'unknown'  # No original source receipt was granted by submission.
    assert next(row for row in final['review']['criterion_results'] if row['criterion_id'] == 'evidence')['outcome'] == outcome


@pytest.mark.parametrize('fault', ['audit_missing', 'criterion_duplicate', 'criterion_missing',
    'requirement_missing', 'requirement_foreign', 'hypothesis_missing', 'hypothesis_foreign',
    'unknown_objection_requirement', 'extra_argument', 'duplicate_json_key', 'sibling'])
def test_invalid_submission_refuses_without_binding_or_record_writes(runtime, fault):
    run, task, args, message = request(runtime, hypothesis=candidate()['hypothesis'], requirements=True)
    value = args['review']
    if fault == 'audit_missing': value.pop('evidence_audit')
    elif fault == 'criterion_duplicate': value['criterion_results'][1] = deepcopy(value['criterion_results'][0])
    elif fault == 'criterion_missing': value['criterion_results'].pop()
    elif fault == 'requirement_missing': value.pop('requirement_results')
    elif fault == 'requirement_foreign': value['requirement_results'][0]['requirement_id'] = 'foreign'
    elif fault == 'hypothesis_missing': value.pop('hypothesis_assessment')
    elif fault == 'hypothesis_foreign': value['hypothesis_assessment']['hypothesis_sha256'] = '0' * 64
    elif fault == 'unknown_objection_requirement': value['evidence_audit']['objections'] = [{
        'criterion_id': 'objective', 'requirement_id': 'foreign', 'classification': 'unverified',
        'claim': 'Unverified benefit', 'evidence_ref': 'No observed consumer evidence.'}]
    elif fault == 'extra_argument': args['force'] = True
    elif fault == 'sibling': message['tool_calls'].append(deepcopy(message['tool_calls'][0]))
    encoded = json.dumps(args)
    if fault == 'duplicate_json_key': encoded = encoded.replace('"review":', '"review":{},"review":', 1)
    message['tool_calls'][0]['function']['arguments'] = encoded
    writes = runtime[1].writes
    with pytest.raises((EvolutionContractError, EvolutionStateError, ValueError)):
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert runtime[1].writes == writes and '_explicitToolDeliveryRequest' not in task
    assert runtime[0].advance_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    assert len(runtime[2]) == 2


@pytest.mark.parametrize('fault', ['cancel', 'unknown_usage', 'undrained_usage', 'unknown_budget_usage', 'deadline', 'foreign_owner',
    'changed_source', 'stale_task', 'unverified_provider', 'failed_receipt', 'missing_ack'])
def test_live_terminal_conditions_still_refuse_after_preparation(runtime, fault):
    import time
    run, task, args, message = request(runtime)
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    if fault == 'cancel': runtime[0].cancel_run(run['run_id'], user_id=OWNER)
    elif fault == 'unknown_usage': task['_executionActualsComplete'] = False
    elif fault in ('undrained_usage', 'unknown_budget_usage'):
        from lib.agent_core.execution_budget import model_budget_admission
        lease = model_budget_admission(task)(input_tokens=1, max_output_tokens=1)
        lease.mark_dispatched()
        if fault == 'unknown_budget_usage': lease.settle()
    elif fault == 'deadline': task['config']['executionLimits']['deadline'] = time.time() - 1
    elif fault == 'foreign_owner': task['_userId'] = OWNER + 1
    elif fault == 'stale_task': task['id'] += '-stale'
    elif fault == 'changed_source':
        from lib.evolution import runs
        from lib.evolution import records
        runtime[1].rows[(records.NAMESPACE_RUNS, runs._key(run['run_id'], OWNER))]['result_source']['source_sha256'] = '0' * 64
    assert not acknowledge(task, message, result, verified=fault != 'unverified_provider',
        status='error' if fault == 'failed_receipt' else 'done', ack=fault != 'missing_ack')
    if fault == 'undrained_usage': lease.settle()
    assert not task.get('content') and '_explicitToolDeliveryCommit' not in task


def test_preparation_without_terminal_commit_never_cold_completes(runtime, monkeypatch):
    run, task, args, _ = request(runtime)
    delivery.prepare(task, args, call_id='review-call', round_num=0)
    task.pop('_explicitToolDeliveryRequest')
    monkeypatch.setattr(runtime[0], 'default_task_loader', lambda *a, **kw: None)
    after = runtime[0].advance_run(run['run_id'], user_id=OWNER)
    assert after['status'] == 'needs_attention' and after['review'] is None
    assert after['actuals_complete'] is False and len(runtime[2]) == 2
    assert stage_outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None


@pytest.mark.parametrize('coverage', [True, False])
@pytest.mark.parametrize('outcome', ['pass', 'fail', 'unknown'])
@pytest.mark.parametrize('terminal_fault', ['none', 'lost_receiver_ack'])
def test_registered_root_commits_exact_review_and_cold_settles_once(persisted_runtime, monkeypatch,
        coverage, outcome, terminal_fault):
    from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
    from lib.tasks_pkg.orchestrator._root_agent_loop import RootLoopRequest, _RootLoopHooks
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.orchestrator._post_loop import finalize_after_loop
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import _terminal
    runtime = persisted_runtime
    run, task, args, message = request(runtime, outcome=outcome, read_sources=coverage)
    if not coverage and outcome != 'fail':
        with pytest.raises(EvolutionStateError, match='inspection is unfinished'):
            delivery.prepare(task, args, call_id='review-call', round_num=0)
        assert '_explicitToolDeliveryRequest' not in task
        assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
        return
    task['status'] = 'running'
    original_messages = deepcopy(task['messages']);messages = task['messages']
    task.pop('_compact_messages')
    tools = delivery.build(SimpleNamespace(cfg=task['config']))
    state = RoundState(model='controlled', preset='default', thinking_enabled=False)
    state.tool_round_num = 3  # Tool ordinals must not replace model-round identity.
    provider = ProviderStreamResult(message, 'tool_calls', {'input_tokens': 10, 'output_tokens': 5},
        ProviderStreamState.PROVIDER_FINISHED, provider_finish_reason='tool_calls', saw_finish_reason=True)
    request_value = RootLoopRequest(task=task, state=state, messages=messages, tool_list=tools,
        all_search_results_text=[], cfg=task['config'], tid=task['id'][:8], thinking_depth=None,
        temperature=None, max_tokens=1000, response_format=None, project_path='',
        project_enabled=False, search_enabled=False)
    hooks = _RootLoopHooks(request_value)
    hooks.round_context = {'stream_accumulator': SimpleNamespace(announced_tc_map={})}
    calls = [];checkpoints = []
    task['_invocationCheckpointSink'] = lambda t, m, n: checkpoints.append(n)
    def model_round(body):
        calls.append(body);assert len(calls) == 1, [(m.get('role'), str(m.get('content'))[:600]) for m in messages[-2:]]
        state.assistant_msg = message;state.last_stream_result = provider
        state.last_finish_reason = 'tool_calls';state.last_usage = provider.usage
        state.accumulated_usage = dict(provider.usage);task['usage'] = dict(provider.usage)
        return provider
    def execute(round_num, tool_calls):
        messages.append(message);task['_compact_messages'] = messages
        return hooks.execute_tools(round_num, tool_calls)
    result = run_agent_loop(abort=AbortSignal(lambda: task['aborted']), round_tools=tools,
        dispatch=lambda n, t: model_round(n), execute_tools=execute, after_tools=hooks.after_tools,
        on_round_end=hooks.close_round)
    # Shared root records tool admission at round 0, then settled round 1.
    assert result.exit_reason == 'explicit_tool_delivery' and len(calls) == 1 and checkpoints == [0, 1]
    expected = json.dumps(args['review'], ensure_ascii=False, separators=(',', ':'))
    assert task['content'] == expected and state.last_stream_result is provider
    def lost_receiver(t): raise RuntimeError('injected loss after owned terminal commit')
    with monkeypatch.context() as patch:
        if terminal_fault == 'lost_receiver_ack': patch.setattr(_terminal, 'notify_terminal_listeners', lost_receiver)
        finalize_after_loop(task, cfg=task['config'], tid=task['id'][:8], model='controlled',
            preset='default', thinking_depth=None, thinking_enabled=False, temperature=None,
            max_tokens=1000, messages=messages, original_messages=original_messages, tool_list=tools,
            assistant_msg=state.assistant_msg, round_num=0, accumulated_usage=state.accumulated_usage,
            api_rounds=[], last_finish_reason='tool_calls', last_usage=provider.usage,
            tool_call_happened=True, all_search_results_text=[], project_path='', project_enabled=False,
            loop_exit_reason=result.exit_reason, abort_detected_phase=None, last_stream_result=provider)
    row = get_storage_client().query('task_results.replay_get', {
        'key': task['id'], 'user_id': OWNER, 'include_metadata': True})
    assert row['status'] == 'done' and task['finishReason'] == 'tool_calls'
    if terminal_fault == 'lost_receiver_ack':
        assert stage_outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None
        assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
        monkeypatch.setattr(runtime[0], 'default_task_loader', lambda *a, **kw: None)
        runtime[0].advance_run(run['run_id'], user_id=OWNER)
    final = runtime[0].get_run(run['run_id'], user_id=OWNER)
    assert final['status'] == ('completed' if coverage and outcome == 'pass' else 'needs_attention')
    assert len(runtime[2]) == 2 and final['review'] is not None
    meta = stage_outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER)
    assert meta['finish_reason'] == 'tool_calls' and meta['actuals_complete']
    assert row['metadata']['terminalContext']['outcome'] == meta
    assert stage_outcomes.page(run['run_id'], task['id'], user_id=OWNER)['content'] == expected
    assert runtime[0].advance_run(run['run_id'], user_id=OWNER) == final
    assert len(calls) == 1 and len(runtime[2]) == 2


def test_future_axes_remain_independent_after_structured_submission(runtime):
    hypothesis = candidate()['hypothesis']
    run, task, args, message = request(runtime, hypothesis=hypothesis)
    read_originals(run, task)
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, result)
    final = finish(runtime, task, task['content'])
    assert final['status'] == 'completed' and final['exploration_hypothesis'] == hypothesis
    value = final['review']['hypothesis_assessment']
    assert value['feasibility']['outcome'] == 'pass'
    assert all(value[axis]['outcome'] == 'unknown' for axis in ('problem', 'novelty', 'net_value'))


def test_provider_schema_is_self_contained_and_admits_future_assessment():
    from jsonschema import Draft202012Validator
    schema = delivery.tool_definition()['function']['parameters']
    assert '$ref' not in json.dumps(schema)
    value = json.loads(review())
    value['hypothesis_assessment'] = assessment(candidate()['hypothesis'])
    value['requirement_results'] = [{'requirement_id':'requirement-1', 'outcome':'unknown',
        'evidence_ref':'Delivery alone does not establish claim validity.',
        'original_sources':[{'task_id':'executor-1','source_sha256':'a'*64,'total_chars':10}]}]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({'review': value})


def test_complete_serialized_review_binds_without_total_length_refusal(runtime):
    _, task, args, message = request(runtime, hypothesis=candidate()['hypothesis'])
    for row in args['review']['criterion_results']: row['evidence_ref'] = '原' * 500
    for axis in ('problem', 'feasibility', 'novelty', 'net_value'):
        args['review']['hypothesis_assessment'][axis]['evidence_ref'] = '原' * 500
    args['review']['evidence_audit']['objections'] = [{
        'criterion_id': 'objective', 'classification': 'unverified',
        'claim': '原' * 300, 'evidence_ref': '原' * 500} for _ in range(8)]
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args, ensure_ascii=False)
    prepared = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert prepared and '_explicitToolDeliveryRequest' in task
    assert acknowledge(task, message, prepared)
    assert json.loads(task['content']) == args['review']


@pytest.mark.parametrize('stage', ['executing', 'inline_review'])
def test_submission_cannot_end_executor_or_inline_review(runtime, stage):
    runtime[0].submit_run({'objective': 'Explain the observed workaround.'}, user_id=OWNER)
    task = runtime[2][-1]
    if stage == 'inline_review':
        finish(runtime, task, 'A short explanation.');task = runtime[2][-1]
    args = {'review': json.loads(review())}
    task['_compact_messages'] = [{'role': 'assistant', 'tool_calls': [{'id': 'review-call',
        'function': {'name': delivery.NAME, 'arguments': json.dumps(args)}}]}]
    with pytest.raises(EvolutionStateError, match='source-backed reviewer'):
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert '_explicitToolDeliveryRequest' not in task


@pytest.mark.parametrize('outcome', ['pass', 'fail', 'unknown'])
def test_history_only_reviewer_submits_through_existing_terminal_port(runtime, outcome):
    run, task, args, message = request(runtime, outcome=outcome, short_history=True)
    assert not any(run.get(key) for key in delivery._SOURCE_KINDS)
    assert task['config']['toolSpecAllowlist'] == ['evolution_source','evolution_review']
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, result)
    final = finish(runtime, task, task['content'])
    assert final['status'] == ('completed' if outcome == 'pass' else 'needs_attention')
    assert final['review']['criterion_results'] == args['review']['criterion_results']


def test_history_change_invalidates_prepared_review_delivery(runtime):
    run, task, args, message = request(runtime, outcome='unknown', short_history=True)
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    value, version = runtime[0]._read(run['run_id'],OWNER)
    context = json.loads(value['context_seed']);context['settled_through'] = 2
    value['context_seed'] = json.dumps(context)
    runtime[0]._save(value,version,OWNER)
    assert not acknowledge(task,message,result)
    assert runtime[0].get_run(run['run_id'],user_id=OWNER)['status'] == 'reviewing'


@pytest.mark.parametrize('outcome', ['pass','unknown'])
@pytest.mark.parametrize('change_source', [False,True])
def test_unread_background_submission_stays_in_reviewer_until_original_delivery(runtime, background_command, outcome, change_source):
    from tests.evolution.test_evolution_background_wait import wait
    run, handle = wait(runtime, background_command)
    complete(handle)
    reviewing = runtime[0].advance_run(run['run_id'],user_id=OWNER)
    task = runtime[2][-1]
    args = {'review':json.loads(review(outcome))}
    message = {'role':'assistant','content':'','tool_calls':[{'id':'review-call','type':'function',
        'function':{'name':delivery.NAME,'arguments':json.dumps(args)}}]}
    task['_compact_messages'] = [message]
    with pytest.raises(EvolutionStateError, match='inspection is unfinished'):
        delivery.prepare(task,args,call_id='review-call',round_num=0)
    assert '_explicitToolDeliveryRequest' not in task
    assert runtime[0].get_run(run['run_id'],user_id=OWNER)['status'] == 'reviewing'
    assert len(runtime[2]) == 2
    source = reviewing['background_evidence_source']
    offset = 0
    while True:
        chunk = delivered(task,{'action':'read','run_id':run['run_id'],'task_id':source['task_id'],
            'source_kind':'background_evidence','source_sha256':source['source_sha256'],'offset':offset},
            call_id='background-'+str(offset))
        if chunk['next_offset'] is None: break
        offset = chunk['next_offset']
    result = delivery.prepare(task,args,call_id='review-call',round_num=0)
    if change_source:
        value, version = runtime[0]._read(run['run_id'],OWNER)
        value['background_evidence_source']['source_sha256'] = '0'*64
        runtime[0]._save(value,version,OWNER)
        assert not acknowledge(task,message,result)
        assert runtime[0].get_run(run['run_id'],user_id=OWNER)['status'] == 'reviewing'
        assert len(runtime[2]) == 2
        return
    assert acknowledge(task,message,result)
    final = finish(runtime,task,task['content'])
    assert final['status'] == ('completed' if outcome == 'pass' else 'needs_attention')
    assert len(runtime[2]) == 2


@pytest.mark.parametrize('change', ['threshold','required_outcome','unchanged'])
def test_prepared_review_keeps_frozen_scientific_acceptance_identity(runtime, change):
    from tests.evolution.test_evolution_scientific_observations import protocol, command
    from tests.support.research_observations import observation_payload
    frozen = protocol()
    initial = runtime[0].submit_run({'objective':'Assess the frozen measured comparison.',
        'scientific_protocol':frozen,'scientific_required_outcome':'supported','max_attempts':1},user_id=OWNER)
    runtime[2][-1].update(command(observation_payload(frozen,'metrics','supported')))
    run = finish(runtime,runtime[2][-1],'Measured result under the frozen protocol.')
    assert run['status'] == 'reviewing'
    task = runtime[2][-1];read_originals(run,task)
    args = {'review':json.loads(review())}
    message = {'role':'assistant','content':'','tool_calls':[{'id':'review-call','type':'function',
        'function':{'name':delivery.NAME,'arguments':json.dumps(args)}}]}
    task['_compact_messages'] = [message]
    result = delivery.prepare(task,args,call_id='review-call',round_num=0)
    if change != 'unchanged':
        value, version = runtime[0]._read(initial['run_id'],OWNER)
        if change == 'threshold': value['scientific_protocol']['decision_rule']['minimum_effect'] += 1
        else: value['scientific_required_outcome'] = 'refuted'
        runtime[0]._save(value,version,OWNER)
        assert not acknowledge(task,message,result)
        assert runtime[0].get_run(initial['run_id'],user_id=OWNER)['status'] == 'reviewing'
        assert len(runtime[2]) == 2
    else:
        assert acknowledge(task,message,result)
        final = finish(runtime,task,task['content'])
        assert final['status'] == 'completed'
        assert final['scientific_protocol'] == frozen
        assert final['scientific_required_outcome'] == 'supported'


@pytest.mark.parametrize('change', ['threshold','required_outcome','remove_protocol','episode','attempt','stage','unchanged'])
def test_review_preparation_matches_sealed_experiment_and_direct_json_cannot_bypass(runtime, change):
    from tests.evolution.test_evolution_scientific_observations import protocol, command
    from tests.support.research_observations import observation_payload
    frozen = protocol()
    initial = runtime[0].submit_run({'objective':'Assess original frozen experimental criteria.',
        'scientific_protocol':frozen,'scientific_required_outcome':'supported','max_attempts':0},user_id=OWNER)
    runtime[2][-1].update(command(observation_payload(frozen,'metrics','supported')))
    run = finish(runtime,runtime[2][-1],'Original measured result.')
    task = runtime[2][-1];read_originals(run,task)
    args = {'review':json.loads(review())}
    task['_compact_messages'] = [{'role':'assistant','content':'','tool_calls':[{'id':'review-call','type':'function',
        'function':{'name':delivery.NAME,'arguments':json.dumps(args)}}]}]
    if change != 'unchanged':
        if change in ('episode','attempt','stage'):
            execution_id = run['receipts'][0]['task_id']
            metadata = stage_outcomes.read_metadata(run['run_id'],execution_id,user_id=OWNER)
            if change == 'episode': metadata['episode_id'] += '-other'
            elif change == 'attempt': metadata['attempt'] += 1
            else:
                metadata['stage'] = 'reviewing';metadata['review_prompt'] = None
            stage_outcomes.records.put_record(stage_outcomes.NAMESPACE,
                stage_outcomes._key(execution_id,OWNER),metadata,kind='outcome')
        else:
            value, version = runtime[0]._read(initial['run_id'],OWNER)
            if change == 'threshold': value['scientific_protocol']['decision_rule']['minimum_effect'] += 1
            elif change == 'required_outcome': value['scientific_required_outcome'] = 'refuted'
            else:
                value.pop('scientific_protocol');value.pop('scientific_required_outcome')
            runtime[0]._save(value,version,OWNER)
        with pytest.raises(EvolutionStateError, match='frozen executor protocol'):
            delivery.prepare(task,args,call_id='review-call',round_num=0)
        assert '_explicitToolDeliveryRequest' not in task
        count = len(runtime[2])
        final = finish(runtime,task,json.dumps(args['review']))
        assert final['status'] == 'needs_attention' and final['error'] == 'scientific_review_binding_changed'
        assert final['review']['outcome'] != 'success'
        assert len(runtime[2]) == count
    else:
        result = delivery.prepare(task,args,call_id='review-call',round_num=0)
        assert result['review_prepared']


@pytest.mark.parametrize('identity', ['exact', 'explicit_deliverable', 'foreign_task', 'changed_digest', 'changed_length', 'wrong_kind'])
def test_requirement_original_links_bind_native_acceptance_to_delivered_executor(runtime, identity):
    run, task, args, _message = request(runtime, requirements=True)
    source = deepcopy(run['result_source'])
    if identity == 'explicit_deliverable': source['source_kind'] = 'deliverable'
    elif identity == 'foreign_task': source['task_id'] = 'another-executor'
    elif identity == 'changed_digest': source['source_sha256'] = '0' * 64
    elif identity == 'changed_length': source['total_chars'] += 1
    elif identity == 'wrong_kind': source['source_kind'] = 'tool_evidence'
    args['review']['requirement_results'][0]['original_sources'] = [source]
    completed = finish(runtime, task, json.dumps(args['review']))
    valid = identity in ('exact', 'explicit_deliverable')
    assert completed['status'] == ('completed' if valid else 'needs_attention')
    result = completed['review']['requirement_results'][0]
    assert result['original_sources'] == [source]
    assert result['outcome'] == ('pass' if valid else 'unknown')
    assert next(item for item in completed['review']['criterion_results']
        if item['criterion_id'] == 'objective')['outcome'] == ('pass' if valid else 'unknown')


def test_requirement_original_link_without_provider_delivery_stays_unknown(runtime):
    run, task, args, _message = request(runtime, requirements=True, read_sources=False)
    args['review']['requirement_results'][0]['original_sources'] = [deepcopy(run['result_source'])]
    completed = finish(runtime, task, json.dumps(args['review']))
    assert completed['status'] == 'needs_attention'
    assert completed['review']['requirement_results'][0]['outcome'] == 'unknown'


def test_requirement_original_checks_reuse_reads_only_within_one_projection(runtime, monkeypatch):
    from lib.evolution import source_tools
    run, task, args, _message = request(runtime, requirements=True)
    run['acceptance_requirements'] = [f'Support original requirement {index}' for index in range(32)]
    judged = deepcopy(args['review'])
    judged['requirement_results'] = [{'requirement_id': f'requirement-{index+1}',
        'outcome':'pass', 'evidence_ref':'Inspected frozen original.',
        'original_sources':[deepcopy(run['result_source'])]} for index in range(32)]
    original = source_tools._review_source
    reads = []
    def counted(*args, **kwargs):
        reads.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(source_tools, '_review_source', counted)
    runtime[0]._enforce_review_evidence(task, run, judged)
    assert all(row['outcome'] == 'pass' for row in judged['requirement_results'])
    assert len(reads) == 1
    task.pop('_evolutionSourceCoverage', None)
    refreshed = deepcopy(args['review'])
    refreshed['requirement_results'] = deepcopy(judged['requirement_results'])
    runtime[0]._enforce_review_evidence(task, run, refreshed)
    assert all(row['outcome'] == 'unknown' for row in refreshed['requirement_results'])
    assert len(reads) == 2


def test_structured_review_terminal_port_preserves_requirement_original_links(runtime):
    run, task, args, message = request(runtime, requirements=True)
    sources = [deepcopy(run['result_source'])]
    args['review']['requirement_results'][0]['original_sources'] = sources
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    prepared = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, prepared)
    completed = finish(runtime, task, task['content'])
    assert completed['status'] == 'completed'
    assert completed['review']['requirement_results'][0]['original_sources'] == sources
    metadata = stage_outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER)
    assert metadata['archive_state'] == 'sealed'


def test_requirement_original_links_without_frozen_requirement_scope_are_refused(runtime):
    run, task, args, message = request(runtime)
    args['review']['requirement_results'] = [{'requirement_id':'requirement-1','outcome':'pass',
        'evidence_ref':'An invented acceptance requirement.',
        'original_sources':[deepcopy(run['result_source'])]}]
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    with pytest.raises(EvolutionContractError, match='frozen acceptance requirements'):
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    assert '_explicitToolDeliveryRequest' not in task


@pytest.mark.parametrize('fault', ['foreign_task', 'changed_digest', 'changed_length', 'wrong_kind'])
def test_bad_requirement_reference_can_be_corrected_without_reexecuting(runtime, fault):
    run, task, args, message = request(runtime, requirements=True)
    source = deepcopy(run['result_source'])
    wrong = deepcopy(source)
    if fault == 'foreign_task': wrong['task_id'] = 'another-executor'
    elif fault == 'changed_digest': wrong['source_sha256'] = '0'*64
    elif fault == 'changed_length': wrong['total_chars'] += 1
    else: wrong['source_kind'] = 'tool_evidence'
    args['review']['requirement_results'][0]['original_sources'] = [wrong]
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    writes = runtime[1].writes
    with pytest.raises(EvolutionStateError, match='requirement original'):
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert '_explicitToolDeliveryRequest' not in task
    assert runtime[1].writes == writes and len(runtime[2]) == 2
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    args['review']['requirement_results'][0]['original_sources'] = [source]
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    prepared = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, prepared)
    completed = finish(runtime, task, task['content'])
    assert completed['status'] == 'completed' and len(runtime[2]) == 2
    assert completed['attempt'] == run['attempt']
    assert completed['episode_id'] == run['episode_id']


def test_correct_requirement_reference_preserves_partial_negative_inspection(runtime):
    run, task, args, message = request(runtime, outcome='fail', requirements=True, read_sources=False)
    args['review']['requirement_results'][0]['original_sources'] = [deepcopy(run['result_source'])]
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    prepared = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, prepared)
    completed = finish(runtime, task, task['content'])
    assert completed['status'] == 'needs_attention' and len(runtime[2]) == 2
    assert completed['review']['requirement_results'][0]['outcome'] == 'fail'
    assert next(row for row in completed['review']['criterion_results']
        if row['criterion_id'] == 'evidence')['outcome'] == 'fail'


@pytest.mark.parametrize('submission', ['json', 'tool', 'wrapped'])
def test_full_requirement_evidence_manifest_fits_native_review_delivery(runtime, submission):
    runs, _, tasks = runtime
    requirements = [f'Inspect original requirement {index}' for index in range(32)]
    runs.submit_run({'objective':'Audit all original requirements with complete evidence.',
        'acceptance_requirements':requirements,'max_attempts':1},user_id=OWNER)
    executor = tasks[-1]
    executor['messages'].append({'role':'tool','tool_call_id':'complete-observation','content':CONTENT})
    run = finish(runtime, executor, CONTENT)
    task = tasks[-1]
    read_originals(run, task)
    sources = [deepcopy(run['result_source']), deepcopy(run['tool_evidence_source'])]
    value = json.loads(review())
    value['requirement_results'] = [{'requirement_id':key, 'outcome':'pass',
        'evidence_ref':'Observed supporting evidence. '+ 'x'*470, 'original_sources':sources}
        for key in runs._requirements(run)]
    content = json.dumps(value,ensure_ascii=False,separators=(',', ':'))
    assert len(content) > 24000
    if submission == 'tool':
        args = {'review':value}
        message = {'role':'assistant','content':'','tool_calls':[{'id':'review-call',
            'type':'function','function':{'name':delivery.NAME,'arguments':json.dumps(args)}}]}
        task['_compact_messages'] = [message]
        prepared = delivery.prepare(task,args,call_id='review-call',round_num=0)
        assert acknowledge(task,message,prepared)
        content = task['content']
    if submission == 'wrapped': content = '```json\n' + content + '\n```'
    completed = finish(runtime,task,content)
    assert completed['status'] == 'completed' and len(tasks) == 2
    assert len(completed['review']['requirement_results']) == 32
    assert all(row['original_sources'] == sources for row in completed['review']['requirement_results'])


def test_large_valid_review_whitespace_does_not_create_retry_or_refusal(runtime):
    run, task, args, _message = request(runtime, requirements=True)
    content = json.dumps(args['review'])
    content = content.replace('{', '{' + ' ' * (2 << 20), 1)
    completed = finish(runtime, task, content)
    assert completed['status'] == 'completed'
    assert {key: completed['review'][key] for key in args['review']} == args['review']
    assert len(runtime[2]) == 2


def test_large_malformed_review_uses_syntax_recovery_without_executor_replay(runtime):
    run, task, args, _message = request(runtime, requirements=True)
    resumed = finish(runtime, task, 'x' * 24001)
    assert resumed['status'] == 'reviewing' and len(runtime[2]) == 3
    assert resumed['receipts'][-1]['review_format_retry']
    assert resumed['attempt'] == run['attempt']


@pytest.mark.parametrize('retry_budget', [0, 1])
def test_required_original_manifest_retries_only_readonly_review_without_executor_replay(runtime, retry_budget):
    from lib.evolution.source_tools import execute
    runs, _, tasks = runtime
    runs.submit_run({'objective':'Audit every original requirement.', 'max_attempts':0,
        'acceptance_requirements':['Original evidence is inspected.'],
        'require_requirement_originals':True,'max_review_format_retries':retry_budget},user_id=OWNER)
    run = finish(runtime,tasks[-1],'Short but fully archived evidence.')
    assert run['result_source']['total_chars'] == len('Short but fully archived evidence.')
    value = json.loads(review())
    value['requirement_results'] = [{'requirement_id':key,'outcome':'pass','evidence_ref':'Claimed complete.'}
        for key in runs._requirements(run)]
    failed = finish(runtime,tasks[-1],json.dumps(value))
    assert failed['receipts'][-1]['review_format_retry']['error'] == 'originals_uninspected'
    assert failed['attempt'] == 1 and failed['episode_id'] == run['episode_id']
    if retry_budget == 0:
        assert failed['status'] == 'needs_attention' and len(tasks) == 2
        return
    assert failed['status'] == 'reviewing' and len(tasks) == 3
    assert tasks[-1]['config']['evolution']['stage'] == 'reviewing'
    assert execute(tasks[-1],{'action':'list'})['sources'][0]['task_id'] == tasks[0]['id']
    read_originals(failed,tasks[-1])
    for row in value['requirement_results']: row['original_sources'] = [deepcopy(failed['result_source'])]
    completed = finish(runtime,tasks[-1],json.dumps(value))
    assert completed['status'] == 'completed' and len(tasks) == 3
    assert completed['actuals']['tokens'] == 45


def test_required_manifest_structured_submission_stays_in_same_reviewer(runtime):
    runs, _, tasks = runtime
    runs.submit_run({'objective':'Audit complete original evidence.','max_attempts':0,
        'acceptance_requirements':['Inspect the original.'],'require_requirement_originals':True},user_id=OWNER)
    run = finish(runtime,tasks[-1],'Short original.')
    task = tasks[-1]
    args = {'review':json.loads(review())}
    args['review']['requirement_results'] = [{'requirement_id':key,'outcome':'pass','evidence_ref':'Claimed inspected.'}
        for key in runs._requirements(run)]
    message = {'role':'assistant','content':'','tool_calls':[{'id':'review-call','type':'function',
        'function':{'name':delivery.NAME,'arguments':json.dumps(args)}}]}
    task['_compact_messages'] = [message]
    with pytest.raises(EvolutionStateError,match='manifest is unfinished'):
        delivery.prepare(task,args,call_id='review-call',round_num=0)
    assert runs.get_run(run['run_id'],user_id=OWNER)['status'] == 'reviewing' and len(tasks) == 2
    read_originals(run,task)
    for row in args['review']['requirement_results']: row['original_sources'] = [deepcopy(run['result_source'])]
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    prepared = delivery.prepare(task,args,call_id='review-call',round_num=0)
    assert acknowledge(task,message,prepared)
    completed = finish(runtime,task,task['content'])
    assert completed['status'] == 'completed' and len(tasks) == 2
