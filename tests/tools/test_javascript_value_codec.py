"""JSON-compatible guest values: explicit rejection without losing codec guarantees."""
import json

import pytest

from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.result_value import CanonicalReceipt

pytestmark = pytest.mark.unit


class CodecWorld:
    def __init__(self):
        self.calls = []
        self.kept = []
        self.receipts = []

    def call(self, name, arguments, call_id=None):
        self.calls.append((name, arguments))
        receipt = CanonicalReceipt(
            {'status': 'done', 'output': arguments.get('value'), 'output_truncated': False},
            canonical={'owned': True})
        self.receipts.append(receipt)
        return receipt

    def keep(self, value):
        self.kept.append(value)
        return {'kind': 'program_value', 'ref': 'input_test', 'bytes': 1}

    def run(self, source):
        return execute_javascript_program(
            source, search=lambda *args: pytest.fail('unexpected search'),
            call=self.call, call_many=lambda *args: pytest.fail('unexpected batch'),
            value_keeper=self.keep)


UNSUPPORTED_VALUES = [
    pytest.param("new Date('2026-01-02T00:00:00Z')", id='date'),
    pytest.param("new Error('bad')", id='error'),
    pytest.param("new TypeError('bad')", id='error-subclass'),
    pytest.param("new Map([['k',1]])", id='map'),
    pytest.param('new Set([1])', id='set'),
    pytest.param('/important/i', id='regexp'),
    pytest.param('new Uint8Array([1,2])', id='typed-array'),
    pytest.param('new ArrayBuffer(8)', id='array-buffer'),
    pytest.param('new DataView(new ArrayBuffer(8))', id='data-view'),
    pytest.param('Promise.resolve(1)', id='nested-promise'),
    pytest.param('new Number(3)', id='boxed-number'),
    pytest.param("new String('text')", id='boxed-string'),
    pytest.param('new Boolean(false)', id='boxed-boolean'),
    pytest.param('Object.create({inherited:1})', id='custom-prototype'),
    pytest.param('new (class Record { constructor() { this.value=1; } })()', id='class'),
]


@pytest.mark.parametrize('expression', UNSUPPORTED_VALUES)
@pytest.mark.parametrize('boundary', [
    'return value;',
    'tools.keep(value); return null;',
    'tools.observe("inspect", value);',
    'await tools.invoke("sink", {value}); return null;',
], ids=['return', 'keep', 'observe', 'invoke'])
def test_unsupported_objects_are_rejected_recursively_before_host_effects(expression, boundary):
    world = CodecWorld()
    with pytest.raises(ProgramExecutionError) as error:
        world.run('const value={outer:[{nested:' + expression + '}]}; ' + boundary)
    assert error.value.code == 'javascript_error'
    assert 'Unsupported object kind' in str(error.value)
    assert 'plain JSON' in str(error.value)
    assert error.value.execution_stats['outcome'] == 'error'
    assert error.value.execution_stats['tool_calls'] == 0
    assert error.value.execution_stats['value_keeps'] == 0
    assert world.calls == world.kept == []


@pytest.mark.parametrize('expression', UNSUPPORTED_VALUES[:4])
def test_root_unsupported_objects_do_not_become_empty_records(expression):
    with pytest.raises(ProgramExecutionError, match='Unsupported object kind'):
        CodecWorld().run('return ' + expression + ';')


def test_error_rejection_has_conversion_guidance_without_reading_diagnostic_accessors():
    with pytest.raises(ProgramExecutionError) as error:
        CodecWorld().run('''
          const error = new Error('bad');
          for (const key of ['name', 'message', 'stack', 'constructor', 'toJSON']) {
            Object.defineProperty(error, key, {get() { throw 'accessor ran'; }});
          }
          Object.defineProperty(error, Symbol.toStringTag, {get() { throw 'tag ran'; }});
          return {error};
        ''')
    assert 'Unsupported object kind' in str(error.value)
    assert 'Error' in str(error.value)
    assert 'name, message, stack' in str(error.value)
    assert 'accessor ran' not in str(error.value) and 'tag ran' not in str(error.value)


def test_explicit_date_map_and_error_conversion_preserves_useful_values():
    result, _ = CodecWorld().run('''
      const date=new Date('2026-01-02T00:00:00Z');
      const map=new Map([['k',1]]);
      const error=new Error('bad');
      return {date:date.toISOString(), map:[...map],
        error:{name:error.name, message:error.message, stack:error.stack.slice(0,512)}};
    ''')
    assert result['date'] == '2026-01-02T00:00:00.000Z'
    assert result['map'] == [['k', 1]]
    assert result['error']['name'] == 'Error'
    assert result['error']['message'] == 'bad'
    assert 0 < len(result['error']['stack']) <= 512


def test_null_prototype_records_and_primitive_compatibility_survive_all_boundaries():
    world = CodecWorld()
    result, stats = world.run('''
      const value=Object.create(null);
      value.id=9007199254740993n;
      value.__proto__={safe:true};
      value.constructor='own data';
      value.text='a\\u0000b';
      value.optional=undefined;
      value.callback=()=>1;
      value.symbol=Symbol('ignored');
      value.items=[undefined, ()=>1, Symbol('null'), NaN, Infinity, null];
      tools.keep(value);
      const echoed=await tools.invoke('echo', {value});
      tools.observe('inspect', {value, echoed});
    ''')
    expected = {'id': '9007199254740993', '__proto__': {'safe': True},
                'constructor': 'own data', 'text': 'a\0b', 'items': [None] * 6}
    assert world.kept == [expected]
    assert world.calls == [('echo', {'value': {**expected, 'id': 9007199254740993}})]
    assert result['data'] == {'value': expected, 'echoed': expected}
    assert stats['outcome'] == 'needs_model'
    assert stats['tool_calls'] == stats['value_keeps'] == 1
    returned, _ = CodecWorld().run('return Object.assign(Object.create(null), JSON.parse('
                                 + json.dumps(json.dumps(expected)) + '));')
    assert returned == expected


def test_codec_keeps_receipt_identity_but_not_copies_or_kept_values():
    world = CodecWorld()
    result, _ = world.run('''
      const receipt=await tools.call('read', {value:{id:9007199254740993n}});
      tools.keep(receipt);
      return {original:receipt, copy:{...receipt}, again:receipt};
    ''')
    assert result['original'] is result['again'] is world.receipts[0]
    assert result['original'].canonical == {'owned': True}
    assert result['original']['output'] == {'id': 9007199254740993}
    assert result['copy'] is not result['original']
    assert not hasattr(result['copy'], 'canonical')
    assert world.kept[0]['output'] == {'id': '9007199254740993'}
    assert not hasattr(world.kept[0], 'canonical')


def test_unsupported_receipt_mutation_is_rejected_without_updating_host_identity():
    world = CodecWorld()
    with pytest.raises(ProgramExecutionError, match='Unsupported object kind'):
        world.run('''
          const receipt=await tools.call('read', {value:{id:1}});
          receipt.output={map:new Map([['k',1]])};
          return receipt;
        ''')
    assert world.calls == [('read', {'value': {'id': 1}})]
    assert world.receipts[0]['output'] == {'id': 1}
    assert world.receipts[0].canonical == {'owned': True}


def test_captured_intrinsics_resist_prototype_poisoning_without_tojson_calls():
    result, _ = CodecWorld().run('''
      Object.getPrototypeOf=()=>null;
      Object.keys=()=>[];
      Object.prototype.toJSON=function(){throw new Error('prototype toJSON ran')};
      Array.prototype.toJSON=function(){throw new Error('array toJSON ran')};
      let calls=0;
      const value=Object.create(null);
      value.rows=[{id:9007199254740993n, toJSON(){calls++; return 'wrong';}}];
      tools.keep(value);
      return {value, calls};
    ''')
    assert result == {'value': {'rows': [{'id': '9007199254740993'}]}, 'calls': 0}


def test_replaced_intrinsics_cannot_bypass_unsupported_object_rejection():
    with pytest.raises(ProgramExecutionError, match='Unsupported object kind'):
        CodecWorld().run('''
          const value=new Map([['k',1]]);
          Object.getPrototypeOf=()=>Object.prototype;
          globalThis.TypeError=function(){return {message:'forged'}};
          return {value};
        ''')
