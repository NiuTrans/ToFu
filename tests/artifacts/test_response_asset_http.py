"""Real handlers over temporary storage; test identities use the shared auth boundary."""
import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image
from quart import Quart, g, request
from lib import response_assets as service
from lib.artifacts import core
from routes.response_assets import response_assets_bp
from routes.artifacts import artifacts_bp

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._artifact_sidecar',)


@pytest.fixture
def prepared(artifact_storage, tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'get_conversation', lambda conv, user_id, **kw:
                        {'id': conv} if (conv, user_id) in [('owned', 7), ('other', 8)] else None)
    image = tmp_path / 'chart.png'
    Image.new('RGB', (8, 10), 'red').save(image)
    asset = service.publish_asset(str(image), 'chart', conv_id='owned', user_id=7, roots=[str(tmp_path)])
    payload = {'version': 1, 'title': 'Result', 'blocks': [{'type': 'image_gallery', 'id': 'images', 'title': 'Chart',
                'images': [{'id': asset['id'], 'alt': 'chart'}]}]}
    rich = service.publish_rich(payload, conv_id='owned', user_id=7)
    app = Quart(__name__)
    app.register_blueprint(response_assets_bp)
    app.register_blueprint(artifacts_bp)
    @app.before_request
    async def identity():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner),
                                         key_id='test-owner', tenant_id=None, scopes=[])
    return app, image, asset, rich


def test_http_auth_bytes_mime_proxy_and_no_raw_asset_bypass(prepared):
    app, image, asset, rich = prepared
    async def exercise():
        client = app.test_client()
        url = '/api/response-assets/' + asset['id']
        assert (await client.get(url)).status_code == 401
        denied = await client.get(url, headers={'X-Test-Owner': '8'})
        assert denied.status_code == 404
        response = await client.get(url, headers={'X-Test-Owner': '7'})
        assert response.status_code == 200
        assert response.content_type == 'image/png'
        assert await response.get_data() == image.read_bytes()
        assert response.headers['Cache-Control'].startswith('private')
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        for suffix in ('raw', 'view', 'export'):
            response = await client.get(f'/api/artifacts/{asset["id"]}/{suffix}', headers={'X-Test-Owner': '7'})
            assert response.status_code in (404, 503)  # Export may report renderer unavailable first.
        assert (await client.get(f'/api/artifacts/{rich["id"]}/raw', headers={'X-Test-Owner': '8'})).status_code == 404
        response = await client.get(f'/api/artifacts/{rich["id"]}/raw', headers={'X-Test-Owner': '7'})
        assert response.status_code == 200
        assert response.content_type.split(';')[0] == 'application/json'
    asyncio.run(exercise())


def test_private_lifecycle_and_collection_references(prepared):
    app, _image, asset, rich = prepared
    async def exercise():
        async with app.test_request_context('/api/artifacts', headers={'X-Test-Owner': '8'}):
            await app.preprocess_request()
            assert core.set_pinned(rich['id'], True) is False
            assert core.delete_artifact(asset['id']) is False
            assert core.list_versions(rich['id']) == []
            assert not core.list_artifacts('owned')
        async with app.test_request_context('/api/artifacts', headers={'X-Test-Owner': '7'}):
            await app.preprocess_request()
            assert [row['id'] for row in core.list_artifacts('owned')] == [rich['id']]
            assert core.set_pinned(rich['id'], True) is True
            assert core.delete_artifact(asset['id']) is True
    asyncio.run(exercise())


def test_publication_refuses_wrong_owner_and_contradictory_green(prepared):
    _app, _image, asset, _rich = prepared
    payload = {'version': 1, 'title': 'x', 'blocks': [{'type': 'action_panel', 'id': 'a', 'title': 'a',
               'actions': [{'kind': 'download_asset', 'label': 'image', 'targetId': asset['id']}]}]}
    with pytest.raises(Exception):
        service.publish_rich(payload, conv_id='other', user_id=8)
    payload['blocks'] = [{'type': 'comparison', 'id': 'a', 'title': 'a', 'baselineLabel': 'baseline',
                         'candidateLabel': 'candidate', 'metrics': [{'label': 'latency', 'baseline': 2000,
                          'candidate': 1200, 'unit': 'ms', 'direction': 'lower', 'status': 'passed', 'budget': 1000}]}]
    with pytest.raises(ValueError, match='contradicts'):
        service.publish_rich(payload, conv_id='owned', user_id=7)


def test_inline_artifact_resolution_requires_owner_and_origin(prepared):
    app, _image, _asset, rich = prepared
    legacy = core.create_artifact(conv_id='other', content='private legacy target', format='markdown', source='write_file')
    async def exercise():
        client = app.test_client()
        base = '/api/response-assets/artifacts/'
        assert (await client.get(base + legacy['id'] + '?conversationId=owned', headers={'X-Test-Owner':'7'})).status_code == 404
        assert (await client.get(base + legacy['id'] + '?conversationId=other', headers={'X-Test-Owner':'7'})).status_code == 404
        assert (await client.get(base + legacy['id'], headers={'X-Test-Owner':'8'})).status_code == 404
        response = await client.get(base + legacy['id'] + '?conversationId=other&download=1', headers={'X-Test-Owner':'8'})
        assert response.status_code == 200
        assert await response.get_data() == b'private legacy target'
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert (await client.get(base + rich['id'] + '?conversationId=owned',headers={'X-Test-Owner':'7'})).status_code == 200
    asyncio.run(exercise())


def test_rich_publication_rejects_legacy_dedupe_collision(prepared):
    payload = {'version':1,'title':'collision','blocks':[{'type':'table','id':'t','title':'t','columns':['A'],'rows':[['1']]}]}
    canonical = service.validate_rich(payload,conv_id='owned',user_id=7)
    core.create_artifact(conv_id='owned',content=canonical,format='markdown',source='write_file')
    with pytest.raises(ValueError,match='incompatible'):
        service.publish_rich(payload,conv_id='owned',user_id=7)
