"""Native plugin requests preserve gate, body, owner and approval semantics."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from quart import g, request

from lib.api_v4 import product_blueprints_for_policy
from lib.app.app_factory import create_base_app
from lib.identity import PrincipalContext


pytestmark = pytest.mark.unit


@pytest.fixture
def native_trading(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'plugins/tofu-trading'))
    from tofu_trading import web
    from tofu_trading.web.handlers import trading_reconcile

    state = {'enabled': True, 'writes': [], 'reads': []}
    monkeypatch.setattr(web, 'trading_enabled', lambda: state['enabled'])

    async def execute(sql, parameters, *, domain):
        state['writes'].append((sql, parameters, domain))

    async def fetchall(sql, parameters, *, domain):
        state['reads'].append((sql, parameters, domain))
        return []

    monkeypatch.setattr(trading_reconcile, 'async_execute', execute)
    monkeypatch.setattr(trading_reconcile, 'async_fetchall', fetchall)
    app = create_base_app('native-trading-api', {'TESTING': True})

    @app.before_request
    def request_owner():
        owner = request.headers.get('X-Test-Owner')
        g.principal_context = (
            PrincipalContext.user(subject_id='plugin-test-owner', owner_user_id=int(owner))
            if owner else PrincipalContext.system(subject_id='unowned-test', scopes=()))

    for blueprint in web.register():
        for mounted in product_blueprints_for_policy(blueprint, legacy_enabled=False, v4_enabled=True):
            app.register_blueprint(mounted)
    return app, state


def test_native_plugin_cannot_default_an_unowned_principal_to_a_user(native_trading):
    app, state = native_trading

    async def run():
        response = await app.test_client().get('/api/v4/trading/reconcile/target')
        assert response.status_code == 403
        assert response.content_type == 'application/problem+json'
        assert state['reads'] == state['writes'] == []

    asyncio.run(run())


def test_native_plugin_reads_and_writes_preserve_request_owner_and_unapproved_intent(native_trading):
    app, state = native_trading

    async def run():
        client = app.test_client()
        for owner in (17, 29):
            response = await client.post('/api/v4/trading/reconcile/target',
                                         headers={'X-Test-Owner': str(owner)},
                                         json={'symbol': 'EXAMPLE', 'target_weight': 25})
            assert response.status_code == 200
            body = await response.get_json()
            assert body['data'] == {'symbol': 'EXAMPLE', 'target_weight': 25, 'approved': 0}
            assert 'requestId' in body['meta']
            result = await client.get('/api/v4/trading/reconcile/target',
                                      headers={'X-Test-Owner': str(owner)})
            assert (await result.get_json())['data']['targets'] == []
        assert [write[1][0] for write in state['writes']] == [17, 29]
        assert all(write[1][6] == 0 for write in state['writes'])
        assert [read[1] for read in state['reads']] == [(17,), (29,)]

    asyncio.run(run())


def test_native_plugin_validation_and_live_gate_precede_storage(native_trading):
    app, state = native_trading

    async def run():
        client = app.test_client()
        headers = {'X-Test-Owner': '17'}
        invalid = await client.post('/api/v4/trading/reconcile/target', headers=headers,
                                    json={'symbol': 'EXAMPLE', 'target_weight': 'invalid'})
        assert invalid.status_code == 400
        assert invalid.content_type == 'application/problem+json'
        assert (await invalid.get_json())['detail'] == 'target_weight must be numeric'
        state['enabled'] = False
        blocked = await client.post('/api/v4/trading/reconcile/target', headers=headers,
                                    json={'symbol': 'EXAMPLE', 'target_weight': 25})
        assert blocked.status_code == 404
        assert 'trading_disabled' in (await blocked.get_json())['detail']
        assert (await client.get('/trading.html', headers=headers)).status_code == 404
        assert (await client.get('/trading-static/js/trading/entry.js', headers=headers)).status_code == 404
        assert state['writes'] == state['reads'] == []
        state['enabled'] = True
        assert (await client.get('/api/v4/trading/reconcile/target', headers=headers)).status_code == 200
        assert len(state['reads']) == 1

    asyncio.run(run())
