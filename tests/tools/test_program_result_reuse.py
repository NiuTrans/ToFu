"""Owned canonical reuse never reexecutes tools or consumes model previews."""
import json

import pytest

from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.program_results import list_program_inputs, retain_program_result, read_program_result
from lib.tools.programmatic import ProgrammaticResultBudget
from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit


def test_parallel_retention_stays_bounded_and_terminal_cannot_reopen_it(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from lib.tools import program_results
    task = {'id': 'parallel-retention', 'status': 'running', '_ptc_local': {'tier': 'program'}}
    cleanups = []
    monkeypatch.setattr(program_results, 'tool_result_cache_capacity', lambda: 16)
    monkeypatch.setattr('lib.agent_core.admission.on_terminal', lambda identity, callback: cleanups.append(callback))
    barrier = threading.Barrier(8)
    def retain(worker):
        barrier.wait(5)
        for i in range(64):
            program_results._admit(task, f'{worker}:{i}', {'retainedBytes': 64000})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(retain, range(8)))
    assert len(task['_programResultRefs']) == 16
    assert len(cleanups) == 1
    task['status'] = 'done'
    cleanups[0](task['id'])
    assert not program_results._admit(task, 'late', {'retainedBytes': 1})
    assert '_programResultRefs' not in task


@pytest.fixture
def world(monkeypatch):
    reads, writes = [], []
    def put(self, **kwargs):
        writes.append(kwargs)
        pytest.fail('reuse must not write an extra artifact')
    def read(self, **kwargs):
        reads.append(kwargs)
        pytest.fail('reuse must not query storage or a model preview')
    monkeypatch.setattr(ToolResultArtifactRepository,'put',put)
    monkeypatch.setattr(ToolResultArtifactRepository,'read_range',read)
    task = {'_ptc_local':{'tier':'program'}}
    def retain(value, arguments=None, **overrides):
        canonical = capture_result(json.dumps(value), **overrides)
        retain_program_result(task,'read_rows',arguments or {},'owned-source',canonical,user_id=7)
        return canonical
    def run(code, *, owner=7, target=None, budget=None, catalog=None):
        current = task if target is None else target
        delivery = budget or ProgrammaticResultBudget()
        return execute_javascript_program(code,search=lambda *a:[],
            call=lambda *a:pytest.fail('reuse reexecuted a source tool'),
            call_many=lambda *a:pytest.fail('reuse dispatched a source batch'),
            result_reader=lambda name,args:read_program_result(current,name,args,
                user_id=owner,budget=delivery,catalog_names={'read_rows'} if catalog is None else catalog),
            input_lister=lambda query:list_program_inputs(current,query,user_id=owner,
                catalog_names={'read_rows'} if catalog is None else catalog))
    return task,reads,writes,retain,run


def test_full_unicode_and_bigint_source_reuses_canonical_bytes(world):
    _, reads, _, retain, run = world
    rows = [{'id':9223372036854775807,'note':'中🙂'*1000} for _ in range(20)]
    retain(rows)
    result, stats = run('const rows=tools.result("read_rows",{}); return {count:rows.length,id:String(rows[0].id)};')
    assert result == {'count':20,'id':'9223372036854775807'}
    assert stats['tool_calls'] == 0 and stats['call_stages'] == 0
    assert stats['result_reads'] == stats['result_read_attempts'] == 1
    assert not reads


def test_directory_discovers_exact_bindings_and_reduces_privately(world):
    _, reads, writes, retain, run = world
    retain([{'amount': 12, 'note': 'private body'}], {'part': 'a'})
    retain([{'amount': 33, 'note': 'private body'}], {'part': 'b'})
    directory, stats = run('return tools.inputs({name:"read_rows"});')
    assert directory['total'] == 2 and directory['nextOffset'] is None
    assert [row['args'] for row in directory['inputs']] == [{'part':'a'}, {'part':'b'}]
    assert 'private body' not in json.dumps(directory)
    assert stats['input_queries'] == 1 and stats['result_reads'] == stats['tool_calls'] == 0
    value, stats = run('let total=0; for(const x of tools.inputs().inputs) '
                       'for(const row of tools.result(x.name,x.args)) total+=row.amount; '
                       'return {total};')
    assert value == {'total':45}
    assert stats['input_queries'] == 1 and stats['result_reads'] == 2
    assert stats['tool_calls'] == 0 and not reads and not writes


def test_directory_argument_snapshots_cannot_be_mutated_by_caller_or_guest(world):
    _, _, _, retain, run = world
    arguments = {'part':'a', 'id':9223372036854775807}
    retain([9], arguments)
    arguments['part'] = 'changed'
    value, _ = run('const x=tools.inputs().inputs[0]; x.args.part="guest-change"; '
                   'return String(x.args.id);')
    assert value == '9223372036854775807'
    value, _ = run('const x=tools.inputs().inputs[0]; return tools.result(x.name,x.args)[0];')
    assert value == 9
    encoded = json.dumps(world[0]['_programResultRefs'])
    assert '9223372036854775807' not in encoded and 'binding_json' not in encoded


@pytest.mark.parametrize('case',['owner','task','catalog','terminal','tier','serialized'])
def test_directory_does_not_advertise_inaccessible_or_replayed_inputs(world, case):
    task, reads, writes, retain, run = world
    retain([1], {'part':'a'})
    kwargs = {'owner':8} if case=='owner' else {'target':{}} if case=='task' else {'catalog':set()} if case=='catalog' else {}
    if case=='terminal':task['status']='done'
    if case=='tier':task['_ptc_local']['tier']='batch'
    if case=='serialized':task['_programResultRefs']=json.loads(json.dumps(task['_programResultRefs']))
    value, stats = run('return tools.inputs();', **kwargs)
    assert value == {'inputs':[], 'total':0, 'nextOffset':None}
    assert stats['tool_calls'] == stats['result_reads'] == 0 and not reads and not writes


def test_directory_paginates_current_working_set_and_releases_terminally(world, monkeypatch):
    task, _, _, retain, run = world
    monkeypatch.setattr('lib.tools.program_results.tool_result_cache_capacity',lambda:64)
    for i in range(17):retain([i], {'part':i})
    first, _ = run('return tools.inputs();')
    assert len(first['inputs']) == 16 and first['total'] == 17 and first['nextOffset'] == 16
    second, _ = run('return tools.inputs({offset:16});')
    assert second['inputs'][0]['args'] == {'part':16} and second['nextOffset'] is None
    task['_programResultRefs'].clear()
    assert run('return tools.inputs();')[0]['total'] == 0


def test_directory_queries_do_not_consume_retained_read_quota(world):
    _, _, _, retain, run = world
    retain([3])
    value, stats = run('for(let i=0;i<16;i++) tools.inputs(); '
                       'let total=0; for(let i=0;i<16;i++) total+=tools.result("read_rows",{})[0]; return total;')
    assert value == 48 and stats['input_queries'] == stats['result_reads'] == 16
    with pytest.raises(ProgramExecutionError, match='Program input query limit') as error:
        run('for(let i=0;i<17;i++) tools.inputs(); return 1;')
    assert error.value.code == 'input_query_limit'


def test_cancelled_directory_cannot_be_caught_to_execute_more_tools(world):
    task, _, _, retain, run = world
    retain([3]);task['aborted']=True
    with pytest.raises(ProgramExecutionError) as error:
        run('try {tools.inputs();} catch(e) {await tools.invoke("write_file",{});} return 1;')
    assert error.value.code == 'cancelled'


def test_public_runtime_discovers_and_reuses_private_program_inputs(monkeypatch):
    import asyncio
    import copy
    import re
    import socket
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    from lib.llm._sse_core import prepare_request
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    import lib.llm

    requests = []
    prompt_texts = []
    codes = [
        'const r=await tools.invoke("custom__read_rows",{dataset:"part-a"}); '
        'return {keys:Object.keys(r[0])};',
        'const x=tools.inputs({name:"custom__read_rows"}).inputs[0]; '
        'const r=tools.result(x.name,x.args); return {total:r.reduce((s,x)=>s+x.amount,0)};',
    ]
    def stream(body, **kwargs):
        plan = prepare_request(body, api_key='synthetic-test-key',
                               base_url='https://gateway.example/v1', api_protocol='openai')
        requests.append(copy.deepcopy(plan.body['tools']))
        assert [row['function']['name'] for row in requests[-1]] == ['execute_program']
        delivered = [m for m in body['messages'] if m.get('role') == 'tool']
        assert 'PRIVATE_SOURCE_MARKER' not in json.dumps(delivered)
        text = '\n'.join(block.get('text', '')
                         for message in body['messages']
                         for block in (message.get('content', [])
                             if isinstance(message.get('content'), list)
                             else [{'text': message.get('content') or ''}])
                         if isinstance(block, dict))
        prompt_texts.append(text)
        if len(requests) == 2:
            expression = re.search(r'tools\.result\(\{ref:"input_[0-9a-f]{32}"\}\)', text)
            assert expression, 'live directory must supply an executable capture selector'
            codes[1] = 'const r=' + expression.group(0) + '; return {total:r.reduce((s,x)=>s+x.amount,0)};'
        if len(requests) <= 2:
            call = {'id':'program-'+str(len(requests)), 'type':'function',
                    'function':{'name':'execute_program',
                                'arguments':json.dumps({'code':codes[len(requests)-1]})}}
            kwargs['on_tool_call_ready'](call)
            message = {'role':'assistant', 'content':'', 'tool_calls':[call]}
            finish = 'tool_calls'
        else:
            assert len(requests) == 3 and json.loads(delivered[-1]['content']) == {'total':45}
            kwargs['on_content']('{"total":45}')
            message, finish = {'role':'assistant', 'content':'{"total":45}'}, 'stop'
        return ProviderStreamResult(message=message, compatibility_finish_reason=finish,
            provider_finish_reason=finish, state=ProviderStreamState.PROVIDER_FINISHED,
            saw_done=True, saw_finish_reason=True, usage={'prompt_tokens':10,'completion_tokens':5})
    monkeypatch.setattr(lib.llm, 'stream_chat', stream)

    async def run(runtime):
        execution = runtime.start([{'role':'user','content':'sum row amounts'}],
            config={'disableModelFallback':True, 'tools':{'programmaticCalling':'on',
                    'programmaticExposure':'code_api', 'toolSearch':'off'},
                    'context':{'programmaticInputs':True},
                    'orchestration':{'multiAgent':'off'}, 'max_tokens':4096},
            custom_tools=[{'type':'function','function':{'name':'custom__read_rows',
                'description':'Read a dataset as JSON rows.', 'parameters':{'type':'object',
                    'properties':{'dataset':{'type':'string'}}, 'required':['dataset'],
                    'additionalProperties':False}}, 'execution':{'mode':'client'}, 'write':False}],
            custom_tools_mode='exclusive', timeout_s=30)
        leaves = 0
        async for event in execution.events_async(timeout_s=30):
            if event['type'] == 'custom_tool_call':
                leaves += 1
                assert execution.resolve_custom_tool_call(event['callId'], json.dumps([
                    {'amount':12,'note':'PRIVATE_SOURCE_MARKER'},
                    {'amount':33,'note':'PRIVATE_SOURCE_MARKER'}]))
        assert (await execution.result_async(timeout_s=2)).content == '{"total":45}'
        assert leaves == 1
        evidence = execution.evidence_snapshot()['programRuns']
        assert evidence[0]['stats']['successful_tool_calls'] == 1
        assert evidence[1]['stats']['tool_calls'] == 0
        assert evidence[1]['stats']['input_queries'] == 0
        assert evidence[1]['stats']['result_reads'] == 1

    with socket.create_server(('127.0.0.1',0)) as endpoint:
        access = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=access, owner_user_id=7, max_inflight=1) as runtime:
            asyncio.run(run(runtime))
    assert requests[0] == requests[1] == requests[2]
    assert all('PRIVATE_SOURCE_MARKER' not in text for text in prompt_texts)
    assert 'Private inputs retained this turn:' not in prompt_texts[0]
    for text in prompt_texts[1:]:
        assert 'Private inputs retained this turn:' in text
        assert re.search(r'tools\.result\(\{ref:"input_[0-9a-f]{32}"\}\)', text)


@pytest.mark.parametrize('case',['owner','task','arguments','catalog','budget'])
def test_unavailable_reuse_hands_back_without_storage_or_source_execution(world, case):
    _, reads, _, retain, run = world
    retain([1,2,3])
    kwargs = {'owner':8} if case=='owner' else {'target':{}} if case=='task' else {'catalog':set()} if case=='catalog' else {'budget':ProgrammaticResultBudget(1)} if case=='budget' else {}
    args = '{different:1}' if case=='arguments' else '{}'
    value, stats = run('const rows=tools.result("read_rows",'+args+'); await tools.write_file({}); return rows;', **kwargs)
    assert value['kind'] == 'observation'
    assert value['data']['error']['code'] == 'retained_result_unavailable'
    error = value['data']['error']
    assert error['retryable'] is False
    assert 'Do not re-execute the source tool' in error['next_action']
    assert ('smaller groups' if case == 'budget' else 'tools.inputs') in error['next_action']
    assert stats['outcome'] == 'needs_model'
    assert stats['result_reads'] == 0 and stats['tool_calls'] == 0
    assert not reads


def test_latest_exact_arguments_and_identical_source_bindings(world):
    _, _, _, retain, run = world
    retain([3], {'part':'a'})
    retain([3], {'part':'b'})
    value, stats = run('return tools.result("read_rows",{part:"a"})[0]+tools.result("read_rows",{part:"b"})[0];')
    assert value == 6 and stats['result_reads'] == 2
    retain([9], {'part':'a'})
    assert run('return tools.result("read_rows",{part:"a"})[0];')[0] == 9


def test_partial_and_small_scalar_results_are_not_promoted(world):
    task, _, writes, retain, _ = world
    retain([1], source_coverage='partial')
    retain([1], execution_status='rejected')
    retain('ready')
    retain(1)
    retain(None)
    retain(False)
    assert not writes and not task.get('_programResultRefs')


@pytest.mark.parametrize('value', [{}, {'datasetId': 'opaque-batch', 'pageSize': 16},
                                  {'cursor': None}, {'items': [], 'nextOffset': 0}])
def test_small_structured_originals_reuse_exact_binding_without_reexecution(world, value):
    _, reads, writes, retain, run = world
    canonical = retain(value, {'batch': 'current'})
    actual, stats = run('return tools.result("read_rows",{batch:"current"});')
    assert actual == value and stats['result_reads'] == 1 and stats['tool_calls'] == 0
    directory, _ = run('return tools.inputs();')
    assert directory['inputs'][0]['jsonType'] == 'object'
    assert directory['inputs'][0]['bytes'] == len(canonical.text().encode())
    assert not reads and not writes


@pytest.mark.parametrize('source', ['{"cursor":NaN}', '[Infinity]', '{bad}'])
def test_small_non_json_sources_are_not_admitted_as_structured_snapshots(world, source):
    from lib.tools.result_value import CapturedToolResult, ResultPart
    task = world[0]
    canonical = CapturedToolResult(parts=(ResultPart('json', source),))
    assert retain_program_result(task, 'read_rows', {}, 'source', canonical, user_id=7) is None
    assert not task.get('_programResultRefs')


def test_released_source_never_falls_back_to_reexecution(world, monkeypatch):
    _, _, _, retain, run = world
    retain([1,2])
    task = world[0]
    task['_programResultRefs'].clear()
    value, stats = run('return tools.result("read_rows",{});')
    assert value['kind'] == 'observation'
    assert stats['result_reads'] == 0 and stats['tool_calls'] == 0


def test_retained_reads_have_their_own_finite_operation_limit(world):
    _, reads, _, retain, run = world
    retain([1])
    with pytest.raises(ProgramExecutionError, match='Retained result read limit') as error:
        run('for(let i=0;i<17;i++) tools.result("read_rows",{}); return 1;')
    assert error.value.code == 'result_read_limit'
    assert not reads


def test_snapshot_index_uses_existing_receipt_capacity(world, monkeypatch):
    task, _, _, retain, run = world
    monkeypatch.setattr('lib.tools.program_results.tool_result_cache_capacity',lambda:16)
    for i in range(20):retain([i],{'part':i})
    assert len(task['_programResultRefs']) == 16
    assert run('return tools.result("read_rows",{part:19})[0];')[0] == 19
    value, _ = run('return tools.result("read_rows",{part:0});')
    assert value['kind'] == 'observation'


def test_storage_free_reuse_retains_only_runtime_canonical_descriptor(world, monkeypatch):
    task, reads, writes, retain, run = world
    task['config'] = {'_storageFreeRuntime':True}
    retain([{'amount':9}])
    assert not writes
    value, stats = run('return tools.result("read_rows",{})[0].amount;')
    assert value == 9 and stats['result_reads'] == 1
    assert not reads
    encoded = json.dumps(task['_programResultRefs'])
    assert 'amount' not in encoded and 'canonical' not in encoded
    # JSON replay is metadata, never a trusted input body.
    task['_programResultRefs'] = json.loads(encoded)
    value, stats = run('return tools.result("read_rows",{});')
    assert value['reason'] == 'retained_result_unavailable'
    assert stats['result_reads'] == 0


def test_transient_working_set_and_terminal_release(world, monkeypatch):
    from lib.agent_core.admission import fire_terminal_callbacks
    task, _, writes, retain, run = world
    task.update(id='owned-reuse-lifecycle', config={'_storageFreeRuntime':True})
    monkeypatch.setattr('lib.tools.program_results.tool_result_cache_capacity',lambda:16)
    for i in range(12):
        retain(['x'*150000],{'part':i})
    index = task['_programResultRefs']
    assert len(index) == 6
    assert sum(row['bytes'] for row in index.values()) == 900024
    assert sum(row['retainedBytes'] for row in index.values()) <= 16*64*1024
    assert not writes
    assert run('return tools.result("read_rows",{part:11})[0].length;')[0] == 150000
    assert run('return tools.result("read_rows",{part:0});')[0]['kind'] == 'observation'
    fire_terminal_callbacks(task['id'])
    assert '_programResultRefs' not in task and '_programResultCleanupBound' not in task
    assert run('return tools.result("read_rows",{part:11});')[0]['kind'] == 'observation'


@pytest.mark.parametrize('arguments',['null','[]','"text"'])
def test_reuse_requires_an_exact_json_argument_object(world, arguments):
    _,reads,writes,_,run = world
    with pytest.raises(ProgramExecutionError) as error:
        run('return tools.result("read_rows",'+arguments+');')
    assert error.value.code == 'type_error'
    assert not reads and not writes


def test_cancellation_rejects_late_capture_and_stops_guest_effects(world):
    task,reads,writes,retain,run = world
    task['aborted'] = True
    retain([1])
    assert not task.get('_programResultRefs')
    with pytest.raises(ProgramExecutionError) as error:
        run('try{tools.result("read_rows",{})}catch(e){} await tools.write_file({}); return 1;')
    assert error.value.code == 'cancelled'
    assert not reads and not writes


_EXECUTED_READ_DOC = {'doc': 'd-1', 'max_chars': 120000, 'include_tables': 'preview'}


def _defaulted_read_doc_task(**fields):
    """A task whose gateway fills a schema default before execution."""
    from lib.tools.contracts import compile_execution_contract_documents
    catalog = [{'type': 'function', 'function': {
        'name': 'mcp__docs__read_doc', 'description': 'Read one document.',
        'parameters': {'type': 'object', 'properties': {
            'doc': {'type': 'string'}, 'max_chars': {'type': 'integer'},
            'include_tables': {'type': 'string', 'enum': ['none', 'preview', 'full'],
                               'default': 'preview'}},
            'required': ['doc'], 'additionalProperties': False}}}]
    return {'_ptc_local': {'tier': 'program'}, '_executable_tool_catalog': catalog,
            '_executableToolNamespaceByName': {'mcp__docs__read_doc': 'docs'},
            '_toolContractDocumentsByName': compile_execution_contract_documents(
                catalog, namespace='docs'),
            **fields}


def _document_capture(doc='d-1'):
    return capture_result(json.dumps({'doc': doc, 'markdown': '中' * 3000}))


def _lookup(task, name, arguments):
    return read_program_result(task, name, arguments, user_id=7,
                               budget=ProgrammaticResultBudget(),
                               catalog_names={'mcp__docs__read_doc'})


@pytest.mark.parametrize('name, arguments', [
    ('mcp__docs__read_doc', {'doc': 'd-1', 'max_chars': 120000}),
    ('mcp__docs__read_doc', {'max_chars': 120000, 'include_tables': 'preview', 'doc': 'd-1'}),
    ('mcp__docs__read_doc', {'doc': 'd-1', 'max_chars': '120000', 'include_tables': 'PREVIEW'}),
    ('read_doc', {'doc': 'd-1', 'max_chars': 120000}),
])
def test_equivalent_call_spellings_share_the_executed_binding(name, arguments):
    task = _defaulted_read_doc_task()
    canonical = _document_capture()
    assert retain_program_result(task, 'mcp__docs__read_doc', dict(_EXECUTED_READ_DOC),
                                 'executed', canonical, user_id=7) is not None
    receipt = _lookup(task, name, arguments)
    assert receipt['status'] == 'done' and receipt['call_id'] == 'executed'
    assert receipt['name'] == 'mcp__docs__read_doc'
    assert receipt.canonical is canonical


@pytest.mark.parametrize('name, arguments', [
    ('mcp__docs__read_doc', {'doc': 'd-2', 'max_chars': 120000}),
    ('mcp__docs__read_doc', {'doc': 'd-1', 'max_chars': 120000, 'include_tables': 'full'}),
    ('mcp__docs__read_doc', {'doc': 'd-1'}),
    ('mcp__docs__read_doc', {'doc': 'd-1', 'max_chars': 120000, 'unknown': True}),
    ('mcp__docs__write_doc', {'doc': 'd-1', 'max_chars': 120000}),
])
def test_a_different_real_call_never_reuses_the_binding(name, arguments):
    task = _defaulted_read_doc_task()
    retain_program_result(task, 'mcp__docs__read_doc', dict(_EXECUTED_READ_DOC),
                          'executed', _document_capture(), user_id=7)
    receipt = _lookup(task, name, arguments)
    assert receipt['status'] == 'error'
    assert receipt['error']['code'] == 'retained_result_unavailable'
    assert receipt['error']['reason'] == 'not_retained_in_this_turn'


def test_retention_binds_and_advertises_the_executed_form():
    task = _defaulted_read_doc_task()
    retain_program_result(task, 'mcp__docs__read_doc', {'max_chars': 120000, 'doc': 'd-1'},
                          'direct', _document_capture(), user_id=7)
    listed = list_program_inputs(task, {}, user_id=7, catalog_names={'mcp__docs__read_doc'})
    assert [(row['name'], row['args']) for row in listed['inputs']] == [
        ('mcp__docs__read_doc', _EXECUTED_READ_DOC)]
    assert _lookup(task, 'mcp__docs__read_doc', dict(_EXECUTED_READ_DOC))['status'] == 'done'
    # Retaining the same executed call again keeps the one advertised binding.
    retain_program_result(task, 'mcp__docs__read_doc', dict(_EXECUTED_READ_DOC),
                          'again', _document_capture(), user_id=7)
    assert len(task['_programResultRefs']) == 1


def test_dispatch_retention_and_guest_lookup_share_one_call_binding(monkeypatch):
    """The real gateway child path retains; the model's own spelling reuses it."""
    import threading
    from lib.agent_core.admission import fire_terminal_callbacks
    from lib.tasks_pkg.handlers import tool_gateway
    from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline
    from lib.tools.gateway import EXECUTE_PROGRAM_NAME

    executed = []

    def execute_leaf(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                     project_path, project_enabled, all_tools=None):
        executed.append((fn_name, dict(fn_args)))
        return tc_id, json.dumps({'doc': fn_args['doc'], 'markdown': '中' * 3000}), False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', execute_leaf)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', execute_leaf)
    task = _defaulted_read_doc_task(
        id='dispatch-retained-binding', _userId=7, model='unit-model',
        status='running', aborted=False, events=[], events_lock=threading.Lock(),
        toolRounds=[], _attended=False, config={'tools': {'resultEnvelope': 'v2'}})

    def run(code):
        call_id = 'program-' + str(len(task.get('programRuns') or []))
        tool_gateway.handle_execute_program(
            task, {'id': call_id, 'type': 'function',
                   'function': {'name': EXECUTE_PROGRAM_NAME}},
            EXECUTE_PROGRAM_NAME, call_id, {'code': code}, 1,
            {'calls': [], 'llmRound': 1},
            {'autoApply': True, 'tools': {'programmaticCalling': 'on'}}, None, False)
        return task['programRuns'][-1]

    try:
        first = run('const r=await tools.invoke("mcp__docs__read_doc",'
                    '{doc:"d-1",max_chars:120000}); return r.markdown.length;')
        assert first['status'] == 'completed' and first['result'] == 3000
        assert executed == [('mcp__docs__read_doc', _EXECUTED_READ_DOC)]

        reused = run('const a=tools.result("mcp__docs__read_doc",{doc:"d-1",max_chars:120000}); '
                     'const b=tools.result("mcp__docs__read_doc",'
                     '{include_tables:"preview",max_chars:120000,doc:"d-1"}); '
                     'return [a.doc,b.doc];')
        assert reused['result'] == ['d-1', 'd-1']
        assert reused['stats']['result_reads'] == 2 and reused['stats']['tool_calls'] == 0

        missed = run('return tools.result("mcp__docs__read_doc",{doc:"d-2",max_chars:120000});')
        assert missed['result']['kind'] == 'observation'
        error = missed['result']['data']['error']
        assert (error['code'], error['reason']) == (
            'retained_result_unavailable', 'not_retained_in_this_turn')
        assert len(executed) == 1
    finally:
        fire_terminal_callbacks(task['id'])
