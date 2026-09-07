"""Fast JSON response contract for large, schema-validated DTOs."""

from __future__ import annotations

import asyncio
import json

import orjson
import pytest

from lib.app_factory import create_base_app


pytestmark = pytest.mark.unit


def _app():
    return create_base_app('prevalidated-payload-test', {'TESTING': True})


def test_prevalidated_payload_uses_compact_utf8_bytes_without_mutating_input():
    from lib.api_response import api_prevalidated_payload

    app = _app()
    payload = {'contract': 'tofu.test/v1', 'title': '豆腐'}

    async def exercise():
        async with app.test_request_context('/api/test'):
            response, status = api_prevalidated_payload(payload, cursor=7)
            return response, status, await response.get_data()

    response, status, body = asyncio.run(exercise())
    assert status == 200
    assert response.mimetype == 'application/json'
    assert body == orjson.dumps({
        'contract': 'tofu.test/v1',
        'title': '豆腐',
        'ok': True,
        'cursor': 7,
    })
    assert payload == {'contract': 'tofu.test/v1', 'title': '豆腐'}


def test_prevalidated_payload_falls_back_to_framework_json_provider(monkeypatch):
    import lib.api_response as api_response

    app = _app()

    def reject_fast_encoding(_body):
        raise TypeError('synthetic unsupported value')

    monkeypatch.setattr(api_response.orjson, 'dumps', reject_fast_encoding)

    async def exercise():
        async with app.test_request_context('/api/test'):
            response, status = api_response.api_prevalidated_payload({'value': 3})
            return response, status, await response.get_data()

    response, status, body = asyncio.run(exercise())
    assert status == 200
    assert response.mimetype == 'application/json'
    assert json.loads(body) == {'ok': True, 'value': 3}
