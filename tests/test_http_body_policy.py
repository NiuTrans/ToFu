"""Native Quart request-body policy registration and enforcement."""

from __future__ import annotations

import asyncio

import pytest
from quart import request

from lib.app_factory import create_base_app
from lib.http_body_policy import HttpBodyPolicy, register_http_body_policy


pytestmark = pytest.mark.unit


def test_body_caps_timeouts_and_idempotent_registration():
    app = create_base_app('body-policy-test', {'TESTING': True})
    policy = HttpBodyPolicy(
        body_timeout=40,
        upload_body_timeout=80,
        route_caps=(('/upload', 10),),
        default_cap=5,
        long_upload_prefixes=('/upload',),
    )
    assert register_http_body_policy(app, policy) is True
    assert register_http_body_policy(app, policy) is False
    assert app.config['BODY_TIMEOUT'] == 40
    assert app.config['RESPONSE_TIMEOUT'] is None
    assert app.extensions['tofu_http_body_policy'] is policy

    @app.post('/normal')
    async def normal():
        return {'ok': True}

    @app.post('/upload')
    async def upload():
        return {'body_timeout': request.body_timeout}

    @app.post('/stream')
    async def stream():
        await request.get_data()
        return {'ok': True}

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            rejected = await client.post(
                '/normal', data=b'123456', headers={'Content-Length': '6'})
            accepted = await client.post(
                '/upload', data=b'12345678', headers={'Content-Length': '8'})
            rejected_payload = await rejected.get_json()
            accepted_payload = await accepted.get_json()
            async with client.request(
                    '/stream', method='POST',
                    headers={'Content-Type': 'application/octet-stream'}) as conn:
                await conn.send(b'123')
                await conn.send(b'456')
                await conn.send_complete()
            streamed = await conn.as_response()
        return rejected, rejected_payload, accepted_payload, streamed

    rejected, rejected_payload, accepted_payload, streamed = asyncio.run(exercise())
    assert rejected.status_code == 413
    assert rejected_payload['ok'] is False
    assert accepted_payload == {'body_timeout': 80}
    assert streamed.status_code == 413
