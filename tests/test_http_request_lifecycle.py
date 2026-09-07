"""Native Quart request-correlation middleware registration contracts."""

from __future__ import annotations

import asyncio
import logging

import pytest
from quart.testing import WebsocketResponseError

from lib.app_factory import create_base_app
from lib.api_response import api_error
from lib.http_request_lifecycle import register_request_lifecycle
from lib.log import req_id


pytestmark = pytest.mark.unit


def test_request_id_is_adopted_echoed_and_context_is_cleared():
    app = create_base_app('request-lifecycle-test', {'TESTING': True})
    assert register_request_lifecycle(app) is True
    assert register_request_lifecycle(app) is False

    @app.get('/items/<item_id>')
    async def item(item_id):
        return {'item': item_id, 'request_id': req_id()}

    async def exercise():
        async with app.test_app():
            response = await app.test_client().get(
                '/items/private-identity',
                headers={'X-Request-ID': 'browser-request-42'},
            )
            payload = await response.get_json()
        return response, payload

    response, payload = asyncio.run(exercise())
    assert response.headers['X-Request-ID'] == 'browser-request-42'
    assert payload['request_id'] == 'browser-request-42'
    assert req_id() == ''


def test_browser_protocol_upgrade_does_not_duplicate_domain_warning(caplog):
    app = create_base_app('request-protocol-log-test', {'TESTING': True})
    register_request_lifecycle(app)

    @app.post('/api/browser/poll')
    async def browser_poll():
        return api_error(
            'Browser protocol upgrade required',
            status=426,
            code='browser_protocol_upgrade_required',
        )

    @app.post('/api/other')
    async def other_client_error():
        return api_error('Upgrade required', status=426)

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            return (
                await client.post('/api/browser/poll'),
                await client.post('/api/other'),
            )

    with caplog.at_level(logging.DEBUG, logger='server.lifecycle'):
        browser_response, other_response = asyncio.run(exercise())

    assert browser_response.status_code == 426
    assert other_response.status_code == 426
    response_records = [
        record for record in caplog.records if '← POST ' in record.getMessage()
    ]
    assert any(
        record.levelno == logging.DEBUG
        and '/api/browser/poll 426' in record.getMessage()
        for record in response_records
    )
    assert any(
        record.levelno == logging.WARNING
        and '/api/other 426' in record.getMessage()
        for record in response_records
    )
    assert not any(
        record.levelno >= logging.WARNING
        and '/api/browser/poll 426' in record.getMessage()
        for record in response_records
    )


def test_production_write_fence_rejects_mutation_until_storage_ready(monkeypatch):
    import lib.storage as storage

    app = create_base_app('request-storage-fence', {'TESTING': True})
    register_request_lifecycle(app)
    lifecycle = {'status': 'ready'}
    app.extensions['tofu_production_lifecycle'] = lifecycle

    @app.post('/api/write')
    async def write():
        return {'written': True}

    state = {'ready': False, 'state': 'restarting'}
    monkeypatch.setattr(storage, 'storage_status', lambda: dict(state))

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            fenced = await client.post('/api/write')
            liveness = await client.get('/api/write')
            state.update(ready=True, state='ready')
            accepted = await client.post('/api/write')
            lifecycle['status'] = 'stopping'
            stopping = await client.post('/api/write')
        return fenced, liveness, accepted, stopping

    fenced, liveness, accepted, stopping = asyncio.run(exercise())
    assert fenced.status_code == 503
    assert (await_json := asyncio.run(fenced.get_json()))['error']
    assert liveness.status_code == 405
    assert accepted.status_code == 200
    assert stopping.status_code == 503


def test_distributed_preview_rejects_http_mutation_before_route_execution():
    app = create_base_app('distributed-preview-fence', {'TESTING': True})
    register_request_lifecycle(app, distributed_preview_read_only=True)
    calls = []

    @app.post('/api/write')
    async def write():
        calls.append('write')
        return {'written': True}

    async def exercise():
        async with app.test_app():
            return await app.test_client().post('/api/write')

    response = asyncio.run(exercise())
    body = asyncio.run(response.get_json())
    assert response.status_code == 503
    assert response.content_type == 'application/problem+json'
    assert response.headers['Retry-After'] == '3600'
    assert body['code'] == 'distributed_preview_read_only'
    assert calls == []


def test_distributed_preview_rejects_websocket_before_route_execution():
    app = create_base_app('distributed-preview-websocket', {'TESTING': True})
    register_request_lifecycle(app, distributed_preview_read_only=True)
    calls = []

    @app.websocket('/ws')
    async def ws():
        calls.append('websocket')

    async def exercise():
        async with app.test_app():
            with pytest.raises(WebsocketResponseError) as raised:
                async with app.test_client().websocket('/ws') as connection:
                    await connection.receive()
            return raised.value.response

    response = asyncio.run(exercise())
    body = asyncio.run(response.get_json())
    assert response.status_code == 503
    assert response.content_type == 'application/problem+json'
    assert body['code'] == 'distributed_preview_read_only'
    assert calls == []
