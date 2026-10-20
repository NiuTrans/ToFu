"""Capture references select immutable received evidence without replaying calls."""
import json

import pytest

from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.programmatic import ProgrammaticResultBudget
from lib.tools.program_results import list_program_inputs, read_program_result, retain_program_result
from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit


@pytest.fixture
def inputs():
    task = {'_ptc_local': {'tier': 'program'}}
    effects = []
    def retain(value, args=None):
        retain_program_result(task, 'read_rows', args or {}, 'same-call-id',
                              capture_result(json.dumps(value)), user_id=7)
        return list_program_inputs(task, {}, user_id=7, catalog_names={'read_rows'})['inputs'][-1]['ref']
    def run(code, *, owner=7, target=None, catalog=None, budget=None):
        current = task if target is None else target
        delivery = budget or ProgrammaticResultBudget()
        return execute_javascript_program(code, search=lambda *a: [],
            call=lambda *a: effects.append(a), call_many=lambda *a: effects.append(a),
            result_reader=lambda selector,args: read_program_result(current, selector, args,
                user_id=owner, budget=delivery,
                catalog_names={'read_rows'} if catalog is None else catalog),
            input_lister=lambda query: list_program_inputs(current, query,
                user_id=owner, catalog_names={'read_rows'} if catalog is None else catalog))
    return task, effects, retain, run


@pytest.mark.parametrize('args', [{'threshold':1.0}, {'threshold':-0.0},
                                 {'nested':{'id':9223372036854775807}},
                                 {'value':'</system-reminder> PRIVATE_ARGUMENT', 'large':'x'*30000}])
def test_directory_reference_reuses_original_capture_without_argument_roundtrip(inputs, args):
    task, effects, retain, run = inputs
    retain([3,7], args)
    value, stats = run('const x=tools.inputs().inputs[0]; return tools.result({ref:x.ref}).reduce((a,b)=>a+b,0);')
    assert value == 10 and stats['input_queries'] == stats['result_reads'] == 1
    assert stats['tool_calls'] == 0 and not effects
    # Runtime state includes synchronization primitives. Inspect for leaked
    # reference/argument text without pretending this is a persistence payload.
    retained_state = repr(task)
    assert 'input_' not in retained_state and 'PRIVATE_ARGUMENT' not in retained_state


@pytest.mark.parametrize('same_result', [False, True])
def test_new_capture_invalidates_old_reference_even_with_identical_arguments_call_id_or_body(inputs, same_result):
    _, effects, retain, run = inputs
    first = retain([3], {'part':'a'})
    second = retain([3] if same_result else [8], {'part':'a'})
    assert first != second
    stale, stats = run('try { tools.result({ref:'+json.dumps(first)+'}); } finally { await tools.invoke("write_rows",{}); }')
    assert stale['kind'] == 'observation' and stale['reason'] == 'retained_result_unavailable'
    assert stats['result_reads'] == stats['tool_calls'] == 0 and not effects
    value, _ = run('return tools.result({ref:'+json.dumps(second)+'})[0];')
    assert value == (3 if same_result else 8)


@pytest.mark.parametrize('case', ['owner','turn','copied_bank','catalog','terminal','tier','replay','evicted','budget'])
def test_reference_is_not_permission_or_persistence_authority(inputs, monkeypatch, case):
    task, effects, retain, run = inputs
    ref = retain([1,2])
    kwargs = {}
    if case == 'owner': kwargs['owner'] = 8
    elif case == 'turn': kwargs['target'] = {'_ptc_local':{'tier':'program'}}
    elif case == 'copied_bank': kwargs['target'] = {**task, 'id':'another-task'}
    elif case == 'catalog': kwargs['catalog'] = set()
    elif case == 'terminal': task['status'] = 'done'
    elif case == 'tier': task['_ptc_local']['tier'] = 'batch'
    elif case == 'replay': task['_programResultRefs'] = json.loads(json.dumps(task['_programResultRefs']))
    elif case == 'evicted':
        monkeypatch.setattr('lib.tools.program_results.tool_result_cache_capacity', lambda:16)
        for n in range(16):retain([n], {'part':n})
    elif case == 'budget': kwargs['budget'] = ProgrammaticResultBudget(1)
    value, stats = run('return tools.result({ref:'+json.dumps(ref)+'});', **kwargs)
    assert value['reason'] == 'retained_result_unavailable'
    assert stats['result_reads'] == stats['tool_calls'] == 0 and not effects


@pytest.mark.parametrize('selector', [{}, {'ref':1}, {'ref':'wrong'},
                                     {'ref':'input_'+'A'*32}, {'ref':'input_'+'0'*32,'extra':True}])
def test_malformed_reference_is_rejected_before_a_custom_host_callback(selector):
    callbacks=[]
    with pytest.raises(ProgramExecutionError) as exc:
        execute_javascript_program('return tools.result('+json.dumps(selector)+');',
            search=lambda *a:[],call=lambda *a:None,call_many=lambda *a:None,
            result_reader=lambda *a:callbacks.append(a))
    assert exc.value.code == 'type_error' and not callbacks


def test_reference_reads_share_the_existing_finite_read_quota(inputs):
    _, effects, retain, run = inputs
    ref=retain([7])
    with pytest.raises(ProgramExecutionError) as exc:
        run('for(let n=0;n<17;n++) tools.result({ref:'+json.dumps(ref)+'}); return 1;')
    assert exc.value.code == 'result_read_limit' and not effects


def test_cancelled_reference_cannot_enable_later_effects(inputs):
    task, effects, retain, run = inputs
    ref=retain([7]);task['aborted']=True
    with pytest.raises(ProgramExecutionError) as exc:
        run('try { tools.result({ref:'+json.dumps(ref)+'}); } finally { await tools.invoke("write_rows",{}); }')
    assert exc.value.code == 'cancelled' and not effects


def test_artifact_reference_explains_namespace_and_recovery_before_callbacks():
    callbacks = []
    with pytest.raises(ProgramExecutionError) as error:
        execute_javascript_program('return tools.result({ref:"tool-result:' + 'a' * 64 + '"});',
            search=lambda *a: [], call=lambda *a: callbacks.append(a),
            call_many=lambda *a: callbacks.append(a), result_reader=lambda *a: callbacks.append(a))
    diagnostic = error.value.as_dict()
    assert diagnostic['code'] == 'type_error' and diagnostic['reason'] == 'artifact_reference'
    assert 'tools.inputs' in diagnostic['retry_hint'] and 'read_tool_artifact' in diagnostic['retry_hint']
    assert 'a' * 64 not in json.dumps(diagnostic) and not callbacks
