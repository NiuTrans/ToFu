"""Contract tests for the reusable orchestration application boundary."""

from __future__ import annotations

import re

import pytest
import lib.orchestration.runtime_service as runtime_module
import lib.orchestration.authoring_contract as authoring_module

from lib.orchestration._builtin_definitions import build_autopilot_definition
from lib.orchestration._validate import validate_definition
from lib.orchestration.authoring_builtin_registry import (
    build_builtin_definition,
    builtin_names,
)
from lib.orchestration.authoring_contract import (
    authoring_contract,
    node_authoring_defaults,
)
from lib.orchestration.authoring_service import (
    OrchestrationAuthoringService,
    compose_definition,
    layout_authoring_definition,
    plan_authoring_definition,
)
from lib.orchestration.definition_inspection import inspect_definition
from lib.orchestration.definition_resolution import resolve_definition
from lib.orchestration.definition_service import (
    DefinitionServiceError,
    OrchestrationDefinitionService,
)
from lib.orchestration.definition_contract_registry import (
    definition_entry_contract,
    definition_list_contract,
)
from lib.orchestration.definition_contract_schema import (
    definition_request_schema,
)
from lib.orchestration.definition_wire_projection import (
    parse_definition_write_precondition,
    project_definition_entry,
    project_definition_list,
)
from lib.orchestration.durable_projection import DurableProjectionError
from lib.orchestration.inspection_wire_contract import (
    inspection_response_fields,
)
from lib.orchestration.runtime_event_sink import FlowEventSink
from lib.orchestration.runtime_outcome import FlowRunOutcome
from lib.orchestration.runtime_service import (
    execute_flow,
    execute_runtime_flow,
    spawn_runtime_flow,
)
from lib.orchestration.wire_formats import (
    AUTHORING_CONTRACT_FORMAT,
    INSPECTION_FORMAT,
)
from lib.orchestration.store import OrchestrationStore
from lib.orchestration.events import runtime_event_contract
from lib.orchestration.run_service import (
    RUN_MUTATION_CONFLICT,
    RUN_MUTATION_PERSISTENCE_FAILED,
    RunMutationResult,
)


pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit
STORE_OWNER = 14_001
OTHER_STORE_OWNER = 14_002


@pytest.fixture(autouse=True)
def clean_definition_store():
    for owner_user_id in (STORE_OWNER, OTHER_STORE_OWNER):
        store = OrchestrationStore(owner_user_id)
        for entry in store.list_entries():
            store.delete_if_current(
                entry['id'], expected_updated_at=entry['updatedAt'])
    yield
    for owner_user_id in (STORE_OWNER, OTHER_STORE_OWNER):
        store = OrchestrationStore(owner_user_id)
        for entry in store.list_entries():
            store.delete_if_current(
                entry['id'], expected_updated_at=entry['updatedAt'])


def _linear_definition(name='Linear'):
    return {
        'schema': 'tofu.orchestration/v1',
        'name': name,
        'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'},
            {'id': 'w', 'type': 'role', 'role': 'worker',
             'params': {'objective': 'Do the work'}},
            {'id': 'z', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [{'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'z'}],
    }


def test_store_owns_crud_shape_and_returns_snapshots():
    store = OrchestrationStore(
        STORE_OWNER, id_factory=lambda: 'orch_fixed')

    created = store.create(_linear_definition())
    assert created['id'] == 'orch_fixed'
    assert created['createdAt'] == created['updatedAt']

    # Consumers cannot mutate the repository through a returned object.
    loaded = store.get_definition('orch_fixed')
    loaded['name'] = 'client mutation'
    assert store.get_definition('orch_fixed')['name'] == 'Linear'

    updated = store.update_if_current(
        'orch_fixed', _linear_definition('Updated'),
        expected_updated_at=created['updatedAt'])
    assert updated.entry['name'] == 'Updated'
    assert store.get_definition('orch_fixed')['name'] == 'Updated'
    assert store.delete_if_current(
        'orch_fixed',
        expected_updated_at=updated.entry['updatedAt'],
    ).deleted is True
    assert store.get_entry('orch_fixed') is None


def test_definition_store_is_owner_scoped():
    owner_store = OrchestrationStore(
        STORE_OWNER, id_factory=lambda: 'orch_private')
    other_store = OrchestrationStore(OTHER_STORE_OWNER)
    owner_store.create(_linear_definition())
    assert other_store.list_entries() == []
    assert other_store.get_entry('orch_private') is None
    assert other_store.delete_if_current(
        'orch_private', expected_updated_at=0).deleted is False
    assert owner_store.get_entry('orch_private') is not None


def test_definition_store_rejects_nonstandard_json_numbers():
    store = OrchestrationStore(
        STORE_OWNER, id_factory=lambda: 'must_not_write')
    definition = _linear_definition('Non-finite')
    definition['nodes'][0]['pos'] = {'x': float('nan'), 'y': 0}

    with pytest.raises(RuntimeError):
        store.create(definition)
    assert store.list_entries() == []


def test_definition_store_rejects_id_collision_atomically():
    store = OrchestrationStore(
        STORE_OWNER, id_factory=lambda: 'orch_collision')
    created = store.create(_linear_definition('Original'))

    with pytest.raises(RuntimeError, match='already exists'):
        store.create(_linear_definition('Must not append'))

    assert store.list_entries() == [created]


def test_store_compare_and_set_is_atomic_and_preserves_newer_definition():
    store = OrchestrationStore(
        STORE_OWNER, id_factory=lambda: 'orch_cas')
    created = store.create(_linear_definition('Original'))

    accepted = store.update_if_current(
        'orch_cas',
        _linear_definition('First writer'),
        expected_updated_at=created['updatedAt'],
    )
    assert accepted.conflict is False
    assert accepted.entry['updatedAt'] > created['updatedAt']

    stale = store.update_if_current(
        'orch_cas',
        _linear_definition('Stale writer'),
        expected_updated_at=created['updatedAt'],
    )
    assert stale.conflict is True
    assert stale.entry is None
    assert stale.current_updated_at == accepted.entry['updatedAt']
    assert store.get_definition('orch_cas')['name'] == 'First writer'

    stale_delete = store.delete_if_current(
        'orch_cas', expected_updated_at=created['updatedAt'])
    assert stale_delete.conflict is True
    assert stale_delete.deleted is False
    assert store.get_definition('orch_cas')['name'] == 'First writer'

    accepted_delete = store.delete_if_current(
        'orch_cas', expected_updated_at=accepted.entry['updatedAt'])
    assert accepted_delete.deleted is True
    assert accepted_delete.conflict is False
    assert store.get_entry('orch_cas') is None


def test_definition_write_precondition_accepts_wire_token_and_rejects_noise():
    from lib.orchestration.definition_wire_projection import (
        definition_write_version_token,
    )

    with pytest.raises(ValueError, match='If-Match is required'):
        parse_definition_write_precondition(None)
    assert parse_definition_write_precondition(
        definition_write_version_token(123)) == 123
    assert parse_definition_write_precondition('W/"456"') == 456
    with pytest.raises(ValueError, match='safe non-negative int'):
        definition_write_version_token(True)
    with pytest.raises(ValueError, match='safe non-negative int'):
        definition_write_version_token(-1)
    with pytest.raises(ValueError):
        parse_definition_write_precondition('*')
    with pytest.raises(ValueError):
        parse_definition_write_precondition('12.5')


def test_definition_list_projection_is_lightweight_and_newest_first():
    class Repository:
        @staticmethod
        def list_entries():
            return [
                {'id': 'older', 'name': 'Older', 'createdAt': 10,
                 'updatedAt': 20,
                 'definition': {'name': 'Older', 'nodes': [{}, {}]}},
                {'id': 'newer-b', 'name': 'Newer B', 'createdAt': 11,
                 'updatedAt': 30,
                 'definition': {'name': 'Newer B', 'nodes': [{}]}},
                {'id': 'newer-a', 'name': 'Newer A', 'createdAt': 11,
                 'updatedAt': 30,
                 'definition': {'name': 'Newer A', 'nodes': []}},
            ]

        @staticmethod
        def get_entry(_orchestration_id):
            raise AssertionError('summary projection must not load entries')

        get_definition = get_entry

        @staticmethod
        def create(_definition):
            raise AssertionError('summary projection must not create entries')

        @staticmethod
        def update_if_current(*_args, **_kwargs):
            raise AssertionError('summary projection must not update entries')

        @staticmethod
        def delete_if_current(*_args, **_kwargs):
            raise AssertionError('summary projection must not delete entries')

    rows = OrchestrationDefinitionService(Repository()).list_summaries()

    assert [row['id'] for row in rows] == ['newer-a', 'newer-b', 'older']
    assert [row['nodeCount'] for row in rows] == [0, 1, 2]
    assert all('definition' not in row for row in rows)
    assert definition_list_contract()['orderBy'] == [
        {'field': 'updatedAt', 'direction': 'desc'},
        {'field': 'createdAt', 'direction': 'desc'},
        {'field': 'id', 'direction': 'asc'},
    ]


def test_definition_http_documents_share_versioned_detached_projectors():
    entry = {
        'id': 'orch_projected', 'name': 'Projected',
        'definition': _linear_definition('Projected'),
        'createdAt': 10, 'updatedAt': 11,
    }
    inspection = inspect_definition(entry['definition'])
    item = project_definition_entry(entry, inspection=inspection)
    listing = project_definition_list([{
        'id': entry['id'], 'name': entry['name'], 'nodeCount': 3,
        'createdAt': 10, 'updatedAt': 11,
    }])

    assert item['format'] == 'tofu.orchestration.definition-entry/v1'
    assert item['inspection']['format'] == INSPECTION_FORMAT
    assert listing['format'] == 'tofu.orchestration.definition-list/v1'
    assert listing['items'][0]['id'] == entry['id']
    assert definition_entry_contract() == {
        'format': 'tofu.orchestration.definition-entry/v1',
        'fields': ['id', 'name', 'definition', 'createdAt', 'updatedAt'],
        'versionField': 'updatedAt',
        'versionRequiredOnWrite': True,
        'inspectionIncludedOnWrite': True,
    }

    item['definition']['name'] = 'client mutation'
    listing['items'][0]['name'] = 'client mutation'
    assert entry['definition']['name'] == 'Projected'
    assert entry['name'] == 'Projected'


def test_definition_service_unifies_validation_crud_and_resolution():
    store = OrchestrationStore(
        STORE_OWNER,
        id_factory=lambda: 'orch_service',
    )
    service = OrchestrationDefinitionService(store)

    broken = _linear_definition('Broken')
    broken['edges'][-1]['to'] = 'missing-node'
    rejected = service.create(broken)
    assert rejected.valid is False
    assert rejected.entry is None
    assert service.list_entries() == []

    created = service.create(_linear_definition())
    assert created.valid is True
    assert created.entry['id'] == 'orch_service'
    summaries = service.list_summaries()
    assert summaries == [{
        'id': 'orch_service',
        'name': 'Linear',
        'nodeCount': 3,
        'createdAt': created.entry['createdAt'],
        'updatedAt': created.entry['updatedAt'],
    }]
    assert 'definition' not in summaries[0]
    resolved = service.resolve(stored_id='orch_service')
    assert resolved.source == 'stored:orch_service'
    assert resolved.definition['name'] == 'Linear'

    updated = service.update(
        'orch_service', _linear_definition('Updated'),
        expected_updated_at=created.entry['updatedAt'])
    assert updated.valid is True
    assert updated.entry['name'] == 'Updated'
    assert service.get_definition('orch_service')['name'] == 'Updated'
    assert service.delete_if_current(
        'orch_service',
        expected_updated_at=updated.entry['updatedAt'],
    ).deleted is True


def test_definition_service_wraps_every_repository_failure():
    class BrokenRepository:
        @staticmethod
        def _fail():
            raise OSError('repository offline')

        list_entries = _fail

        @staticmethod
        def get_entry(_orchestration_id):
            return BrokenRepository._fail()

        get_definition = get_entry

        @staticmethod
        def create(_definition):
            return BrokenRepository._fail()

        @staticmethod
        def update_if_current(*_args, **_kwargs):
            return BrokenRepository._fail()

        @staticmethod
        def delete_if_current(*_args, **_kwargs):
            return BrokenRepository._fail()

    service = OrchestrationDefinitionService(BrokenRepository())
    operations = (
        lambda: service.list_entries(),
        lambda: service.list_summaries(),
        lambda: service.get_entry('flow-1'),
        lambda: service.get_definition('flow-1'),
        lambda: service.create(_linear_definition()),
        lambda: service.update(
            'flow-1', _linear_definition(), expected_updated_at=0),
        lambda: service.delete_if_current(
            'flow-1', expected_updated_at=0),
    )
    for operation in operations:
        with pytest.raises(DefinitionServiceError) as captured:
            operation()
        assert isinstance(captured.value.__cause__, OSError)


def test_successful_definition_writes_canonicalize_fields_recursively():
    store = OrchestrationStore(
        STORE_OWNER,
        id_factory=lambda: 'orch_canonical',
    )
    service = OrchestrationDefinitionService(store)
    definition = _linear_definition('Canonical')
    worker = definition['nodes'][1]
    worker['params'].update({
        'must_do': '  ship it\n\n write tests  ',
        'expected_outcome': None,
    })
    child = _linear_definition('Child')
    child['nodes'][1]['params']['must_do'] = (' inspect ', ' report ')
    definition['nodes'].insert(2, {
        'id': 'g', 'type': 'subflow', 'role': 'general',
        'params': {'scope': 'isolated', 'definition': child},
    })
    definition['edges'] = [
        {'from': 's', 'to': 'w'}, {'from': 'w', 'to': 'g'},
        {'from': 'g', 'to': 'z'},
    ]

    created = service.create(definition)

    assert created.valid is True
    saved = created.entry['definition']
    saved_worker = next(node for node in saved['nodes'] if node['id'] == 'w')
    assert saved_worker['params']['must_do'] == ['ship it', 'write tests']
    assert 'expected_outcome' not in saved_worker['params']
    saved_child = next(node for node in saved['nodes'] if node['id'] == 'g')[
        'params']['definition']
    assert saved_child['nodes'][1]['params']['must_do'] == [
        'inspect', 'report',
    ]
    # Canonicalization is detached from the caller-owned draft.
    assert isinstance(worker['params']['must_do'], str)
    assert isinstance(child['nodes'][1]['params']['must_do'], tuple)


def test_routes_and_chat_depend_on_definition_service_not_store():
    route = open('routes/api_v1/orchestrations.py', encoding='utf-8').read()
    definition_routes = open(
        'routes/api_v1/orchestration_definition_routes.py', encoding='utf-8',
    ).read()
    authoring_routes = open(
        'routes/api_v1/orchestration_authoring_routes.py', encoding='utf-8',
    ).read()
    authoring_http = open(
        'routes/api_v1/orchestration_authoring_http.py', encoding='utf-8',
    ).read()
    definition_request_http = open(
        'routes/api_v1/orchestration_definition_request_http.py',
        encoding='utf-8',
    ).read()
    definition_selection = open(
        'lib/orchestration/definition_selection_contract.py',
        encoding='utf-8',
    ).read()
    runtime_routes = open(
        'routes/api_v1/orchestration_runtime_routes.py', encoding='utf-8',
    ).read()
    application_result_ports = open(
        'lib/orchestration/application_result_ports.py', encoding='utf-8',
    ).read()
    application_service_ports = open(
        'lib/orchestration/application_service_ports.py', encoding='utf-8',
    ).read()
    application_provider_ports = open(
        'lib/orchestration/application_provider_ports.py', encoding='utf-8',
    ).read()
    task_routes = open(
        'routes/api_v1/orchestration_task_routes.py', encoding='utf-8',
    ).read()
    mutation_routes = open(
        'routes/api_v1/orchestration_mutation_routes.py', encoding='utf-8',
    ).read()
    mutation_http = open(
        'routes/api_v1/orchestration_mutation_http.py', encoding='utf-8',
    ).read()
    task_http = open(
        'routes/api_v1/orchestration_task_http.py', encoding='utf-8',
    ).read()
    task_list_http = open(
        'routes/api_v1/orchestration_task_list_http.py', encoding='utf-8',
    ).read()
    run_http = open(
        'routes/api_v1/orchestration_run_http.py', encoding='utf-8',
    ).read()
    runtime_start_http = open(
        'routes/api_v1/orchestration_runtime_start_http.py',
        encoding='utf-8',
    ).read()
    runtime_start_service = open(
        'lib/orchestration/runtime_start_service.py', encoding='utf-8',
    ).read()
    definition_http = open(
        'routes/api_v1/orchestration_definition_http.py', encoding='utf-8',
    ).read()
    definition_service_http = open(
        'routes/api_v1/orchestration_definition_service_http.py',
        encoding='utf-8',
    ).read()
    runner = open(
        'lib/orchestration_chat_flow_runner.py', encoding='utf-8',
    ).read()
    assert 'OrchestrationDefinitionService' in route
    assert 'OrchestrationDefinitionService' in runner
    assert 'from lib.orchestration.store import OrchestrationStore' not in route
    assert ('from lib.orchestration.store import OrchestrationStore'
            not in definition_routes)
    assert ('from lib.orchestration.store import OrchestrationStore'
            not in authoring_routes)
    assert ('from lib.orchestration.store import OrchestrationStore'
            not in runtime_routes)
    assert ('from lib.orchestration.store import OrchestrationStore'
            not in mutation_routes + task_http)
    assert ('from lib.orchestration.store import OrchestrationStore'
            not in run_http)
    assert 'from lib.orchestration.store import OrchestrationStore' not in runner
    assert 'definition_service().create(parse_body())' in definition_routes
    assert 'lambda: definition_service().update(' in definition_routes
    assert 'expected_updated_at=expected_updated_at' in definition_routes
    assert '_DEF_SCHEMA = definition_request_schema()' in definition_routes
    assert 'def definition_precondition(' in definition_request_http
    assert 'def definition_precondition(' not in definition_http
    assert 'def definition_service_call(' not in definition_http
    assert 'def definition_conflict_response(' in definition_http
    assert 'def invalid_definition_response(' in definition_http
    assert 'def with_definition_etag(' in definition_http
    assert 'def definition_list_response(' in definition_http
    assert 'def definition_entry_response(' in definition_http
    assert 'def definition_write_response(' in definition_http
    assert 'def definition_delete_response(' in definition_http
    assert definition_routes.count('orchestration_service_response(') == 2
    assert definition_routes.count(
        'orchestration_definition_write_service_response(') == 2
    assert definition_routes.count(
        'orchestration_definition_delete_service_response(') == 1
    assert definition_service_http.count(
        'orchestration_service_response(') == 2
    assert 'project_definition_entry(' not in definition_routes
    assert 'project_definition_list(' not in definition_routes
    assert 'result.valid' not in definition_routes
    assert 'result.conflict' not in definition_routes
    assert 'result.current_updated_at' not in definition_routes
    assert definition_http.count('project_definition_entry(') == 2
    assert definition_http.count('project_definition_list(') == 1
    assert definition_http.count('invalid_definition_response(') == 2
    assert run_http.count('invalid_definition_response(') == 1
    assert authoring_routes.count('authoring_definition_response(') == 1
    assert authoring_routes.count('authoring_builtin_response(') == 1
    assert authoring_routes.count('authoring_compose_response(') == 1
    assert runtime_routes.count('authoring_plan_response(') == 1
    assert 'def authoring_definition_response(' in authoring_http
    assert 'def authoring_builtin_response(' in authoring_http
    assert 'def authoring_compose_response(' in authoring_http
    assert 'def authoring_plan_response(' in authoring_http
    assert 'def prepare_compose_request(' in authoring_http
    assert 'api_payload(result)' in authoring_http
    assert authoring_routes.count('prepare_compose_request(') == 1
    assert 'role-schema' not in authoring_routes
    assert 'role_contract' not in authoring_http
    assert authoring_routes.count("'schema': _COMPOSE_SCHEMA") == 1
    assert authoring_routes.count(
        "'schema': _DEFINITION_SELECTION_SCHEMA") == 1
    assert "optional_str(\n            body, 'requirement'" not in authoring_routes
    assert 'definition=definition' not in authoring_routes
    assert 'definitionSource=resolved.source' not in authoring_routes
    assert definition_request_http.count('resolve_definition(body)') == 1
    assert definition_request_http.count(
        'definition_selection_contract()') == 1
    assert "'inlineField': 'definition'" in definition_selection
    assert "'storedIdField': 'id'" in definition_selection
    assert authoring_routes.count('resolve_definition_request(') == 1
    assert runtime_routes.count('resolve_definition_request(') == 1
    assert run_http.count('resolve_definition_request(') == 1
    assert runtime_routes.count(
        "'schema': _DEFINITION_SELECTION_SCHEMA") == 1
    assert runtime_routes.count("'schema': _RUN_START_SCHEMA") == 1
    assert task_routes.count("'schema': _RUN_START_SCHEMA") == 1
    assert 'resolve_definition(parse_body())' not in (
        authoring_routes + runtime_routes)
    assert 'definition or id is required' not in (
        authoring_routes + runtime_routes + run_http)
    assert ('Invalid orchestration definition' not in
            definition_routes + run_http)
    assert '_NODE_SCHEMA = {' not in (
        route + definition_routes + authoring_routes + runtime_routes)
    assert 'execute_runtime_flow(' not in (
        route + runtime_routes + task_routes + mutation_routes)
    assert 'spawn_runtime_flow(' not in runtime_routes + task_routes
    assert runtime_start_service.count('spawn_runtime_flow(') == 2
    assert "runtime_start_request_response(\n            " \
        "'api_v1.orchestrations.start_run'" in runtime_routes
    assert "runtime_start_request_response(\n            " \
        "'api_v1.orchestrations.start_task'" in task_routes
    assert runtime_start_http.count('runtime_start_service().start(') == 1
    assert '.start_ephemeral(' not in runtime_routes + task_routes
    assert '.start_durable(' not in runtime_routes + task_routes
    assert 'runtime.create(' not in runtime_routes + task_routes + mutation_routes
    assert 'runtime.spawn(' not in runtime_routes + task_routes + mutation_routes
    assert 'prepare_run_request(' not in runtime_routes + task_routes
    assert 'run_start_response(' not in runtime_routes + task_routes
    assert runtime_start_http.count('prepare_run_request(') == 1
    assert runtime_start_http.count('run_start_response(') == 1
    assert 'run_start_response_fields(' not in runtime_routes + task_routes
    assert run_http.count('def run_start_response(') == 1
    assert run_http.count('def run_start_response_fields(') == 1
    assert run_http.count('prepare_definition(') == 1
    assert 'prepare_definition(' not in runtime_routes + task_routes
    assert 'from lib.orchestration.runtime_service import' not in (
        runtime_routes + task_routes)
    assert 'FlowEventSink(' not in (
        route + runtime_routes + task_routes + mutation_routes)
    assert 'finish_runtime(' not in (
        route + runtime_routes + task_routes + mutation_routes)
    assert 'inspect_definition(' not in runtime_routes
    assert 'inspection_response_fields(' not in runtime_routes
    assert runtime_routes.count('authoring_service().plan(definition)') == 1
    mutation_service_http = open(
        'routes/api_v1/orchestration_mutation_service_http.py',
        encoding='utf-8').read()
    assert 'mutation_http_response(' not in mutation_routes
    assert mutation_service_http.count('mutation_http_response(') == 1
    assert 'mutation_response(' not in mutation_routes
    assert 'api_payload(' not in mutation_routes
    assert mutation_http.count('mutation_response(') == 1
    assert mutation_http.count('api_payload(') == 1
    assert task_routes.count('orchestration_service_response(') == 3
    assert runtime_routes.count('orchestration_service_response(') == 1
    assert task_routes.count('prepare_durable_run_list_query(') == 1
    assert task_routes.count('durable_replay_cursor(') == 1
    assert task_routes.count('durable_run_list_response(') == 1
    assert task_routes.count('durable_run_entry_response(') == 1
    assert task_routes.count('durable_replay_response(') == 1
    assert task_http.count('task_replay_response(payload)') == 1
    assert 'RUN_STATUS_ORDER' not in task_routes
    assert 'safe_replay_cursor' not in task_routes
    assert 'api_payload(' not in task_routes
    assert mutation_routes.count('prepare_human_approval_request(') == 1
    assert mutation_routes.count('prepare_human_input_request(') == 1
    assert mutation_routes.count('human_gate_service().approve(') == 1
    assert mutation_routes.count('human_gate_service().input(') == 1
    assert mutation_routes.count(
        'orchestration_mutation_service_response(') == 5
    assert 'from lib.tasks_pkg import' not in mutation_routes
    assert mutation_routes.count("'schema': _HUMAN_APPROVAL_SCHEMA") == 1
    assert mutation_routes.count("'schema': _HUMAN_INPUT_SCHEMA") == 1
    assert "optional_str(\n            body, 'requestId'" not in mutation_routes
    assert 'register_orchestration_definition_routes(' in route
    assert 'register_orchestration_authoring_routes(' in route
    assert 'register_orchestration_runtime_routes(' in route
    assert 'runtime_start_service=_services.runtime_starts' in route
    assert route.count('runtime_start_service=_services.runtime_starts') == 2
    assert 'register_orchestration_task_routes(' in route
    assert 'register_orchestration_mutation_routes(' in route
    assert 'human_gate_service=_services.human_gates' in route
    assert 'runtime_mutation_service=_services.runtime_mutations' in route
    assert 'definition_service=_services.definitions' in route
    assert route.count('run_service=_services.runs') == 2
    assert 'return _definitions()' in route
    assert 'return _run_instances()' in route
    assert 'lambda: _definitions()' not in route
    assert 'lambda: _run_instances()' not in route
    assert route.count('\n') < 165
    assert definition_routes.count('\n') < 180
    assert definition_http.count('\n') < 180
    assert authoring_http.count('\n') < 180
    assert definition_request_http.count('\n') < 80
    assert authoring_routes.count('\n') < 240
    assert runtime_routes.count('\n') < 145
    assert task_routes.count('\n') < 220
    assert mutation_routes.count('\n') < 220
    assert mutation_http.count('\n') < 150
    assert task_http.count('\n') < 150
    assert task_list_http.count('\n') < 130
    assert run_http.count('\n') < 110
    assert 'DefinitionResolver = Callable[[dict], ResolvedDefinitionPort]' \
        in application_provider_ports
    assert 'OrchestrationApplicationServices(' in route
    assert route.count('resolve_definition=_services.resolve_definition') == 3
    assert ('DefinitionServiceProvider = Callable['
            '[], DefinitionServicePort]') in application_provider_ports
    assert ('RunServiceProvider = Callable[[], RunServicePort]'
            in application_provider_ports)
    assert ('RuntimeStartServiceProvider = Callable['
            '[], RuntimeStartServicePort]') in application_provider_ports
    assert ('AuthoringServiceProvider = Callable['
            '[], AuthoringServicePort]') in application_provider_ports
    assert 'class ResolvedDefinitionPort(Protocol)' in application_result_ports
    assert 'class AuthoringServicePort(Protocol)' in application_service_ports
    assert 'class DefinitionWriteResultPort(Protocol)' \
        in application_result_ports
    assert 'class DefinitionDeleteResultPort(Protocol)' \
        in application_result_ports
    assert 'class DurableReplayResultPort(Protocol)' \
        in application_result_ports
    assert 'class DefinitionServicePort(Protocol)' in application_service_ports
    assert 'class RunServicePort(' in application_service_ports
    assert 'OrchestrationDurableRunPort,' in application_service_ports
    assert 'class RuntimeStartServicePort(Protocol)' \
        in application_service_ports
    assert 'class RuntimeMutationServicePort(Protocol)' \
        in application_service_ports
    runtime_start_port = application_service_ports.split(
        'class RuntimeStartServicePort(Protocol):', 1)[1].split(
            'class HumanGateServicePort(Protocol):', 1)[0]
    assert runtime_start_port.count('def start(') == 1
    assert 'def start_ephemeral(' not in runtime_start_port
    assert 'def start_durable(' not in runtime_start_port
    assert '-> Any' not in (
        application_result_ports + application_service_ports
        + application_provider_ports)
    assert 'from lib.orchestration.definition_service import' \
        not in definition_http
    assert 'def _query_string(' not in task_http + task_list_http
    assert 'query_str(args,' in task_list_http
    assert 'authoring_service=_services.authoring' in route
    assert route.count('authoring_service=_services.authoring') == 2
    assert 'from lib.orchestration.service import' not in authoring_routes
    for adapter in (
        definition_routes, authoring_routes, runtime_routes, task_routes,
        mutation_routes,
    ):
        assert 'from lib.orchestration.application_provider_ports import' \
            in adapter
        assert 'orchestration_route_ports' not in adapter
        assert 'DefinitionServiceProvider = Callable[' not in adapter
    assert 'definition_service: Callable[[], Any]' not in task_routes
    assert 'run_service: Callable[[], Any]' not in task_routes


def test_application_services_are_physically_split():
    authoring_service = open(
        'lib/orchestration/authoring_service.py', encoding='utf-8').read()
    definition_inspection = open(
        'lib/orchestration/definition_inspection.py', encoding='utf-8').read()
    authoring_operations = open(
        'lib/orchestration/authoring_operations.py', encoding='utf-8').read()
    authoring_results = open(
        'lib/orchestration/authoring_results.py', encoding='utf-8').read()
    definition_service = open(
        'lib/orchestration/definition_service.py', encoding='utf-8').read()
    definition_results = open(
        'lib/orchestration/definition_results.py', encoding='utf-8').read()
    definition_resolution = open(
        'lib/orchestration/definition_resolution.py', encoding='utf-8').read()
    definition_store_port = open(
        'lib/orchestration/definition_store_port.py',
        encoding='utf-8',
    ).read()
    runtime_service = open(
        'lib/orchestration/runtime_service.py', encoding='utf-8').read()
    runtime_outcome = open(
        'lib/orchestration/runtime_outcome.py', encoding='utf-8').read()
    runtime_event_sink = open(
        'lib/orchestration/runtime_event_sink.py', encoding='utf-8').read()
    durable_projection = open(
        'lib/orchestration/durable_projection.py', encoding='utf-8').read()
    runtime_start_service = open(
        'lib/orchestration/runtime_start_service.py', encoding='utf-8').read()
    runtime_start_recovery = open(
        'lib/orchestration/runtime_start_recovery.py', encoding='utf-8').read()

    assert 'class OrchestrationAuthoringService' in authoring_service
    assert 'def compose_definition(' not in authoring_service
    assert 'def layout_authoring_definition(' not in authoring_service
    assert 'def plan_authoring_definition(' not in authoring_service
    assert 'def compose_definition(' in authoring_operations
    assert 'def layout_authoring_definition(' in authoring_operations
    assert 'def plan_authoring_definition(' in authoring_operations
    assert 'class AuthoringPlanResult' in authoring_results
    assert 'class AuthoringBuiltinResult' in authoring_results
    assert 'class AuthoringPlanResult' not in authoring_service
    assert 'OrchestrationStore' not in authoring_service
    assert 'from lib.orchestration.authoring_operations import (' \
        in authoring_service
    assert 'class PreparedDefinition' in definition_inspection
    assert 'def inspect_definition(' in definition_inspection
    assert 'def prepare_definition(' in definition_inspection
    assert 'OrchestrationStore' not in definition_inspection
    assert 'class PreparedDefinition' not in definition_service
    assert 'def inspect_definition(' not in definition_service
    assert 'def prepare_definition(' not in definition_service
    assert 'class ResolvedDefinition' in definition_results
    assert 'class DefinitionWriteResult' in definition_results
    assert 'class DefinitionDeleteResult' in definition_results
    assert 'def resolve_definition(' in definition_resolution
    assert 'class ResolvedDefinition' not in definition_service
    assert 'class DefinitionWriteResult' not in definition_service
    assert 'class DefinitionDeleteResult' not in definition_service
    assert 'def resolve_definition(' not in definition_service
    assert 'class OrchestrationDefinitionService' in definition_service
    assert 'bind_orchestration_definition_store(repository)' \
        in definition_service
    assert 'getattr(' not in definition_service
    assert 'class OrchestrationDefinitionStorePort(Protocol)' \
        in definition_store_port
    assert 'class _LegacyDefinitionStoreAdapter' not in definition_store_port
    assert 'def bind_orchestration_definition_store(' \
        in definition_store_port
    assert 'from lib.orchestration.errors import DefinitionServiceError' \
        in definition_service
    assert 'def _repository_call(' in definition_service
    assert 'class FlowEventSink' not in definition_service
    assert 'def execute_runtime_flow(' not in definition_service
    assert 'class FlowEventSink' not in runtime_service
    assert 'class FlowEventSink' in runtime_event_sink
    assert 'class FlowRunOutcome' not in runtime_service
    assert 'class FlowRunOutcome' in runtime_outcome
    assert 'def failure_outcome(' in runtime_outcome
    assert 'def aborted_race_outcome(' in runtime_outcome
    assert 'from lib.orchestration.runtime_event_sink import FlowEventSink' \
        in runtime_service
    assert 'from lib.orchestration.runtime_outcome import (' \
        in runtime_service
    assert 'class DurableRunProjection' not in runtime_service
    assert 'class DurableRunProjection' in durable_projection
    assert 'def project_event(' in durable_projection
    assert 'def append_event(' not in durable_projection
    assert 'def finalize(' in durable_projection
    assert 'def record_error(' in durable_projection
    assert 'def execute_runtime_flow(' in runtime_service
    assert 'def spawn_runtime_flow(' in runtime_service
    assert 'class OrchestrationRuntimeStartService' in runtime_start_service
    assert runtime_start_service.count('spawn_runtime_flow(') == 2
    assert 'def start(' in runtime_start_service
    assert 'def _record_start_failure(' not in runtime_start_service
    assert 'def recover_failed_durable_start(' in runtime_start_recovery
    assert 'class OrchestrationDefinitionService' not in runtime_service
    assert 'OrchestrationStore' not in runtime_service
    assert 'from lib.orchestration.run_service' not in runtime_service
    assert 'from lib.orchestration.mutation_result import MUTATION_CONFLICT' \
        in durable_projection
    assert 'from lib.orchestration.mutation_result import MUTATION_CONFLICT' \
        not in runtime_service
    assert len(runtime_service.splitlines()) < 200
    assert len(runtime_outcome.splitlines()) < 110
    assert len(runtime_event_sink.splitlines()) < 80


def test_graph_build_responsibilities_are_physically_split():
    builtins = open(
        'lib/orchestration/_builtin_definitions.py', encoding='utf-8').read()
    projection = open(
        'lib/orchestration/_chat_projection.py', encoding='utf-8').read()
    expansion = open(
        'lib/orchestration/_subflow_expansion.py', encoding='utf-8').read()
    builtin_registry = open(
        'lib/orchestration/authoring_builtin_registry.py',
        encoding='utf-8').read()
    inspection = open(
        'lib/orchestration/definition_inspection.py', encoding='utf-8').read()
    plan = open('lib/orchestration_plan.py', encoding='utf-8').read()

    assert 'def build_autopilot_definition(' in builtins
    assert 'def build_fanout_definition(' in builtins
    assert 'def build_adversarial_definition(' in builtins
    assert 'def chat_projection_for_flow(' not in builtins
    assert 'def expand_subflows(' not in builtins
    assert 'def chat_projection_for_flow(' in projection
    assert 'def expand_subflows(' in expansion
    assert 'from lib.orchestration._builtin_definitions import (' \
        in builtin_registry
    assert 'from lib.orchestration._chat_projection import ' \
        'chat_projection_for_flow' in inspection
    assert 'from lib.orchestration._subflow_expansion import expand_subflows' \
        in plan
    assert builtins.count('\n') < 230
    assert projection.count('\n') < 50
    assert expansion.count('\n') < 150


def test_definition_request_schema_uses_canonical_domain_vocabulary():
    first = definition_request_schema()
    node = first['properties']['nodes']['items']
    edge = first['properties']['edges']['items']

    assert node['properties']['type']['enum'] == [
        'role', 'subflow', 'control',
    ]
    assert set(node['properties']['kind']['enum']) == {
        'start', 'stop', 'loop', 'parallel', 'barrier', 'branch',
        'artifact', 'human',
    }
    assert edge['required'] == ['from', 'to']
    first['properties']['nodes']['items']['required'].append('mutated')
    assert definition_request_schema()['properties']['nodes']['items'][
        'required'] == ['id', 'type']


def test_spawn_runtime_flow_owns_live_and_durable_task_wiring(monkeypatch):
    class AbortEvent:
        def is_set(self):
            return False

    class Runtime:
        def __init__(self, generated_id):
            self.generated_id = generated_id
            self.created = []
            self.spawned = []

        def create(self, *, user_id, task_id='', meta=None):
            runtime_task_id = task_id or self.generated_id
            self.created.append((user_id, task_id, meta))
            return {'id': runtime_task_id, 'abort_event': AbortEvent()}

        def spawn(self, task_id, worker):
            self.spawned.append(task_id)
            worker()

    executions = []

    def fake_execute(runtime, task_id, definition, **options):
        executions.append((runtime, task_id, definition, options))

    monkeypatch.setattr(
        runtime_module, 'execute_runtime_flow', fake_execute)
    def resolver(name):
        return {'name': name}

    resolver_reads = []

    def resolver_provider():
        resolver_reads.append('read')
        return resolver

    live_runtime = Runtime('live-1')
    live_id = spawn_runtime_flow(
        live_runtime,
        _linear_definition(),
        owner_user_id=41,
        meta={'name': 'Linear'},
        initial_context='start live',
        subflow_resolver_provider=resolver_provider,
    )
    assert live_id == 'live-1'
    assert live_runtime.created == [(41, '', {'name': 'Linear'})]
    assert live_runtime.spawned == ['live-1']
    live_options = executions[0][3]
    assert live_options['initial_context'] == 'start live'
    assert live_options['abort_check']() is False
    assert live_options['subflow_resolver'] is resolver
    assert resolver_reads == ['read']
    assert live_options['durable_runs'] is None
    assert live_options['durable_run_id'] == ''

    durable_runs = object()
    durable_runtime = Runtime('unused')
    durable_id = spawn_runtime_flow(
        durable_runtime,
        _linear_definition(),
        owner_user_id=41,
        task_id='run-1',
        meta={'run_id': 'run-1'},
        initial_context='start durable',
        durable_runs=durable_runs,
    )
    assert durable_id == 'run-1'
    assert durable_runtime.created == [
        (41, 'run-1', {'run_id': 'run-1'}),
    ]
    assert durable_runtime.spawned == ['run-1']
    durable_options = executions[1][3]
    assert durable_options['durable_runs'] is durable_runs
    assert durable_options['durable_run_id'] == 'run-1'

    with pytest.raises(ValueError, match='requires a task_id'):
        spawn_runtime_flow(
            Runtime('orphan'), _linear_definition(),
            owner_user_id=41,
            durable_runs=object(),
        )


def test_runtime_flow_pipeline_unifies_live_and_durable_projection(monkeypatch):
    class Runtime:
        def __init__(self):
            self.events = []
            self.finished = []

        def append_event(self, task_id, event):
            self.events.append((task_id, event))
            return len(self.events) - 1

        def finish(self, task_id, **values):
            self.finished.append((task_id, values))

    class Durable:
        def __init__(self):
            self.events = []
            self.statuses = []

        def project_event(self, run_id, seq, event, status=''):
            self.events.append((run_id, seq, event['type'], status))
            return True

        def transition_status(self, run_id, status, **values):
            self.statuses.append((run_id, status, values))
            return RunMutationResult(True, run_status=status)

    def fake_execute(definition, **options):
        assert definition['name'] == 'Linear'
        assert options['initial_context'] == 'go'
        assert options['abort_check']() is False
        assert options['executor_options']['human_gate_scope'] == 'task-1'
        assert options['executor_options']['subflow_resolver']('child') == {
            'name': 'child',
        }
        options['on_event']({'type': 'flow_start'})
        options['on_event']({'type': 'step_delta', 'delta': 'token'})
        options['on_event']({'type': 'flow_complete'})
        return FlowRunOutcome({
            'ok': True, 'status': 'completed', 'final': 'finished',
        })

    monkeypatch.setattr(runtime_module, 'execute_flow', fake_execute)
    runtime = Runtime()
    durable = Durable()
    outcome = execute_runtime_flow(
        runtime, 'task-1', _linear_definition(),
        owner_user_id=1,
        initial_context='go', abort_check=lambda: False,
        subflow_resolver=lambda name: {'name': name},
        durable_runs=durable, durable_run_id='run-1',
    )

    assert outcome.lifecycle_status == 'done'
    assert [event[1]['type'] for event in runtime.events] == [
        'flow_start', 'step_delta', 'flow_complete',
    ]
    assert durable.events == [
        ('run-1', 0, 'flow_start', 'running'),
        ('run-1', 2, 'flow_complete', ''),
    ]
    assert [status for _, status, _ in durable.statuses] == [
        'done',
    ]
    assert durable.statuses[-1][2]['final'] == 'finished'
    assert runtime.finished[0][0] == 'task-1'
    assert runtime.finished[0][1]['result']['final'] == 'finished'


def test_runtime_flow_turns_durable_event_loss_into_typed_failure(monkeypatch):
    class Runtime:
        def __init__(self):
            self.events = []
            self.finished = []

        def append_event(self, task_id, event):
            self.events.append((task_id, event))
            return len(self.events) - 1

        def finish(self, task_id, **values):
            self.finished.append((task_id, values))

    class Durable:
        def __init__(self):
            self.statuses = []

        def project_event(self, _run_id, _seq, _event, _status=''):
            return False

        def transition_status(self, run_id, status, **values):
            self.statuses.append((run_id, status, values))
            return RunMutationResult(True, run_status=status)

    class EmittingExecutor:
        def __init__(self, on_event):
            self.on_event = on_event

        def run(self, **_kwargs):
            self.on_event({'type': 'flow_start'})
            return {'ok': True, 'status': 'completed', 'final': 'impossible'}

    monkeypatch.setattr(
        runtime_module,
        'create_flow_executor',
        lambda _definition, **options: EmittingExecutor(options['on_event']),
    )
    runtime = Runtime()
    durable = Durable()
    outcome = execute_runtime_flow(
        runtime, 'task-loss', _linear_definition(),
        owner_user_id=1,
        durable_runs=durable, durable_run_id='run-loss',
    )

    assert outcome.lifecycle_status == 'error'
    assert outcome.failure_kind == 'persistence'
    assert isinstance(outcome.exception, DurableProjectionError)
    assert durable.statuses[-1][1] == 'error'
    runtime_error = runtime.finished[-1][1]['error']
    assert runtime_error['kind'] == 'generic'
    assert runtime_error['detail'].startswith('DurableProjectionError:')
    assert runtime_error['outcome']['category'] == 'failure'


def test_runtime_flow_fails_closed_when_live_event_has_no_sequence(monkeypatch):
    class Runtime:
        def __init__(self):
            self.finished = []

        def append_event(self, _task_id, _event):
            return None

        def finish(self, task_id, **values):
            self.finished.append((task_id, values))

    class Durable:
        def __init__(self):
            self.events = []
            self.statuses = []

        def project_event(self, run_id, seq, event, status=''):
            self.events.append((run_id, seq, event, status))
            return True

        def transition_status(self, run_id, status, **values):
            self.statuses.append((run_id, status, values))
            return RunMutationResult(True, run_status=status)

    class EmittingExecutor:
        def __init__(self, on_event):
            self.on_event = on_event

        def run(self, **_kwargs):
            self.on_event({'type': 'flow_start'})
            return {'ok': True, 'status': 'completed', 'final': 'lost'}

    monkeypatch.setattr(
        runtime_module,
        'create_flow_executor',
        lambda _definition, **options: EmittingExecutor(options['on_event']),
    )
    runtime = Runtime()
    durable = Durable()

    outcome = execute_runtime_flow(
        runtime,
        'task-no-seq',
        _linear_definition(),
        owner_user_id=1,
        durable_runs=durable,
        durable_run_id='run-no-seq',
    )

    assert outcome.lifecycle_status == 'error'
    assert outcome.failure_kind == 'persistence'
    assert isinstance(outcome.exception, DurableProjectionError)
    assert 'did not assign a sequence' in str(outcome.exception)
    assert durable.events == []
    assert durable.statuses[-1][1] == 'error'
    assert runtime.finished[-1][1]['result']['ok'] is False


def test_runtime_flow_fails_closed_when_terminal_projection_is_rejected(
        monkeypatch):
    class Runtime:
        def __init__(self):
            self.finished = []

        def append_event(self, _task_id, _event):
            return 0

        def finish(self, task_id, **values):
            self.finished.append((task_id, values))

    class Durable:
        def __init__(self):
            self.transitions = []

        def project_event(self, _run_id, _seq, _event, _status=''):
            return True

        def transition_status(self, run_id, status, **values):
            self.transitions.append((run_id, status, values))
            if status == 'done':
                return RunMutationResult(
                    False,
                    RUN_MUTATION_PERSISTENCE_FAILED,
                    run_status='running',
                )
            return RunMutationResult(True, run_status=status)

    monkeypatch.setattr(
        runtime_module,
        'execute_flow',
        lambda *_args, **_values: FlowRunOutcome({
            'ok': True, 'status': 'completed', 'final': 'not durable',
        }),
    )
    runtime = Runtime()
    durable = Durable()

    outcome = execute_runtime_flow(
        runtime, 'task-finalize', _linear_definition(),
        owner_user_id=1,
        durable_runs=durable, durable_run_id='run-finalize',
    )

    assert outcome.lifecycle_status == 'error'
    assert outcome.failure_kind == 'persistence'
    assert isinstance(outcome.exception, DurableProjectionError)
    assert [status for _, status, _ in durable.transitions] == [
        'done', 'error',
    ]
    assert runtime.finished[-1][1]['result']['ok'] is False
    runtime_error = runtime.finished[-1][1]['error']
    assert runtime_error['kind'] == 'generic'
    assert runtime_error['detail'].startswith('DurableProjectionError:')
    assert runtime_error['outcome']['category'] == 'failure'


def test_runtime_flow_aligns_with_persisted_abort_that_wins_terminal_race(
        monkeypatch):
    class Runtime:
        def __init__(self):
            self.finished = []

        def append_event(self, _task_id, _event):
            return 0

        def finish(self, task_id, **values):
            self.finished.append((task_id, values))

    class Durable:
        def project_event(self, _run_id, _seq, _event, _status=''):
            return True

        def transition_status(self, _run_id, status, **_values):
            assert status == 'done'
            return RunMutationResult(
                False,
                RUN_MUTATION_CONFLICT,
                run_status='aborted',
            )

    monkeypatch.setattr(
        runtime_module,
        'execute_flow',
        lambda *_args, **_values: FlowRunOutcome({
            'ok': True, 'status': 'completed', 'final': 'too late',
        }),
    )
    runtime = Runtime()

    outcome = execute_runtime_flow(
        runtime, 'task-race', _linear_definition(),
        owner_user_id=1,
        durable_runs=Durable(), durable_run_id='run-race',
    )

    assert outcome.lifecycle_status == 'aborted'
    assert outcome.failure_kind == 'aborted'
    assert outcome.result['stop_reason'] == 'aborted'
    assert outcome.result['final'] == ''
    assert runtime.finished[-1][1]['result']['status'] == 'aborted'
    assert runtime.finished[-1][1]['error'] is None


def test_shared_resolver_has_one_precedence_and_copies_inline():
    inline = _linear_definition('Inline')
    resolved = resolve_definition(
        inline=inline,
        builtin='autopilot',
        stored_id='orch_saved',
        load_stored=lambda _id: _linear_definition('Stored'),
    )
    assert resolved.source == 'inline'
    assert resolved.definition is not inline
    resolved.definition['name'] = 'changed'
    assert inline['name'] == 'Inline'

    builtin = resolve_definition(builtin='autopilot')
    assert builtin.source == 'builtin:autopilot'
    assert any(n.get('role') == 'virtual_user'
               for n in builtin.definition['nodes'])

    stored = resolve_definition(
        stored_id='orch_saved',
        load_stored=lambda _id: _linear_definition('Stored'),
    )
    assert stored.source == 'stored:orch_saved'


def test_inspection_is_the_single_authoring_execution_contract():
    inspection = inspect_definition(build_autopilot_definition())
    assert inspection['format'] == INSPECTION_FORMAT
    assert inspection['ok'] is True
    assert inspection['diagnostics'] == []
    assert inspection['contract']['schema'] == 'tofu.orchestration/v1'
    assert inspection['contract']['projection'] == 'autopilot'
    assert inspection['contract']['initialPhase'] == 'working'
    assert inspection['contract']['nodes'] > 0


def test_inspection_projects_structured_and_rolling_response_fields():
    definition = _linear_definition()
    definition['name'] = ''
    definition['nodes'][1]['role'] = 'future-role'
    inspection = inspect_definition(definition)

    assert inspection['ok'] is False
    assert [item['message'] for item in inspection['diagnostics']] == [
        *inspection['errors'], *inspection['warnings']]
    assert [item['severity'] for item in inspection['diagnostics']] == [
        *('error' for _ in inspection['errors']),
        *('warning' for _ in inspection['warnings']),
    ]
    assert all(item['code'] for item in inspection['diagnostics'])
    assert all('path' in item for item in inspection['diagnostics'])
    fields = inspection_response_fields(inspection, include_errors=True)
    assert fields['inspection'] == inspection
    assert fields['errors'] == inspection['errors']
    assert fields['warnings'] == inspection['warnings']
    assert fields['contract'] == inspection['contract']

    fields['inspection']['diagnostics'].clear()
    fields['contract']['nodes'] = 999
    assert inspection['diagnostics']
    assert inspection['contract']['nodes'] != 999


def test_authoring_contract_is_complete_and_returns_detached_snapshots():
    contract = authoring_contract()

    assert tuple(authoring_module.authoring_object_sections()) == \
        authoring_module.AUTHORING_OBJECT_SECTION_NAMES
    assert set(authoring_module.AUTHORING_OBJECT_SECTION_NAMES) < set(contract)
    assert contract['contractSections'] == {
        'authoring': list(authoring_module.AUTHORING_OBJECT_SECTION_NAMES),
        'runtime': list(authoring_module.RUNTIME_CONTRACT_SECTION_NAMES),
        'rollingOptionalFields':
            authoring_module.rolling_optional_section_fields(),
    }
    contract['contractSections']['rollingOptionalFields'][
        'runContract'].append('client-only')
    assert 'client-only' not in authoring_module.authoring_contract()[
        'contractSections']['rollingOptionalFields']['runContract']

    assert contract['format'] == AUTHORING_CONTRACT_FORMAT
    assert contract['schema'] == 'tofu.orchestration/v1'
    assert set(contract['roleNames']) >= {'worker', 'critic'}
    assert set(contract['controlSchemas']) == set(contract['controls'])
    assert contract['inspectionContract'] == {
        'format': INSPECTION_FORMAT,
        'responseFields': [
            'format', 'ok', 'errors', 'warnings', 'diagnostics', 'contract',
        ],
        'responseStringArrayFields': ['errors', 'warnings'],
        'diagnosticSeverities': ['error', 'warning'],
        'diagnosticFields': ['severity', 'code', 'path', 'message'],
        'diagnosticStringFields': ['code', 'path', 'message'],
        'diagnosticPathFormat': 'json-pointer',
        'contractFields': [
            'schema', 'projection', 'initialPhase', 'nodes', 'edges',
        ],
        'contractStringFields': ['schema', 'projection', 'initialPhase'],
        'contractNonNegativeIntegerFields': ['nodes', 'edges'],
    }
    assert contract['ioContract']['maxPorts'] == 12
    assert contract['eventContract']['schema'] == \
        'tofu.orchestration.events/v1'
    assert contract['eventContract']['types']['step_phase']['durable'] is False
    assert contract['eventContract']['previewLimits'] == {
        'wire': 200,
        'timeline': 120,
    }
    assert contract['runContract']['schema'] == \
        'tofu.orchestration.run-status/v1'
    assert contract['runContract']['terminal'] == ['done', 'error', 'aborted']
    assert contract['runContract']['categories'] == {
        'pending': 'queued',
        'running': 'active',
        'paused': 'blocked',
        'done': 'success',
        'error': 'failure',
        'aborted': 'cancelled',
    }
    assert contract['outcomeContract']['format'] == \
        'tofu.orchestration.outcome/v1'
    assert contract['outcomeContract']['categories'] == [
        'success', 'incomplete', 'failure', 'aborted',
    ]
    assert contract['outcomeContract']['displayLimits'] == {
        'final': 16000,
        'error': 4000,
    }
    assert contract['traceContract'] == {
        'format': 'tofu.orchestration.trace/v1',
        'historyLimit': 12,
        'statusMap': {
            'running': 'running',
            'completed': 'done',
            'failed': 'error',
            'done': 'done',
            'error': 'error',
        },
        'activityFields': {
            'stateChanging': 'state_changing',
            'exploratory': 'exploratory',
            'stateChangingTools': 'state_changing_tools',
        },
        'textLimits': {
            'brief': 8000,
            'input': 8000,
            'output': 16000,
            'thinking': 16000,
            'error': 4000,
        },
        'truncationFlags': {
            field: f'{field}_truncated'
            for field in ('brief', 'input', 'output', 'thinking', 'error')
        },
    }
    assert contract['mutationContract']['format'] == \
        'tofu.orchestration.mutation/v1'
    assert set(contract['mutationContract']['actions']) >= {
        'abort_run', 'delete_run', 'approve_gate', 'input_gate',
    }
    assert contract['mutationContract']['retryableReasons'] == [
        'persistence_failed',
    ]
    assert contract['mutationContract']['clientRetryableReasons'] == [
        'persistence_failed', 'transport_failed',
    ]
    assert contract['mutationContract']['transportFailureReason'] == \
        'transport_failed'
    assert contract['mutationContract']['httpStatusByReason'] == {
        'accepted': 200,
        'not_found': 404,
        'terminal': 409,
        'active': 409,
        'conflict': 409,
        'persistence_failed': 500,
    }
    assert contract['mutationContract']['reconcileField'] == \
        'reconcile_required'
    assert contract['mutationContract']['targetExistsField'] == \
        'target_exists'
    assert 'legacyTargetFields' not in contract['mutationContract']
    assert 'legacyStatusFields' not in contract['mutationContract']
    assert contract['replayContract'] == {
        'format': 'tofu.task-replay/v1',
        'httpStatuses': {
            'success': 200,
            'notFound': 404,
            'failure': 500,
        },
        'notFoundReason': 'not_found',
        'statusField': 'status',
        'nextCursorField': 'next_cursor',
        'pageFields': [
            'format', 'ok', 'events', 'next_cursor', 'status', 'done',
            'cursor',
        ],
        'cursor': {
            'queryField': 'cursor',
            'minimum': 0,
            'default': 0,
            'description': 'Producer-owned next event sequence.',
            'field': 'cursor',
            'requestedField': 'requested',
            'nextField': 'next',
            'resetField': 'reset',
            'unit': 'next event sequence',
            'producerOwned': True,
            'futureCursorReset': True,
        },
        'terminalField': 'done',
        'caughtUpField': 'caught_up',
        'eventsField': 'events',
        'eventTypeField': 'type',
        'eventSequenceField': 'seq',
        'eventRequiredFields': ['type', 'seq'],
        'unknownEventTypes': 'allow',
        'terminalEventTypes': ['done', 'error', 'aborted'],
        'terminalSnapshot': {
            'field': 'run',
            'when': {'field': 'done', 'equals': True},
            'optional': True,
        },
    }
    assert contract['fieldValueContract']['format'] == \
        'tofu.orchestration.field-value/v1'
    assert contract['fieldValueContract']['optionalEmpty'] == 'omit'
    assert contract['fieldValueContract']['failureCodes']['maxItems'] == \
        'field.max_items'
    assert contract['fieldValueContract']['failureCodes'][
        'invalidNumber'] == 'field.type.integer'
    assert contract['fieldValueContract']['kinds']['list'] == {
        'wire': 'array<string>',
        'editor': 'newline',
        'trimItems': True,
        'dropEmptyItems': True,
    }
    assert contract['definitionWriteContract'] == {
        'format': 'tofu.orchestration.definition-write/v1',
        'versionField': 'updatedAt',
        'versionResponseHeader': 'ETag',
        'preconditionHeader': 'If-Match',
        'tokenSyntax': 'quoted-decimal',
        'conflictStatus': 409,
        'conflictReason': 'stale_definition',
        'operations': ['replace', 'delete'],
        'conflictFields': {
            'format': {'name': 'format', 'type': 'string'},
            'reason': {'name': 'reason', 'type': 'string'},
            'operation': {'name': 'operation', 'type': 'string'},
            'expectedUpdatedAt': {
                'name': 'expectedUpdatedAt',
                'type': 'non_negative_integer',
            },
            'currentUpdatedAt': {
                'name': 'currentUpdatedAt',
                'type': 'non_negative_integer',
            },
        },
    }
    assert contract['definitionListContract'] == {
        'format': 'tofu.orchestration.definition-list/v1',
        'itemFields': [
            'id', 'name', 'nodeCount', 'createdAt', 'updatedAt',
        ],
        'definitionIncluded': False,
        'orderBy': [
            {'field': 'updatedAt', 'direction': 'desc'},
            {'field': 'createdAt', 'direction': 'desc'},
            {'field': 'id', 'direction': 'asc'},
        ],
    }
    assert contract['definitionEntryContract'] == {
        'format': 'tofu.orchestration.definition-entry/v1',
        'fields': [
            'id', 'name', 'definition', 'createdAt', 'updatedAt',
        ],
        'versionField': 'updatedAt',
        'versionRequiredOnWrite': True,
        'inspectionIncludedOnWrite': True,
    }
    assert contract['runtimeStartContract'] == {
        'format': 'tofu.orchestration.runtime-start/v1',
        'kinds': ['ephemeral', 'durable'],
        'idField': 'id',
        'kindField': 'kind',
        'successStatuses': {
            'ephemeral': 200,
            'durable': 201,
        },
    }
    assert contract['durableRunContract'] == {
        'format': 'tofu.orchestration.durable-run/v1',
        'idField': 'id',
        'statusField': 'status',
        'terminalField': 'terminal',
        'outcomeField': 'outcome',
        'listFields': [
            'id', 'orch_id', 'name', 'status', 'terminal', 'final', 'error',
            'created_by', 'created_at', 'updated_at', 'finished_at',
        ],
        'readFields': [
            'id', 'orch_id', 'name', 'status', 'terminal', 'final', 'error',
            'created_by', 'created_at', 'updated_at', 'finished_at',
            'definition', 'input',
        ],
        'optionalFields': ['outcome'],
        'listEnvelope': {
            'itemsField': 'runs',
            'pageField': 'page',
            'pageFields': ['limit', 'has_more', 'next_limit'],
            'limitField': 'limit',
            'hasMoreField': 'has_more',
            'nextLimitField': 'next_limit',
            'defaultLimit': 50,
            'pageStep': 50,
            'maxLimit': 150,
        },
    }
    assert set(contract['defaultEmits']) == set(contract['roleNames'])
    assert contract['executionOptions'] == {
        'tiers': ['light', 'standard', 'heavy'],
        'isolation': ['fresh-context', 'shared-context'],
        'emits': ['assistant', 'user'],
        'scopes': ['isolated', 'inline'],
    }
    defaults = contract['nodeDefaults']
    assert defaults['roles']['worker']['tier'] == \
        contract['personas']['worker']['tier']
    assert defaults['controls']['loop'] == {
        'max_iterations': 10,
        'stop_condition': 'verdict:STOP',
        'verifier': 'critic',
    }
    assert defaults['controls']['human']['mode'] == 'approve'
    assert defaults['controls']['artifact']['format'] == 'file'
    assert defaults['subflow']['scope'] == 'isolated'
    assert contract['nodeRuntimeDefaults'] == {
        'role': {
            'tier': 'standard',
            'isolation': 'fresh-context',
        },
        'controls': {
            'loop': {
                'max_iterations': 10,
                'stop_condition': 'verdict:STOP',
            },
            'human': {'mode': 'approve', 'timeout_sec': 300},
        },
        'subflow': {'scope': 'inline'},
    }

    contract['roles']['worker'][0]['key'] = 'client-mutation'
    contract['controls']['start']['single'] = False
    contract['ioContract']['types'].append('client-type')
    contract['nodeDefaults']['controls']['loop']['max_iterations'] = 999
    contract['nodeRuntimeDefaults']['subflow']['scope'] = 'mutated'
    contract['executionOptions']['tiers'].append('client-tier')
    contract['eventContract']['types']['flow_start']['timeline'] = False
    contract['eventContract']['previewLimits']['wire'] = 1
    contract['runContract']['terminal'].append('client-status')
    contract['runContract']['categories']['running'] = 'client-status'
    contract['outcomeContract']['displayLimits']['final'] = 1
    contract['traceContract']['textLimits']['output'] = 1
    contract['traceContract']['statusMap']['completed'] = 'error'
    contract['fieldValueContract']['failureCodes']['maxItems'] = 'mutated'
    contract['fieldValueContract']['kinds']['list']['wire'] = 'mutated'
    contract['definitionWriteContract']['preconditionHeader'] = 'mutated'
    contract['definitionListContract']['orderBy'][0]['field'] = 'mutated'
    contract['definitionEntryContract']['fields'].append('mutated')
    contract['runtimeStartContract']['kinds'].append('mutated')
    fresh = authoring_contract()
    assert fresh['roles']['worker'][0]['key'] == 'objective'
    assert fresh['controls']['start']['single'] is True
    assert 'client-type' not in fresh['ioContract']['types']
    assert fresh['nodeDefaults']['controls']['loop']['max_iterations'] == 10
    assert fresh['nodeRuntimeDefaults']['subflow']['scope'] == 'inline'
    assert 'client-tier' not in fresh['executionOptions']['tiers']
    assert fresh['eventContract']['types']['flow_start']['timeline'] is True
    assert fresh['eventContract']['previewLimits']['wire'] == 200
    assert 'client-status' not in fresh['runContract']['terminal']
    assert fresh['runContract']['categories']['running'] == 'active'
    assert fresh['outcomeContract']['displayLimits']['final'] == 16000
    assert fresh['traceContract']['textLimits']['output'] == 16000
    assert fresh['traceContract']['statusMap']['completed'] == 'done'
    assert fresh['fieldValueContract']['failureCodes']['maxItems'] == \
        'field.max_items'
    assert fresh['fieldValueContract']['kinds']['list']['wire'] == \
        'array<string>'
    assert fresh['definitionWriteContract']['preconditionHeader'] == 'If-Match'
    assert fresh['definitionListContract']['orderBy'][0]['field'] == 'updatedAt'
    assert 'mutated' not in fresh['definitionEntryContract']['fields']
    assert 'mutated' not in fresh['runtimeStartContract']['kinds']


def test_all_studio_templates_share_the_backend_builtin_registry():
    assert builtin_names() == (
        'autopilot', 'fanout', 'adversarial', 'blank',
    )
    for name in builtin_names():
        definition = build_builtin_definition(name)
        assert definition is not None
        if name != 'blank':
            assert validate_definition(definition)['ok'] is True
    assert build_builtin_definition('unknown') is None


def test_compose_definition_is_the_shared_authoring_entrypoint(monkeypatch):
    import lib.orchestration_composer as composer

    captured = {}
    response = None

    def fake_compose(requirement, *, current=None, history=None):
        nonlocal response
        captured.update(requirement=requirement, current=current,
                        history=history)
        response = {'ok': True, 'reply': 'done', 'definition': current}
        return response

    monkeypatch.setattr(composer, 'compose', fake_compose)
    current = {'schema': 'tofu.orchestration/v1', 'nodes': [], 'edges': []}
    history = [{'role': 'user', 'content': 'first'}]
    result = compose_definition('revise it', current=current, history=history)

    assert result['ok'] is True
    assert captured == {
        'requirement': 'revise it', 'current': current, 'history': history,
    }
    assert captured['current'] is not current
    assert captured['history'] is not history
    assert result is not response
    assert result['definition'] is not current
    assert 'inspection' not in response
    assert 'validation' not in response


def test_compose_route_uses_service_boundary_not_direct_implementation():
    route_source = open(
        'routes/api_v1/orchestration_authoring_routes.py', encoding='utf-8',
    ).read()
    start = route_source.index('def compose_orchestration')
    end = route_source.index(
        "@orchestration_route(blueprint, 'builtin')", start,
    )
    route = route_source[start:end]
    assert 'authoring_service().compose(' in route
    assert 'orchestration_composer' not in route
    assert 'lib.orchestration.service' not in route_source


def test_chat_autopilot_entrypoint_uses_service_registry():
    runner = open(
        'lib/orchestration_chat_flow_runner.py', encoding='utf-8',
    ).read()
    assert "_build_builtin('autopilot'" in runner
    assert 'from lib.orchestration import build_autopilot_definition' not in runner


def test_chat_runner_uses_normalized_execute_flow_boundary():
    runner = open(
        'lib/orchestration_chat_flow_runner.py', encoding='utf-8',
    ).read()
    runtime = open(
        'lib/orchestration_chat_flow_runtime.py', encoding='utf-8',
    ).read()
    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'outcome = execute_flow(' in runtime
    assert 'from lib.orchestration.runtime_service import execute_flow' \
        in runtime
    assert 'create_flow_executor(' not in runner + runtime
    assert 'FlowExecutionError' not in runner + runtime


def test_layout_authoring_definition_is_detached_and_route_uses_service():
    source = _linear_definition()
    arranged = layout_authoring_definition(source)

    assert all('pos' in node for node in arranged['nodes'])
    assert all('pos' not in node for node in source['nodes'])

    route_source = open(
        'routes/api_v1/orchestration_authoring_routes.py', encoding='utf-8',
    ).read()
    start = route_source.index('def layout_orchestration')
    end = route_source.index('\n\n__all__', start)
    route = route_source[start:end]
    assert 'authoring_service().layout(definition)' in route
    assert 'layout_definition(' not in route


def test_authoring_service_exposes_one_adapter_interface():
    service = OrchestrationAuthoringService()
    definition = _linear_definition()

    assert service.inspect(definition)['ok'] is True
    assert service.builtin('autopilot')['schema'] == 'tofu.orchestration/v1'
    assert service.builtin('unknown') is None
    assert service.contract()['format'] == AUTHORING_CONTRACT_FORMAT
    assert service.layout(definition) == layout_authoring_definition(definition)
    plan = service.plan(definition)
    assert plan == plan_authoring_definition(definition)
    assert plan.plan['ok'] is True
    assert any(step.get('action') == 'run-agent' for step in plan.plan['steps'])
    assert plan.inspection['ok'] is True
    assert 'plan' not in plan.inspection


def test_blank_subflow_authoring_default_is_valid_and_detached():
    defaults = node_authoring_defaults()
    blank = defaults['blankSubflow']
    assert validate_definition(blank)['ok'] is True
    assert blank['nodes'][1]['params'] == defaults['genericRole']

    blank['nodes'][1]['params']['tier'] = 'client-mutation'
    fresh = node_authoring_defaults()
    assert fresh['blankSubflow']['nodes'][1]['params']['tier'] == 'standard'


def test_event_sink_filters_deltas_and_maps_human_lifecycle():
    live, durable = [], []

    def append_live(event):
        live.append(dict(event))
        return len(live) - 1

    sink = FlowEventSink(
        append_live,
        durable_project=lambda seq, event, status: durable.append(
            (seq, dict(event), status)),
    )
    for event in (
        {'type': 'flow_start'},
        {'type': 'step_delta', 'chunk': 'x'},
        {'type': 'step_phase', 'phase': 'waiting_model'},
        {'type': 'human_request'},
        {'type': 'human_resolved'},
        {'type': 'step_trace'},
    ):
        sink(event)

    assert [event['type'] for event in live] == [
        'flow_start', 'step_delta', 'step_phase', 'human_request',
        'human_resolved', 'step_trace',
    ]
    assert [event['type'] for _, event, _ in durable] == [
        'flow_start', 'human_request', 'human_resolved', 'step_trace',
    ]
    assert [status for _, _, status in durable if status] == [
        'running', 'paused', 'running',
    ]


def test_event_sink_requires_a_live_sequence_only_for_durable_events():
    persisted = []
    sink = FlowEventSink(
        lambda _event: None,
        durable_project=lambda seq, event, status: persisted.append(
            (seq, event, status)),
    )

    sink({'type': 'step_delta', 'delta': 'transient'})
    with pytest.raises(DurableProjectionError, match='assign a sequence'):
        sink({'type': 'flow_start'})

    assert persisted == []


def test_every_engine_event_is_declared_in_the_runtime_contract():
    source = ''.join(
        open(path, encoding='utf-8').read()
        for path in (
            'lib/orchestration_engine.py',
            'lib/orchestration_agent_runner.py',
            'lib/orchestration_role_runtime.py',
            'lib/orchestration_subflow_runtime.py',
            'lib/orchestration_loop_runtime.py',
            'lib/orchestration_parallel_runtime.py',
            'lib/orchestration_branch_runtime.py',
            'lib/orchestration_replan_runtime.py',
            'lib/orchestration_execution_runtime.py',
            'lib/orchestration_trace.py',
        )
    )
    emitted = set(re.findall(
        r"self\._emit\(\{\s*'type':\s*'([^']+)'", source,
    ))
    assert {'flow_start', 'flow_complete', 'step_phase', 'step_delta',
            'step_trace', 'no_progress'} <= emitted
    declared = set(runtime_event_contract()['types'])
    assert emitted <= declared, f'undeclared engine events: {emitted - declared}'


@pytest.mark.parametrize(
    ('result', 'status', 'error'),
    [
        ({'ok': True, 'status': 'completed'}, 'done', ''),
        ({'ok': False, 'status': 'completed',
          'stop_reason': 'max_iterations'}, 'error', 'max_iterations'),
        ({'ok': False, 'status': 'aborted',
          'stop_reason': 'aborted'}, 'aborted', ''),
    ],
)
def test_outcome_terminal_mapping_is_honest(result, status, error):
    outcome = FlowRunOutcome(result)
    assert outcome.lifecycle_status == status
    assert outcome.error == error


def test_execute_flow_is_the_shared_executor_entrypoint():
    events = []
    outcome = execute_flow(
        _linear_definition(),
        initial_context='request',
        on_event=lambda event: events.append(dict(event)),
        executor_options={
            'agent_runner': lambda node, context, iteration: {
                'output': 'done', 'status': 'completed', 'error': '',
            },
        },
    )
    assert outcome.lifecycle_status == 'done'
    assert outcome.result['final'].endswith('[worker]\ndone')
    completed = [e for e in events if e['type'] == 'flow_complete'][-1]
    assert completed['ok'] is True
    assert completed['outcome']['format'] == \
        'tofu.orchestration.outcome/v1'
    assert completed['outcome']['category'] == 'success'
    assert completed['lifecycle_status'] == 'done'


@pytest.mark.parametrize(
    ('exc', 'failure_kind'),
    [
        (RuntimeError('runner crashed'), 'exception'),
        (pytest.param(None, 'structural', id='structural')),
    ],
)
def test_execute_flow_preserves_normalized_exception_kind(
        monkeypatch, exc, failure_kind):
    from lib.orchestration_engine import FlowExecutionError

    actual = exc or FlowExecutionError('invalid graph')

    def fail_create(*_args, **_kwargs):
        raise actual

    monkeypatch.setattr(runtime_module, 'create_flow_executor', fail_create)
    outcome = execute_flow(_linear_definition())

    assert outcome.lifecycle_status == 'error'
    assert outcome.exception is actual
    assert outcome.failure_kind == failure_kind
    assert outcome.result['stop_reason'] == failure_kind
