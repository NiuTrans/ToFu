"""Global Quart error mapping and overload-shedding contracts."""

from __future__ import annotations

import asyncio

import pytest

from lib.app_factory import create_base_app
from lib.http_error_handlers import register_http_error_handlers
from lib.storage import StorageError


pytestmark = pytest.mark.unit


def test_api_html_and_storage_error_shapes_remain_distinct():
    app = create_base_app('error-handler-test', {
        'TESTING': True,
        'PROPAGATE_EXCEPTIONS': False,
        'MAX_CONTENT_LENGTH': 1234,
    })
    assert register_http_error_handlers(app) is True
    assert register_http_error_handlers(app) is False

    @app.get('/api/fail')
    async def api_fail():
        raise ValueError('sensitive failure')

    @app.get('/api/storage/<code>')
    async def storage_fail(code):
        raise StorageError(
            code, 'sanitized storage failure', retryable=(code == 'database_busy'),
            retry_after_ms=50, operation_id='op-123')

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            missing_api = await client.get('/api/missing')
            missing_html = await client.get('/missing')
            failure = await client.get('/api/fail')
            storage_busy = await client.get('/api/storage/database_busy')
            storage_conflict = await client.get('/api/storage/database_conflict')
            storage_integrity = await client.get('/api/storage/database_integrity')
            return (
                missing_api, await missing_api.get_json(),
                missing_html, failure, await failure.get_json(),
                storage_busy, await storage_busy.get_json(),
                storage_conflict, await storage_conflict.get_json(),
                storage_integrity, await storage_integrity.get_json(),
            )

    (missing_api, missing_payload, missing_html, failure, failure_payload,
     storage_busy, storage_busy_payload,
     storage_conflict, storage_conflict_payload,
     storage_integrity, storage_integrity_payload) = asyncio.run(exercise())
    assert missing_api.status_code == 404
    assert missing_payload['ok'] is False
    assert missing_html.status_code == 404
    assert 'text/html' in missing_html.content_type
    assert failure.status_code == 500
    assert failure_payload['ok'] is False
    assert failure_payload['error']['kind'] == 'internal'
    assert failure_payload['error']['detail'] == ''
    assert failure_payload['error']['raw'] == ''
    assert 'sensitive failure' not in str(failure_payload)
    assert storage_busy.status_code == 503
    assert storage_busy_payload['error'] == 'database_busy'
    assert storage_busy_payload['operationId'] == 'op-123'
    assert storage_conflict.status_code == 409
    assert storage_conflict_payload['error'] == 'database_conflict'
    assert storage_integrity.status_code == 500
    assert storage_integrity_payload['error'] == 'database_integrity'
