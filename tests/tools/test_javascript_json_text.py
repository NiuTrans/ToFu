"""Guest JSON text parsing preserves integer tokens without new tool effects."""
import json

import pytest

from lib.tools.javascript import execute_javascript_program

pytestmark = pytest.mark.unit


def run(source, *, call=None):
    def unexpected(*args, **kwargs):
        pytest.fail('JSON parsing acquired a host capability')

    value, stats = execute_javascript_program(source, search=unexpected,
        call=call or unexpected, call_many=unexpected)
    return value, stats


@pytest.mark.parametrize('literal,expected_type', [
    ('9007199254740991', 'number'), ('-9007199254740991', 'number'),
    ('9007199254740992', 'bigint'), ('9007199254740993', 'bigint'),
    ('-9007199254740993', 'bigint'), ('9223372036854775807', 'bigint'),
    ('18446744073709551615', 'bigint'), ('1' + '0' * 400, 'bigint'),
])
def test_integer_source_is_recovered_before_native_rounding(literal, expected_type):
    value, stats = run('const n=tools.parseJSON(' + json.dumps(literal) +
        '); return {value:String(n),type:typeof n};')
    assert value == {'value': literal, 'type': expected_type}
    assert stats['tool_calls'] == stats['result_reads'] == stats['input_queries'] == 0


def test_noninteger_tokens_strings_and_negative_zero_keep_native_semantics():
    text = '{"decimal":1.25,"exponent":1e3,"largeExponent":9.223372036854776e18,"negativeZero":-0,"label":"9223372036854775807","nested":[null,true,9007199254740993],"__proto__":{"id":9223372036854775807}}'
    value, _ = run('const v=tools.parseJSON(' + json.dumps(text) + ''');
      return {decimal:v.decimal,exponent:v.exponent,largeExponentType:typeof v.largeExponent,
        negativeZero:Object.is(v.negativeZero,-0),label:v.label,
        nested:v.nested,ownProto:Object.hasOwn(v,'__proto__'),
        prototypeUnchanged:Object.getPrototypeOf(v)===Object.prototype,
        protoId:String(v.__proto__.id),polluted:Object.prototype.id!==undefined};''')
    assert value == {'decimal': 1.25, 'exponent': 1000, 'largeExponentType': 'number',
        'negativeZero': True, 'label': '9223372036854775807',
        'nested': [None, True, '9007199254740993'], 'ownProto': True,
        'prototypeUnchanged': True, 'protoId': '9223372036854775807', 'polluted': False}


@pytest.mark.parametrize('text', ['bad', '{', '[1,]', '01', 'NaN', 'undefined'])
def test_syntax_errors_are_catchable_guest_errors(text):
    value, stats = run('try {tools.parseJSON(' + json.dumps(text) +
        ')} catch(e) {return e.name;}')
    assert value == 'SyntaxError' and stats['tool_calls'] == 0


@pytest.mark.parametrize('expression', ['null', '3', '{}', '[]', 'true', 'undefined'])
def test_nonstring_input_does_not_coerce_or_call_tools(expression):
    value, stats = run('try {tools.parseJSON(' + expression +
        ')} catch(e) {return e.name;}')
    assert value == 'TypeError' and stats['tool_calls'] == 0


def test_helper_uses_captured_builtins_and_colliding_tool_remains_callable():
    from lib.tools.result_value import CanonicalReceipt
    calls = []

    def call(name, args, call_id=None):
        calls.append((name, args))
        return CanonicalReceipt({'status': 'done', 'output': '"allowed"',
                                 'output_truncated': False}, canonical={'owned': True})

    value, stats = run('''
      JSON.parse=()=>0; Number.isSafeInteger=()=>true; BigInt=()=>0;
      RegExp.prototype.test=()=>false;
      const n=tools.parseJSON('9223372036854775807');
      return {id:String(n),type:typeof n,frozen:Object.isFrozen(tools),
        collision:await tools.invoke('parseJSON',{id:n})};''', call=call)
    assert value == {'id': '9223372036854775807', 'type': 'bigint',
                     'frozen': True, 'collision': 'allowed'}
    assert calls == [('parseJSON', {'id': 9223372036854775807})]
    assert stats['tool_calls'] == 1


def test_repeated_parser_work_obeys_guest_fuel_and_does_not_poison_next_run():
    from lib.tools.javascript import JavaScriptLimits
    from lib.tools.program_contract import ProgramExecutionError

    def unexpected(*args, **kwargs):
        pytest.fail('Exhausted parser dispatched a tool')

    with pytest.raises(ProgramExecutionError) as error:
        execute_javascript_program(
            "while(true) {tools.parseJSON('[9007199254740993]');} await tools.invoke('late');",
            search=unexpected, call=unexpected, call_many=unexpected,
            limits=JavaScriptLimits(heap_bytes=16 * 1024**2, fuel=20_000_000))
    assert error.value.code == 'execution_limit'
    assert error.value.execution_stats['tool_calls'] == 0
    value, _ = run("return tools.parseJSON('9007199254740993');")
    assert value == '9007199254740993'
