"""Native Quart response-compression middleware contracts."""

from __future__ import annotations

import asyncio
import gzip
import threading

import pytest
from quart import Response

import lib.http_compression as compression
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


def test_server_assembly_registers_extracted_compression_boundary():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / 'server.py').read_text()
    assembly = (Path(__file__).resolve().parents[1]
                / 'lib/app_assembly.py').read_text()
    assert 'from lib.http_compression import' not in source
    assert 'from lib.http_compression import register_http_compression' \
        in assembly
    assert 'configure_application(' in source
    assert 'register_http_compression(app)' in assembly
    assert 'async def _compress_response' not in source
