"""HTTP wiring contract for the deterministic integration control plane."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.api


def test_status_route_preserves_explicit_project_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    seen = []
    # The route lazy-loads the control plane via _integration_api() on first
    # use (boot-path deferral), so patch the authority module attribute.
    monkeypatch.setattr(
        'lib.integration_control.integration_status',
        lambda path, *, user_id: (
            seen.append((path, user_id))
            or {'ok': True, 'repo': {'root': path}, 'counts': {}}),
    )
    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)

    async def _run():
        response = await app.test_client().get(
            '/api/v1/project/integration/status?path=%2Ftmp%2Fexplicit-repo')
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    assert body['ok'] is True
    assert body['repo']['root'] == '/tmp/explicit-repo'
    assert seen == [('/tmp/explicit-repo', 1)]


def test_reconcile_route_preserves_explicit_user_and_project_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    seen = []
    monkeypatch.setattr(
        'lib.integration_control.reconcile_candidate_with_head',
        lambda path, *, user_id: (
            seen.append((path, user_id))
            or {'ok': True, 'changed': False, 'candidateSha': 'abc'}),
    )
    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)

    async def _run():
        response = await app.test_client().post(
            '/api/v1/project/integration/reconcile-head',
            json={'path': '/tmp/explicit-repo'},
        )
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 200
    assert body['ok'] is True
    assert body['changed'] is False
    assert seen == [('/tmp/explicit-repo', 1)]


def test_all_control_actions_are_registered() -> None:
    from quart import Quart
    from routes.api_v1.integration import api_v1_integration_bp

    app = Quart(__name__)
    app.register_blueprint(api_v1_integration_bp)
    paths = {rule.rule: set(rule.methods or ()) for rule in app.url_map.iter_rules()}
    base = '/api/v1/project/integration/'
    assert 'GET' in paths[base + 'status']
    for action in ('create', 'register', 'checkpoint', 'submit', 'retry',
                   'discard', 'promote', 'reconcile-head', 'prune'):
        assert 'POST' in paths[base + action]


def test_unexpected_control_plane_failure_is_redacted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route

    def _crash(*_args, **_kwargs):
        raise RuntimeError('/secret/repo?credential=do-not-expose')

    monkeypatch.setattr(
        'lib.integration_control.integration_status', _crash)
    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_integration_bp)

    async def _run():
        response = await app.test_client().get(
            '/api/v1/project/integration/status?path=%2Ftmp%2Frepo')
        return response.status_code, await response.get_json()

    status, body = asyncio.run(_run())
    assert status == 500
    assert body['ok'] is False
    assert '/secret/repo' not in str(body)
    assert 'do-not-expose' not in str(body)


def test_repeated_mutations_preserve_auth_and_domain_errors_without_peer_quota(monkeypatch):
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import integration as route
    import lib.rate_limit.rate_limit_store as store
    monkeypatch.setenv('TOFU_RATE_LIMIT', 'on')
    monkeypatch.setattr(store, 'get_store', lambda: pytest.fail('endpoint counter entered'))
    state = {'authenticated': True, 'failure': False}
    seen = []
    class IntegrationStateError(Exception):
        pass
    def reconcile(path, *, user_id):
        seen.append((path, user_id))
        if state['failure']:
            raise IntegrationStateError('candidate changed')
        return {'ok': True, 'changed': False}
    monkeypatch.setattr(route, '_integration_api', lambda name: reconcile)
    app = Quart(__name__)
    @app.before_request
    async def authenticate():
        g.auth_ctx = local_admin_context() if state['authenticated'] else None
    app.register_blueprint(route.api_v1_integration_bp)
    async def run():
        client = app.test_client()
        for _ in range(129):
            response = await client.post('/api/v1/project/integration/reconcile-head',
                                         json={'path': '/tmp/synthetic-project'})
            assert response.status_code == 200
            assert 'Retry-After' not in response.headers
        state['failure'] = True
        response = await client.post('/api/v1/project/integration/reconcile-head',
                                     json={'path': '/tmp/synthetic-project'})
        assert response.status_code == 400
        state['authenticated'] = False
        response = await client.post('/api/v1/project/integration/reconcile-head',
                                     json={'path': '/tmp/synthetic-project'})
        assert response.status_code == 401
    asyncio.run(run())
    assert len(seen) == 130
    assert all(item == ('/tmp/synthetic-project', 1) for item in seen)
