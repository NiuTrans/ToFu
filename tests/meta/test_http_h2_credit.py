"""Actual TLS/Headless cancellation remains observable with paused H2 DATA."""
import asyncio
from contextlib import asynccontextmanager
import socket

import httpx
import pytest

from benchmarks.http_h2_credit import check
from benchmarks.http_runtime import TOKEN
from benchmarks.http_stream_lifecycle import StreamingRuntime

pytestmark = pytest.mark.api


def test_lifecycle_observer_cannot_be_used_as_performance_sample():
    from benchmarks.native_http import trial
    with pytest.raises(ValueError, match='cannot collect performance samples'):
        trial('owned', 'h2', 'short_requests', None, operation=lambda *args: None)


@asynccontextmanager
async def server(tmp_path, runtime):
    from hypercorn.config import Config, Sockets
    from hypercorn.utils import wrap_app
    from quart import jsonify
    from lib.hypercorn_runtime import worker_serve
    from lib.server.server_tls import ensure_tls_certificates
    from tofu_agent.server import HeadlessServerConfig, create_app

    app = create_app(runtime=runtime, model_routing_setup=object(),
        config=HeadlessServerConfig(token=TOKEN, auth_mode='token', setup_enabled=False))

    @app.get('/benchmark/stream/<task_id>')
    async def observation(task_id):
        item = runtime.get(task_id)
        return jsonify(active=item.active, closed=item.closed, produced=item.produced,
                       status=item.status)

    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); listener.listen(128)
        config = Config()
        config.certfile, config.keyfile = ensure_tls_certificates(
            data_root=str(tmp_path), bind_host='127.0.0.1', environ={})
        config.accesslog = config.errorlog = None
        config.graceful_timeout = 1
        origin = f'https://127.0.0.1:{listener.getsockname()[1]}'
        stop = asyncio.Event()
        task = asyncio.create_task(worker_serve(wrap_app(app, config.wsgi_max_body_size, 'asgi'),
            config, sockets=Sockets([listener], [], []), shutdown_trigger=stop.wait))
        try:
            async with asyncio.timeout(5), httpx.AsyncClient(
                    verify=config.certfile, trust_env=False, timeout=.2) as client:
                while True:
                    if task.done():
                        task.result()
                        raise AssertionError('owned server exited before readiness')
                    try:
                        if (await client.get(origin+'/health/ready')).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.01)
            yield origin, config.certfile
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize('abort_applied', [True, False])
def test_credit_probe_distinguishes_handling_from_delivery_and_joins(tmp_path, monkeypatch, abort_applied):
    if not abort_applied:
        from benchmarks.http_runtime import ExecutionFixture
        monkeypatch.setattr(ExecutionFixture, 'abort', lambda self: True)

    async def run():
        runtime = StreamingRuntime()
        async with server(tmp_path, runtime) as (origin, verify):
            if abort_applied:
                row = await check(origin, verify)
                assert row['starved'] == dict(connection_window=0, sse_bytes=65535,
                                              abort_bytes=0, abort_status=200)
                assert row['stream_credit_released'] == 0
                assert row['connection_credit_released'] == 4096
                assert row['flow_bytes'][1] == 65535
                assert row['flow_bytes'][3] > 0 and row['connection_reused']
            else:
                # HTTP 200 alone must not certify that cancellation was applied.
                with pytest.raises(AssertionError, match='abort was not handled'):
                    await check(origin, verify)
            async with asyncio.timeout(2):
                while any(item.active for item in runtime.executions.values()):
                    await asyncio.sleep(.01)
            assert len(runtime.executions) == 1
            assert all(item.closed == 1 for item in runtime.executions.values())

    asyncio.run(run())
