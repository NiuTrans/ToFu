"""Native Quart response-compression middleware contracts."""

from __future__ import annotations

import asyncio
import gzip
import threading

import pytest
from quart import Response

import lib.http_compression as compression
from lib.app_assembly import configure_application
from lib.app_factory import create_base_app


pytestmark = pytest.mark.unit


def _run(awaitable):
    return asyncio.run(awaitable)


def _test_app():
    app = create_base_app('compression-test', {'TESTING': True})
    assert compression.register_http_compression(app) is True
    assert compression.register_http_compression(app) is False

    @app.get('/large')
    async def large():
        return Response(b'x' * 4096, mimetype='application/json')

    @app.get('/very-large')
    async def very_large():
        return Response(
            b'z' * (compression.LARGE_DYNAMIC_RESPONSE_BYTES + 1),
            mimetype='application/json',
        )

    @app.get('/partial')
    async def partial():
        response = Response(b'x' * 1024, status=206, mimetype='text/javascript')
        response.headers['Content-Range'] = 'bytes 0-1023/4096'
        return response

    return app


def test_large_body_is_gzipped_off_the_serving_loop(monkeypatch):
    app = _test_app()
    compression_threads = []
    real_compress = compression.compress_bytes

    def observed(data, encoding, quality):
        compression_threads.append(threading.get_ident())
        return real_compress(data, encoding, quality)

    monkeypatch.setattr(compression, 'compress_bytes', observed)

    async def exercise():
        loop_thread = threading.get_ident()
        async with app.test_app():
            response = await app.test_client().get(
                '/large', headers={'Accept-Encoding': 'gzip'})
            body = await response.get_data()
        return loop_thread, response, body

    loop_thread, response, body = _run(exercise())
    assert response.headers['Content-Encoding'] == 'gzip'
    assert response.headers['Vary'] == 'Accept-Encoding'
    assert gzip.decompress(body) == b'x' * 4096
    assert compression_threads
    assert all(thread_id != loop_thread for thread_id in compression_threads)


def test_large_personal_dynamic_body_uses_low_cpu_gzip_off_loop(monkeypatch):
    app = _test_app()
    observed = []
    real_compress = compression.compress_bytes

    def record_quality(data, encoding, quality):
        observed.append((threading.get_ident(), encoding, quality))
        return real_compress(data, encoding, quality)

    monkeypatch.setattr(compression, 'compress_bytes', record_quality)
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')

    async def exercise():
        loop_thread = threading.get_ident()
        async with app.test_app():
            response = await app.test_client().get(
                '/very-large', headers={'Accept-Encoding': 'gzip'})
            return loop_thread, response, await response.get_data()

    loop_thread, response, body = _run(exercise())
    assert gzip.decompress(body) == (
        b'z' * (compression.LARGE_DYNAMIC_RESPONSE_BYTES + 1)
    )
    assert observed == [(
        observed[0][0],
        'gzip',
        compression.GZIP_LEVEL_PERSONAL_LARGE,
    )]
    assert observed[0][0] != loop_thread


def test_compression_quality_preserves_distributed_and_cached_bandwidth_policy():
    threshold = compression.LARGE_DYNAMIC_RESPONSE_BYTES
    for encoding, live, personal_large, cached in (
        ('br', 4, 2, 9),
        ('gzip', 6, 1, 6),
    ):
        assert compression.compression_quality(
            encoding, threshold - 1, cached=False, deployment_mode='personal'
        ) == live
        assert compression.compression_quality(
            encoding, threshold, cached=False, deployment_mode='personal'
        ) == personal_large
        assert compression.compression_quality(
            encoding, threshold, cached=False, deployment_mode='distributed'
        ) == live
        assert compression.compression_quality(
            encoding, threshold, cached=True, deployment_mode='personal'
        ) == cached


def test_partial_response_is_never_compressed():
    app = _test_app()

    async def exercise():
        async with app.test_app():
            return await app.test_client().get(
                '/partial', headers={'Accept-Encoding': 'gzip'})

    response = _run(exercise())
    assert response.status_code == 206
    assert 'Content-Encoding' not in response.headers
    assert response.headers['Content-Range'] == 'bytes 0-1023/4096'


def test_application_assembly_serves_compressed_responses(
        monkeypatch, tmp_path):
    import logging
    import routes

    monkeypatch.setattr(routes, 'register_all', lambda *_args, **_kwargs: None)
    app = create_base_app('assembled-compression-test', {'TESTING': True})
    assert configure_application(
        app,
        static_dir=str(tmp_path),
        logger=logging.getLogger('test.assembled-compression'),
        secret_key='test-secret',
    ) is True

    @app.get('/assembled-large')
    async def assembled_large():
        return Response(b'y' * 4096, mimetype='application/json')

    async def exercise():
        async with app.test_app():
            response = await app.test_client().get(
                '/assembled-large', headers={'Accept-Encoding': 'gzip'})
            return response, await response.get_data()

    response, body = _run(exercise())
    assert response.headers['Content-Encoding'] == 'gzip'
    assert gzip.decompress(body) == b'y' * 4096
