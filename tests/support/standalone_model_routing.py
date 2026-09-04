"""Minimal complete v2 envelope for storage-free runtime tests."""

from __future__ import annotations

from copy import deepcopy


def standalone_model_routing_envelope(
    *,
    base_url: str = 'https://models.example/v1',
    secret: str = 'sk-test-secret',
    model_id: str = 'model-a',
    provider_id: str = 'provider-a',
) -> dict:
    access_id = f'{provider_id}-access'
    connection_id = f'{provider_id}-connection'
    credential_id = f'{provider_id}-credential'
    offering_id = f'{provider_id}-offering'
    deployment_id = f'{provider_id}-deployment'
    secret_reference = f'{provider_id}-secret'
    model_ref = {'creator_id': 'test-creator', 'model_id': model_id}
    return {
        'model_routing': {
            'contract_version': 'tofu.model-routing/v2',
            'revision': 0,
            'creators': [{
                'creator_id': 'test-creator',
                'name': 'Test creator',
            }],
            'models': [{
                **model_ref,
                'display_name': model_id,
                'capabilities': ['text', 'thinking'],
                'context_window': 131_072,
                'quality_rank': 10,
            }],
            'providers': [{
                'provider_id': provider_id,
                'name': provider_id,
                'scope': 'owner',
            }],
            'provider_accesses': [{
                'provider_access_id': access_id,
                'provider_id': provider_id,
                'enabled': True,
                'quota_policy': {},
            }],
            'connections': [{
                'connection_id': connection_id,
                'provider_access_id': access_id,
                'base_url': base_url,
                'protocol': 'openai',
                'enabled': True,
                'priority': 0,
                'extra_headers': {},
            }],
            'credentials': [{
                'credential_id': credential_id,
                'provider_access_id': access_id,
                'kind': 'api_key',
                'secret_reference': secret_reference,
                'key_hint': 'configured',
                'enabled': True,
                'authorization': {
                    'connection_ids': [connection_id],
                    'models': [deepcopy(model_ref)],
                },
                'quota_policy': {},
            }],
            'offerings': [{
                'offering_id': offering_id,
                'provider_access_id': access_id,
                'identity_state': 'confirmed',
                'model': deepcopy(model_ref),
                'enabled': True,
                'capabilities': ['text', 'thinking'],
                'context_window': 131_072,
                'priority': 0,
            }],
            'deployments': [{
                'deployment_id': deployment_id,
                'offering_id': offering_id,
                'connection_id': connection_id,
                'wire_model_id': f'wire/{model_id}',
                'enabled': True,
                'identity_confidence': 'high',
                'probe_status': 'passed',
                'priority': 0,
            }],
        },
        'model': deepcopy(model_ref),
        'routing': {'preferred_provider_id': provider_id},
        'credential_secrets': {secret_reference: secret},
    }


__all__ = ['standalone_model_routing_envelope']
