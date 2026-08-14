"""Native Quart request-correlation middleware registration contracts."""

from __future__ import annotations

import asyncio

import pytest

from lib.app_factory import create_base_app
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
