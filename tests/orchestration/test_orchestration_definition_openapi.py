"""Contract tests for stored-definition OpenAPI projections."""

from __future__ import annotations

import pytest

from lib.orchestration.definition_contract_registry import (
    definition_entry_contract,
    definition_list_contract,
    definition_write_contract,
)
from lib.orchestration.definition_contract_schema import (
    definition_delete_response_schema,
)
from lib.orchestration.inspection_wire_contract import (
    inspection_response_schema,
)
from routes.api_v1.orchestration_definition_openapi import (
    definition_conflict_response_schema,
    definition_entry_response_schema,
    definition_list_response_schema,
    definition_precondition_parameters,
    definition_route_response_registry,
    definition_route_responses,
)


pytestmark = pytest.mark.unit


def test_definition_success_schemas_derive_formats_and_field_registries():
    import lib.orchestration.definition_contract_schema as schema_module
    import routes.api_v1.orchestration_definition_openapi as openapi_module

    listed = definition_list_response_schema()
    entry = definition_entry_response_schema()
    written = definition_entry_response_schema(written=True)
    deleted = definition_delete_response_schema()

    list_contract = definition_list_contract()
    entry_contract = definition_entry_contract()
    assert listed['properties']['format']['enum'] == [list_contract['format']]
    assert listed['properties']['items']['items']['required'] == \
        list_contract['itemFields']
    assert entry['properties']['format']['enum'] == [entry_contract['format']]
    assert entry['required'] == ['ok', 'format', *entry_contract['fields']]
    assert {'inspection', 'warnings', 'contract'} <= set(written['required'])
    assert 'inspection' not in entry['properties']
    assert written['properties']['inspection'] == inspection_response_schema()
    assert deleted['required'] == ['ok']
    assert openapi_module.definition_list_response_schema is \
        schema_module.definition_list_response_schema
    assert openapi_module.definition_entry_response_schema is \
        schema_module.definition_entry_response_schema
    assert openapi_module.definition_delete_response_schema is \
        schema_module.definition_delete_response_schema
    source = open(openapi_module.__file__, encoding='utf-8').read()
    assert 'def definition_list_response_schema' not in source
    assert 'def definition_entry_response_schema' not in source
    assert 'def definition_delete_response_schema' not in source


def test_definition_write_docs_share_version_and_conflict_contract():
    import lib.orchestration.definition_conflict_schema as conflict_module
    import routes.api_v1.orchestration_definition_openapi as openapi_module

    contract = definition_write_contract()
    parameter = definition_precondition_parameters()[0]
    conflict = definition_conflict_response_schema()

    assert parameter['name'] == contract['preconditionHeader']
    assert parameter['required'] is True
    assert conflict['properties']['conflict']['enum'] == [
        contract['conflictReason'],
    ]
    write = conflict['properties']['write']['properties']
    field_names = [
        spec['name'] for spec in contract['conflictFields'].values()
    ]
    assert conflict['properties']['write']['required'] == field_names
    assert list(write) == field_names
    assert conflict['properties']['write']['additionalProperties'] is False
    assert write['format']['enum'] == [contract['format']]
    assert write['operation']['enum'] == contract['operations']
    assert write['expectedUpdatedAt']['type'] == 'integer'
    assert write['currentUpdatedAt']['type'] == 'integer'
    assert openapi_module.definition_conflict_response_schema is \
        conflict_module.definition_conflict_response_schema
    assert 'def definition_conflict_response_schema' not in open(
        openapi_module.__file__, encoding='utf-8').read()


def test_definition_route_response_sets_cover_each_crud_outcome():
    contract = definition_write_contract()
    conflict_status = str(contract['conflictStatus'])

    standard = {'401', '403', '500'}
    assert set(definition_route_responses('list')) == {'200', *standard}
    assert set(definition_route_responses('read')) == {
        '200', '404', *standard,
    }
    assert set(definition_route_responses('create')) == {
        '201', '400', *standard,
    }
    assert set(definition_route_responses('replace')) == {
        '200', '400', '404', conflict_status, *standard,
    }
    assert set(definition_route_responses('delete')) == {
        '200', '400', '404', conflict_status, *standard,
    }
    for operation, status in (('read', '200'), ('create', '201'),
                              ('replace', '200')):
        headers = definition_route_responses(operation)[status]['headers']
        assert contract['versionResponseHeader'] in headers
    with pytest.raises(ValueError, match='unknown definition operation'):
        definition_route_responses('patch')
    assert set(definition_route_response_registry()) == {
        'list', 'read', 'create', 'replace', 'delete',
    }
