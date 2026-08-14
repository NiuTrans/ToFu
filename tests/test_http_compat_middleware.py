"""Quart HTTP compatibility and static-cache middleware contracts."""

from __future__ import annotations

import asyncio
import json

import pytest
from quart import request

from lib.app_factory import create_base_app
from lib.http_compat_middleware import (
    register_method_override,
    register_static_cache_headers,
)


pytestmark = pytest.mark.unit


def test_method_override_and_double_encoded_json_are_preserved():
    app = create_base_app('compat-test', {'TESTING': True})
    assert register_method_override(app) is True
    assert register_method_override(app) is False

    @app.route('/echo', methods=['POST', 'PATCH'])
    async def echo():
        return {'method': request.method, 'body': await request.get_json()}

    async def exercise():
        async with app.test_app():
            response = await app.test_client().post(
                '/echo?_method=patch',
                data=json.dumps(json.dumps({'value': 42})),
                headers={'Content-Type': 'application/json'},
            )
            return await response.get_json()

    assert asyncio.run(exercise()) == {
        'method': 'PATCH', 'body': {'value': 42},
    }


def test_vite_cache_headers_keep_redirects_uncached():
    app = create_base_app('static-cache-test', {'TESTING': True})
    assert register_static_cache_headers(app) is True
    assert register_static_cache_headers(app) is False

    @app.get('/static/vite/assets/main-1234abcd.js')
    async def bundle():
        return 'const ready = true;'

    @app.get('/static/vite/stale.js')
    async def stale():
        return '', 302, {'Location': '/static/vite/assets/main-1234abcd.js'}

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            bundle_response = await client.get('/static/vite/assets/main-1234abcd.js')
            redirect_response = await client.get('/static/vite/stale.js')
        return bundle_response, redirect_response

    bundle_response, redirect_response = asyncio.run(exercise())
    assert bundle_response.content_type.startswith('text/javascript')
    assert bundle_response.headers['Cache-Control'] == (
        'public, max-age=31536000, immutable')
    assert redirect_response.headers['Cache-Control'] == 'no-store'
