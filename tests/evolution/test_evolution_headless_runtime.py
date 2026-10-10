"""One objective through the actual task loop, model dispatch and durable store.

Only the provider transport is deterministic. The execution/review tasks,
terminal handoff, episode settlement and readback are production paths.
"""
from __future__ import annotations

import importlib
import json
import asyncio
import time
import uuid
import subprocess
import shlex
import sys

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


def paired_search_command(protocol, *, conflicting_claim=False):
    """The subprocess computes every sample and identity; no model-supplied metrics."""
    code = """
import hashlib,json,math,statistics
xs=list(range(101))
partitions={'dataset_size':101,'train':[[0,34]],'validation':[[34,67]],'test':[[67,101]]}
def binary_probes(target):
 low=0; high=len(xs)-1; count=0
 while low<=high:
  mid=(low+high)//2; value=xs[mid]; count+=1
  if value==target: return count
  if value<target: low=mid+1
  else: high=mid-1
 raise ValueError('missing target')
def linear_probes(target):
 for count,value in enumerate(xs,1):
  if value==target:return count
 raise ValueError('missing target')
method=[];baseline=[]
for seed in protocol['random_seeds']:
 target=67+(seed*10)%34
 assert 67<=target<101
 method.append({'seed':seed,'value':binary_probes(target)})
 baseline.append({'seed':seed,'value':linear_probes(target)})
context={'dataset':protocol['dataset'],'metric':protocol['primary_metric'],
 'dataset_digest':hashlib.sha256(json.dumps(xs,separators=(',',':')).encode()).hexdigest(),
 'split_digest':hashlib.sha256(json.dumps(partitions,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'evaluation_partition':'test',
 'budget_unit':'element_probe_cap','budget':101}
method_values=[row['value'] for row in method]
baseline_values=[row['value'] for row in baseline]
differences=[a-b for a,b in zip(method_values,baseline_values)]
mean=statistics.mean(differences);error=statistics.stdev(differences)/math.sqrt(len(differences))
rule=protocol['decision_rule'];effect=mean if rule['direction']=='higher' else -mean
margin=rule['uncertainty_multiplier']*error
outcome=('supported' if effect-margin>rule['minimum_effect'] else
 'refuted' if effect+margin<rule['minimum_effect'] else 'inconclusive')
result={'scientific_observations':{'format':protocol['observation_format'],
 'comparison':{'method':dict(context,system='binary-search'),'baseline':dict(context,system=protocol['baseline'])},
 'partitions':partitions,'samples':{'seeds':protocol['random_seeds'],
 'method_seeds':protocol['random_seeds'],'baseline_seeds':protocol['random_seeds'],
 'method_values':method_values,'baseline_values':baseline_values,
 'mean_difference':mean,'standard_error':error}},'scientific_outcome':outcome}
print(json.dumps(result))
"""
    if conflicting_claim:
        code = code.replace('print(json.dumps(result))', "result['scientific_outcome']='refuted'\nprint(json.dumps(result))")
    code = 'import json\nprotocol=json.loads(' + repr(json.dumps(protocol)) + ')\n' + code
    return shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True, scope='module')
@pytest.mark.parametrize('scenario', ['accepted', 'missing_review_audit', 'missing_measurements', 'archive_recovered', 'required_originals_uninspected', 'executor_tool_delivery', 'report_then_submit', 'local_command', 'local_command_failed', 'measured_science', 'measured_claim_conflict'])
def test_headless_goal_executes_reviews_and_persists_without_browser(
        credential_storage, monkeypatch, scenario):
    include_evidence_audit = scenario != 'missing_review_audit'
    measured_science = scenario in ('measured_science', 'measured_claim_conflict')
    scientific = scenario == 'missing_measurements' or measured_science
    local_command = scenario in ('local_command', 'local_command_failed') or measured_science
    from lib.llm.stream_result import ProviderStreamResult
    from lib.llm_dispatch import reset_dispatcher
    from tests.support.model_routing import install_native_test_model_route
    from lib.evolution import runs

    owner = 1
    install_native_test_model_route(owner_user_id=owner)
    reset_dispatcher()
    calls = []
    frozen_protocol = None
    review_read_offsets = []
    handoff_blocked = scenario == 'archive_recovered'
    original_prepare = runs._prepare_stage
    def prepare_stage(*args, **kwargs):
        if handoff_blocked and kwargs.get('stage') == 'reviewing':
            raise RuntimeError('injected interruption after terminal archive')
        return original_prepare(*args, **kwargs)
    monkeypatch.setattr(runs, '_prepare_stage', prepare_stage)

    def response(body, *, on_content=None, **kwargs):
        request = str(body)
        reviewing = 'Independently assess the delivered work' in request
        calls.append('review' if reviewing else 'execute')
        if reviewing and measured_science:
            def strings(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, dict):
                    for item in value.values(): yield from strings(item)
                elif isinstance(value, list):
                    for item in value: yield from strings(item)
            prompt = next(text for text in strings(body) if 'Independently assess the delivered work' in text)
            source = json.loads(prompt.rsplit('\n', 1)[-1])['observed_tool_results_source']
            offset = len(review_read_offsets) * 6000
            if offset < source['total_chars']:
                review_read_offsets.append(offset)
                arguments = {'action':'read', 'run_id':source['run_id'],
                    'task_id':source['task_id'], 'source_sha256':source['source_sha256'],
                    'source_kind':'tool_evidence', 'offset':offset}
                return ProviderStreamResult.from_legacy({'role':'assistant','content':'',
                    'tool_calls':[{'id':'read-measurement-'+str(offset),'type':'function',
                        'function':{'name':'evolution_source','arguments':json.dumps(arguments)}}]},
                    'tool_calls', {'prompt_tokens':40,'completion_tokens':40,'total_tokens':80})
            assert any('method_values' in text and 'element_probes' in text
                for message in body['messages'] if message.get('role') == 'tool'
                for text in strings(message)), 'Reviewer must receive the measured original'
        if reviewing:
            verdict = {'criterion_results': [
                {'criterion_id': key, 'outcome': 'pass',
                 'evidence_ref': 'The worked example and complexity explanation are present.'}
                for key in ('objective', 'evidence', 'delivery')]}
            if include_evidence_audit:
                verdict['evidence_audit'] = {'complete': True, 'objections': []}
            if scenario == 'required_originals_uninspected':
                verdict['requirement_results'] = [{'requirement_id':'requirement-1','outcome':'pass','evidence_ref':'Claimed inspected.'}]
            content = json.dumps(verdict)
        else:
            content = ('Binary search halves the sorted range each step. '
                       'For [1, 3, 5, 7, 9], seek 7: inspect 5 at index 2, '
                       'then 7 at index 3. The result is index 3. '
                       'At most ceil(log2(n)) comparisons give O(log n) time.')
        if local_command and not reviewing and calls.count('execute') == 1:
            code = ('import json,sys\nxs=[1,3,5,7,9]; target=7; low=0; high=len(xs)-1; index=-1; comparisons=0\n'
                    'while low<=high:\n mid=(low+high)//2; comparisons+=1\n'
                    ' if xs[mid]==target: index=mid; break\n'
                    ' if xs[mid]<target: low=mid+1\n else: high=mid-1\n'
                    'print(json.dumps({"index":index,"comparisons":comparisons}))\n'
                    + ('sys.exit(1)' if scenario == 'local_command_failed' else ''))
            command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)
            if measured_science:
                command = paired_search_command(frozen_protocol, conflicting_claim=scenario == 'measured_claim_conflict')
            return ProviderStreamResult.from_legacy({'role':'assistant','content':'',
                'tool_calls':[{'id':'actual-python','type':'function','function':{
                    'name':'run_command','arguments':json.dumps({'command':command})}}]}, 'tool_calls',
                {'prompt_tokens':40,'completion_tokens':40,'total_tokens':80})
        if local_command and not reviewing:
            assert any(message.get('role') == 'tool' for message in body['messages'])
        if scenario in ('executor_tool_delivery', 'report_then_submit') and not reviewing:
            action = 'submit_result'
            if scenario == 'report_then_submit':
                if calls.count('execute') == 1:
                    action = 'report'
                else:
                    assert any(message.get('role') == 'tool'
                               and 'completion_action' in str(message.get('content'))
                               and 'submit_result' in str(message.get('content'))
                               for message in body['messages'])
            return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'executor-answer', 'type': 'function', 'function': {
                    'name': 'evolution_coordinate', 'arguments': json.dumps({
                        'action': action, 'content': content})}}]}, 'tool_calls',
                {'prompt_tokens': 40, 'completion_tokens': 40, 'total_tokens': 80})
        if on_content:
            on_content(content)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': content}, 'stop',
            {'prompt_tokens': 40, 'completion_tokens': 40, 'total_tokens': 80})

    def stream(body, **kwargs):
        if not measured_science:
            return response(body, **kwargs)
        from types import SimpleNamespace
        from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched, strip_internal_request_fields
        with ModelRequestBudgetScope(body) as scope:
            admit = scope.body.get('_model_budget_admit')
            assert callable(admit)
            lease = admit(input_tokens=40, max_output_tokens=40)
            mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease,
                body=strip_internal_request_fields(scope.body)))
            result = response(scope.body, **kwargs)
            scope.usage = result.usage
            return result

    async def async_stream(body, **kwargs):
        return stream(body, **kwargs)

    monkeypatch.setattr('lib.llm.stream_chat', stream)
    monkeypatch.setattr(importlib.import_module('lib.llm.astream'),
                        'async_stream_chat', async_stream)
    try:
        from quart import Quart
        from routes.api_v1.auth import attach_rate_headers, bearer_auth_before_request
        from routes.api_v1.evolution import api_v1_evolution_bp
        from lib.api_keys import create_key
        app = Quart(__name__, static_folder=None)
        app.config['TESTING'] = True
        app.before_request(bearer_auth_before_request)
        app.after_request(attach_rate_headers)
        app.register_blueprint(api_v1_evolution_bp)
        _key, token = create_key(owner_user_id=owner, name='headless-runtime', scopes=['tasks'])
        client = app.test_client()
        headers = {'Authorization': f'Bearer {token}', 'Idempotency-Key': 'headless-runtime-' + uuid.uuid4().hex}
        body = {'objective': 'Explain binary search with a worked example and complexity.',
                'model': {'creator_id': 'tofu-test', 'model_id': 'stub-model'},
                'max_attempts': 1}
        if local_command:
            body['require_command_execution'] = True
        if scenario == 'required_originals_uninspected':
            body.update(require_requirement_originals=True, acceptance_requirements=['Inspect original evidence.'], max_review_format_retries=0)
        if scientific:
            from lib.research.program import normalize_program_fields
            body['scientific_protocol'] = normalize_program_fields({'protocol': {
                'dataset': 'held-out-search-cases', 'baseline': 'linear-search',
                'primary_metric': ('element_probes' if measured_science else 'comparisons'), 'random_seeds': [1, 2, 3],
                'decision_rule': {'direction': 'lower', 'minimum_effect': 0,
                                  'uncertainty_multiplier': 2}}})['protocol']
            if measured_science:
                body['scientific_protocol']['observation_format'] = 'tofu.scientific-observations/v2'
            frozen_protocol = body['scientific_protocol']

        async def journey():
            nonlocal handoff_blocked
            response = await client.post('/api/v4/evolution/runs', json=body, headers=headers)
            assert response.status_code == 201, await response.get_json()
            started = (await response.get_json())['run']
            repeated = await client.post('/api/v4/evolution/runs', json=body, headers=headers)
            assert (await repeated.get_json())['run']['run_id'] == started['run_id']
            deadline = time.monotonic() + 40
            current = started
            if scenario == 'archive_recovered':
                from lib.evolution.stage_outcomes import read_metadata, recover
                metadata = None
                while time.monotonic() < deadline:
                    metadata = read_metadata(started['run_id'], started['task_id'], user_id=owner)
                    if metadata and metadata['archive_state'] == 'sealed':
                        break
                    await asyncio.sleep(.1)
                assert metadata and metadata['archive_state'] == 'sealed'
                before = runs.get_run(started['run_id'], user_id=owner)
                assert before['status'] == 'executing' and before['receipts'] == []
                root = str(credential_storage._supervisor._project_root)
                backend = credential_storage.status()['backend']
                credential_storage.stop()
                script = """
import json, sys, signal
def expired(signum, frame):
    raise TimeoutError('cold archive read deadline exceeded')
signal.signal(signal.SIGALRM, expired)
signal.alarm(70)
from lib.storage import StorageRuntime, StorageSupervisor
from lib.storage.service import install_runtime_for_test
from lib.evolution import runs
from lib.evolution.stage_outcomes import recover
request = json.loads(sys.stdin.read())
runtime = StorageRuntime(StorageSupervisor(project_root=request['root'],
    backend=request['backend'], startup_timeout=60), auto_restart=False)
install_runtime_for_test(runtime)
try:
    runtime.start()
    run = runs.get_run(request['run_id'], user_id=request['owner'])
    outcome, task = recover(run, user_id=request['owner'])
    print(json.dumps({'run_id': run['run_id'], 'status': run['status'],
        'receipt_count': len(run['receipts']), 'task_id': task['id'],
        'content': task['content'], 'tokens': outcome['actuals']['tokens'],
        'archive_state': outcome['archive_state']}))
finally:
    signal.alarm(0)
    runtime.stop()
    install_runtime_for_test(None)
"""
                try:
                    process = await asyncio.to_thread(subprocess.run,
                        [sys.executable, '-c', script], input=json.dumps({
                            'root': root, 'backend': backend, 'owner': owner,
                            'run_id': started['run_id']}), text=True,
                        capture_output=True, timeout=90, check=True)
                    independent = json.loads(process.stdout)
                    assert independent['run_id'] == started['run_id']
                    assert independent['task_id'] == started['task_id']
                    assert independent['status'] == 'executing' and independent['receipt_count'] == 0
                    assert independent['archive_state'] == 'sealed' and independent['tokens'] >= 80
                    assert 'seek 7' in independent['content']
                finally:
                    credential_storage.start()
                cold = runs.get_run(started['run_id'], user_id=owner)
                outcome, restored_task = recover(cold, user_id=owner)
                assert outcome['task_id'] == started['task_id']
                assert outcome['actuals']['tokens'] >= 80
                assert restored_task['id'] == started['task_id']
                assert 'seek 7' in restored_task['content']
                assert calls == ['execute']
                handoff_blocked = False
                reconciled = await client.post(
                    f"/api/v4/evolution/runs/{started['run_id']}/reconcile", headers=headers)
                assert reconciled.status_code == 200, await reconciled.get_json()
            while current['status'] not in runs.TERMINAL and time.monotonic() < deadline:
                await asyncio.sleep(.1)
                snapshot = await client.get(f"/api/v4/evolution/runs/{started['run_id']}", headers=headers)
                assert snapshot.status_code == 200
                current = (await snapshot.get_json())['run']
            return current

        current = asyncio.run(journey())
        from lib.evolution.bridge import default_task_loader
        task = default_task_loader(current['task_id'], user_id=owner)
        if local_command:
            from lib.evolution.stage_outcomes import page
            assert current['receipts'], (current['status'], current.get('error'), calls, (task or {}).get('status'))
            original_task_id = current['receipts'][0]['task_id']
            evidence = page(current['run_id'], original_task_id, user_id=owner, source_kind='tool_evidence')['content']
            command_messages = [json.loads(line) for line in evidence.splitlines() if line]
            command_messages = [row for row in command_messages if row.get('tool_call_id') == 'actual-python']
            original_messages = [row for row in command_messages if row.get('source') == 'acknowledged_scientific_command_original']
            execution_messages = [row for row in command_messages if row.get('source') != 'acknowledged_scientific_command_original']
            assert len(execution_messages) == 1
            assert len(original_messages) == (1 if measured_science else 0)
            text = (original_messages or execution_messages)[0]['content']
            if measured_science:
                measurement_lines = [line for line in text.splitlines() if line.startswith('{"scientific_observations"')]
                assert len(measurement_lines) == 1, text
                measured = json.loads(measurement_lines[0])
                assert measured['scientific_outcome'] == ('refuted' if scenario == 'measured_claim_conflict' else 'supported')
                samples = measured['scientific_observations']['samples']
                assert samples['baseline_values'] == [78,88,98]
                assert samples['method_values'] == [7, 7, 5]
            else:
                measured = next(json.loads(line) for line in text.splitlines() if line.startswith('{"index"'))
                assert measured == {'index': 3, 'comparisons': 2}
            assert '[exit code: '+ ('1' if scenario == 'local_command_failed' else '0') +']' in text
        assert current['status'] == ('completed' if scenario in ('accepted', 'archive_recovered', 'executor_tool_delivery', 'report_then_submit', 'local_command', 'measured_science') else 'needs_attention' if scenario == 'required_originals_uninspected' else 'failed'), (current.get('error'), calls,
            (task or {}).get('status'), (task or {}).get('error'),
            (task or {}).get('finishReason'))
        assert calls == (['execute','execute','execute'] if scenario == 'local_command_failed' else ['execute', 'execute'] if scenario == 'measured_claim_conflict' else ['execute'] if scenario == 'missing_measurements' else ['execute', 'execute'] + ['review'] * (len(review_read_offsets) + 1) if scenario == 'measured_science' else ['execute', 'execute', 'review']
                         if scenario in ('report_then_submit','local_command','measured_science') else ['execute', 'review'])
        assert len(current['receipts']) == (1 if scenario in ('missing_measurements','local_command_failed','measured_claim_conflict') else 2)
        assert current['actuals']['tokens'] >= (80 if scientific else 160)
        if scenario in ('accepted', 'archive_recovered', 'executor_tool_delivery', 'report_then_submit', 'local_command', 'measured_science'):
            assert current['review']['outcome'] == 'success'
            assert current['learning']['ok'] is True
        else:
            assert current['review'] is None
            if scenario == 'required_originals_uninspected':
                from lib.evolution.stage_outcomes import read_metadata
                assert current['error'] == 'required originals were not linked and completely inspected'
                assert current['receipts'][-1]['review_format_retry']['error'] == 'originals_uninspected'
                assert current['receipts'][-1]['review_format_retry']['admitted'] is False
                assert all(read_metadata(current['run_id'], row['task_id'], user_id=owner)['require_requirement_originals'] is True
                    for row in current['receipts'])
            elif scenario == 'local_command_failed':
                assert current['error'] == 'required_command_execution_unobserved'
            elif not scientific:
                assert current['error'] == 'review must include the frozen evidence audit'
            else:
                from lib.evolution.stage_outcomes import read_metadata
                from lib.evolution.tool_evidence import scientific_audit_valid
                outcome = read_metadata(current['run_id'], current['receipts'][0]['task_id'], user_id=owner)
                assert not scientific_audit_valid(outcome['scientific_audit'])
        if measured_science:
            from lib.evolution.stage_outcomes import read_metadata
            from lib.evolution.tool_evidence import scientific_audit_valid
            audit = read_metadata(current['run_id'], current['receipts'][0]['task_id'], user_id=owner)['scientific_audit']
            assert audit['observation_count'] == 1
            assert audit['failure_count'] == (1 if scenario == 'measured_claim_conflict' else 0)
            assert scientific_audit_valid(audit) is (scenario == 'measured_science')
        from lib.evolution import get_episode
        episode = get_episode(current['episode_id'], user_id=owner)
        assert episode['status'] == ('success' if scenario in ('accepted', 'archive_recovered', 'executor_tool_delivery', 'report_then_submit', 'local_command', 'measured_science') else 'aborted')
        if scenario == 'missing_measurements':
            assert not (episode['learning'].get('strategy_outcome') or {}).get('distilled_template_id')
        from lib.evolution import records, stage_inputs
        for receipt in current['receipts']:
            assert records.get_record(stage_inputs.WORKING_NAMESPACE,
                stage_inputs._working_key(receipt['task_id'], owner)) is None
            assert credential_storage.client().query('record.list', {
                'namespace': stage_inputs.WORKING_PARTS_NAMESPACE,
                'prefix': stage_inputs._working_key(receipt['task_id'], owner, '-bank-'),
                'limit': 1}) == []
        assert episode['actuals']['tokens'] == current['actuals']['tokens']
        assert runs.get_run(current['run_id'], user_id=owner) == current
    finally:
        reset_dispatcher()


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True, scope='module')
def test_final_program_manifest_completes_through_real_source_and_terminal_tools(credential_storage, monkeypatch):
    from lib.llm.stream_result import ProviderStreamResult
    from lib.llm_dispatch import reset_dispatcher
    from tests.support.model_routing import install_native_test_model_route
    from lib.evolution import programs, runs, get_episode
    from lib.evolution.stage_outcomes import read_metadata
    owner = 1
    install_native_test_model_route(owner_user_id=owner)
    reset_dispatcher()
    calls = []
    observed_source = {}
    last_tools = []

    def strings(value):
        if isinstance(value,str): yield value
        elif isinstance(value,dict):
            for child in value.values(): yield from strings(child)
        elif isinstance(value,list):
            for child in value: yield from strings(child)

    def objects(value):
        if isinstance(value,dict):
            yield value
            for child in value.values(): yield from objects(child)
        elif isinstance(value,list):
            for child in value: yield from objects(child)

    def response(body, *, on_content=None, **kwargs):
        prompt = next((text for text in strings(body)
                       if 'Independently assess the delivered work' in text), '')
        usage = {'prompt_tokens':40,'completion_tokens':40,'total_tokens':80}
        if not prompt:
            calls.append('execute')
            content = 'Binary search halves a sorted interval. For [1,3,5,7,9], seeking 7 checks 5 then 7; O(log n) comparisons.'
            if on_content: on_content(content)
            return ProviderStreamResult.from_legacy({'role':'assistant','content':content},'stop',usage)
        last_tools[:] = [text[:1200] for row in objects(body) if row.get('role') == 'tool' or row.get('type') == 'function_call_output'
                         for text in strings(row) if 'function' not in text]
        context = json.loads(prompt.rsplit('\n',1)[-1])
        source = context['submission_source']
        if calls == ['execute']:
            calls.append('source')
            observed_source.update({key:value for key,value in source.items() if key != 'run_id'})
            name = 'evolution_source'
            arguments = {'action':'read','run_id':source['run_id'],'task_id':source['task_id'],
                         'source_sha256':source['source_sha256'],'offset':0}
        else:
            calls.append('submit')
            assert any((row.get('role') == 'tool' or row.get('type') == 'function_call_output' or 'functionResponse' in row)
                       and any('Binary search halves' in text for text in strings(row))
                       for row in objects(body)), 'Original must occur in actual reviewer tool inputs'
            name = 'evolution_review'
            arguments = {'review':{'evidence_audit':{'complete':True,'objections':[]},
                'criterion_results':[{'criterion_id':key,'outcome':'pass','evidence_ref':'Inspected original source.'} for key in runs.RUBRIC],
                'requirement_results':[{'requirement_id':key,'outcome':'pass','evidence_ref':'Inspected exact original.',
                    'original_sources':[dict(observed_source)]} for key in context['acceptance_requirements']]}}
        reply = {'role':'assistant','content':'','tool_calls':[{'id':name+'-call','type':'function',
                 'function':{'name':name,'arguments':json.dumps(arguments)}}]}
        return ProviderStreamResult.from_legacy(reply,'tool_calls',usage)

    def stream(body, **kwargs):
        from types import SimpleNamespace
        from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched, strip_internal_request_fields
        with ModelRequestBudgetScope(body) as scope:
            admit = scope.body.get('_model_budget_admit')
            assert callable(admit), 'Actual task must supply the transport admission port'
            lease = admit(input_tokens=40,max_output_tokens=40)
            mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease,
                body=strip_internal_request_fields(scope.body)))
            result = response(scope.body,**kwargs)
            scope.usage = result.usage
            return result

    async def async_stream(body, **kwargs):
        return stream(body, **kwargs)
    monkeypatch.setattr('lib.llm.stream_chat',stream)
    monkeypatch.setattr(importlib.import_module('lib.llm.astream'),'async_stream_chat',async_stream)
    try:
        from quart import Quart
        from routes.api_v1.auth import attach_rate_headers, bearer_auth_before_request
        from routes.api_v1.evolution import api_v1_evolution_bp
        from lib.api_keys import create_key
        app = Quart(__name__,static_folder=None)
        app.config['TESTING'] = True
        app.before_request(bearer_auth_before_request)
        app.after_request(attach_rate_headers)
        app.register_blueprint(api_v1_evolution_bp)
        _,token = create_key(owner_user_id=owner,name='manifest-headless',scopes=['tasks'])
        headers = {'Authorization':'Bearer '+token,'Idempotency-Key':uuid.uuid4().hex}
        client = app.test_client()
        async def journey():
            response = await client.post('/api/v4/evolution/programs',headers=headers,json={
                'objective':'Explain binary search with a worked example and complexity.',
                'acceptance':['The example and complexity are explained.'],
                'model':{'creator_id':'tofu-test','model_id':'stub-model'},'max_attempts':1,
                'steps':[{'kind':'final','title':'Final audit','instruction':'Verify and deliver the original objective.',
                          'checks':['Exact original evidence supports the explanation.']}]})
            assert response.status_code == 201, await response.get_json()
            program = (await response.get_json())['program']
            assert program['final_evidence_version'] == 1
            pid = program['program_id']
            response = await client.post(f'/api/v4/evolution/programs/{pid}/commands',headers=headers,json={'action':'start'})
            assert response.status_code == 200, await response.get_json()
            program = (await response.get_json())['program']
            deadline = time.monotonic()+40
            while program['status'] == 'active' and time.monotonic() < deadline:
                await asyncio.sleep(.1)
                program = programs.advance_program(pid,user_id=owner)
            return program
        program = asyncio.run(journey())
        assert program['status'] == 'completed' and program['settled_through'] == 1, (program.get('error'),calls,last_tools)
        assert calls == ['execute','source','submit']
        receipt = programs.experiment_page(program['program_id'],user_id=owner)['experiments'][0]
        run = runs.get_run(receipt['run_id'],user_id=owner)
        assert run['require_requirement_originals'] is True and run['attempt'] == 1
        assert len(run['receipts']) == 2 and run['actuals']['tokens'] >= 240
        assert all(row['original_sources'] == [observed_source] for row in run['review']['requirement_results'])
        review_meta = read_metadata(run['run_id'],run['task_id'],user_id=owner)
        assert review_meta['source_coverage']['ranges'] == [[0,observed_source['total_chars']]]
        assert get_episode(run['episode_id'],user_id=owner)['status'] == 'success'
    finally:
        reset_dispatcher()
