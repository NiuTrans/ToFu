"""Structured orchestration validation diagnostics stay navigable."""

from lib.orchestration._validate import validate_definition
from lib.orchestration.definition_inspection import inspect_definition
from lib.orchestration.validation_issues import ValidationIssueList


def _invalid_definition() -> dict:
    return {
        'schema': 'tofu.orchestration/v1',
        'name': 'Broken flow',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {
                'id': 'worker',
                'type': 'role',
                'role': 'worker',
                'params': {
                    'tier': 'impossible',
                    'objective': 7,
                    'a/b~c': 'future',
                    'io': {
                        'inputs': [{
                            'name': '',
                            'type': 'mystery',
                            'from': 'missing.result',
                        }],
                    },
                },
            },
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'worker'},
            {'from': 'worker', 'to': 'stop'},
            {'from': 'worker', 'to': 'missing'},
        ],
    }


def test_verdict_keeps_legacy_lists_and_projects_navigable_diagnostics():
    verdict = validate_definition(_invalid_definition())

    assert isinstance(verdict['errors'], list)
    assert isinstance(verdict['warnings'], list)
    assert [item['message'] for item in verdict['diagnostics']] == [
        *verdict['errors'], *verdict['warnings']]
    assert [item['severity'] for item in verdict['diagnostics']] == [
        *('error' for _ in verdict['errors']),
        *('warning' for _ in verdict['warnings']),
    ]

    by_code = {item['code']: item for item in verdict['diagnostics']}
    assert by_code['role.tier.invalid']['path'] == \
        '/nodes/1/params/tier'
    assert by_code['field.type.string']['path'] == \
        '/nodes/1/params/objective'
    assert by_code['field.unknown']['path'] == \
        '/nodes/1/params/a~1b~0c'
    assert by_code['io.port.name.required']['path'] == \
        '/nodes/1/params/io/inputs/0/name'
    assert by_code['io.port.type.invalid']['path'] == \
        '/nodes/1/params/io/inputs/0/type'
    assert by_code['io.input.from.unknown_node']['path'] == \
        '/nodes/1/params/io/inputs/0/from'
    assert by_code['edge.to.unknown_node']['path'] == '/edges/2/to'


def test_embedded_subflow_prefixes_child_json_pointer_without_rewording():
    child = {
        'schema': 'tofu.orchestration/v1',
        'name': '',
        'nodes': [],
        'edges': [],
    }
    definition = {
        'schema': 'tofu.orchestration/v1',
        'name': 'Parent',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'nested', 'type': 'subflow',
             'params': {'definition': child}},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'nested'},
            {'from': 'nested', 'to': 'stop'},
        ],
    }

    verdict = validate_definition(definition)
    diagnostic = next(item for item in verdict['diagnostics']
                      if item['code'] == 'definition.name.required')
    assert diagnostic['path'] == '/nodes/1/params/definition/name'
    assert diagnostic['message'].startswith("node 'nested' subflow: ")
    assert diagnostic['message'] in verdict['errors']


def test_inspection_detaches_and_preserves_backend_diagnostic_metadata():
    inspection = inspect_definition(_invalid_definition())
    first = inspection['diagnostics'][0]

    assert set(first) == {'severity', 'code', 'path', 'message'}
    assert all(item['code'] for item in inspection['diagnostics'])
    assert inspection['diagnostics'] is not inspection['errors']

    first['path'] = '/changed-by-consumer'
    fresh = inspect_definition(_invalid_definition())
    assert fresh['diagnostics'][0]['path'] != '/changed-by-consumer'


def test_structured_issue_collector_remains_a_normal_string_list():
    issues = ValidationIssueList('error')
    issues.append('legacy append')
    issues.add('structured add', code='sample.code', path='/name')

    assert issues == ['legacy append', 'structured add']
    assert issues.diagnostics == [
        {'severity': 'error', 'code': '', 'path': '',
         'message': 'legacy append'},
        {'severity': 'error', 'code': 'sample.code', 'path': '/name',
         'message': 'structured add'},
    ]
