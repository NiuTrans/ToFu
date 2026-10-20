"""Full JavaScript, effect scheduling, provenance and per-invocation isolation."""
from concurrent.futures import ThreadPoolExecutor
import gc
import json
import threading
import weakref

import pytest

from lib.tools.javascript import JavaScriptLimits, execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.result_value import CanonicalReceipt

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('character,count', [('x', 1048577), ('大', 400000)])
def test_large_valid_return_is_complete(character, count):
    world = World()
    result, stats = world.run('return ' + json.dumps(character) + '.repeat(' + str(count) + ');')
    assert result == character * count
    assert stats['outcome'] == 'completed' and world.waves == []


def receipt(value, **fields):
    return CanonicalReceipt({'status': 'done', 'output': json.dumps(value),
                             'output_truncated': False, **fields}, canonical={'owned': True})


class World:
    def __init__(self):
        self.waves = []
        self.receipts = []

    def call(self, name, arguments, call_id=None):
        self.waves.append(([name], 'single'))
        return self.read(name, arguments)

    def read(self, name, arguments):
        result = receipt(arguments.get('value', {'name': name}))
        self.receipts.append(result)
        return result

    def batch(self, calls, execution):
        self.waves.append(([x['name'] for x in calls], execution))
        return [self.read(x['name'], x.get('arguments', {})) for x in calls]

    def run(self, code, **kwargs):
        return execute_javascript_program(code, search=lambda *args: list(args),
                                          call=self.call, call_many=self.batch, **kwargs)


@pytest.mark.parametrize('query', ['null','[]','"rows"','{extra:true}',
                                 '{offset:-1}','{offset:true}','{limit:0}',
                                 '{limit:17}','{limit:1.5}','{name:""}'])
def test_private_input_directory_validates_before_callback(query):
    received = []
    with pytest.raises(ProgramExecutionError) as error:
        World().run('return tools.inputs('+query+');', input_lister=lambda q:received.append(q))
    assert error.value.code == 'type_error' and not received


def test_private_input_directory_is_optional_and_named_tool_uses_invoke():
    world = World()
    value, stats = world.run('const d=tools.inputs(); const r=await tools.invoke("inputs",{value:3}); '
                             'return {directory:d,value:r};')
    assert value == {'directory':{'inputs':[], 'total':0, 'nextOffset':None}, 'value':3}
    assert stats['input_queries'] == 1 and stats['tool_calls'] == 1


def test_private_input_queries_remain_separate_from_handoff_execution_receipts():
    value, stats = World().run('tools.inputs(); tools.observe("choose",{keys:["amount"]});')
    assert value == {'kind':'observation', 'reason':'choose', 'data':{'keys':['amount']},
                     'executionReceipt':{'attemptedToolCalls':0,'successfulToolCalls':0,'retainedReads':0}}
    assert stats['input_queries'] == 1 and stats['tool_calls'] == stats['result_reads'] == 0


@pytest.mark.parametrize('query', ['"read"', '{query:"read",limit:1}'])
def test_catalog_query_limit_refuses_seventeenth_callback(query):
    queries = []
    with pytest.raises(ProgramExecutionError) as error:
        execute_javascript_program(
            f'for(let i=0;i<17;i++) catalog.search({query}); return 1;',
            search=lambda *args: queries.append(args) or [],
            call=lambda *a:pytest.fail('unexpected effect'), call_many=lambda *a:[])
    assert error.value.code == 'catalog_query_limit'
    assert error.value.detail['limit'] == len(queries) == 16
    stats = error.value.execution_stats
    assert stats['catalog_queries'] == 16 and stats['input_queries'] == 0
    assert stats['outcome'] == 'error' and stats['tool_calls'] == 0


def test_catalog_and_private_input_query_budgets_are_independent():
    queries, inputs = [], []
    value, stats = execute_javascript_program(
        'for(let i=0;i<16;i++){catalog.search("read");tools.inputs();} return 7;',
        search=lambda *args:queries.append(args) or [],
        input_lister=lambda query:inputs.append(query) or {
            'inputs': [], 'total': 0, 'nextOffset': None},
        call=lambda *a:pytest.fail('unexpected effect'), call_many=lambda *a:[])
    assert value == 7 and len(queries) == len(inputs) == 16
    assert stats['catalog_queries'] == stats['input_queries'] == 16
    assert stats['tool_calls'] == stats['result_reads'] == 0
    _, fresh_stats = World().run('return catalog.search("read");')
    assert fresh_stats['catalog_queries'] == 1 and fresh_stats['input_queries'] == 0


def test_invalid_catalog_query_does_not_count_a_callback():
    with pytest.raises(ProgramExecutionError) as error:
        World().run('catalog.search({query:""}); return 1;')
    assert error.value.code == 'invalid_search_arguments'
    assert error.value.execution_stats['catalog_queries'] == 0


@pytest.mark.parametrize('code', [
    'Promise.reject(new Error("detached failure")); return {ok:true};',

    'Promise.reject("detached failure"); return {ok:true};',
    'Promise.resolve().then(()=>{throw new Error("detached failure")}); return {ok:true};',
    'Promise.reject(new Error("detached failure")).then(()=>1); return {ok:true};',
    'return {get value(){Promise.reject(new Error("detached failure")); return 1;}};',
    'return {get value(){Promise.resolve().then(()=>{throw new Error("detached failure")}); return 1;}};',
])
def test_unhandled_promise_rejection_never_claims_completion(code):
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run(code)
    assert error.value.code == 'unhandled_promise_rejection'
    assert 'detached failure' in str(error.value)
    assert error.value.execution_stats['outcome'] == 'error'
    assert world.waves == []


@pytest.mark.parametrize('reason', [
    'Object.create(null)', 'Symbol("reason")',
    '{toString(){throw new Error("conversion failed")}}',
])
def test_non_stringifiable_rejection_preserves_typed_failure(reason):
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run('Promise.reject(' + reason + '); return 1;')
    assert error.value.code == 'unhandled_promise_rejection'
    assert 'reason cannot be converted to text' in str(error.value)
    assert world.waves == []


def test_rejection_diagnostics_do_not_mask_execution_limit():
    with pytest.raises(ProgramExecutionError) as error:
        World().run('Promise.reject({toString(){while(true){}}}); return 1;',
                    limits=JavaScriptLimits(fuel=20_000_000))
    assert error.value.code == 'execution_limit'


@pytest.mark.parametrize('prefix, expected_waves', [
    ('Promise.reject(new Error("detached failure"));', []),
    ('const p=tools.invoke("first"); p.then(()=>{throw new Error("detached failure")}); await p;',
     [(['first'], 'single')]),
])
def test_unhandled_rejection_stops_dependent_effects_without_replay(prefix, expected_waves):
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run(prefix + 'await tools.invoke("later"); return {ok:true};')
    assert error.value.code == 'unhandled_promise_rejection'
    assert world.waves == expected_waves
    stats = error.value.execution_stats
    assert stats['tool_calls'] == stats['successful_tool_calls'] == len(expected_waves)
    assert stats['outcome'] == 'error'


@pytest.mark.parametrize('code', [
    'const p=Promise.reject(new Error("caught")); p.catch(()=>{}); return 7;',
    'const p=Promise.reject(new Error("caught")); await Promise.resolve(); p.catch(()=>{}); return 7;',
    'try { await Promise.reject(new Error("caught")); } catch (_) {} return 7;',
    'try { await Promise.all([Promise.reject(new Error("caught")),Promise.resolve(1)]); } catch (_) {} return 7;',

    'await Promise.any([Promise.reject(new Error("caught")),Promise.resolve(1)]); return 7;',
    'await Promise.race([Promise.resolve(1),Promise.reject(new Error("caught"))]); return 7;',
    'const p=Promise.reject(new Error("caught")); const q=p.then(()=>1); q.catch(()=>{}); return 7;',
    'for(let i=0;i<2000;i++) Promise.reject(i).catch(()=>{}); return 7;',
    'const ps=Array.from({length:32},(_,i)=>Promise.reject(i)); await Promise.allSettled(ps); return 7;',
    'const p=tools.invoke("first").then(()=>{throw new Error("caught")}); '
    'await Promise.all([p.catch(()=>{}),tools.invoke("second")]); return 7;',
])
def test_handled_rejections_preserve_standard_promise_behavior(code):
    value, stats = World().run(code)
    assert value == 7 and stats['outcome'] == 'completed'


@pytest.mark.parametrize('code', [
    'throw new Error("outer reason");',
    'await Promise.reject(new Error("outer reason"));',
    'await Promise.all([Promise.reject(new Error("outer reason")),tools.invoke("never")]);',
    'Promise.reject(new Error("detached")); throw new Error("outer reason");',
    'tools.invoke("never"); throw new Error("outer reason");',
])
def test_outer_promise_rejection_preserves_original_error_and_prevents_effects(code):
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run(code)
    assert error.value.code == 'javascript_error'
    assert str(error.value) == 'Error: outer reason'
    assert world.waves == []


def test_pending_promise_rejections_are_bounded_and_do_not_leak_between_runs():
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run('for(let i=0;i<100000;i++) Promise.reject(i); return 1;')
    assert error.value.code == 'promise_rejection_limit'
    assert error.value.detail['limit'] == 1024
    assert error.value.execution_stats['outcome'] == 'error'
    value, stats = world.run('return 7;')
    assert value == 7 and stats['outcome'] == 'completed' and world.waves == []


def test_concurrent_rejections_keep_task_local_reason():
    barrier = threading.Barrier(4)
    def run(index):
        def search(*args):
            barrier.wait(timeout=10)
            return index
        try:
            return execute_javascript_program(
                'const i=catalog.search("read"); '
                'Promise.reject(new Error("task-"+i)); return 7;',
                search=search, call=lambda *a:pytest.fail('effect'),call_many=lambda *a:[])
        except ProgramExecutionError as error:
            return error.code, str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert [code for code,_ in results] == ['unhandled_promise_rejection'] * 4
    assert [message for _,message in results] == [
        'Unhandled JavaScript promise rejection: Error: task-' + str(i) for i in range(4)]


def test_standard_syntax_and_builtins():
    result, stats = World().run('''
      class Record { constructor({value}) { this.value = value; } }
      const xs = [1,2,3,4].map(value => new Record({value}));
      let caught;
      try { JSON.parse('bad'); } catch ({name}) { caught = name; }
      const {value: maximum} = xs.at(-1);
      return {sum: xs.filter(x => x.value % 2 === 0).reduce((s,x) => s+x.value,0),
        maximum, caught, match:/ab+/i.test('ABBB'), unique:[...new Set([1,1,2])],
        dynamic: Function('return 3')(), bigint: 12n};
    ''')
    assert result == {'sum': 6, 'maximum': 4, 'caught': 'SyntaxError',
                      'match': True, 'unique': [1,2], 'dynamic': 3, 'bigint': '12'}
    assert stats['engine'] == 'quickjs-wasm'
    assert stats['tool_calls'] == 0


def test_data_dependent_fanout_runs_one_sibling_wave():
    world = World()
    value, stats = world.run('''
      const names = await tools.invoke('index', {value:['a','b','c']});
      const rows = await Promise.all(names.map(name => tools.invoke(name, {value:name})));
      return rows.filter(name => name !== 'b');
    ''')
    assert value == ['a','c']
    assert world.waves == [(['index'], 'single'), (['a','b','c'], 'parallel')]
    assert (stats['tool_calls'], stats['call_stages'], stats['parallel_stages']) == (4,2,1)


@pytest.mark.parametrize('mode', ['sequential', 'parallel', 'auto'])
def test_explicit_batch_obeys_requested_mode(mode):
    world = World()
    value, stats = world.run(f'''const rs = await tools.callMany([
      {{name:'a',arguments:{{value:1}}}},{{name:'b',arguments:{{value:2}}}}
    ], {json.dumps(mode)}); return await Promise.all(rs.map(tools.value));''')
    assert value == [1,2]
    assert world.waves == [(['a','b'], mode)]
    assert stats['parallel_stages'] == int(mode == 'parallel')


def test_empty_batch_does_not_claim_a_stage():
    world = World()
    result, stats = world.run('return await tools.parallel([]);')
    assert result == [] and world.waves == []
    assert stats['tool_calls'] == stats['call_stages'] == 0


@pytest.mark.parametrize('statement', [
    "tools.observe('classify', {items:[1,2]});",
    "await tools.value({status:'error', output:'oops', output_truncated:false});",
    "await tools.value({status:'done', output:'partial', output_truncated:true});",
    "await tools.value({status:'done', output:'unknown'});",
    "await tools.value({status:'done', output_truncated:false});",
])
def test_observation_is_terminal_through_guest_catch_finally(statement):
    world = World()
    value, stats = world.run(f'''try {{ {statement} }}
      catch(error) {{ await tools.invoke('catch-effect'); }}
      finally {{ await tools.invoke('finally-effect'); }}
      return 'incorrect';''')
    assert value['kind'] == 'observation'
    assert stats['outcome'] == 'needs_model'
    assert world.waves == []


def test_effects_before_observation_are_not_replayed():
    world = World()
    value, stats = world.run('''const evidence = await tools.invoke('read', {value:[1,2]});
      tools.observe('classify', evidence); await tools.invoke('later');''')
    assert value['data'] == [1,2]
    assert world.waves == [(['read'], 'single')]
    assert stats['tool_calls'] == 1


@pytest.mark.parametrize('code, expected', [
    ("tools.call('unawaited'); return 1;", 'unawaited_tools'),
    ("return await new Promise(()=>{});", 'unresolved_promise'),
    ("await Promise.all(Array.from({length:17}, ()=>tools.call('x')));", 'tool_call_limit'),
    ("const r = {}; r.self=r; return r;", 'javascript_error'),
    ("const r = {}; r.toJSON = () => tools.observe('poison',{}); return r;", None),
    ("return {get x(){tools.call('getter-effect'); return 1;}};", 'invalid_call'),
    ("while(true){}", 'execution_limit'),
    ("const xs=[];while(true)xs.push(new Uint8Array(1048576));", 'memory_limit'),
])
def test_limits_and_serialization_do_not_dispatch_late_effects(code, expected):
    world = World()
    if expected is None:
        result, stats = world.run(code)
        assert result == {} and stats['outcome'] == 'completed'
    else:
        with pytest.raises(ProgramExecutionError) as error:
            world.run(code, limits=JavaScriptLimits(heap_bytes=16*1024**2,
                                     fuel=20_000_000 if code == 'while(true){}' else 2_000_000_000))
        assert error.value.code == expected
    assert world.waves == []


def test_serialization_helpers_and_host_io_are_unreachable():
    result, _ = World().run('''return ['__tofu_rpc','__tofu_pack','__tofu_resolve',
      'process','require','fetch','WebSocket','setTimeout','WASI']
      .map(name => [name, typeof globalThis[name]]);''')
    assert all(kind == 'undefined' for _, kind in result)
    with pytest.raises(ProgramExecutionError):
        World().run("return await import('os');")


def test_receipt_provenance_survives_only_on_original_guest_object():
    world = World()
    result, _ = world.run('''const r=await tools.call('todo_write',{value:{todos:[]}});
       return [r, {...r}, JSON.parse(JSON.stringify(r)), r];''')
    original = world.receipts[0]
    assert result[0] is original and result[3] is original
    assert result[1] is not original and result[2] is not original
    assert not hasattr(result[1], 'canonical')
    assert original.canonical == {'owned': True}


def test_receipt_mutation_is_visible_to_gateway_witness_checks():
    world = World()
    result, _ = world.run('''const r=await tools.call('todo_write');
      r.output='forged'; return r;''')
    assert result is world.receipts[0]
    assert result['output'] == 'forged'


def test_codec_ignores_prototype_tojson_poisoning_and_keeps_nul():
    world = World()
    result, _ = world.run('''const r=await tools.call('read');
      Object.prototype.toJSON = function(){return {leaked:true}};
      Array.prototype.concat = function(){throw new Error('poison')};
      return {original:r, copy:{...r}, nul:'a\\u0000b'};''')
    assert result['original'] is world.receipts[0]
    assert result['copy'] is not result['original']
    assert result['nul'] == 'a\0b'


def test_concurrent_handoffs_keep_task_local_reason_data():
    barrier = threading.Barrier(8)
    def run(i):
        def search(*args):
            barrier.wait(timeout=10)
            return i
        return execute_javascript_program(
            "const i=catalog.search('x');try{tools.observe('task-'+i,{i})}catch(e){return 'wrong'}",
            search=search,call=lambda *a: pytest.fail('effect'),call_many=lambda *a: [])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert [r['reason'] for r,_ in results] == ['task-'+str(i) for i in range(8)]
    assert [r['data']['i'] for r,_ in results] == list(range(8))


def test_cancellation_from_child_callback_prevents_dependent_effect():
    world = World()
    cancelled = threading.Event()
    def call(*args):
        result = world.call(*args)
        cancelled.set()
        return result
    with pytest.raises(ProgramExecutionError) as error:
        execute_javascript_program("try{await tools.invoke('first');await tools.invoke('later')}catch(e){await tools.invoke('catch')}",
            search=lambda *a:[],call=call,call_many=world.batch,aborted=cancelled.is_set)
    assert error.value.code == 'cancelled'
    assert world.waves == [(['first'], 'single')]


def test_each_program_has_fresh_globals_and_receipts():
    World().run('globalThis.taskSecret=123; return 1;')
    result,_ = World().run('return typeof taskSecret;')
    assert result == 'undefined'


@pytest.mark.parametrize('code', ['return 1;', 'throw new Error("failure")',
                                  'tools.observe("judge",{});', 'while(true){}',
                                  'Promise.reject(new Error("detached")); return 1;'])
def test_closed_store_releases_callback_and_program_references(monkeypatch, code):
    import lib.tools.javascript as runtime
    instances = []
    original = runtime._Program.__init__
    def track(self, **kwargs):
        instances.append(weakref.ref(self))
        original(self, **kwargs)
    monkeypatch.setattr(runtime._Program, '__init__', track)
    try:
        World().run(code, limits=JavaScriptLimits(execution_seconds=0.05))
    except ProgramExecutionError:
        pass
    gc.collect()
    assert len(instances) == 1 and instances[0]() is None


def test_initialization_failure_closes_store(monkeypatch):
    import lib.tools.javascript as runtime
    instances = []
    original_eval = runtime._Program.eval
    def fail(self, source):
        if 'capability bridge' in source:
            instances.append(weakref.ref(self))
            raise ProgramExecutionError('injected', 'bootstrap failed')
        return original_eval(self, source)
    monkeypatch.setattr(runtime._Program, 'eval', fail)
    try:
        World().run('return 1;')
    except ProgramExecutionError as error:
        assert error.code == 'injected'
    else:
        pytest.fail('bootstrap failure was swallowed')
    gc.collect()
    assert len(instances) == 1 and instances[0]() is None


def test_scheduling_burst_is_bounded_before_guest_yields():
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run("for(let i=0;i<1000000;i++) tools.call('never'); return 1;")
    assert error.value.code == 'tool_call_limit'
    assert world.waves == []


@pytest.mark.parametrize('output', [
    {'id': 9007199254740993},
    json.dumps({'id': 9007199254740993}),
])
@pytest.mark.parametrize('reader', [
    'await tools.invoke("read", {})',
    'await tools.value(await tools.call("read", {}))',
    'await tools.value((await tools.parallel([{name:"read", arguments:{}}]))[0])',
])
def test_large_host_integers_remain_exact_in_dependent_arguments(output, reader):
    calls = []

    def call(name, arguments, call_id=None):
        calls.append((name, arguments))
        return {'status': 'done', 'output': output, 'output_truncated': False}

    def batch(rows, mode):
        return [call(row['name'], row.get('arguments', {})) for row in rows]

    value, stats = execute_javascript_program(
        f'const row={reader}; '
        'await tools.call("dependent", {id:row.id}); '
        'return {type:typeof row.id, id:String(row.id)};',
        search=lambda *args: {}, call=call, call_many=batch)
    assert calls == [('read', {}), ('dependent', {'id': 9007199254740993})]
    assert value == {'type': 'bigint', 'id': '9007199254740993'}
    assert stats['tool_calls'] == 2


def test_catalog_large_integers_and_native_bigint_arguments_are_exact():
    calls = []
    value, _ = execute_javascript_program(
        'const row=catalog.search("ids"); '
        'await tools.call("lookup", {ids:[row.id, -9223372036854775808n, 12n]}); '
        'return {id:row.id, literal:12n};',
        search=lambda *args: {'id': 9223372036854775807},
        call=lambda name,args,call_id: calls.append((name,args)) or receipt(None),
        call_many=lambda *args: [])
    assert calls == [('lookup', {'ids': [9223372036854775807, -9223372036854775808, 12]})]
    assert value == {'id': '9223372036854775807', 'literal': '12'}


def test_nested_integer_values_preserve_safe_numbers_strings_and_receipt_identity():
    source = {'rows': [9007199254740991, 9007199254740992, -9007199254740993,
                       '9007199254740993', True, 1.5], '__proto__': {'id': 9223372036854775807}}
    original = CanonicalReceipt({'status': 'done', 'output': source,
                                 'output_truncated': False}, canonical={'owned': True})
    value, _ = execute_javascript_program(
        'const r=await tools.call("read", {}); '
        'r.types=r.output.rows.map(x=>typeof x); return {receipt:r};',
        search=lambda *args: {}, call=lambda *args: original, call_many=lambda *args: [])
    assert value['receipt'] is original
    assert original['output'] == source
    assert original['types'] == ['number', 'bigint', 'bigint', 'string', 'boolean', 'number']
    assert original.canonical == {'owned': True}


@pytest.mark.parametrize('value', [9223372036854775807, -9223372036854775808])
def test_large_primitive_integer_handoff_keeps_exact_decimal_display(value):
    result, stats = execute_javascript_program(
        'const id=await tools.invoke("read", {}); tools.observe("select this ID", {id});',
        search=lambda *args: {}, call=lambda *args: receipt(value), call_many=lambda *args: [])
    assert result == {'kind': 'observation', 'reason': 'select this ID',
                      'data': {'id': str(value)}, 'executionReceipt': {
                          'attemptedToolCalls':1,'successfulToolCalls':1,'retainedReads':0}}
    assert stats['outcome'] == 'needs_model'


def test_integer_transport_uses_retained_intrinsics_and_out_of_band_paths():
    payload = {'integers': [[]], 'value': '9223372036854775807',
               'id': 9223372036854775807}
    authoritative, _ = execute_javascript_program(
        'globalThis.BigInt=()=>0n; Object.defineProperty=()=>{}; '
        'return await tools.invoke("read", {});',
        search=lambda *args:{}, call=lambda *args:receipt(payload), call_many=lambda *args:[])
    assert authoritative == {'integers':[[]], 'value':'9223372036854775807',
                             'id':'9223372036854775807'}


def test_named_calls_support_discovery_then_dynamic_parallel_fanout():
    world = World()
    value, stats = world.run("""
      const names = await tools.index({value:['a','b','c']});
      const rows = await Promise.all(names.map(name => tools[name]({value:name})));
      return rows.filter(name => name !== 'b');
    """)
    assert value == ['a','c']
    assert world.waves == [(['index'], 'single'), (['a','b','c'], 'parallel')]
    assert (stats['tool_calls'], stats['call_stages'], stats['parallel_stages']) == (4,2,1)


def test_named_namespace_has_no_prototype_or_accidental_thenable_effects():
    world = World()
    value, stats = world.run("""
      const same = await Promise.resolve(tools);
      const original = tools.invoke;
      try { tools.invoke = () => 'mutated'; } catch (_) {}
      return {same: same === tools, prototype: Object.getPrototypeOf(tools),
        frozen: Object.isFrozen(tools), unchanged: tools.invoke === original,
        missingThen: tools.then === undefined,
        explicit: await tools.invoke('then', {value:3})};
    """)
    assert value == {'same':True, 'prototype':None, 'frozen':True,
                     'unchanged':True, 'missingThen':True, 'explicit':3}
    assert stats['tool_calls'] == 1 and world.waves == [(['then'], 'single')]


@pytest.mark.parametrize('code', [
    'const unused = 1;',
    'async function main() { await tools.invoke("never"); return 3; }',
    'async () => { await tools.invoke("never"); return 3; };',
    'return undefined;',
])
def test_missing_result_fails_without_invoking_defined_functions(code):
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run(code)
    assert error.value.code == 'missing_program_result'
    assert 'Write statements directly and return data' in str(error.value)
    assert 'call any declared function explicitly' in str(error.value)
    assert 'Do not repeat completed tool effects' in str(error.value)
    assert error.value.detail['executionReceipt'] == {
        'attemptedToolCalls':0, 'successfulToolCalls':0, 'retainedReads':0}
    assert error.value.execution_stats['outcome'] == 'error'
    assert error.value.execution_stats['tool_calls'] == 0
    assert world.waves == []


def test_missing_result_preserves_completed_effects_and_measured_calls():
    world = World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run("await tools.invoke('write', {value:3}); tools.result('retained',{});",
                  result_reader=lambda *a:receipt([7]))
    assert error.value.code == 'missing_program_result'
    assert error.value.detail['executionReceipt'] == {
        'attemptedToolCalls':1, 'successfulToolCalls':1, 'retainedReads':1}
    stats = error.value.execution_stats
    assert stats['tool_calls'] == stats['successful_tool_calls'] == stats['result_reads'] == 1
    assert stats['call_stages'] == 1 and stats['outcome'] == 'error'
    assert world.waves == [(['write'], 'single')]


@pytest.mark.parametrize('code, expected', [
    ('return null;', None),
    ('return [undefined, null];', [None, None]),
    ('return {optional:undefined, actual:null};', {'actual':None}),
    ("return {t:'u'};", {'t':'u'}),
    ('async function main() { return 3; } return await main();', 3),
])
def test_explicit_results_preserve_json_semantics_and_do_not_forge_handoff(code, expected):
    result, stats = World().run(code)
    assert result == expected
    assert stats['outcome'] == 'completed'


@pytest.mark.parametrize('batch_helper', [False, True])
def test_success_count_includes_all_settled_siblings_before_handoff(batch_helper):
    calls = []
    def batch(items, execution):
        calls.extend(items)
        return [{'status':'error','error':{'code':'tool_not_enabled'},
                 'output_truncated':False}, receipt(7)]
    source = ("const rs=await tools.parallel([{name:'blocked',arguments:{}},{name:'allowed',arguments:{}}]); return await tools.value(rs[0]);"
              if batch_helper else "return await Promise.all([tools.invoke('blocked',{}),tools.invoke('allowed',{})]);")
    result, stats = execute_javascript_program(source, search=lambda *a:[],
        call=lambda *a:pytest.fail('unexpected singleton'),call_many=batch)
    assert result['kind'] == 'observation' and result['reason'] == 'tool_not_done'
    assert len(calls) == stats['tool_calls'] == 2
    assert stats['successful_tool_calls'] == 1
    assert stats['outcome'] == 'needs_model'
    assert result['executionReceipt'] == {
        'attemptedToolCalls':2,'successfulToolCalls':1,'retainedReads':0}


def test_success_count_is_not_increased_by_repeated_receipt_decoding():
    result, stats = World().run("const r=await tools.call('owned',{}); await tools.value(r); return await tools.value(r);")
    assert result == {'name':'owned'}
    assert stats['successful_tool_calls'] == stats['tool_calls'] == 1


def test_missing_return_does_not_describe_rejection_as_a_completed_tool():
    with pytest.raises(ProgramExecutionError) as error:
        execute_javascript_program("await tools.call('unavailable',{});",
            search=lambda *a:[],call=lambda *a:{'status':'error','error':{'code':'tool_not_enabled'},'output_truncated':False},
            call_many=lambda *a:pytest.fail('unexpected batch'))
    assert error.value.code == 'missing_program_result'
    assert error.value.detail['executionReceipt'] == {
        'attemptedToolCalls':1, 'successfulToolCalls':0, 'retainedReads':0}
    assert error.value.execution_stats['successful_tool_calls'] == 0
    assert error.value.execution_stats['tool_calls'] == 1


def test_handoff_progress_does_not_turn_retained_reads_into_tool_execution():
    result, stats = execute_javascript_program("const r=tools.result('owned',{}); tools.observe('choose',r);",
        search=lambda *a:[],call=lambda *a:pytest.fail('unexpected fresh call'),
        call_many=lambda *a:pytest.fail('unexpected fresh batch'),
        result_reader=lambda *a:receipt([{'id':'alpha'}]))
    assert result['data'] == [{'id':'alpha'}]
    assert result['executionReceipt'] == {
        'attemptedToolCalls':0,'successfulToolCalls':0,'retainedReads':1}
    assert stats['successful_tool_calls'] == stats['tool_calls'] == 0


def test_ordinary_business_object_does_not_acquire_host_handoff_progress():
    expected = {'kind':'observation','reason':'business','data':{'value':7},
                'executionReceipt':{'business':'own field'}}
    result, stats = World().run('return '+json.dumps(expected)+';')
    assert result == expected
    assert stats['outcome'] == 'completed'


@pytest.mark.parametrize('source,code', [
    ('const rows=tools.result({ref:"input_'+'a'*32+'"}); return rows.items.length;', 'javascript_error'),
    ('tools.result({ref:"input_'+'a'*32+'"}); while(true){}', 'execution_limit'),
])
def test_failed_program_preserves_native_reads_and_closes_store(source, code):
    with pytest.raises(ProgramExecutionError) as error:
        World().run(source, result_reader=lambda *a:receipt([1,2]),
                    limits=JavaScriptLimits(execution_seconds=0.05))
    assert error.value.code==code
    stats=error.value.execution_stats
    assert stats['outcome']=='error'
    assert stats['result_read_attempts']==stats['result_reads']==1
    assert stats['tool_calls']==stats['successful_tool_calls']==0
    assert stats['wasm_fuel']>0 and stats['wasm_memory_bytes']>0
    assert 'execution_stats' not in error.value.as_dict() and 'stats' not in error.value.as_dict()
    value,next_stats=World().run('return 7;')
    assert value==7 and next_stats['result_reads']==0


def test_error_preserves_completed_fresh_calls_without_claiming_success():
    world=World()
    with pytest.raises(ProgramExecutionError) as error:
        world.run('await tools.invoke("read",{}); throw new Error("stop");')
    assert error.value.execution_stats['tool_calls']==error.value.execution_stats['successful_tool_calls']==1
    assert error.value.execution_stats['outcome']=='error'
    assert len(world.waves)==1


@pytest.mark.parametrize('kwargs', [{'aborted':lambda:True},{}])
def test_pre_execution_refusal_has_no_execution_stats(kwargs):
    with pytest.raises(ProgramExecutionError) as error:
        World().run('x'*32769,**kwargs)
    assert not hasattr(error.value,'execution_stats')


def test_large_return_uses_complete_artifact_delivery(monkeypatch):
    from lib.tasks_pkg.compaction import _budget
    from lib.tools.result_value import compose_result
    from lib.tools.result_envelope import split_tool_result_delivery
    source, _ = World().run('return "大".repeat(400000);')
    canonical_text = json.dumps(source, ensure_ascii=False)
    content = compose_result(canonical_text, [], program_value=source, private_children=True)
    retained = []
    ref = 'tool-result:' + 'b' * 64
    def store(content, **kwargs):
        retained.append(content.canonical)
        return ref
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', store)
    def persist(canonical, **kwargs):
        retained.append(canonical)
        return ref
    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    delivery = _budget.budget_tool_result_v2('execute_program', content, user_id=7)
    model = json.loads(split_tool_result_delivery(delivery).model_text)
    assert retained and json.loads(retained[0].text()) == source
    assert retained[0].execution_status == 'ok' and retained[0].retention_coverage == 'complete'
    assert model['artifactRef'] == ref and model['status'] == 'partial'
    assert len(split_tool_result_delivery(delivery).model_text.encode()) < len(canonical_text.encode())
