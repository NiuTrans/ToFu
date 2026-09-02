"""Executable contract for the normalized model-catalog vertical slice."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from quart import Quart, g

from lib.api_keys import AuthContext
from lib.identity import PrincipalContext
from lib.model_catalog import (
    CONTRACT_VERSION,
    ModelCatalogError,
    catalog_from_providers,
    normalize_catalog,
    offering_id,
    project_providers,
    public_provider_metadata,
    resolve_catalog,
)


pytestmark = pytest.mark.unit


def _model(model_id: str, *, capabilities: list[str] | None = None,
           enabled: bool = True, **extra) -> dict:
    row = {
        'model_id': model_id,
        'enabled': enabled,
        'capabilities': capabilities or ['text'],
        'rpm': 30,
    }
    row.update(extra)
    return row


def _providers() -> list[dict]:
    return [
        {
            'id': 'alpha',
            'name': 'Alpha',
            'protocol': 'openai',
            'base_url': 'https://alpha.invalid/v1',
            'api_keys': ['alpha-secret'],
            'models': [_model(
                'shared', capabilities=['text'], request_ids=['wire-alpha'])],
        },
        {
            'id': 'beta',
            'label': 'Beta label',
            'protocol': 'anthropic',
            'base_url': 'https://beta.invalid/v1',
            'api_keys': ['beta-secret'],
            'models': [_model(
                'shared', capabilities=['thinking'], request_ids=['wire-beta'])],
        },
    ]


def test_schema_accepts_compiler_output_and_ids_are_deterministic():
    catalog = catalog_from_providers(_providers())
    schema_path = Path(__file__).parents[1] / 'contracts/model_catalog_v1.schema.json'
    schema = json.loads(schema_path.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(catalog)

    assert catalog['contract_version'] == CONTRACT_VERSION
    assert catalog['revision'] == 0
    assert list(catalog['models']) == ['shared']
    assert catalog['models']['shared']['capabilities'] == ['text', 'thinking']
    alpha_id = offering_id('alpha', 'shared')
    beta_id = offering_id('beta', 'shared')
    assert list(catalog['offerings']) == sorted([alpha_id, beta_id])
    assert catalog['routes']['shared']['offering_ids'] == sorted([
        alpha_id, beta_id,
    ])


def test_projection_round_trip_and_stale_snapshot_preserves_authored_state():
    providers = _providers()
    initial = catalog_from_providers(providers)
    disabled = copy.deepcopy(initial)
    disabled['revision'] = 1
    disabled['models']['shared']['enabled'] = False
    for offering in disabled['offerings'].values():
        offering['enabled'] = False
    disabled = normalize_catalog(disabled, provider_ids={'alpha', 'beta'})

    projected = project_providers(
        [{key: value for key, value in provider.items() if key != 'models'}
         for provider in providers],
        disabled,
    )
    assert all(provider['_catalog_revision'] == 1 for provider in projected)
    assert all(
        model['enabled'] is False
        for provider in projected for model in provider['models']
    )
    round_trip = catalog_from_providers(projected, previous=disabled)
    assert all(
        offering['enabled'] is False
        for offering in round_trip['offerings'].values()
    )

    # A legacy Settings snapshot with no revision marker is stale. It cannot
    # silently re-enable the authored catalog even if its old rows say true.
    stale = catalog_from_providers(providers, previous=disabled)
    assert all(
        offering['enabled'] is False for offering in stale['offerings'].values()
    )


@pytest.mark.parametrize('mutation,match', [
    (lambda value: value.update(revision=True), 'revision must be an integer'),
    (lambda value: value['models']['shared'].update(model_id='other'),
     'model key/body identity mismatch'),
    (lambda value: value['offerings'][next(iter(value['offerings']))].update(
        provider_id='unknown'), 'unknown provider'),
    (lambda value: value['routes']['shared'].update(offering_ids=[]),
     'route/offering mismatch'),
    (lambda value: value['routes']['shared']['offering_ids'].append(
        value['routes']['shared']['offering_ids'][0]), 'contains duplicate'),
    (lambda value: value['offerings'][next(iter(value['offerings']))][
        'configuration'].update(cost=0.01), 'cost'),
    (lambda value: value['offerings'][next(iter(value['offerings']))][
        'configuration'].update(rpm=True), 'rpm must be numeric'),
])
def test_normalizer_rejects_identity_reference_and_legacy_cost_drift(
        mutation, match):
    catalog = catalog_from_providers(_providers())
    mutation(catalog)
    with pytest.raises(ModelCatalogError, match=match):
        normalize_catalog(catalog, provider_ids={'alpha', 'beta'})


def test_provider_metadata_is_an_object_map_and_never_leaks_connections():
    metadata = public_provider_metadata({'providers': _providers()})
    assert metadata == {
        'alpha': {
            'id': 'alpha', 'name': 'Alpha', 'protocol': 'openai',
        },
        'beta': {
            'id': 'beta', 'label': 'Beta label', 'protocol': 'anthropic',
        },
    }
    encoded = json.dumps(metadata)
    assert 'api_keys' not in encoded
    assert 'base_url' not in encoded
    assert 'secret' not in encoded


def _route_app(principal: PrincipalContext) -> Quart:
    from routes.api_v1.model_catalog import api_v1_model_catalog_bp

    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True

    @app.before_request
    def _identity() -> None:
        g.auth_ctx = AuthContext(
            key_id='catalog-test',
            name='catalog-test',
            scopes=frozenset({'admin'}),
            owner_user_id=principal.owner_user_id,
        )
        g.principal_context = principal

    app.register_blueprint(api_v1_model_catalog_bp)
    return app


def _configure_route_test(tmp_path: Path, monkeypatch) -> tuple[Path, list[str]]:
    import lib
    import lib.llm_dispatch
    import routes.config
    import routes.api_v1.model_catalog as route

    config_path = tmp_path / 'server_config.json'
    config_path.write_text(json.dumps({
        'unrelated': {'keep': True},
        'providers': _providers(),
    }), encoding='utf-8')
    monkeypatch.setattr(routes.config, '_SERVER_CONFIG_PATH', str(config_path))
    calls: list[str] = []
    monkeypatch.setattr(lib, 'reload_config', lambda: calls.append('reload'))
    monkeypatch.setattr(
        lib.llm_dispatch, 'reset_dispatcher', lambda: calls.append('reset'))
    monkeypatch.setattr(route, '_offering_health', lambda catalog: {
        offering_id: {'healthy': True, 'status': 'healthy'}
        for offering_id in catalog['offerings']
    })
    monkeypatch.setattr(route, 'audit_log', lambda *_args, **_kwargs: None)
    return config_path, calls


def test_get_is_read_only_and_put_is_atomic_cas_with_secret_redaction(
        tmp_path, monkeypatch):
    config_path, calls = _configure_route_test(tmp_path, monkeypatch)
    before = config_path.read_bytes()
    app = _route_app(PrincipalContext.user(
        subject_id='catalog-test',
        owner_user_id=41,
        scopes={'admin'},
    ))

    async def exercise():
        client = app.test_client()
        response = await client.get('/api/v1/model-catalog')
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload['revision'] == payload['catalog']['revision'] == 0
        assert config_path.read_bytes() == before
        encoded_providers = json.dumps(payload['providers'])
        assert 'api_keys' not in encoded_providers
        assert 'base_url' not in encoded_providers
        assert 'secret' not in encoded_providers

        first, second = await asyncio.gather(
            app.test_client().put('/api/v1/model-catalog', json={
                'expected_revision': 0,
                'catalog': payload['catalog'],
            }),
            app.test_client().put('/api/v1/model-catalog', json={
                'expected_revision': 0,
                'catalog': payload['catalog'],
            }),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        winner = first if first.status_code == 200 else second
        loser = second if winner is first else first
        winner_payload = await winner.get_json()
        loser_payload = await loser.get_json()
        assert winner_payload['revision'] == 1
        assert loser_payload['current_revision'] == 1

    asyncio.run(exercise())
    persisted = json.loads(config_path.read_text(encoding='utf-8'))
    assert persisted['unrelated'] == {'keep': True}
    assert persisted['model_catalog']['revision'] == 1
    assert all(provider['api_keys'] for provider in persisted['providers'])
    assert calls == ['reload', 'reset']


def test_put_rejects_unknown_provider_without_write_or_reload(
        tmp_path, monkeypatch):
    config_path, calls = _configure_route_test(tmp_path, monkeypatch)
    config = json.loads(config_path.read_text(encoding='utf-8'))
    catalog = resolve_catalog(config)
    oid = next(iter(catalog['offerings']))
    catalog['offerings'][oid]['provider_id'] = 'not-configured'
    before = config_path.read_bytes()
    app = _route_app(PrincipalContext.user(
        subject_id='catalog-test', owner_user_id=41, scopes={'admin'}))

    async def exercise():
        response = await app.test_client().put(
            '/api/v1/model-catalog',
            json={'expected_revision': 0, 'catalog': catalog},
        )
        assert response.status_code == 400

    asyncio.run(exercise())
    assert config_path.read_bytes() == before
    assert calls == []


def test_route_denies_admin_principal_without_owner(tmp_path, monkeypatch):
    _configure_route_test(tmp_path, monkeypatch)
    app = _route_app(PrincipalContext.system(
        subject_id='ownerless-admin', scopes={'admin'}))

    async def exercise():
        response = await app.test_client().get('/api/v1/model-catalog')
        assert response.status_code == 403

    asyncio.run(exercise())
