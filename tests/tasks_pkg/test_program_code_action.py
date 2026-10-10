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
        return [CanonicalReceipt({'call_id':c['id'], 'name':c['function']['name'], 'status':'done',
                 'output':'[{"amount":3},{"amount":8}]', 'output_truncated':False},
                 canonical=capture_result('[{"amount":3},{"amount":8}]'))
                for c in batch]
    monkeypatch.setattr(tool_gateway, '_execute_normalized', execute)
    monkeypatch.setattr(tool_gateway, '_finalize', lambda *a, **kw: settled.append(a))
    def run(arguments, cfg=None):
        _, content, aborted = tool_gateway.handle_execute_program(
            task, {}, EXECUTE_PROGRAM_NAME, 'owned-code-call', arguments, 0,
            {'llmRound':0}, cfg or {}, None, False)
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
    ('program', {'tools':{'programmaticCalling':'off'}}, 'mode_off'),
])
def test_operator_disabled_program_never_reaches_guest(world, monkeypatch, tier, config, reason):
    task, calls, _, run = world
    task['_ptc_local']['tier'] = tier
    monkeypatch.setattr(tool_gateway, '_execute_program', lambda *a, **kw: pytest.fail('disabled program executed'))
    result = run({'code':'return await tools.read_rows({});'}, config)
    assert result['error']['code'] == 'program_disabled'
    assert result['error']['reason'] == reason
    assert not calls


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
    assert result['inputs'][0]['shape'] == {
        'type':'array', 'length':2,
        'firstItem':{'type':'object','keys':['amount'],'omittedKeys':0,'shortenedKeys':False}}
    assert result['inputs'][0]['executionStatus'] == 'ok'
    assert len(calls) == 1
    content = settled[-1][4]
    assert '"amount":3' not in str(content)
    assert json.loads(content.canonical.children[0].canonical.parts[0].text) == [{'amount':3},{'amount':8}]
    assert json.loads(content.canonical.parts[0].text) == result
    assert task['programRuns'][0]['status'] == 'error'
    stats=task['programRuns'][0]['stats']
    assert stats['outcome']=='error'
    assert stats['tool_calls']==stats['successful_tool_calls']==1
    assert stats['result_reads']==0


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
        assert result['inputs'][0]['callId'] == 'previous-read'
        assert result['inputs'][0]['shape']['type'] == 'array'
        assert result['inputs'][0]['shape']['length'] == 2
        assert result['inputs'][0]['shape']['firstItem']['keys'] == ['amount']
        assert '"amount":3' not in str(settled[-1][4])
    assert not settled[-1][4].canonical.children


def test_missing_return_hands_back_and_replay_does_not_repeat_completed_tool(world):
    task, calls, settled, run = world
    args = {'code':'await tools.read_rows({});'}
    first = run(args)
    assert first['reason'] == 'missing_program_result'
    assert first['data']['completedToolCalls'] == 1
    assert run(args) == first
    assert len(calls) == 1 and len(settled) == 2
    program = task['programRuns'][0]
    assert program['stats']['outcome'] == 'needs_model'
    assert program['stats']['tool_calls'] == 1
