"""HTTP wiring for the user-tier site pack management routes."""

from __future__ import annotations

import asyncio

import pytest

from lib.site_packs import reset_cache_for_tests

pytestmark = pytest.mark.api


def _app():
    from lib.api_keys import local_admin_context
    from quart import Quart, g
    from routes.api_v1 import site_packs as route

    app = Quart(__name__)

    @app.before_request
    async def _auth():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(route.api_v1_site_packs_bp)
    return app


@pytest.fixture(autouse=True)
def _user_tier(tmp_path, monkeypatch):
    fake = lambda *parts: str(tmp_path.joinpath(*parts))
    # Module-level ``from ... import config_path`` bindings must be patched in
    # each consuming namespace; otherwise tests would write the real data dir.
    monkeypatch.setattr('lib.config_dir.config_path', fake)
    monkeypatch.setattr('lib.site_packs._loader.config_path', fake)
    monkeypatch.setattr('routes.api_v1.site_packs.config_path', fake)
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


def _pack_doc(pack_id='intranet-wiki', domain='wiki.internal.example'):
    return {
        'pack': 'tofu.site-pack/v1',
        'id': pack_id,
        'name': 'Intranet Wiki',
        'version': 1,
        'routing_hint': 'page keywords',
        'domains': [domain],
        'auth': {'login_url': f'https://{domain}/login',
                 'fields': [{'name': 'sid', 'importance': 'required'}]},
        'operations': [{
            'name': 'search',
            'description': '搜索内网 wiki 页面',
            'input_schema': {'type': 'object',
                             'properties': {'query': {'type': 'string'},
                                            'limit': {'type': 'integer'}}},
            'steps': [
                {'do': 'navigate', 'url': f'https://{domain}/search?q={{query}}'},
                {'do': 'ensure_auth'},
                {'do': 'normalize', 'kind': 'search_records',
                 'query_from_param': 'query', 'limit_from_param': 'limit',
                 'source_name': 'Intranet Wiki'},
            ],
        }],
    }


def test_install_list_and_delete_round_trip(tmp_path):
    async def _run():
        client = _app().test_client()
        install = await client.post('/api/v1/site-packs', json=_pack_doc())
        assert install.status_code == 200
        installed = (await install.get_json())['data' if 'data' in await install.get_json() else 'pack']
        listing = await client.get('/api/v1/site-packs')
        body = await listing.get_json()
        packs = body.get('packs') or body['data']['packs']
        by_id = {row['id']: row for row in packs}
        assert by_id['intranet-wiki']['layer'] == 'user'
        assert by_id['intranet-wiki']['writable'] is True
        assert by_id['intranet-wiki']['auth_projected'] is True
        assert by_id['xiaohongshu']['layer'] == 'core'
        assert by_id['xiaohongshu']['writable'] is False

        delete = await client.delete('/api/v1/site-packs/intranet-wiki')
        assert delete.status_code == 200
        listing = await client.get('/api/v1/site-packs')
        body = await listing.get_json()
        packs = body.get('packs') or body['data']['packs']
        assert 'intranet-wiki' not in {row['id'] for row in packs}
        return installed

    installed = asyncio.run(_run())
    assert installed['operations'][0]['name'] == 'search'


def test_install_rejects_invalid_manifest():
    async def _run():
        client = _app().test_client()
        bad = _pack_doc()
        bad['operations'][0]['steps'].append({'do': 'teleport'})
        response = await client.post('/api/v1/site-packs', json=bad)
        assert response.status_code == 400
        again = await client.post('/api/v1/site-packs', json={'not': 'a pack'})
        assert again.status_code == 400

    asyncio.run(_run())


def test_delete_unknown_user_pack_is_404():
    async def _run():
        client = _app().test_client()
        response = await client.delete('/api/v1/site-packs/never-existed')
        assert response.status_code == 404
        # Core packs are deployment content: not deletable over HTTP.
        response = await client.delete('/api/v1/site-packs/xiaohongshu')
        assert response.status_code == 404

    asyncio.run(_run())


def test_user_pack_overrides_core_layer(tmp_path):
    doc = _pack_doc(pack_id='xiaohongshu', domain='xiaohongshu.com')

    async def _run():
        client = _app().test_client()
        response = await client.post('/api/v1/site-packs', json=doc)
        body = await response.get_json()
        assert response.status_code == 200
        assert (body.get('overridden_core_or_bundle')
                or body['data']['overridden_core_or_bundle']) is True
        delete = await client.delete('/api/v1/site-packs/xiaohongshu')
        body = await delete.get_json()
        restored = (body.get('core_or_bundle_restored')
                    or body['data']['core_or_bundle_restored'])
        assert restored['id'] == 'xiaohongshu'
        assert restored['layer'] == 'core'

    asyncio.run(_run())
