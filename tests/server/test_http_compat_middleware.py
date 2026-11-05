"""Quart HTTP compatibility and static-cache middleware contracts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from quart import request

from lib.app.app_factory import create_base_app
from lib.http_transport.compat_middleware import (
    register_method_override,
    register_static_cache_headers,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('method', ['POST', 'PUT'])
@pytest.mark.parametrize('body', [
    b'{"value":"normal JSON"}', b' \r\n\t[1,2,3]', b'123',
    b'true', b'false', b'null', b'{invalid JSON', b'\xff', b'',
])
def test_non_string_json_bodies_are_not_parsed_by_proxy_repair(
        monkeypatch, method, body):
    """Only a JSON string can be a proxy's extra encoding layer.

    Ordinary JSON and malformed non-string data belong to the handler's
    parser. The compatibility hook must not parse and discard them first.
    """
    from lib.http_transport import compat_middleware

    def unexpected_parse(_value):
        pytest.fail('proxy repair decoded a body without an encoding layer')

    monkeypatch.setattr(compat_middleware, 'json',
                        SimpleNamespace(loads=unexpected_parse,
                                        JSONDecodeError=json.JSONDecodeError))
    app = create_base_app('compat-no-double-parse', {'TESTING': True})
    register_method_override(app)

    @app.route('/echo', methods=['POST', 'PUT'])
    async def echo():
        return await request.get_data(), 200, {'Content-Type': 'application/octet-stream'}

    async def exercise():
        async with app.test_app():
            response = await app.test_client().open(
                '/echo', method=method, data=body,
                headers={'Content-Type': 'application/json'})
            assert response.status_code == 200
            assert await response.get_data() == body

    asyncio.run(exercise())


@pytest.mark.parametrize('body', [b'"unterminated', b'"not JSON inside"'])
def test_unrepairable_json_strings_keep_the_original_bytes(body):
    app = create_base_app('compat-invalid-encoding', {'TESTING': True})
    register_method_override(app)

    @app.post('/echo')
    async def echo():
        return await request.get_data(), 200, {'Content-Type': 'application/octet-stream'}

    async def exercise():
        async with app.test_app():
            response = await app.test_client().post(
                '/echo', data=body, headers={'Content-Type': 'application/json'})
            assert response.status_code == 200
            assert await response.get_data() == body

    asyncio.run(exercise())


def test_query_method_override_is_retired_but_double_encoded_json_is_unwrapped():
    """The proxy-body repair stays; ``?_method=`` no longer mutates verbs."""
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
        'method': 'POST', 'body': {'value': 42},
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
