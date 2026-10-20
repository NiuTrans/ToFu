"""First-class code action retains task catalog authority and replay settlement."""
import json

import pytest

from lib.tasks_pkg.handlers import tool_gateway
from lib.tools.gateway import EXECUTE_PROGRAM_NAME, execute_program_schema, normalize_execute_request
from lib.tools.result_value import CanonicalReceipt, capture_result

pytestmark = pytest.mark.unit


def schema(name):
    return {'type':'function', 'function':{'name':name, 'parameters':{
        'type':'object', 'properties':{}, 'additionalProperties':False}}}


@pytest.fixture
def world(monkeypatch):
    calls = []
    settled = []
    task = {'model':'unit-model', '_executable_tool_catalog':[schema('read_rows')],
            '_ptc_local':{'tier':'program'}}
    def execute(owner, batch, execution, **kwargs):
        calls.extend(batch)
        results = [CanonicalReceipt({'call_id':c['id'], 'name':c['function']['name'], 'status':'done',
                 'output':'[{"amount":3},{"amount":8}]', 'output_truncated':False},
                 canonical=capture_result('[{"amount":3},{"amount":8}]'))
                for c in batch]
        # Match the real pipeline's post-execution retention boundary.
        from lib.tools.program_results import retain_program_result
        for call, result in zip(batch, results):
            retain_program_result(owner, result['name'],
                json.loads(call['function']['arguments']), result['call_id'],
                result.canonical, user_id=owner.get('_userId', 0))
        return results
    monkeypatch.setattr(tool_gateway, '_execute_normalized', execute)
    monkeypatch.setattr(tool_gateway, '_finalize', lambda *a, **kw: settled.append(a))
    def run(arguments, cfg=None):
        _, content, aborted = tool_gateway.handle_execute_program(
            task, {}, EXECUTE_PROGRAM_NAME, 'owned-code-call', arguments, 0,
            {'llmRound':0}, cfg if cfg is not None else
            {'tools': {'programmaticCalling': 'on'}}, None, False)
        assert aborted is False
        return json.loads(content)
    return task, calls, settled, run


def test_code_action_computes_private_data_and_replays_without_reexecuting(world):
    task, calls, settled, run = world
    args = {'code':'const rows=await tools.read_rows({}); return rows.reduce((s,r)=>s+r.amount,0);'}
    first = run(args)
    replay = run(args)
    assert first == replay
    assert first == 11
    assert len(calls) == 1 and len(task['programRuns']) == 1
    assert len(settled) == 2
    assert task['programRuns'][0]['source'] == 'execute_program'


@pytest.mark.parametrize('arguments', [None, [], {}, {'code':3}, {'code':' '},
    {'program':'return 3;'}, {'code':'return 3;', 'calls':[]}])
def test_invalid_code_action_never_executes_or_repairs_arguments(world, arguments):
    task, calls, _, run = world
    payload = run(arguments)
    assert payload['status'] == 'error'
    assert payload['errors'][0]['code'] == 'invalid_request'
    assert not calls and not task.get('programRuns')


def test_named_call_does_not_grant_unknown_tool_authority(world):
    _, calls, _, run = world
    result = run({'code':'return await tools.unavailable({});'})
    assert result['kind'] == 'observation'
    assert not calls


@pytest.mark.parametrize('tier, config, reason', [
    ('batch', {}, 'batch_tier'),
    ('program', {}, 'batch_tier'),
    ('program', {'tools':{'programmaticCalling':'auto'}}, 'batch_tier'),
    ('program', {'tools':{'programmaticCalling':'off'}}, 'mode_off'),
])
def test_operator_disabled_program_never_reaches_guest(world, monkeypatch, tier, config, reason):
    task, calls, _, run = world
    task['_ptc_local']['tier'] = tier
    monkeypatch.setattr(tool_gateway, '_execute_program', lambda *a, **kw: pytest.fail('disabled program executed'))
    result = run({'code':'return await tools.read_rows({});'}, config)
    assert result['error']['code'] == 'program_disabled'
    assert result['items'][0]['detail']['reason'] == reason
    assert not calls


@pytest.mark.parametrize('mode', [None, 'auto', 'off'])
def test_legacy_program_cannot_bypass_policy_and_json_batch_still_executes_once(world, monkeypatch, mode):
    task, calls, _, _ = world
    task.pop('_ptc_local')
    cfg = {} if mode is None else {'tools': {'programmaticCalling': mode}}
    monkeypatch.setattr(tool_gateway, '_execute_program',
                        lambda *a, **kw: pytest.fail('disabled program reached VM'))

    def invoke(call_id, arguments):
        _, content, aborted = tool_gateway.handle_execute_tools(
            task, {}, 'execute_tools', call_id, arguments, 0,
            {'llmRound': 0}, cfg, None, False)
        assert not aborted
        return json.loads(content)

    failure = invoke('legacy-code', {'program': 'return await tools.read_rows({});'})
    assert failure['program']['error']['code'] == 'program_disabled'
    assert not calls and not task.get('programRuns')
    batch = {'calls': [{'name': 'read_rows', 'arguments': {}}], 'execution': 'parallel'}
    first = invoke('json-batch', batch)
    assert first['status'] == 'ok'
    assert invoke('json-batch', batch) == first
    assert len(calls) == 1


def test_code_schema_and_normalizer_require_one_unambiguous_lane():
    params = execute_program_schema()['function']['parameters']
    assert set(params['properties']) == {'code'} and params['required'] == ['code']
    assert params['additionalProperties'] is False
    result = normalize_execute_request({'code':'return 3;'}, program_entry=True,
        catalog=[], namespace_by_name={}, gateway_call_id='test')
    assert result['program'] == 'return 3;' and not result['calls'] and not result['errors']


@pytest.mark.parametrize('expression, expected', [
    ('null', None), ('false', False), ('0', 0), ('""', ''),
    ('JSON.stringify({x:1})', '{"x":1}'),
    ('({stats:"business",program:{result:7}})', {'stats':'business','program':{'result':7}}),
])
def test_code_action_returns_exact_value_and_retains_canonical_root(world, expression, expected):
    task, _, settled, run = world
    assert run({'code':'return ' + expression + ';'}) == expected
    content = settled[-1][4]
    assert content.canonical.private_children is True
    assert json.loads(content.canonical.parts[0].text) == expected
    assert task['programRuns'][0]['stats']['engine'] == 'quickjs-wasm'


def test_failed_code_reports_actual_array_structure_without_child_body(world):
    task, calls, settled, run = world
    result = run({'code':'const rows=await tools.read_rows({}); return rows.items.length;'})
    assert result['status'] == 'error'
    assert result['error']['code'] == 'javascript_error'
    inputs = result['items'][0]['inputs']
    assert inputs[0]['shape'] == {
        'type':'array', 'length':2,
        'firstItem':{'type':'object','keys':['amount'],'omittedKeys':0,'shortenedKeys':False}}
    assert inputs[0]['executionStatus'] == 'ok'
    assert len(calls) == 1
    content = settled[-1][4]
    assert '"amount":3' not in str(content)
    assert json.loads(content.canonical.children[0].canonical.parts[0].text) == [{'amount':3},{'amount':8}]
    canonical = json.loads(content.canonical.parts[0].text)
    assert canonical['inputs'] == inputs
    assert canonical['error']['code'] == result['error']['code']
    assert canonical['error']['retry_hint'] == result['error']['next_action']
    assert task['programRuns'][0]['status'] == 'error'
    stats=task['programRuns'][0]['stats']
    assert stats['outcome']=='error'
    assert stats['tool_calls']==stats['successful_tool_calls']==1
    assert stats['result_reads']==0


@pytest.mark.parametrize('with_completed_child', [False, True])
def test_missing_input_diagnostics_reach_model_and_canonical_error(world, with_completed_child):
    task, calls, settled, run = world
    task['_userId'] = 7
    prefix = 'await tools.read_rows({}); ' if with_completed_child else ''
    result = run({'code': prefix + "const page=tools.inputs({name:'absent'}); "
                  "return page.inputs[page.inputs.length-1].ref;"})
    assert result['status'] == 'error'
    assert result['error']['code'] == 'javascript_error'
    lookup = result['items'][0]['detail']['lastInputLookup']
    assert lookup == {'queryIndex': 1, 'name': 'absent',
                      'returned': 0, 'total': 0, 'nextOffset': None}
    hint = result['error']['next_action']
    assert 'check length before indexing' in hint
    assert 'Never replay effects' in hint
    assert 'read_tool_artifact for artifactRef' in hint
    assert len(hint) <= 300
    canonical = json.loads(settled[-1][4].canonical.parts[0].text)
    assert canonical['error']['lastInputLookup'] == lookup
    assert task['programRuns'][0]['error']['lastInputLookup'] == lookup
    assert len(calls) == int(with_completed_child)


@pytest.mark.parametrize('entry', ['program', 'tools'])
def test_late_authoring_error_keeps_effect_guidance_before_specific_retry(world, entry):
    task, calls, _, run = world
    code = 'await tools.read_rows({}); return catalog.search({query:"rows",limit:false});'
    if entry == 'program':
        result = run({'code': code})
        error = result['error']
        hint = error['next_action']
    else:
        _, content, aborted = tool_gateway.handle_execute_tools(
            task, {}, 'execute_tools', 'gateway-error', {'program': code}, 0,
            {'llmRound': 0}, {'tools': {'programmaticCalling': 'on'}}, None, False)
        assert aborted is False
        error = json.loads(content)['program']['error']
        hint = error['retry_hint']
    assert error['code'] == 'invalid_search_arguments'
    assert hint.startswith('Do not repeat completed child operations;')
    assert 'inspect uncertain outcomes' in hint
    assert 'tools.inputs' in hint and 'tools.result' in hint
    assert 'catalog.search' in hint and len(hint) <= 300
    assert len(calls) == 1
    assert task['programRuns'][0]['error']['retry_hint'] == hint


def test_program_failure_can_reuse_completed_child_without_redispatch(world):
    task, calls, _, run = world
    task['_userId'] = 7
    error = run({'code': 'const rows=await tools.read_rows({}); return rows.items.length;'})
    assert error['status'] == 'error'
    assert 'Do not repeat completed child operations' in error['error']['next_action']
    value = run({'code': 'return tools.result("read_rows",{}).reduce((s,r)=>s+r.amount,0);'})
    assert value == 11
    assert len(calls) == 1
    assert task['programRuns'][-1]['stats']['result_reads'] == 1


@pytest.mark.parametrize('code,expected', [
    ('runtime_unavailable', 'Repair the reported runtime problem'),
    ('runtime_integrity', 'Repair the reported runtime problem'),
    ('cancelled', 'Respect cancellation'),
])
def test_program_environment_and_cancellation_errors_do_not_advise_code_repair(
        world, monkeypatch, code, expected):
    from lib.tools.program_contract import ProgramExecutionError

    def fail(*args, **kwargs):
        raise ProgramExecutionError(code, 'specific failure')

    monkeypatch.setattr('lib.tools.javascript.execute_javascript_program', fail)
    _, calls, _, run = world
    error = run({'code': 'return 1;'})['error']
    assert error['code'] == code and error['message'] == 'specific failure'
    assert error['retryable'] is False
    assert expected in error['next_action']
    assert 'Correct' not in error['next_action']
    assert not calls


@pytest.mark.parametrize('code,success', [
    ('const rows=tools.result("read_rows",{}); return rows.reduce((s,r)=>s+r.amount,0);',True),
    ('const rows=tools.result("read_rows",{}); return rows.items.filter(r=>r.amount>0);',False),
])
def test_reused_inputs_preserve_shapes_without_inventing_child_execution(world, code, success):
    from lib.tools.program_results import retain_program_result
    task,calls,settled,run = world
    task['_userId'] = 7
    source = capture_result('[{"amount":3},{"amount":8}]')
    retain_program_result(task,'read_rows',{},'previous-read',source,user_id=7)
    result = run({'code':code})
    assert not calls and not task['programRuns'][0]['childCalls']
    if success:
        assert result == 11
        assert task['programRuns'][0]['stats']['result_reads'] == 1
    else:
        stats=task['programRuns'][0]['stats']
        assert stats['result_read_attempts']==stats['result_reads']==1
        assert stats['tool_calls']==stats['successful_tool_calls']==0
        assert stats['outcome']=='error'
        assert stats['result_delivery']['rawBytes']==len(source.text().encode())
        assert stats['result_bytes']==len(json.dumps(task['programRuns'][0]['error'],ensure_ascii=False,separators=(',',':')).encode())
        assert 'stats' not in result
        inputs = result['items'][0]['inputs']
        assert inputs[0]['callId'] == 'previous-read'
        assert inputs[0]['shape']['type'] == 'array'
        assert inputs[0]['shape']['length'] == 2
        assert inputs[0]['shape']['firstItem']['keys'] == ['amount']
        assert '"amount":3' not in str(settled[-1][4])
    assert not settled[-1][4].canonical.children


def test_missing_return_is_error_and_replay_does_not_repeat_completed_tool(world):
    task, calls, settled, run = world
    args = {'code':'await tools.read_rows({});'}
    first = run(args)
    assert first['error']['code'] == 'missing_program_result'
    assert first['items'][0]['detail']['executionReceipt']['successfulToolCalls'] == 1
    assert run(args) == first
    assert len(calls) == 1 and len(settled) == 2
    program = task['programRuns'][0]
    assert program['stats']['outcome'] == 'error'
    assert program['stats']['tool_calls'] == 1
    assert program['stats']['successful_tool_calls'] == 1
    # An error after an effect must not become automatic retry/batch recovery.
    assert task['_toolScriptConsecutiveAuthoringFailures'] == 0


@pytest.mark.parametrize('code', [
    'const value = 3;',
    'Promise.reject(new Error("unhandled")); return 3;',
])
def test_repeated_no_effect_authoring_errors_use_existing_batch_fallback(world, code):
    task, calls, _, run = world
    first = run({'code': code})
    assert first['status'] == 'error'
    # Replaying the same occurrence returns its receipt without recounting it.
    assert run({'code': code}) == first
    assert task['_toolScriptConsecutiveAuthoringFailures'] == 1
    second = run({'code': code + '\n// distinct occurrence'})
    assert second['status'] == 'error'
    assert task['_toolScriptConsecutiveAuthoringFailures'] == 2
    assert task['_ptc_local']['tier'] == 'batch'
    assert run({'code': 'return 3;'})['error']['code'] == 'program_disabled'
    assert not calls


def test_canonical_schema_return_example_executes_as_documented(world):
    _, calls, _, run = world
    description = execute_program_schema()['function']['description']
    example = description.split('Example: ', 1)[1].split(' await tools.invoke', 1)[0]
    assert run({'code': example}) == {'sum': 6}
    assert 'programInput is metadata, NOT a global' in description
    assert 'not console.log' in description
    assert 'with no resume' in description
    assert 'offset:0,limit:16' in description
    assert not calls
