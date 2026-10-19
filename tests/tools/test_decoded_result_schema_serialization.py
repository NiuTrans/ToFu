"""Exact producer-declaration rendering and refusal at the wire boundary."""
import json
from collections import UserDict

import pytest

from lib.tools.contracts import clone_decoded_result_schema, serialize_decoded_result_schema
from lib.tools.gateway import sanitize_wire_tools
from tests.tools.test_decoded_result_schema import tool

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('declaration', [
    {}, {'properties': {'中文': {'enum': ('a', '😀', None)}}},
    {2: {'values': {11: 'a', 2: 'b'}}, 11: []},
    {'nested': [{False: 'no', True: 'yes'}]},
    UserDict({'x': {'type': 'object'}}),
])
def test_rendering_matches_normalized_declaration_and_wire_bytes(declaration):
    expected = json.dumps(clone_decoded_result_schema(declaration),
                          ensure_ascii=True, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    assert serialize_decoded_result_schema(declaration) == expected
    raw = tool(declared=declaration)
    wire = sanitize_wire_tools([raw])[0]
    assert wire['function']['description'] == (
        'Read JSON\nSuccessful decoded JSON value (producer declaration; '
        'coverage/freshness unknown): ' + expected)
    assert 'decoded_result_schema' not in wire
    assert raw['decoded_result_schema'] is declaration


@pytest.mark.parametrize('defect', ['list', 'nan', 'infinity', 'cycle', 'mixed_keys', 'unsupported', 'oversized'])
def test_rendering_preserves_refusal_and_wire_only_withdraws_bad_metadata(defect):
    declaration = {'type': 'object'}
    if defect == 'list': declaration = []
    elif defect in ('nan', 'infinity'): declaration['maximum'] = float('nan' if defect == 'nan' else 'inf')
    elif defect == 'cycle': declaration['recursive'] = declaration
    elif defect == 'mixed_keys': declaration[1] = 'mixed'
    elif defect == 'unsupported': declaration['bad'] = object()
    else: declaration['description'] = 'x' * 32769
    with pytest.raises(ValueError) as previous:
        clone_decoded_result_schema(declaration)
    with pytest.raises(ValueError) as rendered:
        serialize_decoded_result_schema(declaration)
    assert str(rendered.value) == str(previous.value)
    raw = tool(declared=declaration)
    wire = sanitize_wire_tools([raw])[0]
    assert wire['function']['description'] == 'Read JSON'
    assert 'decoded_result_schema' not in wire and raw['decoded_result_schema'] is declaration


def test_rendering_uses_frozen_bytes_if_producer_mutates_after_encoding(monkeypatch):
    declaration = {'nested': {}}
    encode = json.dumps
    def changing_producer(value, *args, **kwargs):
        result = encode(value, *args, **kwargs)
        if isinstance(value, dict) and value.get('nested') is declaration['nested']:
            declaration['nested']['cycle'] = declaration['nested']
        return result
    monkeypatch.setattr(json, 'dumps', changing_producer)
    assert serialize_decoded_result_schema(declaration) == '{"nested":{}}'
