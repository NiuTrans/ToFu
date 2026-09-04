"""One valid owner-scoped model route shared by native chat integration tests."""

from __future__ import annotations

from contextlib import contextmanager
import os

from lib.model_routing import ModelRoutingRepository, OwnerBoundary, empty_document


_MODEL_REF = {'creator_id': 'tofu-test', 'model_id': 'stub-model'}


def native_test_model(
    *,
    creator_id: str = _MODEL_REF['creator_id'],
    model_id: str = _MODEL_REF['model_id'],
) -> dict[str, str]:
    """Return a fresh structured selection so callers cannot mutate the fixture."""
    return {'creator_id': creator_id, 'model_id': model_id}


def native_test_provider_bundle(
    *,
    expected_revision: int,
    provider_id: str = 'extra-provider',
    base_url: str = 'https://api.openai.com/v1',
    extra_headers: dict[str, object] | None = None,
    secret: str = 'sk-internal-secret',
) -> dict:
    """Build one complete v2 ProviderAccess CAS payload for route tests."""
    access_id = f'{provider_id}-access'
    connection_id = f'{provider_id}-connection'
    credential_id = f'{provider_id}-credential'
    return {
        'expected_revision': expected_revision,
        'provider': {
            'provider_id': provider_id,
            'name': provider_id,
            'scope': 'owner',
        },
        'provider_access': {
            'provider_access_id': access_id,
            'provider_id': provider_id,
            'enabled': True,
            'quota_policy': {},
        },
        'connections': [{
            'connection_id': connection_id,
            'provider_access_id': access_id,
            'base_url': base_url,
            'protocol': 'openai',
            'enabled': True,
            'priority': 0,
            'extra_headers': dict(extra_headers or {}),
        }],
        'credentials': [{
            'credential_id': credential_id,
            'provider_access_id': access_id,
            'kind': 'api_key',
            'secret_reference': '',
            'key_hint': '',
            'enabled': True,
            'authorization': {
                'connection_ids': [connection_id],
                'models': [],
            },
            'quota_policy': {},
        }],
        'credential_secrets': {credential_id: secret},
        'offerings': [],
        'deployments': [],
        'creators': [],
        'models': [],
    }


@contextmanager
def allow_native_test_endpoint():
    """Allow the inert fixture hostname without weakening global SSRF policy."""
    name = 'TOFU_BYO_ALLOW_HOSTS'
    previous = os.environ.get(name)
    hosts = {
        value.strip()
        for value in str(previous or '').split(',')
        if value.strip()
    }
    hosts.add('api.openai.com')
    os.environ[name] = ','.join(sorted(hosts))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def install_native_test_model_route(
    *,
    owner_user_id: int,
    creator_id: str = _MODEL_REF['creator_id'],
    model_id: str = _MODEL_REF['model_id'],
) -> None:
    """Install the minimum valid v2 route used before the stubbed task runner."""
    model_ref = native_test_model(
        creator_id=creator_id, model_id=model_id)
    repository = ModelRoutingRepository()
    boundary = OwnerBoundary.create(owner_user_id)
    current = repository.get(boundary)
    if any(
        row.get('creator_id') == model_ref['creator_id']
        and row.get('model_id') == model_ref['model_id']
        for row in current.document['models']
    ):
        return
    document = {
        'contract_version': 'tofu.model-routing/v2',
        'revision': current.revision,
        'creators': [{
            'creator_id': model_ref['creator_id'],
            'name': 'Tofu integration tests',
        }],
        'models': [{
            **model_ref,
            'display_name': 'Stub model',
            'capabilities': ['text'],
            'context_window': 1_000_000,
            'quality_rank': 1,
        }],
        'providers': [{
            'provider_id': 'test-provider',
            'name': 'Test provider',
            'scope': 'owner',
        }],
        'provider_accesses': [{
            'provider_access_id': 'test-access',
            'provider_id': 'test-provider',
            'enabled': True,
            'quota_policy': {},
        }],
        'connections': [{
            'connection_id': 'test-connection',
            'provider_access_id': 'test-access',
            # The task runner is stubbed before dispatch. A public-domain URL
            # avoids the self-hosted reachability probe without creating a
            # hidden test-only branch in production routing.
            'base_url': 'https://api.openai.com/v1',
            'protocol': 'openai',
            'enabled': True,
            'priority': 0,
            'extra_headers': {},
        }, {
            'connection_id': 'test-anthropic-connection',
            'provider_access_id': 'test-access',
            'base_url': 'https://api.openai.com/v1',
            'protocol': 'anthropic',
            'enabled': True,
            'priority': 0,
            'extra_headers': {},
        }],
        'credentials': [{
            'credential_id': 'test-local-identity',
            'provider_access_id': 'test-access',
            'kind': 'local_identity',
            'secret_reference': '',
            'key_hint': '',
            'enabled': True,
            'authorization': {
                'connection_ids': [
                    'test-anthropic-connection',
                    'test-connection',
                ],
                'models': [dict(model_ref)],
            },
            'quota_policy': {},
        }],
        'offerings': [{
            'offering_id': 'test-offering',
            'provider_access_id': 'test-access',
            'identity_state': 'confirmed',
            'model': dict(model_ref),
            'enabled': True,
            'capabilities': ['text'],
            'context_window': 1_000_000,
            'priority': 0,
        }],
        'deployments': [{
            'deployment_id': 'test-deployment',
            'offering_id': 'test-offering',
            'connection_id': 'test-connection',
            'wire_model_id': f'{model_id}-wire',
            'enabled': True,
            'identity_confidence': 'high',
            'probe_status': 'passed',
            'priority': 0,
        }, {
            'deployment_id': 'test-anthropic-deployment',
            'offering_id': 'test-offering',
            'connection_id': 'test-anthropic-connection',
            'wire_model_id': f'{model_id}-anthropic-wire',
            'enabled': True,
            'identity_confidence': 'high',
            'probe_status': 'passed',
            'priority': 0,
        }],
    }
    repository.compare_and_swap(
        boundary,
        document,
        expected_revision=current.revision,
    )


def clear_test_model_routing(*, owner_user_id: int) -> None:
    """Clear one test owner's route and reclaim abandoned test secrets.

    Route tests share the process-wide storage sidecar. Resetting through the
    repository (instead of reaching into SQLite) keeps test isolation on the
    same owner/CAS boundary as production and makes a failed prior test safe to
    retry.
    """
    repository = ModelRoutingRepository()
    boundary = OwnerBoundary.create(owner_user_id)
    current = repository.get(boundary)
    stale_secret_references = {
        str(credential.get('secret_reference') or '')
        for credential in current.document['credentials']
        if credential.get('secret_reference')
    }
    repository.compare_and_swap(
        boundary,
        empty_document(revision=current.revision),
        expected_revision=current.revision,
    )
    for secret_reference in stale_secret_references:
        repository.delete_secret(boundary, secret_reference)


def reset_native_test_model_route(
    *,
    owner_user_id: int,
    creator_id: str = _MODEL_REF['creator_id'],
    model_id: str = _MODEL_REF['model_id'],
) -> None:
    """Install the canonical fixture from an owner-isolated empty authority."""
    clear_test_model_routing(owner_user_id=owner_user_id)
    install_native_test_model_route(
        owner_user_id=owner_user_id,
        creator_id=creator_id,
        model_id=model_id,
    )
