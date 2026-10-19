"""Wire admission rechecks mutable source and changed provider projections."""
from copy import deepcopy
from types import MappingProxyType

import pytest

from lib.tools.gateway import sanitize_wire_tools

pytestmark = pytest.mark.unit


def tool(parameters):
    return {'type': 'function', 'function': {'name': 'probe', 'parameters': parameters}}


@pytest.mark.parametrize('model', ['', 'gemini-2.5-pro', 'gpt-4.1'])
def test_later_source_mutation_is_rejected_without_retained_admission(model):
    parameters = {'type': 'object', 'properties': {'path': {'type': 'string'}},
                  'required': ['path']}
    tools = [tool(parameters)]
    assert sanitize_wire_tools(tools, model=model) is tools
    parameters['properties'].clear()
    reports = []
    assert sanitize_wire_tools(tools, model=model, on_tool_isolated=reports.append) == []
    assert len(reports) == 1
    assert reports[0]['reasonCode'] == 'invalid_schema'
    assert 'path' in reports[0]['detail']
    assert parameters['required'] == ['path']


@pytest.mark.parametrize('model', ['', 'gemini-2.5-pro', 'gpt-4.1'])
def test_changed_provider_projection_is_checked_before_delivery(model, monkeypatch):
    from lib.tools import gateway

    tools = [tool({'type': 'object'})]
    before = deepcopy(tools)

    def malformed_projection(source, **_):
        projected = deepcopy(source)
        projected['function']['parameters']['required'] = ['undeclared']
        return projected, ('$.function.parameters',), ''

    monkeypatch.setattr(gateway, '_normalize_provider_tool_schema', malformed_projection)
    reports = []
    assert sanitize_wire_tools(tools, model=model, on_tool_isolated=reports.append) == []
    assert reports[0]['reasonCode'] == 'invalid_schema'
    assert 'undeclared' in reports[0]['detail']
    assert tools == before


@pytest.mark.parametrize('keyword,shape', [
    ('$defs', 'map'), ('definitions', 'map'), ('dependentSchemas', 'map'),
    ('patternProperties', 'map'), ('properties', 'map'),
    ('allOf', 'list'), ('anyOf', 'list'), ('oneOf', 'list'), ('prefixItems', 'list'),
    ('additionalProperties', 'single'), ('contains', 'single'),
    ('contentSchema', 'single'), ('else', 'single'), ('if', 'single'),
    ('items', 'single'), ('items', 'list'), ('not', 'single'),
    ('propertyNames', 'single'), ('then', 'single'),
    ('unevaluatedItems', 'single'), ('unevaluatedProperties', 'single'),
])
def test_nested_mapping_schemas_keep_required_property_rejection(keyword, shape):
    invalid = MappingProxyType({'type': 'object', 'required': ['missing']})
    value = MappingProxyType({'child': invalid}) if shape == 'map' else (
        [invalid] if shape == 'list' else invalid)
    tools = [tool(MappingProxyType({'type': 'object', keyword: value}))]
    reports = []
    assert sanitize_wire_tools(tools, on_tool_isolated=reports.append) == []
    assert reports[0]['reasonCode'] == 'invalid_schema'
    assert f'.{keyword}' in reports[0]['detail']
    assert 'missing' in reports[0]['detail']


def test_absent_none_boolean_and_annotation_values_are_not_subschemas():
    parameters = MappingProxyType({
        'type': 'object', 'properties': None, 'additionalProperties': False,
        'items': None, 'if': True, 'default': {'required': ['annotation-only']},
    })
    tools = [tool(parameters)]
    assert sanitize_wire_tools(tools) is tools
