"""Conditional v4 product reads: unchanged data costs one empty 304.

The v4 envelope adds a per-request ``meta`` block, so a validator over the
final body could never match. translate_v4_envelope therefore derives a weak
validator from the handler's own representation, answers a matching
If-None-Match with 304, and leaves writes, failures, handler-owned validators,
cache policy and cookies untouched.
"""
from __future__ import annotations

import asyncio

import pytest
from quart import Blueprint, jsonify

from lib.api_v4 import attach_v4_envelope

pytestmark = pytest.mark.unit


def _probe_app(state: dict):
    from lib.app.app_factory import create_base_app
    from lib.http_transport.compression import register_http_compression
    from lib.http_transport.error_handlers import register_http_error_handlers

    bp = Blueprint('v4_conditional_probe', __name__)

    @bp.route('/api/v4/probe', methods=['GET', 'POST'])
    async def probe():
        return jsonify({'ok': True, 'value': state['value']})

    @bp.get('/api/v4/large')
    async def large():
        return jsonify({'ok': True, 'rows': ['row'] * 400})

    @bp.get('/api/v4/owned')
    async def owned():
        response = jsonify({'ok': True, 'value': state['value']})
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.get('/api/v4/cookie')
    async def cookie():
        response = jsonify({'ok': True, 'value': state['value']})
        response.set_cookie('session', 'rotated')
        return response

    @bp.get('/api/v4/missing')
    async def missing():
        return jsonify({'ok': False, 'error': 'missing'}), 404

    app = create_base_app('v4-conditional', {'TESTING': True})
    register_http_compression(app)
    app.register_blueprint(attach_v4_envelope(bp))
    register_http_error_handlers(app)
    return app


def test_unchanged_read_revalidates_to_an_empty_304():
    state = {'value': 1}
    client = _probe_app(state).test_client()

    async def exercise():
        first = await client.get('/api/v4/probe')
        repeat = await client.get('/api/v4/probe')
        tag = first.headers['ETag']
        assert first.status_code == 200 and tag.startswith('W/"')
        assert first.headers['Cache-Control'] == 'private, no-cache'
        assert repeat.headers['ETag'] == tag
        body = await first.get_json()
        assert body['data'] == {'value': 1} and body['meta']['requestId']

        revalidated = await client.get('/api/v4/probe', headers={'If-None-Match': tag})
        assert revalidated.status_code == 304
        assert await revalidated.get_data() == b''
        assert revalidated.headers['ETag'] == tag
        assert revalidated.headers['Cache-Control'] == 'private, no-cache'

        state['value'] = 2
        changed = await client.get('/api/v4/probe', headers={'If-None-Match': tag})
        assert changed.status_code == 200
        assert (await changed.get_json())['data'] == {'value': 2}
        assert changed.headers['ETag'] != tag

    asyncio.run(exercise())


def test_validator_accepts_proxy_stripped_and_wildcard_forms():
    client = _probe_app({'value': 1}).test_client()

    async def exercise():
        tag = (await client.get('/api/v4/probe')).headers['ETag']
        strong = tag.removeprefix('W/')
        assert (await client.get('/api/v4/probe', headers={'If-None-Match': strong})).status_code == 304
        assert (await client.get('/api/v4/probe', headers={'If-None-Match': '*'})).status_code == 304
        listed = f'"other", {tag}'
        assert (await client.get('/api/v4/probe', headers={'If-None-Match': listed})).status_code == 304

    asyncio.run(exercise())


def test_compressed_success_keeps_the_weak_validator():
    client = _probe_app({'value': 1}).test_client()

    async def exercise():
        compressed = await client.get('/api/v4/large', headers={'Accept-Encoding': 'gzip'})
        assert compressed.headers['Content-Encoding'] == 'gzip'
        tag = compressed.headers['ETag']
        plain = await client.get('/api/v4/large')
        assert plain.headers['ETag'] == tag

        revalidated = await client.get('/api/v4/large', headers={
            'Accept-Encoding': 'gzip', 'If-None-Match': tag})
        assert revalidated.status_code == 304
        assert 'Content-Encoding' not in revalidated.headers
        assert await revalidated.get_data() == b''

    asyncio.run(exercise())


def test_projected_product_route_revalidates_in_the_route_assembly():
    from tests.api.test_api_v4_contract import _bootstrap_app

    client = _bootstrap_app(active_major=1).test_client()

    async def exercise():
        first = await client.get('/api/v4/capabilities')
        assert first.status_code == 200
        tag = first.headers['ETag']
        again = await client.get('/api/v4/capabilities')
        assert again.headers['ETag'] == tag
        assert (await again.get_json())['meta']['requestId'] != (
            await first.get_json())['meta']['requestId']
        revalidated = await client.get('/api/v4/capabilities', headers={'If-None-Match': tag})
        assert revalidated.status_code == 304
        assert await revalidated.get_data() == b''

    asyncio.run(exercise())


def test_writes_failures_and_handler_owned_caching_keep_their_semantics():
    client = _probe_app({'value': 1}).test_client()

    async def exercise():
        write = await client.post('/api/v4/probe', headers={'If-None-Match': '*'})
        assert write.status_code == 200 and 'ETag' not in write.headers

        owned = await client.get('/api/v4/owned', headers={'If-None-Match': '*'})
        assert owned.status_code == 200 and 'ETag' not in owned.headers
        assert owned.headers['Cache-Control'] == 'no-store'

        cookie = await client.get('/api/v4/cookie', headers={'If-None-Match': '*'})
        assert cookie.status_code == 200 and 'ETag' not in cookie.headers
        assert 'session=rotated' in cookie.headers['Set-Cookie']

        missing = await client.get('/api/v4/missing', headers={'If-None-Match': '*'})
        assert missing.status_code == 404 and 'ETag' not in missing.headers

    asyncio.run(exercise())
