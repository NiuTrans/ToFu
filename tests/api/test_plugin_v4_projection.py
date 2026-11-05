"""Plugin registration shares product versioning and blueprint policies."""
from __future__ import annotations

import asyncio

import pytest
from quart import Blueprint, abort, request

from lib.api_v4 import configure_api_version_policy, copy_blueprint_to_v4
from lib.app.app_factory import create_base_app


pytestmark = pytest.mark.unit


def test_projected_hooks_keep_the_original_policy_owner():
    class Policy:
        calls = 0

        def admit(self):
            self.calls += 1

    policy = Policy()
    source = Blueprint('owned_policy', __name__, url_prefix='/api/v1/optional')
    source.before_request(policy.admit)
    source.add_url_rule('/status', view_func=lambda: {'value': 7})
    projected = copy_blueprint_to_v4(source, name='owned_policy_v4')
    projected_calls = []
    projected.before_request(lambda: projected_calls.append(request.path))
    app = create_base_app('bound-plugin-policy', {'TESTING': True})
    app.register_blueprint(source)
    app.register_blueprint(projected)

    async def run():
        client = app.test_client()
        assert (await client.get('/api/v1/optional/status')).status_code == 200
        assert (await client.get('/api/v4/optional/status')).status_code == 200
        assert policy.calls == 2
        assert projected_calls == ['/api/v4/optional/status']

    asyncio.run(run())


def test_projection_preserves_blueprint_guards_and_response_hooks():
    source = Blueprint('guarded_plugin', __name__)
    allowed = False
    calls = []

    @source.before_request
    def guard():
        if not allowed:
            abort(403)

    @source.errorhandler(403)
    def refusal(_error):
        return {'error': 'plugin_disabled'}, 403

    @source.after_request
    def trace(response):
        response.headers['X-Plugin-Policy'] = 'preserved'
        return response

    @source.teardown_request
    def finish(_error):
        calls.append('finished')

    @source.get('/api/v1/example')
    def view():
        return {'value': 7}

    app = create_base_app('plugin-projection', {'TESTING': True})
    app.register_blueprint(copy_blueprint_to_v4(source, name='guarded_plugin_v4'))

    async def run():
        nonlocal allowed
        client = app.test_client()
        denied = await client.get('/api/v4/example')
        assert denied.status_code == 403
        assert await denied.get_json() == {'error': 'plugin_disabled'}
        assert denied.headers['X-Plugin-Policy'] == 'preserved'
        allowed = True
        accepted = await client.get('/api/v4/example')
        assert await accepted.get_json() == {'value': 7}
        assert accepted.headers['X-Plugin-Policy'] == 'preserved'
        assert calls == ['finished', 'finished']

    asyncio.run(run())


@pytest.mark.parametrize('major,v4_enabled', [(4, True), (1, True), (1, False)])
def test_discovered_product_plugin_uses_the_active_api_major(monkeypatch, major, v4_enabled):
    import routes
    from routes import api_v4
    from lib.app import plugin_registry

    product = Blueprint('optional_product', __name__)
    product.add_url_rule('/api/v1/optional/status', view_func=lambda: {'value': 7})
    page = Blueprint('optional_page', __name__)
    page.add_url_rule('/optional.html', view_func=lambda: '<h1>Optional</h1>')
    monkeypatch.setattr(routes, 'ALL_BLUEPRINTS', [])
    monkeypatch.setattr(routes, 'ALL_V1_BLUEPRINTS', [])
    monkeypatch.setattr(api_v4, 'API_V4_PRODUCT_BLUEPRINTS', ())
    monkeypatch.setattr(plugin_registry, 'discover_blueprint_plugins', lambda: [product, page])
    app = create_base_app('plugin-registration', {
        'TESTING': True, 'ACTIVE_API_MAJOR': major, 'API_V4_ENABLED': v4_enabled,
    })
    configure_api_version_policy(app)
    routes.register_all(app)

    async def run():
        client = app.test_client()
        result = await client.get('/api/v4/optional/status')
        assert result.status_code == (200 if v4_enabled else 404)
        if v4_enabled:
            body = await result.get_json()
            assert body['data'] == {'value': 7}
            assert 'requestId' in body['meta']
        legacy = await client.get('/api/v1/optional/status')
        assert legacy.status_code == (200 if major == 1 else 404)
        if major == 1:
            assert await legacy.get_json() == {'value': 7}
        page_result = await client.get('/optional.html')
        assert await page_result.get_data() == b'<h1>Optional</h1>'

    asyncio.run(run())
