"""Cross-language parity for orchestration HTTP endpoint declarations.

NEUTER anchor: a stale generated frontend route silently sends orchestration
actions to a backend URL that no longer exists, surfacing only as runtime 404s.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
ESBUILD = ROOT / 'node_modules/.bin/esbuild'


def _backend_contracts() -> set[tuple[str, str]]:
    from lib.orchestration.http_endpoint_contract import (
        orchestration_http_endpoints,
    )
    return {
        (contract.method, contract.route)
        for contract in orchestration_http_endpoints().values()
    }


def _frontend_contracts() -> dict[str, tuple[str, str]]:
    script = r"""
const fs=require('fs');
global.window=global;
eval(fs.readFileSync('static/js/api/orchestration-http-contract.generated.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-response-contracts.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-client-methods.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-endpoint-transport.js','utf8'));
eval(fs.readFileSync('static/js/api/orchestration-endpoints.js','utf8'));
const contracts=ApiOrchestrationEndpoints.contracts();
process.stdout.write(JSON.stringify(Object.fromEntries(
  Object.entries(contracts).map(([name,value])=>[
    name,[value.method,value.route],
  ])
)));
"""
    result = subprocess.run(
        ['node', '-e', script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return {
        name: (str(contract[0]), str(contract[1]))
        for name, contract in json.loads(result.stdout).items()
    }


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_frontend_registry_matches_every_backend_orchestration_route():
    frontend = _frontend_contracts()
    backend = _backend_contracts()

    assert len(frontend) == len(set(frontend.values())) == 23
    assert set(frontend.values()) == backend


def test_http_endpoint_interface_has_focused_physical_owners():
    facade = (ROOT / 'lib/orchestration/http_endpoint_contract.py').read_text()
    model = (ROOT / 'lib/orchestration/http_endpoint_model.py').read_text()
    registry = (
        ROOT / 'lib/orchestration/http_endpoint_registry.py').read_text()
    validation = (
        ROOT / 'lib/orchestration/http_endpoint_validation.py').read_text()

    assert 'class OrchestrationHttpEndpoint' in model
    assert 'class OrchestrationHttpEndpoint' not in facade
    assert 'ORCHESTRATION_HTTP_ENDPOINTS:' in registry
    assert "'run-start': _endpoint(" in registry
    assert "'run-start': _endpoint(" not in facade
    assert 'def validate_orchestration_http_endpoints(' in validation
    assert 're.fullmatch(' not in facade
    assert len(facade.splitlines()) < 50
    assert len(model.splitlines()) < 75
    assert len(registry.splitlines()) < 190
    assert len(validation.splitlines()) < 120

    browser_policy = (
        ROOT / 'lib/orchestration/browser_endpoint_policy.py').read_text()
    browser_contract = (
        ROOT / 'lib/orchestration/browser_endpoint_contract.py').read_text()
    browser_validation = (
        ROOT / 'lib/orchestration/browser_endpoint_validation.py').read_text()
    response_fields = (
        ROOT / 'lib/orchestration/response_required_fields.py').read_text()
    assert 'ORCHESTRATION_RESPONSE_OPTIONS:' in browser_policy
    assert 'ORCHESTRATION_CLIENT_METHODS:' in browser_policy
    assert 'def orchestration_browser_request_contract_dicts(' \
        in browser_contract
    assert 'def validate_orchestration_browser_endpoint_policy(' \
        in browser_validation
    assert 'coverage mismatch' not in browser_contract
    assert 'ORCHESTRATION_CLIENT_METHODS' not in registry
    assert 'ORCHESTRATION_RESPONSE_OPTIONS' not in registry
    assert 'definition_list_response_schema' in response_fields
    assert 'definition_list_response_schema' not in browser_policy
    assert len(browser_contract.splitlines()) < 80
    assert len(browser_validation.splitlines()) < 55


def test_browser_endpoint_policy_validation_rejects_partial_join():
    from lib.orchestration.browser_endpoint_contract import (
        orchestration_client_methods,
        orchestration_response_options,
    )
    from lib.orchestration.browser_endpoint_validation import (
        validate_orchestration_browser_endpoint_policy,
    )
    from lib.orchestration.http_endpoint_contract import (
        orchestration_http_endpoints,
    )

    endpoints = dict(orchestration_http_endpoints())
    responses = dict(orchestration_response_options())
    methods = dict(orchestration_client_methods())

    partial_methods = dict(methods)
    partial_methods.pop('task-remove')
    with pytest.raises(ValueError, match='client method.*coverage mismatch'):
        validate_orchestration_browser_endpoint_policy(
            endpoints, responses, partial_methods)

    partial_responses = dict(responses)
    partial_responses.pop('mutation')
    with pytest.raises(ValueError, match='response policy coverage mismatch'):
        validate_orchestration_browser_endpoint_policy(
            endpoints, partial_responses, methods)

    empty_method = dict(methods)
    empty_method['task-remove'] = ('', 'taskRemove')
    with pytest.raises(ValueError, match='Empty.*client method'):
        validate_orchestration_browser_endpoint_policy(
            endpoints, responses, empty_method)

    with pytest.raises(ValueError, match='Unknown.*response field'):
        validate_orchestration_browser_endpoint_policy(
            endpoints, responses, methods, {'missing': ('ok',)})
    with pytest.raises(ValueError, match='Invalid.*response fields'):
        validate_orchestration_browser_endpoint_policy(
            endpoints, responses, methods, {'compose': ('ok', '')})


def test_response_required_fields_derive_from_owned_openapi_schemas():
    from lib.orchestration.authoring_action_wire_contracts import (
        compose_response_schema,
        definition_action_response_schema,
        plan_response_schema,
    )
    from lib.orchestration.authoring_contract_schema import (
        authoring_contract_response_schema,
    )
    from lib.orchestration.response_required_fields import (
        ORCHESTRATION_RESPONSE_REQUIRED_FIELDS,
    )
    from lib.orchestration.definition_contract_schema import (
        definition_delete_response_schema,
        definition_entry_response_schema,
        definition_list_response_schema,
    )
    from lib.orchestration.durable_run_wire_contract import (
        durable_replay_response_schema,
        durable_run_list_response_schema,
        durable_run_read_response_schema,
    )
    from lib.orchestration.inspection_wire_contract import (
        inspection_response_schema,
    )
    from lib.orchestration.mutation_contract import mutation_response_schema
    from lib.orchestration.mutation_endpoint_contract import (
        mutation_endpoint_contracts,
    )
    from lib.orchestration.runtime_wire_contracts import (
        run_start_response_schema,
    )
    from lib.task_replay import live_task_replay_response_schema

    mutation_schemas = [
        mutation_response_schema(
            config['action'], reasons, config['compatibility'])
        for config in mutation_endpoint_contracts().values()
        for reasons in config['outcomes']
    ]
    mutation_required = tuple(
        field for field in mutation_schemas[0]['required']
        if all(field in schema['required'] for schema in mutation_schemas[1:]))

    assert dict(ORCHESTRATION_RESPONSE_REQUIRED_FIELDS) == {
        'definition-list': tuple(
            definition_list_response_schema()['required']),
        'definition-read': tuple(
            definition_entry_response_schema()['required']),
        'definition-save': tuple(
            definition_entry_response_schema(written=True)['required']),
        'definition-delete': tuple(
            definition_delete_response_schema()['required']),
        'validation': tuple(inspection_response_schema()['required']),
        'compose': tuple(compose_response_schema()['required']),
        'builtin': tuple(definition_action_response_schema(
            inspection=True)['required']),
        'layout': tuple(definition_action_response_schema(
            definition_source=True, layout=True)['required']),
        'authoring-contract': tuple(
            authoring_contract_response_schema()['required']),
        'plan': tuple(plan_response_schema()['required']),
        'run-start': tuple(run_start_response_schema(
            'ephemeral')['required']),
        'run-poll': tuple(live_task_replay_response_schema()['required']),
        'mutation': mutation_required,
        'task-list': tuple(durable_run_list_response_schema()['required']),
        'task-read': tuple(durable_run_read_response_schema()['required']),
        'task-create': tuple(run_start_response_schema('durable')['required']),
        'task-events': tuple(durable_replay_response_schema()['required']),
    }


def test_generated_classic_and_native_endpoint_policies_are_current():
    from scripts.gen_orchestration_http_contract import (
        METHOD_OUTPUT,
        OUTPUT,
        RESPONSE_OUTPUT,
        TYPESCRIPT_OUTPUT,
        render,
        render_client_methods,
        render_response_contracts,
        render_typescript,
    )

    expected = {
        OUTPUT: render(),
        RESPONSE_OUTPUT: render_response_contracts(),
        METHOD_OUTPUT: render_client_methods(),
        TYPESCRIPT_OUTPUT: render_typescript(),
    }
    for path, content in expected.items():
        assert Path(path).read_text(encoding='utf-8') == content


@pytest.mark.skipif(
    not shutil.which('node') or not ESBUILD.is_file(),
    reason='node + esbuild unavailable',
)
def test_native_request_registry_is_self_contained_and_matches_classic(
    tmp_path,
):
    built = tmp_path / 'request-contract.js'
    compiled = subprocess.run(
        [str(ESBUILD),
         'frontend/src/features/orchestration/request-contract.ts',
         '--bundle', '--format=iife', '--platform=browser',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr

    script = r"""
const fs=require('fs');global.window=global;
require(process.argv[1]);
const native=global._ORCHESTRATION_REQUEST_CONTRACTS;
const nativeSnapshot=JSON.parse(JSON.stringify(native));
const nativeFrozen=Object.isFrozen(native)
  &&Object.values(native).every(value=>Object.isFrozen(value)
    &&Object.isFrozen(value.responseRequiredFields));
const nativeUnknown=global.orchestrationRequestContract('missing');
eval(fs.readFileSync(
  'static/js/api/orchestration-http-contract.generated.js','utf8'));
eval(fs.readFileSync(
  'static/js/api/orchestration-response-contracts.js','utf8'));
eval(fs.readFileSync(
  'static/js/api/orchestration-client-methods.js','utf8'));
eval(fs.readFileSync(
  'static/js/api/orchestration-endpoint-transport.js','utf8'));
eval(fs.readFileSync(
  'static/js/api/orchestration-endpoints.js','utf8'));
const classic=Object.fromEntries(Object.entries(
  ApiOrchestrationEndpoints.contracts()).map(([name,value])=>[name,{
    resultMethod:value.resultMethod,
    directMethod:value.directMethod,
    optionName:value.optionName,
    responseContract:value.responseContract,
    responseRequiredFields:value.responseRequiredFields,
  }]));
process.stdout.write(JSON.stringify({
  native:nativeSnapshot,classic,nativeFrozen,nativeUnknown,
}));
"""
    run = subprocess.run(
        ['node', '-e', script, str(built)], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result['native'] == result['classic']
    assert len(result['native']) == 23
    assert result['nativeFrozen'] is True
    assert result['nativeUnknown'] is None

    native_source = (
        ROOT / 'frontend/src/features/orchestration/request-contract.ts'
    ).read_text()
    assert 'ApiOrchestrationEndpoints' not in native_source
    assert '_ORCHESTRATION_ENDPOINT_REGISTRY' not in native_source
    assert "from './request-contracts.generated'" in native_source
