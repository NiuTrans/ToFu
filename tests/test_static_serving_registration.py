"""Native Quart registration boundary for the FUSE-safe static route."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lib.app_factory import create_base_app
from lib.http_compat_middleware import register_static_cache_headers
from lib.static_serving import load_static_bytes, register_static_route


pytestmark = pytest.mark.unit


def test_static_route_registration_is_idempotent_and_instance_local(tmp_path):
    static_dir = tmp_path / 'static'
    static_dir.mkdir()
    (static_dir / 'ready.js').write_text('window.ready = true;')
    (static_dir / 'vite/assets').mkdir(parents=True)
    (static_dir / 'vite/assets/chunk.mjs').write_text('export const ready = true;')
    (static_dir / 'vite/manifest.json').write_text('{}')
    app = create_base_app('static-registration', {'TESTING': True})
    register_static_cache_headers(app)

    async def offload(loop, filename):
        return await loop.run_in_executor(
            None, load_static_bytes, str(static_dir), filename)

    assert register_static_route(
        app, offload=offload, timeout=1,
        logger=logging.getLogger('test.static'),
    ) is True
    assert register_static_route(
        app, offload=offload, timeout=1,
        logger=logging.getLogger('test.static'),
    ) is False

    rules = [rule for rule in app.url_map.iter_rules()
             if rule.rule == '/static/<path:filename>']
    assert len(rules) == 1
    assert rules[0].endpoint == 'tofu_static'

    async def exercise():
        async with app.test_app():
            response = await app.test_client().get('/static/ready.js')
            assert response.status_code == 200
            assert await response.get_data() == b'window.ready = true;'
            assert response.headers['Accept-Ranges'] == 'bytes'
            assert response.content_type.startswith('text/javascript')

            chunk = await app.test_client().get('/static/vite/assets/chunk.mjs')
            assert chunk.status_code == 200
            assert chunk.content_type.startswith('text/javascript')
            assert chunk.headers['Cache-Control'] == (
                'public, max-age=31536000, immutable')

            manifest = await app.test_client().get('/static/vite/manifest.json')
            assert manifest.status_code == 200
            assert manifest.headers['Cache-Control'] == 'no-store'

    asyncio.run(exercise())
