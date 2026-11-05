"""Global Quart error mapping and overload-shedding contracts."""

from __future__ import annotations

import asyncio

import pytest

from lib.app.app_factory import create_base_app
from lib.http_transport.error_handlers import register_http_error_handlers
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
    # The exception TYPE name is a code identifier — it crosses the wire so
    # the client can name the failure class; the message text stays redacted.
    assert failure_payload['error']['detail'] == 'ValueError'
    assert failure_payload['error']['raw'] == ''
    assert 'sensitive failure' not in str(failure_payload)
    assert storage_busy.status_code == 503
    assert storage_busy_payload['error'] == 'database_busy'
    assert storage_busy_payload['operationId'] == 'op-123'
    assert storage_conflict.status_code == 409
    assert storage_conflict_payload['error'] == 'database_conflict'
    assert storage_integrity.status_code == 500
    assert storage_integrity_payload['error'] == 'database_integrity'


def test_domain_error_raised_from_a_storage_outage_keeps_the_storage_503():
    from lib.model_routing import (
        ModelRoutingError, ModelRoutingRepository, OwnerBoundary,
    )

    class _UnavailableClient:
        def query(self, _operation, _payload):
            raise StorageError(
                'database_unavailable', 'Storage sidecar is unavailable',
                retryable=True, retry_after_ms=100)

    def read_routing_authority():
        repository = ModelRoutingRepository()
        repository._client = lambda **_kwargs: _UnavailableClient()  # type: ignore[method-assign]
        repository.get(OwnerBoundary.create(11))

    app = create_base_app('error-handler-domain-outage', {
        'TESTING': True,
        'PROPAGATE_EXCEPTIONS': False,
    })
    register_http_error_handlers(app)

    @app.get('/api/model-routing')
    async def legacy_read():
        read_routing_authority()

    @app.get('/api/v4/model-routing')
    async def v4_read():
        read_routing_authority()

    @app.get('/api/wrapped-integrity')
    async def wrapped_integrity():
        try:
            raise StorageError('database_integrity', 'torn aggregate row')
        except StorageError as exc:
            raise ModelRoutingError(
                'stored aggregate is damaged',
                kind='model_routing_storage_integrity') from exc

    async def exercise():
        async with app.test_app():
            client = app.test_client()
            legacy = await client.get('/api/model-routing')
            v4 = await client.get('/api/v4/model-routing')
            integrity = await client.get('/api/wrapped-integrity')
            return (legacy, await legacy.get_json(), v4, await v4.get_json(),
                    integrity, await integrity.get_json())

    (legacy, legacy_payload, v4, v4_payload,
     integrity, integrity_payload) = asyncio.run(exercise())
    assert legacy.status_code == 503
    assert legacy_payload['error'] == 'database_unavailable'
    assert v4.status_code == 503
    assert v4_payload['code'] == 'storage_unavailable'
    # Only an outage changes the class; a wrapped integrity failure is still
    # an internal fault with its traceback.
    assert integrity.status_code == 500
    assert integrity_payload['error']['kind'] == 'internal'
