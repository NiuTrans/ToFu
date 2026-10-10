"""Boot-gate and boot-projection contracts for the early-serving lifespan.

The production lifespan opens the HTTP listener after the inline phases and
runs the remaining required phases in the background boot runner.  These
tests pin the request-facing contract of that window:

- non-probe ``/api/*`` requests get a truthful 503 (``server_booting`` with
  the live phase, ``server_boot_failed`` with the latched error) instead of
  an unstructured storage failure;
- probes, the boot endpoints and non-API paths stay answerable;
- the gate opens the instant the runner reports ready;
- assemblies without a production runner (test factories, embedders) are
  never fenced.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from lib.app_factory import create_base_app
from lib.production_lifecycle import BootPhase, BootRunner
from lib.http_request_lifecycle import register_request_lifecycle


pytestmark = pytest.mark.unit


class _StubRunner:
    def __init__(self, status):
        self.status = status

    def snapshot(self):
        return {
            'status': self.status,
            'phaseTotal': 9,
            'phases': [],
            'currentPhase': {
                'index': 3, 'key': 'storage_sidecar',
                'label': 'Storage sidecar', 'status': 'running',
                'duration_s': None, 'error': None,
            },
            'failedPhase': {
                'index': 3, 'key': 'storage_sidecar',
                'label': 'Storage sidecar', 'status': 'failed',
                'duration_s': None, 'error': 'RuntimeError: boom',
            } if self.status == 'failed' else None,
            'error': 'phase 3/9 Storage sidecar: RuntimeError: boom'
            if self.status == 'failed' else None,
            'retryCount': 1,
            'elapsedS': 4.2,
            'readyAt': None,
        }


def _gated_app(runner):
    app = create_base_app('boot-gate-test', {'TESTING': True})
    register_request_lifecycle(app)

    @app.route('/api/some-resource')
    async def some_resource():
        return {'ok': True}

    @app.route('/api/health')
    async def health():
        return {'ok': True}

    @app.route('/plain-page')
    async def plain_page():
        return {'ok': True}

    if runner is not None:
        app.extensions['tofu_boot_runner'] = runner
    return app


def test_boot_gate_fences_api_until_ready():
    app = _gated_app(_StubRunner('running'))

    async def call():
        async with app.test_app():
            async with app.test_client() as client:
                resp = await client.get('/api/some-resource')
                return resp.status_code, await resp.get_json()

    status, body = asyncio.run(call())
    assert status == 503
    assert body['ok'] is False
    assert 'server_booting' in str(body)


def test_boot_gate_response_carries_phase_truth():
    app = _gated_app(_StubRunner('running'))

    async def call():
        async with app.test_app():
            async with app.test_client() as client:
                resp = await client.get('/api/some-resource')
                return resp.status_code, await resp.get_json()

    status, body = asyncio.run(call())
    assert status == 503
    assert body['ok'] is False
    assert body['boot']['status'] == 'running'
    assert body['boot']['currentPhase']['key'] == 'storage_sidecar'
    assert body['boot']['phaseTotal'] == 9


def test_boot_gate_failed_reports_latched_error_and_retry_hint():
    app = _gated_app(_StubRunner('failed'))

    async def call():
        async with app.test_app():
            async with app.test_client() as client:
                resp = await client.get('/api/some-resource')
                return resp.status_code, await resp.get_json()

    status, body = asyncio.run(call())
    assert status == 503
    assert body['ok'] is False
    assert 'server_boot_failed' in str(body)
    assert body['boot']['failedPhase']['key'] == 'storage_sidecar'
    assert body['boot']['retryCount'] == 1


def test_boot_gate_allows_probes_and_non_api_paths_while_booting():
    app = _gated_app(_StubRunner('running'))
    for path in ('/api/health', '/plain-page'):
        async def call(path=path):
            async with app.test_app():
                async with app.test_client() as client:
                    return await client.get(path)
        assert asyncio.run(call()).status_code == 200


def test_boot_gate_opens_when_ready_and_absent_without_runner():
    ready_app = _gated_app(_StubRunner('ready'))

    async def call(app):
        async with app.test_app():
            async with app.test_client() as client:
                return await client.get('/api/some-resource')

    assert asyncio.run(call(ready_app)).status_code == 200
    runnerless_app = _gated_app(None)
    assert asyncio.run(call(runnerless_app)).status_code == 200


# ── BootRunner unit contracts ────────────────────────────────────────


def _make_runner(app, *, background, inline=(), stop=None):
    return BootRunner(
        app,
        phase_total=len(inline) + len(background),
        inline_phases=tuple(inline),
        background_phases=tuple(background),
        stop_event=stop or threading.Event(),
        logger=__import__('logging').getLogger('test.boot-runner'),
        boot=lambda *_args, **_kwargs: None,
    )


def _phase(index, fn):
    return BootPhase(
        index=index, key=f'p{index}', label=f'Phase {index}', callback=fn)


def test_boot_runner_snapshot_tracks_phase_progress():
    app = create_base_app('boot-runner-progress', {'TESTING': True})
    entered = []
    release = threading.Event()

    def slow_two():
        entered.append('two')
        release.wait(5)

    runner = _make_runner(app, background=[
        _phase(1, lambda: entered.append('one')),
        _phase(2, slow_two),
        _phase(3, lambda: entered.append('three')),
    ])

    async def exercise():
        async with app.test_app():
            await runner.run_inline()
            runner.start_background()
            while runner.snapshot()['currentPhase'] is None or (
                    runner.snapshot()['currentPhase']['index'] != 2):
                await asyncio.sleep(0.01)
            snapshot = runner.snapshot()
            assert snapshot['status'] == 'running'
            assert snapshot['currentPhase']['index'] == 2
            by_index = {p['index']: p for p in snapshot['phases']}
            assert by_index[1]['status'] == 'done'
            assert by_index[1]['duration_s'] is not None
            assert by_index[2]['status'] == 'running'
            assert by_index[3]['status'] == 'pending'
            release.set()
            assert await runner.wait_done() == 'ready'
            assert runner.snapshot()['readyAt'] is not None

    asyncio.run(exercise())
    assert entered == ['one', 'two', 'three']


def test_boot_runner_retry_is_bounded_to_the_failed_phase():
    app = create_base_app('boot-runner-retry', {'TESTING': True})
    calls = []
    fail = {'two': True}

    def two():
        calls.append('two')
        if fail['two']:
            raise RuntimeError('boom')

    runner = _make_runner(app, background=[
        _phase(1, lambda: calls.append('one')),
        _phase(2, two),
        _phase(3, lambda: calls.append('three')),
    ])

    async def exercise():
        async with app.test_app():
            await runner.run_inline()
            runner.start_background()
            assert await runner.wait_done() == 'failed'
            assert calls == ['one', 'two']
            assert runner.snapshot()['error'] is not None
            assert runner.request_retry() == 'started'
            assert await runner.wait_done() == 'failed'
            fail['two'] = False
            assert runner.request_retry() == 'started'
            assert await runner.wait_done() == 'ready'
            assert runner.request_retry() == 'ready'

    asyncio.run(exercise())
    assert calls == ['one', 'two', 'two', 'two', 'three']
    assert runner.snapshot()['retryCount'] == 2


def test_boot_runner_args_factory_feeds_optional_result_to_announce():
    app = create_base_app('boot-runner-feed', {'TESTING': True})
    seen = []

    runner = _make_runner(app, background=[
        BootPhase(index=1, key='opt', label='Optional',
                  callback=lambda: ({'mcp': 1}, True), result_key='optional'),
        BootPhase(index=2, key='ann', label='Announce',
                  callback=lambda mcp, feishu: seen.append((mcp, feishu)),
                  args_factory=lambda results: (
                      (results['optional'][0], results['optional'][1]), {})),
    ])

    async def exercise():
        async with app.test_app():
            await runner.run_inline()
            runner.start_background()
            assert await runner.wait_done() == 'ready'

    asyncio.run(exercise())
    assert seen == [({'mcp': 1}, True)]


def test_boot_endpoints_project_runner_state():
    import server

    app = server.create_app({'TESTING': True})

    class RouteStub(_StubRunner):
        def __init__(self):
            super().__init__('failed')
            self.retried = 0

        def request_retry(self):
            self.retried += 1
            self.status = 'running'
            return 'started'

    stub = RouteStub()
    app.extensions['tofu_boot_runner'] = stub

    async def call():
        async with app.test_app():
            async with app.test_client() as client:
                boot_resp = await client.get('/api/v4/boot')
                boot_body = await boot_resp.get_json()
                health_resp = await client.get('/api/health')
                health_body = await health_resp.get_json()
                retry_resp = await client.post('/api/v4/boot/retry')
                retry_body = await retry_resp.get_json()
                fenced = await client.get('/api/conversations')
                return (boot_resp.status_code, boot_body,
                        health_body['boot'], retry_resp.status_code,
                        retry_body, fenced.status_code)

    (boot_status, boot_body, health_boot, retry_status, retry_body,
     fenced_status) = asyncio.run(call())
    assert boot_status == 200
    assert boot_body['status'] == 'failed'
    assert boot_body['failedPhase']['key'] == 'storage_sidecar'
    assert health_boot['status'] == 'failed'
    assert health_boot['retryCount'] == 1
    assert retry_status == 200
    assert retry_body['result'] == 'started'
    assert stub.retried == 1
    # A running boot fences storage routes but never the probes above.
    assert fenced_status == 503
