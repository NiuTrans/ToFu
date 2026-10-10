"""Local metadata references preserve exact schemas and bound amplification."""
import copy
import json

import pytest

from lib.tools.code_api import _type, MAX_CODE_API_BYTES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('reference,key', [
    ('#/$defs/value', 'value'), ('#/%24defs/value', 'value'),
    ('#/$defs/a~1b', 'a/b'), ('#/$defs/a~0b', 'a~b'),
    ('#/$defs/a%7E1b', 'a/b'), ('#/$defs/', ''),
])
def test_local_pointers_resolve_only_within_their_schema(reference, key):
    schema = {'$defs': {key: {'type': 'integer', 'minimum': 7}}, '$ref': reference}
    original = copy.deepcopy(schema)
    assert _type(schema) == 'number | bigint'
    assert schema == original


def test_local_pointer_can_index_a_schema_array():
    assert _type({'branches': [{'type': 'string'}], '$ref': '#/branches/0'}) == 'string'


@pytest.mark.parametrize('reference', [
    'https://example.test/schema', 'file:///private/schema', '#anchor',
    '#/$defs/missing', '#/$defs/invalid~2escape', '#/$defs/%FF',
    '#/branches/01', '#/branches/99', '#/$defs/scalar', None, False,
])
def test_unresolved_or_external_references_remain_unknown(reference, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs:
                        pytest.fail('Type projection must not fetch a schema'))
    schema = {'$defs': {'scalar': 4}, 'branches': [{'type': 'string'}], '$ref': reference}
    assert _type(schema) == 'unknown'


@pytest.mark.parametrize('schema', [
    {'$ref': '#'},
    {'$defs': {'a': {'$ref': '#/$defs/b'}, 'b': {'$ref': '#/$defs/a'}}, '$ref': '#/$defs/a'},
])
def test_cyclic_reference_projection_terminates(schema):
    assert _type(schema) == 'unknown'


def test_reference_sibling_types_are_conjoined_and_constraints_stay_exact():
    schema = {'$defs': {'value': {'type': 'integer', 'minimum': 7}},
              '$ref': '#/$defs/value', 'enum': [8]}
    original = copy.deepcopy(schema)
    assert _type(schema) == '(number | bigint) & (8)'
    assert schema == original


def test_repeated_aliases_are_not_mistaken_for_cycles():
    schema = {'$defs': {'value': {'type': 'string'}}, 'type': 'object',
              'properties': {'first': {'$ref': '#/$defs/value'},
                             'second': {'$ref': '#/$defs/value'}},
              'required': ['first', 'second'], 'additionalProperties': False}
    result = _type(schema)
    assert '"first": string' in result and '"second": string' in result


def test_alias_fanout_cannot_amplify_convenience_text_beyond_metadata_budget():
    definitions = {'leaf': {'type': 'string'}}
    for level in range(4):
        child = 'leaf' if level == 0 else f'node{level-1}'
        definitions[f'node{level}'] = {'type': 'object', 'properties': {
            f'field{i}': {'$ref': f'#/$defs/{child}'} for i in range(64)}}
    schema = {'$defs': definitions, '$ref': '#/$defs/node3'}
    assert len(json.dumps(schema, separators=(',', ':')).encode()) < MAX_CODE_API_BYTES
    assert _type(schema) == 'unknown'


def test_code_api_keeps_argument_and_result_reference_roots_separate():
    from lib.tools.contracts import ToolContractV2
    from lib.tools.gateway import ptc_local_wire_tools
    parameters = {'$defs': {'value': {'type': 'string', 'minLength': 2}},
        'type': 'object', 'properties': {'value': {'$ref': '#/$defs/value'}},
        'required': ['value'], 'additionalProperties': False}
    result = {'$defs': {'value': {'type': 'integer', 'minimum': 7}}, '$ref': '#/$defs/value'}
    contract = ToolContractV2('read_ref', parameters, 'Read', decoded_result_schema=result)
    catalog = [contract.provider_schema()]
    original = copy.deepcopy(catalog)
    wire = ptc_local_wire_tools(catalog, tier='program', eligible=['read_ref'], exposure='code_api')
    text = wire[0]['function']['description']
    assert '"value": string' in text and 'Promise<number | bigint>' in text
    assert json.dumps(parameters, sort_keys=True, separators=(',', ':')) in text
    assert json.dumps(result, sort_keys=True, separators=(',', ':')) in text
    assert catalog == original
