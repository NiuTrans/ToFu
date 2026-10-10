"""Programs stop at reasoning boundaries without executing later effects."""

import json

import pytest

from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError

pytestmark = pytest.mark.unit


def execute(source, receipt=None, *, aborted=None):
    calls = []

    def call(name, arguments, call_id):
        calls.append((name, arguments))
        return receipt if receipt is not None else {
            'status':'done', 'output':None, 'output_truncated':False}

    value, stats = execute_javascript_program(
        source, search=lambda *args: {}, call=call,
        call_many=lambda calls, mode: [
            {'status':'done', 'output':None, 'output_truncated':False}
            for _call in calls], aborted=aborted)
    return value, stats, calls


@pytest.mark.parametrize('boundary', [
    'tools.observe("choose a candidate", {ids:["a","b"]});',
    'for (const id of ["a","b"]) { tools.observe("choose a candidate", {ids:["a","b"]}); }',
    '[1].map(x => { tools.observe("choose a candidate", {ids:["a","b"]}); return x; });',
    '[1].forEach(x => tools.observe("choose a candidate", {ids:["a","b"]}));',
])
def test_observation_unwinds_all_scopes_and_stops_later_effects(boundary):
    value, stats, calls = execute(
        'await tools.call("already_done", {});' + boundary
        + 'await tools.call("must_not_run", {}); return "incorrect";')
    assert calls == [('already_done', {})]
    assert value == {'kind': 'observation', 'reason': 'choose a candidate',
                     'data': {'ids': ['a', 'b']},
                     'executionReceipt': {'attemptedToolCalls':1,
                                          'successfulToolCalls':1, 'retainedReads':0}}
    assert stats['outcome'] == 'needs_model'
    assert stats['tool_calls'] == stats['call_stages'] == 1


@pytest.mark.parametrize(('receipt', 'reason', 'successful_calls'), [
    ({'status': 'error', 'error': {'code': 'denied'}}, 'tool_not_done', 0),
    ({'status': 'aborted'}, 'tool_not_done', 0),
    ({'status': 'running'}, 'tool_not_done', 0),
    ({'status': 'done', 'output': '[]', 'output_truncated': True},
     'incomplete_tool_output', 1),
    ({'status': 'done', 'output': '[]'}, 'incomplete_tool_output', 1),
    ({'status': 'done', 'output_truncated': False}, 'missing_tool_output', 1),
])
def test_receipt_value_does_not_feed_incomplete_evidence_to_a_dependency(receipt, reason, successful_calls):
    original = json.dumps(receipt, sort_keys=True)
    value, stats, calls = execute(
        'const data = await tools.value(await tools.call("read", {}));'
        'await tools.call("write", {data:data}); return data;', receipt)
    assert calls == [('read', {})]
    assert value == {'kind': 'observation', 'reason': reason, 'data': receipt,
                     'executionReceipt': {'attemptedToolCalls':1,
                                          'successfulToolCalls':successful_calls, 'retainedReads':0}}
    assert stats['outcome'] == 'needs_model'
    assert json.dumps(receipt, sort_keys=True) == original


@pytest.mark.parametrize(('output', 'expected'), [
    ('{"id":"resolved"}', {'id': 'resolved'}), ('普通文本', '普通文本'),
    ('null', None), ('false', False), ('[1,2]', [1, 2]),
])
def test_receipt_value_decodes_complete_json_and_preserves_text(output, expected):
    value, stats, calls = execute('return await tools.value(await tools.call("read", {}));',
        {'status': 'done', 'output': output, 'output_truncated': False})
    assert value == expected
    assert calls == [('read', {})]
    assert stats['outcome'] == 'completed'


@pytest.mark.parametrize('source', [
    'return tools.observe("", {});', 'return tools.observe(1, {});',
    'return await tools.value(1);',
])
def test_invalid_boundary_arguments_are_typed_errors(source):
    with pytest.raises(ProgramExecutionError) as exc:
        execute(source)
    assert exc.value.code == 'type_error'


def test_observation_can_return_to_model_without_data():
    value, stats, calls = execute('tools.observe("model judgment is required");')
    assert value == {'kind': 'observation', 'reason': 'model judgment is required',
                     'data': None, 'executionReceipt': {'attemptedToolCalls':0,
                                                       'successfulToolCalls':0, 'retainedReads':0}}
    assert stats['outcome'] == 'needs_model'
    assert calls == []


def test_observation_preserves_large_data_and_cancellation():
    with pytest.raises(ProgramExecutionError) as exc:
        execute('tools.observe("reason", {});', aborted=lambda: True)
    assert exc.value.code == 'cancelled'
    value, stats, calls = execute('tools.observe("reason", "大".repeat(400000));')
    assert value['data'] == '大' * 400000
    assert stats['outcome'] == 'needs_model' and calls == []


def test_stage_counts_describe_requests_without_claiming_round_trip_savings():
    value, stats, _ = execute(
        'await tools.parallel([{name:"a",arguments:{}},{name:"b",arguments:{}}]);'
        'await tools.callMany([{name:"c",arguments:{}}], "sequential");'
        'await tools.parallel([]); return 7;')
    assert value == 7
    assert stats['tool_calls'] == 3
    assert stats['call_stages'] == 2
    assert stats['parallel_stages'] == 1
