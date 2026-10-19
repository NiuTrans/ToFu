"""Real body consumption, file overflow and receiver cancellation ownership."""
from __future__ import annotations

import asyncio
import hashlib
import threading
from types import SimpleNamespace

import pytest
from werkzeug.exceptions import RequestEntityTooLarge

from lib.http_transport.request_body import DemandRequestBody, READ_CHUNK_BYTES, memory_hint_bytes

pytestmark = pytest.mark.unit


def test_hint_scales_in_both_directions_and_zero_uses_file_overflow():
    assert memory_hint_bytes(SimpleNamespace(effective_memory_capacity_mb=1024,effective_memory_available_mb=512)) == 2*1024*1024
    assert memory_hint_bytes(SimpleNamespace(effective_memory_capacity_mb=8192,effective_memory_available_mb=4096)) == 16*1024*1024
    assert memory_hint_bytes(SimpleNamespace(effective_memory_capacity_mb=8192,effective_memory_available_mb=0)) == 0
    assert memory_hint_bytes(SimpleNamespace(effective_memory_capacity_mb=None,effective_memory_available_mb=None)) == READ_CHUNK_BYTES


@pytest.mark.parametrize('hint',[0,1,READ_CHUNK_BYTES])
def test_complete_file_overflow_and_bounded_iteration_preserve_every_byte(hint):
    async def exercise():
        body = DemandRequestBody(None,None,memory_hint=hint)
        source = bytes(range(256)) * 4096
        try:
            await body.append_async(source)
            body.set_complete()
            assert body._data.file is not None
            assert await body == source
            chunks = [chunk async for chunk in body]
            assert max(map(len,chunks)) <= READ_CHUNK_BYTES
            assert hashlib.sha256(b''.join(chunks)).digest() == hashlib.sha256(source).digest()
            assert len(body._data) == 0
            assert body._data.file.seek(0,2) == 0
        finally:
            file = body._data.file
            body.close()
            assert file.closed
    asyncio.run(exercise())


def test_explicit_byte_limit_counts_consumed_chunks_too():
    async def exercise():
        body = DemandRequestBody(None,5,memory_hint=0)
        try:
            await body.append_async(b'123')
            assert await body.__anext__() == b'123'
            await body.append_async(b'456')
            with pytest.raises(RequestEntityTooLarge):
                await body
        finally:
            body.close()
    asyncio.run(exercise())


def test_cancellation_joins_inflight_file_write_before_owner_close():
    async def exercise():
        body = DemandRequestBody(None,None,memory_hint=0)
        entered = threading.Event()
        release = threading.Event()
        original = body._data.extend
        def blocked_write(data):
            entered.set()
            assert release.wait(5)
            original(data)
        body._data.extend = blocked_write
        writer = asyncio.create_task(body.append_async(b'complete'))
        try:
            assert await asyncio.to_thread(entered.wait,5)
            writer.cancel()
            await asyncio.sleep(0)
            assert not writer.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await writer
            assert body._data.materialize() == b'complete'
        finally:
            release.set()
            await asyncio.gather(writer,return_exceptions=True)
            body.close()
    asyncio.run(exercise())


@pytest.mark.slow
def test_complete_video_transport_exceeds_former_512_mib_without_retaining_body_in_ram():
    import time
    from quart import request
    from lib.app.app_factory import create_base_app
    from lib.http_transport.body_policy import build_http_body_policy, register_http_body_policy
    app = create_base_app('large-body-demand',{'TESTING':True})
    register_http_body_policy(app,build_http_body_policy())
    app.extensions['tofu_http_body_memory_hint'] = 0
    payload = b'x' * (1024*1024)
    count = 513
    expected = hashlib.sha256()
    for _ in range(count): expected.update(payload)
    observed = []
    @app.post('/api/v4/videos/upload')
    async def consume():
        digest = hashlib.sha256()
        total = 0
        body = request.body
        observed.append(body)
        async for chunk in body:
            assert len(chunk) <= READ_CHUNK_BYTES
            total += len(chunk)
            digest.update(chunk)
        return {'bytes':total,'digest':digest.hexdigest()}
    async def exercise():
        started = time.monotonic()
        async with app.test_app():
            client = app.test_client()
            async with client.request('/api/v4/videos/upload',method='POST',
                headers={'Content-Type':'application/octet-stream'}) as connection:
                for _ in range(count): await connection.send(payload)
                await connection.send_complete()
            response = await connection.as_response()
            assert response.status_code == 200
            assert await response.get_json() == {'bytes':count*len(payload),'digest':expected.hexdigest()}
        assert observed[0].closed
        assert len(observed[0]._data.memory) == 0
        print(f'http_body_demand: {count*len(payload)} bytes, zero RAM hint, {time.monotonic()-started:.3f}s')
    asyncio.run(exercise())


def test_ram_allocation_failure_preserves_prefix_and_complete_new_chunk_on_disk():
    class FailingBuffer(bytearray):
        def extend(self,data):
            super().extend(data[:1])
            raise MemoryError('injected RAM allocation failure')
    async def exercise():
        body = DemandRequestBody(None,None,memory_hint=1024)
        try:
            body.append(b'prefix-')
            body._data.memory = FailingBuffer(body._data.memory)
            await body.append_async(b'complete')
            body.set_complete()
            assert await body == b'prefix-complete'
            assert body._data.file is not None
        finally:
            body.close()
    asyncio.run(exercise())


def test_incomplete_multipart_closes_partial_parser_files_and_request_overflow():
    from quart import request
    from lib.app.app_factory import create_base_app
    app = create_base_app('partial-multipart',{'TESTING':True})
    observed = []
    @app.post('/partial')
    async def partial():
        observed.append(request._get_current_object())
        files = await request.files
        return {'files':len(files)}
    async def exercise():
        async with app.test_app():
            client = app.test_client()
            async with client.request('/partial',method='POST',headers={'Content-Type':'multipart/form-data; boundary=partial'}) as connection:
                await connection.send(b'--partial\r\nContent-Disposition: form-data; name="file"; filename="partial.bin"\r\nContent-Type: application/octet-stream\r\n\r\n')
                await connection.send(b'x'*(256*1024))
                await connection.send_complete()
            response = await connection.as_response()
            assert response.status_code in (200,400)
        streams = observed[0].__dict__.get('_owned_form_streams', [])
        assert streams and all(stream.closed for stream in streams)
        assert observed[0].body.closed
    asyncio.run(exercise())
