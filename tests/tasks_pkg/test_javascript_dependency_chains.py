"""Dependent JavaScript chains through the real local gateway and pipeline.

Only the leaf tool implementation is substituted: catalog search, argument
validation, admission, receipts, private result delivery and program execution
are production code. No provider or synthetic model turn drives these chains.
"""

from __future__ import annotations

import json
import threading

import pytest

from lib.tasks_pkg.handlers import tool_gateway as handler
from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline
from lib.tools.gateway import EXECUTE_TOOLS_NAME


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('execution', ['implicit', 'explicit'])
@pytest.mark.parametrize('invalid_index', [0, 1])
def test_mixed_invalid_batch_hands_off_without_executing_siblings(
        local_gateway, execution, invalid_index):
    _, dispatched, invoke = local_gateway
    names = ['read_document', 'disabled_document']
    if invalid_index == 0:
        names.reverse()
    calls = [{'name': name, 'arguments': {}} for name in names]
    if execution == 'implicit':
        source = 'return await Promise.all([' + ','.join(
            f'tools.invoke({json.dumps(name)}, {{}})' for name in names) + ']);'
    else:
        source = 'const rows=await tools.callMany(' + json.dumps(calls) + ', "parallel"); return rows.map(tools.value);'
    program = invoke(source, [_tool('read_document')], {
        'read_document': lambda args: pytest.fail('invalid batch executed a sibling'),
    })
    assert dispatched == []
    assert program['status'] == 'ok', program
    assert program['stats']['outcome'] == 'needs_model'
    assert program['stats']['tool_calls'] == 2
    assert program['stats']['successful_tool_calls'] == 0
    result = program['result']
    assert result['reason'] == 'tool_not_done'
    assert result['executionReceipt'] == {
        'attemptedToolCalls': 2, 'successfulToolCalls': 0, 'retainedReads': 0}
    error = result['data']['error']
    assert error['code'] == ('tool_not_enabled' if invalid_index == 0 else 'batch_rejected')
    if invalid_index == 1:
        assert error['cause']['code'] == 'tool_not_enabled'


def test_rejected_batch_keeps_one_receipt_per_input_in_original_order(local_gateway):
    _, dispatched, invoke = local_gateway
    program = invoke('''
      const rows=await tools.callMany([
        {name:'read_document', arguments:{}},
        {name:'disabled_document', arguments:{}},
        {name:'read_document', arguments:{bad:true}}
      ], 'parallel');
      return rows.map(r=>({name:r.name,status:r.status,error:r.error.code}));
    ''', [_tool('read_document')], {
        'read_document': lambda args: pytest.fail('invalid batch executed a sibling'),
    })
    assert dispatched == []
    assert program['status'] == 'ok', program
    assert program['result'] == [
        {'name':'read_document','status':'error','error':'batch_rejected'},
        {'name':'disabled_document','status':'error','error':'tool_not_enabled'},
        {'name':'read_document','status':'error','error':'unknown_arguments'},
    ]
    assert program['stats']['successful_tool_calls'] == 0


def _tool(name, properties=None, required=None, description=''):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description or name,
            'parameters': {
                'type': 'object', 'properties': properties or {},
                'required': required or [], 'additionalProperties': False,
            },
        },
    }


@pytest.fixture
def local_gateway(monkeypatch):
    """Keep the actual gateway/dispatch stack; isolate only tool I/O."""
    dispatched = []
    implementations = {}
    task = {
        'id': 'dependency-chain', '_userId': 1, 'model': 'local-test',
        'status': 'running', 'aborted': False,
        'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
        '_attended': False, '_tool_stream_active': True,
        'config': {'tools': {'resultEnvelope': 'v2'}},
        '_executable_tool_catalog': [],
        '_ptc_local': {'tier': 'program', 'eligible': []},
    }
    invocations = []

    def execute_leaf(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        dispatched.append((fn_name, dict(fn_args)))
        status, value = implementations[fn_name](fn_args)
        round_entry['status'] = status
        # Real handlers return content strings. The gateway wraps these in
        # output/error, NOT content; programs must parse receipt.output.
        return tc_id, json.dumps(value), False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', execute_leaf)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', execute_leaf)

    def invoke(source, catalog, tools, *, expected_status="ok"):
        assert not invocations, 'a dependent chain must use one gateway call'
        invocations.append(source)
        task['_executable_tool_catalog'] = catalog
        implementations.update(tools)
        tc = {
            'id': 'chain-gateway', 'type': 'function',
            'function': {'name': EXECUTE_TOOLS_NAME,
                         'arguments': json.dumps({'program': source})},
        }
        call_id, content, aborted = handler.handle_execute_tools(
            task, tc, EXECUTE_TOOLS_NAME, tc['id'], {'program': source},
            1, {'calls': [], 'llmRound': 1},
            {'autoApply': True, 'tools': {'programmaticCalling': 'on'}}, None, False)
        assert call_id == tc['id']
        assert aborted is False
        payload = json.loads(content)
        assert payload['status'] == expected_status, payload
        assert payload['program']['status'] == expected_status, payload
        assert len(task['programRuns']) == 1
        run = task['programRuns'][0]
        assert run['source'] == 'execute_program'
        assert run['status'] == ('completed' if expected_status == 'ok' else 'error')
        if expected_status == 'ok':
            assert run['result'] == payload['program']['result']
        assert [child['name'] for child in run['childCalls']] == [
            name for name, _ in dispatched]
        assert len(invocations) == 1
        return payload['program']

    return task, dispatched, invoke


def test_upstream_output_supplies_downstream_arguments(local_gateway):
    _, dispatched, invoke = local_gateway
    catalog = [
        _tool('lookup_document'),
        _tool('read_document', {'document_id': {'type': 'string'}},
              ['document_id']),
    ]
    program = invoke('''
        const lookup = await tools.call('lookup_document', {});
        const document = JSON.parse(lookup.output);
        const read = await tools.call('read_document', {document_id: document.id});
        return {lookup: lookup, read: read, title: JSON.parse(read.output).title};
    ''', catalog, {
        'lookup_document': lambda args: ('done', {'id': 'doc-from-lookup'}),
        'read_document': lambda args: ('done', {'title': 'Resolved title'}),
    })

    assert dispatched == [
        ('lookup_document', {}),
        ('read_document', {'document_id': 'doc-from-lookup'}),
    ]
    result = program['result']
    assert result['title'] == 'Resolved title'
    for key, expected in [('lookup', {'id': 'doc-from-lookup'}),
                          ('read', {'title': 'Resolved title'})]:
        receipt = result[key]
        assert receipt['status'] == 'done'
        assert receipt['source'] == 'execute_program'
        assert 'content' not in receipt
        assert json.loads(receipt['output']) == expected
        assert receipt['output_truncated'] is False
    assert program['stats']['tool_calls'] == 2


def test_ambiguous_data_returns_to_model_after_actual_gateway_calls(local_gateway):
    task, dispatched, invoke = local_gateway
    program = invoke('''
        const candidates = await tools.invoke('candidates', {});
        if (candidates.length > 1) {
            tools.observe('select the matching document', candidates);
        }
        tools.call('must_not_write', {});
        return candidates;
    ''', [_tool('candidates'), _tool('must_not_write')], {
        'candidates': lambda args: ('done', [{'id': 'a'}, {'id': 'b'}]),
        'must_not_write': lambda args: pytest.fail('effect after observation'),
    })
    assert dispatched == [('candidates', {})]
    assert program['result']['kind'] == 'observation'
    assert program['stats']['outcome'] == 'needs_model'
    parent = next(row for row in task['toolRounds'] if row.get('_programSynthetic'))
    assert parent['programStats'] == program['stats']
    assert parent['programStatus'] == 'completed'


def test_program_records_actual_private_and_returned_utf8_bytes(local_gateway):
    _, dispatched, invoke = local_gateway
    rows = [{'id': str(i), 'score': i, 'body': '大量数据' * 50} for i in range(100)]
    program = invoke('''
        const rows = await tools.invoke('rows', {});
        return rows.filter(row => row.score > 97).map(row => row.id);
    ''', [_tool('rows')], {'rows': lambda args: ('done', rows)})
    assert dispatched == [('rows', {})]
    assert program['result'] == ['98', '99']
    stats = program['stats']
    assert stats['result_delivery']['outputBytes'] > 10_000
    assert stats['result_delivery']['truncatedResults'] == 0
    assert stats['result_bytes'] == len(json.dumps(
        program['result'], ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    assert stats['outcome'] == 'completed'


def test_failed_upstream_conditionally_skips_side_effect(local_gateway):
    _, dispatched, invoke = local_gateway
    writes = []
    program = invoke('''
        const upstream = await tools.call('lookup_document', {});
        if (upstream.status === 'done') {
            return tools.call('publish_document', {
                document_id: JSON.parse(upstream.output).id
            });
        }
        return {skipped: true, failure: upstream};
    ''', [
        _tool('lookup_document'),
        _tool('publish_document', {'document_id': {'type': 'string'}},
              ['document_id']),
    ], {
        'lookup_document': lambda args: ('error', {'reason': 'not found'}),
        'publish_document': lambda args: (writes.append(args) or
                                           ('done', {'published': True})),
    })

    assert dispatched == [('lookup_document', {})]
    assert writes == []
    assert program['result']['skipped'] is True
    failure = program['result']['failure']
    assert failure['status'] == 'error'
    assert 'output' not in failure
    assert json.loads(failure['error']) == {'reason': 'not found'}
    assert program['stats']['tool_calls'] == 1


def test_bounded_result_derived_fanout_then_aggregation(local_gateway):
    _, dispatched, invoke = local_gateway
    program = invoke('''
        const upstream = await tools.call('list_documents', {});
        const ids = JSON.parse(upstream.output).ids.slice(0, 2);
        const reads = [];
        for (const id of ids) reads.push(await tools.call('score_document', {id}));
        const total = reads.reduce((sum, row) =>
            sum + JSON.parse(row.output).score, 0);
        return tools.call('save_summary', {ids: ids, total: total});
    ''', [
        _tool('list_documents'),
        _tool('score_document', {'id': {'type': 'string'}}, ['id']),
        _tool('save_summary', {
            'ids': {'type': 'array', 'items': {'type': 'string'}},
            'total': {'type': 'integer'},
        }, ['ids', 'total']),
    ], {
        'list_documents': lambda args: ('done', {'ids': ['a', 'b', 'excluded']}),
        'score_document': lambda args: (
            'done', {'score': {'a': 7, 'b': 11}[args['id']]}),
        'save_summary': lambda args: ('done', {'saved': args}),
    })

    # Serial bounded fan-out intentionally pins dependency order, not thread
    # scheduling. The third upstream candidate never reaches a leaf handler.
    assert dispatched == [
        ('list_documents', {}), ('score_document', {'id': 'a'}),
        ('score_document', {'id': 'b'}),
        ('save_summary', {'ids': ['a', 'b'], 'total': 18}),
    ]
    assert json.loads(program['result']['output']) == {
        'saved': {'ids': ['a', 'b'], 'total': 18}}
    assert program['stats']['tool_calls'] == 4


@pytest.mark.parametrize(('target', 'arguments', 'error_code'), [
    ('publish_document', '{wrong: data.id}', 'missing_required_arguments'),
    ('disabled_remote_action', '{document_id: data.id}', 'tool_not_enabled'),
])
def test_derived_call_rejection_has_no_side_effect(
        local_gateway, target, arguments, error_code):
    task, dispatched, invoke = local_gateway
    side_effects = []
    # Even a wire-visible name does not grant authority outside the immutable
    # executable catalog. The invalid enabled call still needs schema checks.
    task['_tool_schema'] = [_tool('disabled_remote_action')]
    program = invoke('''
        const upstream = await tools.call('lookup_document', {});
        const data = JSON.parse(upstream.output);
        return tools.call('%s', %s);
    ''' % (target, arguments), [
        _tool('lookup_document'),
        _tool('publish_document', {'document_id': {'type': 'string'}},
              ['document_id']),
    ], {
        'lookup_document': lambda args: ('done', {'id': 'doc-guarded'}),
        target: lambda args: (side_effects.append(args) or ('done', {})),
    })

    assert dispatched == [('lookup_document', {})]
    assert side_effects == []
    assert program['result']['status'] == 'error'
    assert program['result']['error']['code'] == error_code
    assert program['stats']['tool_calls'] == 2


def test_object_catalog_search_limit_one_then_exact_known_tool(local_gateway):
    task, dispatched, invoke = local_gateway
    visible = _tool('grep_search', {'pattern': {'type': 'string'}}, ['pattern'],
                    'Search repository text')
    hidden = [
        _tool('mcp__docs__search', {'query': {'type': 'string'}}, ['query'],
              'Search document text'),
        _tool('mcp__archive__search', {'query': {'type': 'string'}}, ['query'],
              'Search archived text'),
    ]
    task['_tool_schema'] = [visible]
    task['_executableToolNamespaceByName'] = {
        'grep_search': 'project',
        'mcp__docs__search': 'mcp', 'mcp__archive__search': 'mcp',
    }
    program = invoke('''
        const found = catalog.search({query: 'search', limit: 1});
        const selected = found.items[0];
        const searched = await tools.call(selected.name, {query: 'release notes'});
        const local = await tools.call('grep_search', {
            pattern: JSON.parse(searched.output).needle
        });
        return {found: found, selected: selected.name, local: local};
    ''', [visible, *hidden], {
        'mcp__docs__search': lambda args: ('done', {'needle': 'release-v2'}),
        'mcp__archive__search': lambda args: ('done', {'needle': 'release-v2'}),
        'grep_search': lambda args: ('done', {'matches': ['CHANGELOG.md:12']}),
    })

    result = program['result']
    found = result['found']
    assert found['status'] == 'ok'
    assert found['query'] == 'search'
    assert len(found['items']) == 1
    assert found['total'] == 2
    assert found['next_cursor']
    assert found['execute_with'] == EXECUTE_TOOLS_NAME
    assert result['selected'] in {tool['function']['name'] for tool in hidden}
    assert found['items'][0]['arguments_schema']['required'] == ['query']
    assert dispatched == [
        (result['selected'], {'query': 'release notes'}),
        ('grep_search', {'pattern': 'release-v2'}),
    ]
    assert json.loads(result['local']['output']) == {
        'matches': ['CHANGELOG.md:12']}
    assert program['stats']['tool_calls'] == 2


def test_exact_incident_query_recovers_visible_tool(local_gateway):
    task, dispatched, invoke = local_gateway
    visible = _tool('grep_search', {'pattern': {'type': 'string'}}, ['pattern'])
    task['_tool_schema'] = [visible]
    program = invoke('''
        const ts = catalog.search({query:"grep_search",limit:1});
        return ts;
    ''', [visible, _tool('mcp__docs__search')], {})
    assert program['result']['query'] == 'grep_search'
    assert program['result']['items'] == []
    assert program['result']['already_visible'] == 'grep_search'
    assert program['stats']['tool_calls'] == 0
    assert dispatched == []


def test_result_derived_parallel_branches_join_in_one_program(local_gateway):
    _, dispatched, invoke = local_gateway
    program = invoke('''
        const upstream = await tools.call('list_documents', {});
        if (upstream.status != 'done' || upstream.output_truncated) return upstream;
        const ids = JSON.parse(upstream.output).ids.slice(0, 2);
        const rows = await tools.parallel(ids.map(id => ({
            name: 'score_document', arguments: {id: id}
        })));
        if (rows.filter(row => row.status != 'done' || row.output_truncated).length)
            return rows;
        return rows.reduce((sum, row) => sum + JSON.parse(row.output).score, 0);
    ''', [
        _tool('list_documents'),
        _tool('score_document', {'id': {'type': 'string'}}, ['id']),
    ], {
        'list_documents': lambda args: ('done', {'ids': ['a', 'b', 'excluded']}),
        'score_document': lambda args: ('done', {'score': {'a': 7, 'b': 11}[args['id']]}),
    })
    assert dispatched[0] == ('list_documents', {})
    assert sorted(args['id'] for name, args in dispatched[1:]) == ['a', 'b']
    assert program['result'] == 18
    assert program['stats']['tool_calls'] == 3


@pytest.mark.parametrize('value', [0, 1.5, False, True, None, [], [1, 'two'], ''])
def test_json_result_types_settle_without_losing_value(local_gateway, value):
    _, dispatched, invoke = local_gateway
    program = invoke('return ' + json.dumps(value) + ';', [], {})
    assert program['result'] == value
    assert type(program['result']) is type(value)
    assert dispatched == []


def test_standard_javascript_joins_actual_parallel_child_execution(local_gateway):
    task, dispatched, invoke = local_gateway
    barrier = threading.Barrier(2)
    def score(args):
        barrier.wait(timeout=5)
        return 'done', {'score': {'a':7, 'b':11}[args['id']]}
    program = invoke('''
      const {ids} = await tools.invoke('list_documents', {});
      const scores = await Promise.all(ids.map(id => tools.invoke('score_document', {id})));
      class Summary { constructor(total) { this.total = total; } }
      try { JSON.parse('bad'); } catch ({name}) {
        if (name !== 'SyntaxError') throw new Error('unexpected');
      }
      return new Summary(scores.reduce((sum, {score}) => sum + score, 0));
    ''', [_tool('list_documents'), _tool('score_document',
        {'id': {'type':'string'}}, ['id'])], {
        'list_documents': lambda args: ('done', {'ids':['a','b']}),
        'score_document': score,
    })
    assert program['status'] == 'ok', program
    assert program['result'] == {'total':18}
    assert dispatched[0] == ('list_documents', {})
    assert sorted(args['id'] for name,args in dispatched[1:]) == ['a','b']
    stats = program['stats']
    assert stats['engine'] == 'quickjs-wasm'
    assert (stats['tool_calls'],stats['call_stages'],stats['parallel_stages']) == (3,2,1)
    parent = next(row for row in task['toolRounds'] if row.get('_programSynthetic'))
    assert parent['programBackend'] == 'local_javascript'
    assert parent['programStats'] == stats
    assert parent['programLimits']['maxWasmFuel'] >= stats['wasm_fuel']
    assert parent['programLimits']['maxLinearMemoryBytes'] >= stats['wasm_memory_bytes']
    assert 'maxAstNodes' not in parent['programLimits']


def test_observation_unwinds_guest_recovery_through_real_gateway(local_gateway):
    _, dispatched, invoke = local_gateway
    program = invoke('''
      const candidates=await tools.invoke('candidates', {});
      try { tools.observe('choose the matching clause', candidates); }
      catch(e) { await tools.invoke('must_not_write', {}); }
      finally { await tools.invoke('must_not_write', {}); }
      return 'incorrect';
    ''', [_tool('candidates'), _tool('must_not_write')], {
        'candidates': lambda args: ('done', [{'id':'alpha'},{'id':'beta'}]),
        'must_not_write': lambda args: pytest.fail('effect after observation'),
    })
    assert dispatched == [('candidates', {})]
    assert program['result'] == {'kind':'observation',
        'reason':'choose the matching clause','data':[{'id':'alpha'},{'id':'beta'}],
        'executionReceipt':{'attemptedToolCalls':1,'successfulToolCalls':1,'retainedReads':0}}
    assert program['stats']['outcome'] == 'needs_model'


def test_exact_large_integer_dependency_crosses_real_schema_and_receipt_pipeline(local_gateway):
    task, dispatched, invoke = local_gateway
    identifier = 9223372036854775807
    program = invoke(
        'const {id}=await tools.invoke("read_id", {}); '
        'const row=await tools.invoke("fetch_id", {id}); '
        'return {id:String(id), matched:row.matched};',
        [_tool('read_id'), _tool('fetch_id', {'id': {'type': 'integer'}}, ['id'])],
        {'read_id': lambda args: ('done', {'id': identifier}),
         'fetch_id': lambda args: ('done', {'matched': args['id'] == identifier})})
    assert dispatched == [('read_id', {}), ('fetch_id', {'id': identifier})]
    assert program['status'] == 'ok'
    assert program['result'] == {'id': str(identifier), 'matched': True}
    assert program['stats']['tool_calls'] == 2
    child_results = program['childResults']
    assert len(child_results) == 2
    assert [row['name'] for row in child_results] == ['read_id', 'fetch_id']
    assert task['programRuns'][-1]['status'] == 'completed'


def test_program_failure_projects_typed_recovery_instead_of_nested_business_json(local_gateway):
    task, dispatched, _ = local_gateway
    from lib.tools.gateway import EXECUTE_PROGRAM_NAME
    from lib.tools.result_envelope import split_tool_result_delivery, tool_result_error
    tc = {'id':'bad-input', 'type':'function', 'function':{'name':EXECUTE_PROGRAM_NAME}}
    _, content, aborted = handler.handle_execute_program(task, tc, EXECUTE_PROGRAM_NAME,
        tc['id'], {'code':'return tools.result({ref:"tool-result:' + 'a' * 64 + '"});'},
        1, {'calls':[], 'llmRound':1}, {'tools': {'programmaticCalling': 'on'}}, None, False)
    error = tool_result_error(content)
    assert error.code == 'type_error' and 'tools.inputs' in error.next_action
    value = json.loads(split_tool_result_delivery(content).model_text)
    assert value['code'] == 'type_error' and value['status'] == 'error'
    assert 'Tool returned a structured result.' not in json.dumps(value)
    assert not aborted and not dispatched and content.canonical.execution_status == 'error'
