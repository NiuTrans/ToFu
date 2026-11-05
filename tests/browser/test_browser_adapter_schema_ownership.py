"""Caller-owned schema dictionaries cannot change an admitted adapter."""

from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('boundary', ['constructor', 'public_projection'])
def test_adapter_owns_input_and_output_schema_snapshots(boundary):
    from lib.browser.adapters import AdapterCommand

    supplied = {
        'input_schema': {'type': 'object', 'properties': {'count': {'type': 'integer'}}},
        'output_schema': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}},
    }
    expected = deepcopy(supplied)
    command = AdapterCommand('inspect', 'Inspect owned input', **supplied)
    exposed = supplied if boundary == 'constructor' else command.public_dict()
    exposed['input_schema']['properties']['count']['type'] = 'string'
    exposed['output_schema']['properties']['ok']['type'] = 'string'
    assert command.input_schema == expected['input_schema']
    assert command.output_schema == expected['output_schema']
