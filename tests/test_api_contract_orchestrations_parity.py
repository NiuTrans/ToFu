#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for orchestration HTTP adapters.

15 of the 16 sites are dict payloads → api_ok / api_created (byte-identical
when the lib result already carries ``ok``, additive +ok otherwise).

The 16th is the FIRST executed instance of docs/API_CONTRACT.md §4's
coordinated bare-array migration: ``GET /api/v1/orchestrations`` returned a
bare top-level ARRAY (``jsonify(_read_all())``). Enveloping changes the
top-level type, so it ships as ONE front+back change:

  * backend  → ``api_ok({'items': _read_all()})``
  * frontend ``Api.orchestrations.list`` unwraps ``.items`` — with an
    ``Array.isArray(d)`` fallback so a rolling-deploy skew (old server,
    new client) still yields the array callers expect. Every caller of
    ``list()`` keeps receiving a bare array: zero call-site change.

Layers:
  1. PARITY — legacy literal vs new call per dict site; for the list
     endpoint the array moves under ``items`` verbatim (+ok).
  2. FRONT+BACK COORDINATION — backend wraps, api.js unwraps with fallback.
  3. SHIPPED-SOURCE — no ``jsonify(`` / no flask jsonify import remains.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
from tests._runtime_sections import native_module_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGETS = tuple(
    os.path.join(_ROOT, 'routes', 'api_v1', name)
    for name in (
        'orchestrations.py',
        'orchestration_definition_routes.py',
        'orchestration_authoring_routes.py',
        'orchestration_authoring_http.py',
        'orchestration_authoring_action_openapi.py',
        'orchestration_authoring_openapi.py',
        'orchestration_openapi.py',
        'orchestration_definition_openapi.py',
        'orchestration_definition_request_http.py',
        'orchestration_endpoint_routes.py',
        'orchestration_runtime_routes.py',
        'orchestration_run_http.py',
        'orchestration_runtime_start_http.py',
        'orchestration_run_openapi.py',
        'orchestration_replay_openapi.py',
        'orchestration_task_openapi.py',
        'orchestration_task_routes.py',
        'orchestration_mutation_routes.py',
        'orchestration_mutation_http.py',
        'orchestration_mutation_service_http.py',
        'orchestration_mutation_openapi.py',
        'orchestration_task_http.py',
        'orchestration_task_list_http.py',
        '../task_http.py',
        '../_task_routes.py',
    )
)
_API_CLIENT_SOURCE = Path(
    _ROOT, 'frontend/src/features/orchestration/api-client.ts')
_REQUEST_CONTRACT_SOURCE = Path(
    _ROOT,
    'frontend/src/features/orchestration/request-contracts.generated.ts',
)
_API_CLIENT_JS = native_module_path(
    '.native/orchestration-api-client.js', _API_CLIENT_SOURCE,
)

pytestmark = pytest.mark.unit


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


async def _resolve(resp):
    response, status = resp
    body = await response.get_data(as_text=True)
    return status, (json.loads(body) if body else {})


def _sites():
    from lib.api_response import api_created, api_ok
    entry = {'id': 'orch_x', 'name': 'n', 'definition': {'nodes': []},
             'createdAt': 1, 'updatedAt': 2}
    verdict = {'ok': False, 'errors': ['e'], 'warnings': []}
    compose_result = {'ok': True, 'reply': 'r',
                      'definition': {'nodes': []}, 'validation': {'ok': True}}
    plan = {'ok': True, 'steps': [{'node': 'a'}]}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('get-one', dict(entry), 200, lambda: api_ok(dict(entry)), False),
        ('validate', dict(verdict), 200, lambda: api_ok(dict(verdict)),
         False),
        ('compose', dict(compose_result), 200,
         lambda: api_ok(dict(compose_result)), False),
        ('create-201', dict(entry, warnings=[]), 201,
         lambda: api_created(dict(entry, warnings=[])), False),
        ('update', dict(entry, warnings=[]), 200,
         lambda: api_ok(dict(entry, warnings=[])), False),
        ('builtin', {'ok': True, 'definition': {'nodes': []}}, 200,
         lambda: api_ok({'definition': {'nodes': []}}), False),
        ('layout', {'ok': True, 'definition': {'nodes': []}}, 200,
         lambda: api_ok({'definition': {'nodes': []}}), False),
        ('plan', dict(plan), 200, lambda: api_ok(dict(plan)), False),
        ('run', {'ok': True, 'task_id': 't1'}, 200,
         lambda: api_ok({'task_id': 't1'}), False),
        ('task-create-201', {'ok': True, 'run_id': 'r1'}, 201,
         lambda: api_created({'run_id': 'r1'}), False),
        ('task-list', {'ok': True, 'runs': [{'id': 'r1'}]}, 200,
         lambda: api_ok({'runs': [{'id': 'r1'}]}), False),
        ('task-get', {'ok': True, 'run': {'id': 'r1'}}, 200,
         lambda: api_ok({'run': {'id': 'r1'}}), False),
        ('task-events', {'ok': True, 'events': [], 'next_cursor': 0,
                         'status': 'done', 'done': True}, 200,
         lambda: api_ok({'events': [], 'next_cursor': 0,
                         'status': 'done', 'done': True}), False),
    ]


def test_envelope_parity():
    """status identical; legacy keys byte-identical; additions ⊆
    {ok, request_id}; ok flag correct per branch."""
    from quart import jsonify
    app = _make_app()

    async def _t():
        async with app.test_request_context('/test'):
            for label, legacy_body, legacy_status, new, is_error in _sites():
                leg_status, leg_body = await _resolve(
                    (jsonify(legacy_body), legacy_status))
                new_status, new_body = await _resolve(new())

                assert new_status == leg_status, (
                    f'{label}: status {new_status} != legacy {leg_status}')
                new_body.pop('request_id', None)
                for k, v in leg_body.items():
                    assert k in new_body and new_body[k] == v, (
                        f'{label}: legacy key {k!r} lost/changed')
                added = set(new_body) - set(leg_body)
                assert added <= {'ok'}, (
                    f'{label}: unexpected added keys {added}')
                # A lib-result passthrough carries its OWN ok (e.g. the
                # validate endpoint's logical-failure 200 with ok:False);
                # otherwise the envelope default applies (ok = not is_error).
                expected_ok = leg_body.get('ok', not is_error)
                assert new_body.get('ok') is expected_ok, (
                    f'{label}: ok flag {new_body.get("ok")} != '
                    f'expected {expected_ok}')

    asyncio.run(_t())


def test_bare_array_coordinated_migration():
    """The list endpoint: the array moves under ``items`` verbatim (+ok),
    and api.js unwraps ``.items`` with an ``Array.isArray`` fallback so
    callers still receive a bare array under either server generation."""
    from lib.api_response import api_ok
    app = _make_app()
    rows = [{'id': 'a'}, {'id': 'b'}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': rows}))
            assert s == 200
            assert body['ok'] is True
            assert body['items'] == rows, 'the array must move verbatim'

    asyncio.run(_t())

    src = _API_CLIENT_SOURCE.read_text(encoding='utf-8')
    contract = _REQUEST_CONTRACT_SOURCE.read_text(encoding='utf-8')
    assert 'const listResult = async' in src and 'methods.listResult' in src, (
        'Api.orchestrations must expose one status-preserving list read while '
        'keeping list() as the array compatibility facade')
    assert re.search(r'Array\.isArray\(body\)', src), (
        'the normalized list read must retain a bare-array fallback for '
        'rolling-deploy skew against a pre-migration server')
    assert "normalized ? { parse: 'response' }" in src, (
        'listResult must preserve HTTP status instead of folding failures '
        'into the empty-list state')
    assert 'ORCHESTRATION_REQUEST_CONTRACTS' in src
    assert 'for (const [endpoint, contract] of Object.entries(' in src
    assert contract.count(
        '/api/v1/orchestrations/authoring-contract') == 1
    assert '/api/v1/orchestrations/role-schema' not in contract
    assert '/api/v1/orchestrations' not in src
    assert 'methods.save = (' in src, (
        'Api.orchestrations.save must normalize create/update persistence')
    assert 'return normalized ? httpResult.normalize(response) : response' \
        in src
    assert 'if (contract.writeOperation)' in src
    assert 'args[Number(contract.writeVersionArg)]' in src, (
        'definition updates must use the shared contract-aware '
        'optimistic-write options')
    assert "method === 'list' || method === 'listResult' || method === 'save'" in src
    assert 'installMethod(endpoint, contract.resultMethod, true)' in src
    assert 'installMethod(endpoint, contract.directMethod, false)' in src
    assert 'methodOwners.get(method)' in src, (
        'normalized methods must win when result/direct names intentionally '
        'share one public facade method')


def _normalise_route(path: str) -> str:
    return re.sub(r'<[^>]+>', '<>', path).rstrip('/') or '/'


def _frontend_endpoint_contracts() -> dict[str, dict]:
    script = """
const fs=require('fs');global.window=global;
eval(fs.readFileSync(process.argv[1],'utf8'));
process.stdout.write(JSON.stringify(orchestrationEndpointContracts()));
"""
    proc = subprocess.run(
        ['node', '-e', script, _API_CLIENT_JS],
        cwd=_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    contracts = json.loads(proc.stdout)
    assert isinstance(contracts, dict)
    return contracts


def test_endpoint_registry_exactly_matches_live_backend(flask_app):
    """The frontend endpoint IDs are an exact route/verb view of the
    registered backend orchestration surface, not a hand-maintained subset."""
    contracts = _frontend_endpoint_contracts()
    from lib.orchestration.http_endpoint_contract import (
        orchestration_http_endpoint_dicts,
    )
    backend_contracts = orchestration_http_endpoint_dicts()
    assert len(contracts) == len(backend_contracts) == 22
    assert {
        name: {'route': contract['route'], 'method': contract['method']}
        for name, contract in contracts.items()
    } == {
        name: {'route': contract['route'], 'method': contract['method']}
        for name, contract in backend_contracts.items()
    }
    assert {
        name: contract['responseContract']
        for name, contract in contracts.items()
    } == {
        name: contract['responseContract']
        for name, contract in backend_contracts.items()
    }
    assert {
        name: contract.get('pathArgs', {})
        for name, contract in contracts.items()
    } == {
        name: contract.get('pathArgs', {})
        for name, contract in backend_contracts.items()
    }
    assert {
        name: contract.get('queryArgs', {})
        for name, contract in contracts.items()
    } == {
        name: contract.get('queryArgs', {})
        for name, contract in backend_contracts.items()
    }
    assert {
        name: contract.get('bodyArgs', {})
        for name, contract in contracts.items()
    } == {
        name: contract.get('bodyArgs', {})
        for name, contract in backend_contracts.items()
    }
    for field, default in (
        ('bodyArg', None),
        ('requestOptionsArg', None),
        ('writeOperation', ''),
        ('writeVersionArg', None),
        ('writeContractArg', None),
    ):
        assert {
            name: contract.get(field, default)
            for name, contract in contracts.items()
        } == {
            name: contract.get(field, default)
            for name, contract in backend_contracts.items()
        }
    frontend = {
        (_normalise_route(contract['route']), contract['method'])
        for contract in contracts.values()
    }
    backend = set()
    for rule in flask_app.url_map.iter_rules():
        route = _normalise_route(str(rule.rule))
        if not route.startswith('/api/v1/orchestrations'):
            continue
        backend.update(
            (route, method)
            for method in (rule.methods or set())
            if method not in {'HEAD', 'OPTIONS'}
        )
    assert frontend == backend

    from lib.openapi import build_spec
    paths = build_spec(flask_app)['paths']
    for contract in backend_contracts.values():
        path = re.sub(
            r'<(?:[^:<>]+:)?([^<>]+)>', r'{\1}', contract['route'])
        operation = paths[path][contract['method'].lower()]
        assert operation.get('x-tofu-response-contract') == \
            contract['responseContract'], (contract, operation)
        declares_body = bool(contract.get('bodyArgs')) \
            or contract.get('bodyArg') is not None
        assert ('requestBody' in operation) is declares_body, \
            (contract, operation)

    facade = _API_CLIENT_SOURCE.read_text(encoding='utf-8')
    # Ordinary facade methods are generated from these exact contracts at
    # runtime; the companion frontend endpoint test compares the resulting
    # method set to every resultMethod/directMethod. Keep this parity test from
    # forcing the old hand-copied method table back into the source.
    assert 'ORCHESTRATION_REQUEST_CONTRACTS' in facade
    assert 'installMethod(endpoint, contract.resultMethod, true)' in facade
    assert 'installMethod(endpoint, contract.directMethod, false)' in facade
    for special in ('listResult', 'list', 'save'):
        assert f'methods.{special} =' in facade


def test_generated_http_contract_is_current():
    proc = subprocess.run(
        [sys.executable, 'scripts/gen_orchestration_http_contract.py',
         '--check'],
        cwd=_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')


def test_backend_route_adapters_consume_canonical_registry():
    """No orchestration adapter may reintroduce hand-written route identity."""

    names = (
        'orchestration_definition_routes.py',
        'orchestration_authoring_routes.py',
        'orchestration_runtime_routes.py',
        'orchestration_task_routes.py',
        'orchestration_mutation_routes.py',
    )
    sources = {
        name: open(  # noqa: SIM115
            os.path.join(_ROOT, 'routes', 'api_v1', name),
            encoding='utf-8',
        ).read()
        for name in names
    }
    joined = '\n'.join(sources.values())
    assert '@blueprint.route' not in joined
    assert joined.count('@orchestration_route(') == 20
    assert "orchestration_endpoint_path('run-poll', method='GET')" \
        in sources['orchestration_runtime_routes.py']
    assert "orchestration_endpoint_path('run-abort', method='POST')" \
        in sources['orchestration_mutation_routes.py']

    from lib.orchestration.http_endpoint_contract import (
        orchestration_http_endpoint,
        orchestration_http_endpoints,
    )
    registry = orchestration_http_endpoints()
    assert len(registry) == 22
    for contract in registry.values():
        placeholders = tuple(re.findall(
            r'<(?:[^:<>]+:)?([^<>]+)>', contract.route))
        assert placeholders == contract.path_fields
    with pytest.raises(TypeError):
        registry['future'] = orchestration_http_endpoint('plan')
    with pytest.raises(KeyError, match='Unknown orchestration HTTP endpoint'):
        orchestration_http_endpoint('missing')


def test_shared_request_schemas_reach_live_openapi(flask_app):
    """Executable ingress contracts and generated interface docs share the
    same schemas for definition selection, run starts and human gates."""
    from lib.openapi import build_spec

    paths = build_spec(flask_app)['paths']
    from routes.api_v1.orchestration_authoring_openapi import (
        authoring_contract_response_schema,
    )

    def schema(path: str, method: str = 'post') -> dict:
        return paths[path][method]['requestBody']['content'][
            'application/json']['schema']

    from lib.orchestration.definition_contract_schema import (
        definition_candidate_schema,
        definition_request_schema,
    )
    assert schema('/api/v1/orchestrations/validate') == \
        definition_candidate_schema()
    assert schema('/api/v1/orchestrations') == definition_request_schema()

    from routes.api_v1.orchestration_definition_openapi import (
        definition_entry_response_schema,
        definition_list_response_schema,
    )
    from routes.api_v1.orchestration_authoring_action_openapi import (
        compose_response_schema,
        definition_action_response_schema,
        inspection_response_schema,
        plan_response_schema,
    )
    definitions = paths['/api/v1/orchestrations']
    definition_entry = paths['/api/v1/orchestrations/{orch_id}']
    assert definitions['get']['responses']['200']['content'][
        'application/json']['schema'] == definition_list_response_schema()
    assert definitions['post']['responses']['201']['content'][
        'application/json']['schema'] == \
        definition_entry_response_schema(written=True)
    assert definition_entry['get']['responses']['200']['content'][
        'application/json']['schema'] == definition_entry_response_schema()
    assert definition_entry['put']['responses']['200']['content'][
        'application/json']['schema'] == \
        definition_entry_response_schema(written=True)
    assert definition_entry['put']['parameters'][-1]['name'] == 'If-Match'
    assert definition_entry['delete']['parameters'][-1]['name'] == 'If-Match'
    for method in ('get', 'put'):
        assert 'ETag' in definition_entry[method]['responses']['200']['headers']

    authoring_actions = {
        ('/api/v1/orchestrations/validate', 'post'):
            inspection_response_schema(),
        ('/api/v1/orchestrations/compose', 'post'):
            compose_response_schema(),
        ('/api/v1/orchestrations/builtin/{name}', 'get'):
            definition_action_response_schema(inspection=True),
        ('/api/v1/orchestrations/layout', 'post'):
            definition_action_response_schema(
                definition_source=True, layout=True),
        ('/api/v1/orchestrations/plan', 'post'): plan_response_schema(),
    }
    for (path, method), expected in authoring_actions.items():
        actual = paths[path][method]['responses']['200']['content'][
            'application/json']['schema']
        assert actual == expected

    layout = schema('/api/v1/orchestrations/layout')
    plan = schema('/api/v1/orchestrations/plan')
    run = schema('/api/v1/orchestrations/run')
    durable = schema('/api/v1/orchestrations/tasks')
    assert layout == plan
    assert layout['anyOf'] == [
        {'required': ['definition']}, {'required': ['id']},
    ]
    assert run == durable
    assert run['properties']['input']['maxLength'] == 8000

    from routes.api_v1.orchestration_run_openapi import run_start_response_schema
    live_start_response = paths['/api/v1/orchestrations/run']['post'][
        'responses']['200']['content']['application/json']['schema']
    durable_start_response = paths['/api/v1/orchestrations/tasks']['post'][
        'responses']['201']['content']['application/json']['schema']
    assert live_start_response == run_start_response_schema('ephemeral')
    assert durable_start_response == run_start_response_schema('durable')
    assert live_start_response['properties']['start']['required'] == \
        durable_start_response['properties']['start']['required']

    compose = schema('/api/v1/orchestrations/compose')
    assert compose['required'] == ['requirement']
    assert compose['properties']['requirement']['maxLength'] == 4000
    assert compose['properties']['history']['x-retainedItems'] == 8

    approval = schema('/api/v1/orchestrations/run/human-approve')
    guidance = schema('/api/v1/orchestrations/run/human-input')
    assert approval['properties']['approved']['type'] == 'boolean'
    assert guidance['required'] == ['requestId', 'response']
    assert guidance['properties']['response']['minLength'] == 1
    assert guidance['properties']['response']['maxLength'] == 8000

    list_parameters = {
        parameter['name']: parameter
        for parameter in paths['/api/v1/orchestrations/tasks']['get'][
            'parameters']
    }
    assert list_parameters['status']['schema'] == {
        'type': 'string',
        'enum': ['pending', 'running', 'paused', 'done', 'error', 'aborted'],
    }
    assert list_parameters['orch_id']['schema'] == {'type': 'string'}
    replay_parameters = {
        parameter['name']: parameter
        for parameter in paths[
            '/api/v1/orchestrations/tasks/{run_id}/events']['get'][
                'parameters']
    }
    assert replay_parameters['run_id']['required'] is True
    assert replay_parameters['cursor']['schema'] == {
        'type': 'integer', 'minimum': 0, 'default': 0,
    }
    assert 'Terminal pages include' in paths[
        '/api/v1/orchestrations/tasks/{run_id}/events']['get']['description']

    from routes.api_v1.orchestration_task_openapi import (
        durable_replay_response_schema,
        durable_run_list_response_schema,
        durable_run_read_response_schema,
    )
    task_list_response = paths['/api/v1/orchestrations/tasks']['get'][
        'responses']['200']['content']['application/json']['schema']
    task_read_response = paths[
        '/api/v1/orchestrations/tasks/{run_id}']['get']['responses']['200'][
            'content']['application/json']['schema']
    replay_response = paths[
        '/api/v1/orchestrations/tasks/{run_id}/events']['get'][
            'responses']['200']['content']['application/json']['schema']
    assert task_list_response == durable_run_list_response_schema()
    assert task_read_response == durable_run_read_response_schema()
    assert replay_response == durable_replay_response_schema()

    from routes.api_v1.orchestration_mutation_openapi import (
        mutation_route_response_registry,
    )
    mutation_responses = mutation_route_response_registry()
    assert paths[
        '/api/v1/orchestrations/tasks/{run_id}/abort']['post'][
            'responses'] == mutation_responses['task-abort']
    assert paths['/api/v1/orchestrations/tasks/{run_id}']['delete'][
        'responses'] == mutation_responses['task-remove']
    assert paths[
        '/api/v1/orchestrations/run/abort/{task_id}']['post'][
            'responses'] == mutation_responses['run-abort']

    authoring_response = paths[
        '/api/v1/orchestrations/authoring-contract']['get']['responses'][
            '200']['content']['application/json']['schema']
    assert authoring_response == authoring_contract_response_schema()
    assert 'contractSections' in authoring_response['required']
    assert 'requestLimits' in authoring_response['required']

    assert '/api/v1/orchestrations/role-schema' not in paths

    live_replay_parameters = {
        parameter['name']: parameter
        for parameter in paths[
            '/api/v1/orchestrations/run/poll/{task_id}']['get'][
                'parameters']
    }
    assert live_replay_parameters['task_id']['required'] is True
    assert live_replay_parameters['cursor']['schema'] == \
        replay_parameters['cursor']['schema']
    assert paths['/api/v1/orchestrations/run/poll/{task_id}']['get'][
        'tags'] == ['orchestrations']
    from routes.api_v1.orchestration_replay_openapi import (
        orchestration_live_replay_responses,
    )
    assert paths['/api/v1/orchestrations/run/poll/{task_id}']['get'][
        'responses'] == orchestration_live_replay_responses()
    assert paths['/api/v1/orchestrations/run/abort/{task_id}']['post'][
        'tags'] == ['orchestrations']


def test_shipped_source_converted():
    """Orchestration adapters carry no ad-hoc ``jsonify`` responses."""
    src = '\n'.join(
        open(target, encoding='utf-8').read()  # noqa: SIM115
        for target in _TARGETS
    )
    assert 'jsonify(' not in src, (
        'an orchestration HTTP adapter still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'an orchestration HTTP adapter still imports jsonify')
    assert 'api_created(' in src, (
        'expected the shared orchestration response boundary to retain an '
        'api_created( call for 201 projections')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_bare_array_coordinated_migration,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
