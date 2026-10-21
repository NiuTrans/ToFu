#!/usr/bin/env python3
"""tests/model_routing/test_deployment_request_headers.py — per-request-name selectors.

WHY
===
A gateway can serve one wire spelling only under a header that names its
serving provider, while another spelling of the SAME Offering rejects that
header. Measured on your-llm-gateway.example.com (2026-10-21), for ``gpt-6.1-sol``:

    /v1/responses  kunlunbedrock.gpt-6.1-sol-m17 + x-context-provider: kunlun -> 200
    /v1/openai/native  kunlunbedrock.gpt-6.1-sol-m17 (no selector)            -> 400 配置不存在
    /v1/responses  gpt-6.1-sol + x-context-provider: kunlun                   -> 400
                                    该模型在 provider 下无可用账户

The selector is therefore per-DEPLOYMENT: it cannot live on the shared
Connection (that would send it to the public id and break it) and it cannot be
dropped (that breaks the alias). This file pins the merge order at every path
that builds outbound headers, and the probe plan that must mirror it.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/model_routing/test_deployment_request_headers.py -v
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

from lib.model_routing import OwnerBoundary, empty_document, normalize_document
from lib.model_routing.domain import ModelRoutingError

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_OWNER = 5150251


def _document(deployment_headers=None):
    document = empty_document(revision=0)
    document['creators'].append({
        'creator_id': 'openai', 'name': 'OpenAI'})
    document['models'].append({
        'creator_id': 'openai', 'model_id': 'gpt-6.1-sol',
        'display_name': 'GPT-6.1 Sol', 'capabilities': ['text'],
        'context_window': 200000, 'quality_rank': 1.0,
    })
    document['providers'].append({
        'provider_id': 'gw', 'name': 'gateway', 'scope': 'owner'})
    document['provider_accesses'].append({
        'provider_access_id': 'access', 'provider_id': 'gw',
        'display_name': 'gateway', 'enabled': True, 'quota_policy': {},
    })
    document['connections'].append({
        'connection_id': 'conn-kunlun', 'provider_access_id': 'access',
        'base_url': 'https://gw.example.com/v1', 'protocol': 'openai_responses',
        # The Connection's own header set stays EMPTY: the selector belongs to
        # the request name, and a provider-wide copy would break the public id.
        'extra_headers': {}, 'enabled': True, 'priority': 100,
    })
    document['credentials'].append({
        'credential_id': 'cred', 'provider_access_id': 'access',
        'kind': 'api_key', 'secret_reference': 'secret-1', 'key_hint': '***',
        'enabled': True,
        'authorization': {'connection_ids': ['conn-kunlun'],
                          'models': [{'creator_id': 'openai',
                                      'model_id': 'gpt-6.1-sol'}]},
        'quota_policy': {},
    })
    document['offerings'].append({
        'offering_id': 'offering', 'provider_access_id': 'access',
        'model': {'creator_id': 'openai', 'model_id': 'gpt-6.1-sol'},
        'identity_state': 'confirmed', 'capabilities': ['text'],
        'context_window': 200000,
        'enabled': True, 'priority': 100,
    })
    deployment = {
        'deployment_id': 'dep-alias', 'offering_id': 'offering',
        'connection_id': 'conn-kunlun',
        'wire_model_id': 'kunlunbedrock.gpt-6.1-sol-m17',
        'identity_confidence': 'high', 'probe_status': 'unprobed',
        'priority': 100,
    }
    if deployment_headers is not None:
        deployment['extra_headers'] = deployment_headers
    document['deployments'].append(deployment)
    return document


class _StubRepository:
    """Only credential resolution: the plan needs no storage authority."""

    def resolve_secret(self, boundary, reference):
        return 'sk-test'


def _boundary() -> OwnerBoundary:
    return OwnerBoundary.create(_OWNER)


def test_domain_keeps_request_name_headers():
    document = normalize_document(
        _document({'x-context-provider': 'kunlun'}))
    row = document['deployments'][0]
    assert row['extra_headers'] == {'x-context-provider': 'kunlun'}


def test_domain_drops_an_empty_header_map_instead_of_echoing_it():
    document = normalize_document(_document({}))
    assert 'extra_headers' not in document['deployments'][0]


@pytest.mark.parametrize('name', ['Authorization', 'X-Api-Key', 'Cookie',
                                  'Host', 'Content-Length'])
def test_domain_refuses_transport_reserved_names(name):
    with pytest.raises(ModelRoutingError):
        normalize_document(_document({name: 'nope'}))


def test_probe_plan_layers_request_name_headers_over_the_connection():
    """The matrix must probe with the headers the REAL request sends.

    Probing without the selector returned ``invalid_response`` for a cell that
    answers 200 through the dispatcher — a false verdict on a working model.
    """
    from lib.model_routing.probe_plan import matrix_probe_plan

    document = normalize_document(
        _document({'x-context-provider': 'kunlun'}))
    document['connections'][0]['extra_headers'] = {'x-secret-scope': 'a'}
    bundle = {
        'provider': document['providers'][0],
        'provider_access': document['provider_accesses'][0],
        'connections': document['connections'],
        'credentials': document['credentials'],
        'offerings': document['offerings'],
        'deployments': document['deployments'],
    }
    plan = matrix_probe_plan(_StubRepository(), _boundary(), bundle, manual=True)
    context = plan['request_contexts']['kunlunbedrock.gpt-6.1-sol-m17']
    assert context['extra_headers'] == {
        'x-secret-scope': 'a',
        'x-context-provider': 'kunlun',
    }, context['extra_headers']


def test_dispatch_adapter_and_dispatcher_merge_the_same_way():
    """Both dispatch paths must produce Connection + Deployment + Credential."""
    import inspect
    from lib.llm_dispatch import dispatcher
    from lib.model_routing import dispatch_adapter

    for module in (dispatcher, dispatch_adapter):
        source = inspect.getsource(module)
        assert "candidate.deployment.get('extra_headers')" in source \
            or 'candidate.deployment.get("extra_headers")' in source, module.__name__


def test_probe_routes_openai_responses_to_the_responses_endpoint(monkeypatch):
    """The stored v2 spelling must reach ``/responses``, not chat.

    Only ``lib/llm_dispatch/slot.py`` normalized ``openai_responses``; the
    probe did not, so a Responses Connection was asked at
    ``/chat/completions`` and answered 400 ``配置不存在`` — a false not-found on
    the exact cell this feature exists to reach.
    """
    import json

    import lib.provider.provider_probe as probes

    calls = []

    class _Response:
        status_code = 200
        text = json.dumps({
            'id': 'resp_1', 'object': 'response', 'status': 'completed',
            'output': [{'type': 'message', 'role': 'assistant',
                        'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'OK',
                                     'annotations': []}]}],
        })

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({'url': url, 'headers': dict(headers or {})})
        return _Response()

    monkeypatch.setattr('lib.http_transport.client.http_post', _post)
    verdict, _detail = probes.probe_one_cell(
        'https://gw.example.com/v1', 'sk-test',
        'kunlunbedrock.gpt-6.1-sol-m17',
        {'x-context-provider': 'kunlun'}, 10, protocol='openai_responses')

    assert verdict == 'ok', verdict
    assert calls[0]['url'] == 'https://gw.example.com/v1/responses', calls[0]['url']
    assert calls[0]['headers']['x-context-provider'] == 'kunlun'


def test_native_route_includes_request_name_headers():
    import inspect
    from lib.model_routing import native_route

    source = inspect.getsource(native_route)
    assert 'chosen.deployment.get("extra_headers")' in source


_CREDENTIAL_HEADER_NAMES = [
    'x-access-token', 'X-Access-Token', 'X-ACCESS-TOKEN',
    'x-goog-api-key', 'X-Goog-Api-Key', 'X-GOOG-API-KEY',
    'x-client-secret', 'X-Client-Secret', 'X-CLIENT-SECRET',
]


@pytest.mark.parametrize('name', _CREDENTIAL_HEADER_NAMES)
def test_domain_refuses_credential_named_deployment_headers(name):
    with pytest.raises(ModelRoutingError) as error:
        normalize_document(_document({name: 'synthetic-header-secret'}))
    assert error.value.kind == 'model_routing_invalid'
    assert error.value.field == 'deployments[0].extra_headers'
    assert 'credential' in str(error.value).lower()
    assert 'synthetic-header-secret' not in str(error.value)


@pytest.mark.parametrize('name', _CREDENTIAL_HEADER_NAMES)
def test_connection_header_admission_contract_is_unchanged(name):
    document = _document({'x-context-provider': 'kunlun'})
    document['connections'][0]['extra_headers'] = {name: 'existing-metadata'}
    normalized = normalize_document(document)
    assert normalized['connections'][0]['extra_headers'] == {
        name: 'existing-metadata'}
    assert normalized['deployments'][0]['extra_headers'] == {
        'x-context-provider': 'kunlun'}


@pytest.fixture
def header_repository():
    from lib.model_routing.repository import InMemoryModelRoutingRepository

    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    repository.put_secret(boundary, 'synthetic-api-key',
                          secret_reference='secret-1')
    repository.compare_and_swap(
        boundary, _document({'x-context-provider': 'kunlun'}),
        expected_revision=0)
    return repository, boundary


@pytest.mark.parametrize('name', _CREDENTIAL_HEADER_NAMES)
def test_cas_rejects_credential_headers_without_changing_authority(
        header_repository, name):
    repository, boundary = header_repository
    before = repository.get(boundary)
    candidate = deepcopy(before.document)
    candidate['deployments'][0]['extra_headers'][name] = 'synthetic-header-secret'

    with pytest.raises(ModelRoutingError) as error:
        repository.compare_and_swap(
            boundary, candidate, expected_revision=before.revision)

    assert error.value.kind == 'model_routing_invalid'
    assert error.value.field == 'deployments[0].extra_headers'
    after = repository.get(boundary)
    assert after.revision == before.revision
    assert after.document == before.document
    assert 'synthetic-header-secret' not in json.dumps(after.public_document())


@pytest.fixture
def header_api(header_repository, monkeypatch):
    from quart import Quart
    import routes.api_v1.auth as auth
    import routes.api_v1.providers as routes

    repository, boundary = header_repository
    context = SimpleNamespace(
        owner_user_id=boundary.owner_user_id, tenant_id=boundary.tenant_id,
        is_authenticated=True, has_scope=lambda scope: scope == 'providers')
    monkeypatch.setattr(auth, 'current_auth', lambda: context)
    monkeypatch.setattr(routes, 'current_auth', lambda: context)
    monkeypatch.setattr(routes, '_repository', lambda: repository)
    reloads = []
    monkeypatch.setattr(routes, '_hot_reload_dispatcher_slots',
                        lambda: reloads.append(True))
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    app.register_blueprint(routes.api_v1_providers_bp)

    def request(method, body=None):
        async def go():
            response = await app.test_client().open(
                '/api/v1/model-routing', method=method, json=body)
            return response.status_code, await response.get_json()
        return asyncio.run(go())

    return request, repository, boundary, reloads


@pytest.mark.parametrize('name', _CREDENTIAL_HEADER_NAMES)
def test_api_rejects_credential_headers_and_preserves_safe_projection(
        header_api, name):
    request, repository, boundary, reloads = header_api
    status, before = request('GET')
    assert status == 200
    candidate = deepcopy(before['model_routing'])
    candidate['deployments'][0]['extra_headers'][name] = 'synthetic-header-secret'
    status, rejected = request('PUT', {
        'expected_revision': before['revision'], 'model_routing': candidate})

    assert status == 400, rejected
    assert rejected['field'] == 'deployments[0].extra_headers'
    assert 'credential' in rejected['error'].lower()
    assert 'synthetic-header-secret' not in json.dumps(rejected)
    status, after = request('GET')
    assert status == 200
    assert after == before
    assert after['model_routing']['deployments'][0]['extra_headers'] == {
        'x-context-provider': 'kunlun'}
    assert repository.get(boundary).revision == before['revision']
    assert reloads == []


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
