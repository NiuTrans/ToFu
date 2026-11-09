"""Deterministic guards for subscription multi-route selection."""

from __future__ import annotations

import threading
import time
import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from lib.network_routes import (
    ProbeResult,
    Route,
    RouteManager,
    is_safe_connect_failure,
)

pytestmark = pytest.mark.unit


def _route(route_id: str, priority: int = 0) -> Route:
    mode = 'direct' if route_id == 'direct' else 'proxy'
    return Route(route_id, route_id, mode, priority=priority,
                 proxy_url='' if mode == 'direct'
                 else f'http://{route_id}.invalid:8080')


def test_cold_race_returns_first_success_without_waiting_for_slow_failure():
    slow_done = threading.Event()

    def probe(_url, route):
        if route.route_id == 'slow':
            time.sleep(0.2)
            slow_done.set()
            return ProbeResult('network_fail')
        time.sleep(0.01)
        return ProbeResult('ok', 10.0, 401)

    manager = RouteManager(probe=probe, jitter=lambda value: value)
    try:
        started = time.monotonic()
        out = manager.candidates(
            'https://chatgpt.com/backend-api/codex/responses',
            [_route('slow'), _route('fast')], wait_timeout=1)
        elapsed = time.monotonic() - started
        assert [route.route_id for route in out] == ['fast']
        assert elapsed < 0.15
        assert not slow_done.is_set()
        assert slow_done.wait(1)
    finally:
        manager.close()


def test_cold_race_can_wait_for_better_quality_without_waiting_for_all():
    slow_done = threading.Event()

    def probe(_url, route):
        if route.route_id == 'sso':
            time.sleep(0.01)
            return ProbeResult('ok', 10.0, 302, quality=1)
        if route.route_id == 'git':
            time.sleep(0.04)
            return ProbeResult('ok', 40.0, 200, quality=2)
        time.sleep(0.2)
        slow_done.set()
        return ProbeResult('network_fail')

    manager = RouteManager(probe=probe, jitter=lambda value: value)
    try:
        started = time.monotonic()
        out = manager.candidates(
            'https://git.example.test/repo.git/info/refs',
            [_route('sso'), _route('git'), _route('slow')],
            wait_timeout=1, wait_for_all=True, minimum_quality=2)
        elapsed = time.monotonic() - started
        assert [route.route_id for route in out] == ['git', 'sso']
        assert 0.03 <= elapsed < 0.15
        assert not slow_done.is_set()
        assert slow_done.wait(1)
    finally:
        manager.close()


def test_higher_quality_route_overrides_lower_quality_incumbent():
    manager = RouteManager(
        probe=lambda _url, route: ProbeResult(
            'ok', 10.0 if route.route_id == 'direct' else 40.0, 200,
            quality=1 if route.route_id == 'direct' else 2),
        jitter=lambda value: value)
    url = 'https://git.example.test/repo.git/info/refs'
    direct, proxy = _route('direct'), _route('proxy')
    try:
        assert manager.candidates(url, [direct], wait_timeout=1) == [direct]
        # The known incumbent returns immediately while the new route probes
        # in the background.
        assert manager.candidates(url, [direct, proxy], wait_timeout=1) == [direct]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            route_status = manager.status()['routes'].get('https://git.example.test:443', {})
            if route_status.get('proxy', {}).get('healthy'):
                break
            time.sleep(0.005)
        assert manager.cached_candidates(url, [direct, proxy])[0] == proxy
    finally:
        manager.close()


def test_cold_race_wait_is_promptly_cancellable_without_killing_probe_worker():
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()

    def probe(_url, _route):
        entered.set()
        release.wait(1)
        return ProbeResult('network_fail')

    manager = RouteManager(probe=probe, jitter=lambda value: value)
    timer = threading.Timer(0.05, cancelled.set)
    try:
        timer.start()
        started = time.monotonic()
        out = manager.candidates(
            'https://chatgpt.com/backend-api/codex/responses',
            [_route('direct')], wait_timeout=1,
            cancelled=cancelled.is_set)
        assert entered.is_set()
        assert out == []
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
        timer.cancel()
        manager.close()


def test_probe_singleflight_is_per_host_and_route():
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def probe(_url, _route):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        release.wait(1)
        return ProbeResult('ok', 12.0, 401)

    manager = RouteManager(probe=probe, jitter=lambda value: value)
    route = _route('direct')
    results = []

    def choose():
        results.append(manager.candidates(
            'https://chatgpt.com/backend-api/codex/responses',
            [route], wait_timeout=1))

    threads = [threading.Thread(target=choose) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(1)
        release.set()
        for thread in threads:
            thread.join(1)
        assert calls == 1
        assert len(results) == 6
        assert all(result[0].route_id == 'direct' for result in results)
    finally:
        release.set()
        manager.close()


def test_max_pool_plus_direct_and_env_all_enter_same_cold_race():
    release = threading.Event()
    all_entered = threading.Event()
    entered = set()
    lock = threading.Lock()

    def probe(_url, route):
        with lock:
            entered.add(route.route_id)
            if len(entered) == 18:  # 16 pool rows + direct + env
                all_entered.set()
        release.wait(1)
        return ProbeResult('network_fail')

    manager = RouteManager(probe=probe, jitter=lambda value: value)
    routes = [_route('direct'), _route('env')]
    routes.extend(_route(f'pool-{idx}') for idx in range(16))
    worker = threading.Thread(target=lambda: manager.candidates(
        'https://chatgpt.com/backend-api/codex/responses', routes,
        wait_timeout=1))
    try:
        worker.start()
        assert all_entered.wait(1), entered
    finally:
        release.set()
        worker.join(2)
        manager.close()


def test_network_circuit_half_opens_and_recovers():
    now = [100.0]
    outcomes = iter((ProbeResult('network_fail'),
                     ProbeResult('ok', 20.0, 401)))
    manager = RouteManager(
        probe=lambda _url, _route: next(outcomes),
        clock=lambda: now[0], jitter=lambda value: value)
    route = _route('direct')
    url = 'https://chatgpt.com/backend-api/codex/responses'
    try:
        assert manager.candidates(url, [route], wait_timeout=1) == []
        status = manager.status()['routes']['https://chatgpt.com:443']['direct']
        assert status['retry_in_s'] == 5.0
        assert manager.candidates(url, [route], wait_timeout=0) == []
        now[0] += 5.1
        assert manager.candidates(url, [route], wait_timeout=1) == [route]
        assert manager.status()['routes']['https://chatgpt.com:443']['direct']['healthy']
    finally:
        manager.close()


def test_probe_recovery_admits_single_route_without_forgiving_request_backoff():
    now = [100.0]
    probe = mock.Mock(return_value=ProbeResult('ok', 10.0, 401))
    manager = RouteManager(
        probe=probe, clock=lambda: now[0], jitter=lambda value: value)
    route = _route('proxy')
    url = 'https://chatgpt.com/backend-api/codex/responses'
    try:
        assert manager.candidates(url, [route], wait_timeout=1) == [route]
        for expected_delay in (5.0, 15.0, 30.0, 60.0, 120.0, 120.0):
            manager.report(url, route, False)
            assert manager.recovery_delay(url, [route]) == expected_delay
            calls_before = probe.call_count
            for _ in range(5):
                assert manager.candidates(url, [route], wait_timeout=0) == []
            assert probe.call_count == calls_before

            now[0] += expected_delay
            assert manager.candidates(url, [route], wait_timeout=1) == [route]
            # Reachability is enough to admit the only route, even though
            # another failed real setup must pay the next backoff interval.
            assert manager.cached_candidates(url, [route]) == [route]
            assert manager.verdict(url, [route]) == 'ok'
            assert manager.recovery_delay(url, [route]) == 0.0
        assert probe.call_count == 7

        manager.report(url, route, True, 20.0)
        assert manager.cached_candidates(url, [route]) == [route]
        manager.report(url, route, False)
        assert manager.recovery_delay(url, [route]) == 5.0
    finally:
        manager.close()


def test_real_request_success_recovers_early_and_clears_backoff_debt():
    now = [100.0]
    probe = mock.Mock(return_value=ProbeResult('network_fail'))
    manager = RouteManager(
        probe=probe, clock=lambda: now[0], jitter=lambda value: value)
    route = _route('proxy')
    url = 'https://chatgpt.com/backend-api/codex/responses'
    try:
        for expected_delay in (5.0, 15.0, 30.0):
            manager.report(url, route, False)
            assert manager.recovery_delay(url, [route]) == expected_delay
        # An already-running sibling can prove recovery before the circuit
        # expires. All other callers must immediately share that evidence.
        manager.report(url, route, True, 20.0)
        assert manager.candidates(url, [route], wait_timeout=0) == [route]
        assert manager.recovery_delay(url, [route]) == 0.0
        assert manager.status()['routes']['https://chatgpt.com:443']['proxy']['failures'] == 0
        assert probe.call_count == 0
        manager.report(url, route, False)
        assert manager.recovery_delay(url, [route]) == 5.0
    finally:
        manager.close()


@pytest.mark.parametrize('probe_verdict', ['ok', 'network_fail'])
@pytest.mark.parametrize('request_verdict', [
    'ok', 'network_fail', 'policy_blocked', 'proxy_auth', 'midstream_io',
])
def test_singleflight_probe_cannot_overwrite_newer_request_outcome(
        probe_verdict, request_verdict):
    now = [100.0]
    entered = threading.Event()
    release = threading.Event()
    probe = mock.Mock()

    def run_probe(_url, _route):
        entered.set()
        assert release.wait(2)
        return ProbeResult(probe_verdict, 1.0, 401 if probe_verdict == 'ok' else 0)

    probe.side_effect = run_probe
    manager = RouteManager(
        probe=probe, clock=lambda: now[0], jitter=lambda value: value)
    route = _route('proxy')
    url = 'https://chatgpt.com/backend-api/codex/responses'
    results = []

    def choose():
        results.append(manager.candidates(
            url, [route], wait_timeout=0, force_probe=True))

    callers = [threading.Thread(target=choose) for _ in range(8)]
    try:
        manager.report(url, route, True, 20.0)
        for caller in callers:
            caller.start()
        assert entered.wait(1)
        for caller in callers:
            caller.join(1)
            assert not caller.is_alive()
        assert results == [[route]] * len(callers)
        assert probe.call_count == 1
        # Synchronize publication, not just the probe function's return. The
        # fake clock deliberately stays unchanged across both observations.
        with manager._lock:
            pending_probe = manager._inflight[('https://chatgpt.com:443', 'proxy')]
        manager.report(url, route, request_verdict == 'ok', 40.0,
                       failure_kind=request_verdict)
        expected_status = manager.status()
        expected_plan = manager.cached_candidates(url, [route])
        expected_delay = manager.recovery_delay(url, [route])
        release.set()
        pending_probe.result(timeout=1)
        assert manager.status() == expected_status
        assert manager.cached_candidates(url, [route]) == expected_plan
        assert manager.recovery_delay(url, [route]) == expected_delay
        assert probe.call_count == 1
    finally:
        release.set()
        for caller in callers:
            if caller.ident is not None:
                caller.join(2)
        manager.close()


def test_request_probe_fencing_is_isolated_by_host_and_route():
    entered = threading.Event()
    release = threading.Event()

    def probe(_url, _route):
        entered.set()
        assert release.wait(2)
        return ProbeResult('ok', 10.0, 401)

    manager = RouteManager(probe=probe, jitter=lambda value: value)
    route, other_route = _route('proxy'), _route('other-proxy')
    url = 'https://chatgpt.com/backend-api/codex/responses'
    other_url = 'https://api.anthropic.com/v1/messages'
    try:
        assert manager.candidates(url, [route], wait_timeout=0) == []
        assert entered.wait(1)
        with manager._lock:
            pending_probe = manager._inflight[('https://chatgpt.com:443', 'proxy')]
        manager.report(other_url, route, False)
        manager.report(url, other_route, False)
        release.set()
        pending_probe.result(timeout=1)
        assert manager.cached_candidates(url, [route, other_route]) == [route]
        assert manager.cached_candidates(other_url, [route]) == []
    finally:
        release.set()
        manager.close()


def test_health_isolated_by_target_host():
    manager = RouteManager(
        probe=lambda _url, _route: ProbeResult('ok', 10.0, 401),
        jitter=lambda value: value)
    route = _route('shared-proxy')
    chatgpt = 'https://chatgpt.com/backend-api/codex/responses'
    anthropic = 'https://api.anthropic.com/v1/messages'
    try:
        assert manager.candidates(chatgpt, [route], wait_timeout=1)
        manager.report(chatgpt, route, False)
        assert manager.candidates(chatgpt, [route], wait_timeout=0) == []
        assert manager.candidates(anthropic, [route], wait_timeout=1) == [route]
    finally:
        manager.close()


def test_preferred_route_hysteresis_prevents_latency_flap():
    manager = RouteManager(
        probe=lambda _url, _route: ProbeResult('network_fail'),
        jitter=lambda value: value)
    url = 'https://chatgpt.com/backend-api/codex/responses'
    direct, proxy = _route('direct'), _route('proxy')
    try:
        manager.report(url, direct, True, 100)
        assert manager.cached_candidates(url, [direct, proxy]) == [direct]
        manager.report(url, proxy, True, 90)
        assert manager.cached_candidates(url, [direct, proxy])[0] == direct
        manager.report(url, proxy, True, 20)
        manager.report(url, proxy, True, 20)
        assert manager.cached_candidates(url, [direct, proxy])[0] == proxy
    finally:
        manager.close()


def test_reset_discards_late_probe_result():
    entered = threading.Event()
    release = threading.Event()

    def probe(_url, _route):
        entered.set()
        release.wait(1)
        return ProbeResult('ok', 10, 401)

    manager = RouteManager(probe=probe, jitter=lambda value: value)
    route = _route('direct')
    url = 'https://chatgpt.com/backend-api/codex/responses'
    worker = threading.Thread(
        target=lambda: manager.candidates(url, [route], wait_timeout=1))
    try:
        worker.start()
        assert entered.wait(1)
        manager.reset()
        release.set()
        worker.join(1)
        assert manager.status()['routes'] == {}
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize('lifecycle_action', ['reset', 'close'])
def test_probe_publication_is_fenced_atomically_with_reset_and_close(
        monkeypatch, lifecycle_action):
    publishing = threading.Event()
    release = threading.Event()
    manager = RouteManager(probe=lambda *_: ProbeResult('ok', 1.0, 401))
    record = manager._record

    def paused_record(*args, **kwargs):
        if kwargs.get('source') == 'probe':
            publishing.set()
            assert release.wait(2)
        return record(*args, **kwargs)

    monkeypatch.setattr(manager, '_record', paused_record)
    route = _route('proxy')
    url = 'https://chatgpt.com/backend-api/codex/responses'
    closer = None
    try:
        manager.report(url, route, True, 20.0)
        manager.candidates(url, [route], wait_timeout=0, force_probe=True)
        assert publishing.wait(1)
        with manager._lock:
            pending_probe = manager._inflight[('https://chatgpt.com:443', 'proxy')]
        if lifecycle_action == 'reset':
            manager.reset()
        else:
            closer = threading.Thread(target=manager.close)
            closer.start()
            deadline = time.monotonic() + 1
            while manager.status()['routes']:
                assert time.monotonic() < deadline
                time.sleep(0.005)
            # Already-running requests may also finish during shutdown.
            manager.report(url, route, True, 30.0)
        release.set()
        pending_probe.result(timeout=1)
        if closer is not None:
            closer.join(1)
            assert not closer.is_alive()
        assert manager.status()['routes'] == {}
    finally:
        release.set()
        if closer is not None:
            closer.join(2)
        manager.close()


def test_probe_executor_retires_after_each_batch_and_rebuilds_lazily():
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls = 0
    calls_lock = threading.Lock()

    def probe(_url, _route):
        nonlocal calls
        with calls_lock:
            batch = calls // 2
            calls += 1
            if calls % 2 == 0:
                entered[batch].set()
        release[batch].wait(1)
        return ProbeResult('network_fail')

    manager = RouteManager(
        probe=probe, jitter=lambda value: value, max_workers=2)
    url = 'https://chatgpt.com/backend-api/codex/responses'
    routes = [_route('direct'), _route('proxy')]

    def run_batch():
        manager.candidates(
            url, routes, wait_timeout=1, force_probe=True)

    try:
        first_call = threading.Thread(target=run_batch)
        first_call.start()
        assert entered[0].wait(1)
        with manager._lock:
            first_executor = manager._executor
            first_threads = tuple(first_executor._threads)
        assert len(first_threads) == 2
        assert all(thread.is_alive() for thread in first_threads)
        release[0].set()
        first_call.join(2)

        deadline = time.monotonic() + 1
        while (manager._executor is not None
               or any(thread.is_alive() for thread in first_threads)):
            assert time.monotonic() < deadline
            time.sleep(0.01)

        second_call = threading.Thread(target=run_batch)
        second_call.start()
        assert entered[1].wait(1)
        with manager._lock:
            second_executor = manager._executor
            second_threads = tuple(second_executor._threads)
        assert second_executor is not first_executor
        assert len(second_threads) == 2
        release[1].set()
        second_call.join(2)

        deadline = time.monotonic() + 1
        while (manager._executor is not None
               or any(thread.is_alive() for thread in second_threads)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        release[0].set()
        release[1].set()
        manager.close()


def test_route_repr_never_exposes_proxy_credentials():
    route = Route('pool:hk', 'proxy hk', 'proxy',
                  proxy_url='http://secret:password@gw.invalid:8080')
    assert 'secret' not in repr(route)
    assert 'password' not in repr(route)


def test_only_unambiguous_connect_failures_are_replay_safe():
    assert is_safe_connect_failure(requests.exceptions.ConnectTimeout('down'))
    assert is_safe_connect_failure(requests.exceptions.ProxyError('down'))
    from lib.http_transport.connection_setup import TLSConnectionSetupError
    assert is_safe_connect_failure(requests.exceptions.SSLError(TLSConnectionSetupError('handshake')))
    assert not is_safe_connect_failure(requests.exceptions.SSLError('response TLS failure'))
    assert not is_safe_connect_failure(requests.exceptions.ConnectionError(
        'connection reset after send'))


def test_http_client_fails_over_only_on_safe_connect_error(monkeypatch):
    from lib.http_transport import client as http_client
    from lib import proxy

    first, second = _route('p1'), _route('p2')
    response = requests.Response()
    response.status_code = 200
    response._content = b'ok'
    monkeypatch.setattr(
        proxy, 'network_route_candidates',
        lambda _url, **kw: [first, second])
    report = mock.Mock()
    monkeypatch.setattr(proxy, 'report_network_route', report)
    request = mock.Mock(side_effect=[
        requests.exceptions.ProxyError('proxy unavailable'), response])
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', request)

    out = http_client.http_post(
        'https://chatgpt.com/backend-api/codex/responses', json={})
    assert out is response
    assert request.call_count == 2
    assert request.call_args_list[0].kwargs['proxies']['https'].startswith(
        'http://p1.')
    assert request.call_args_list[1].kwargs['proxies']['https'].startswith(
        'http://p2.')
    assert [call.args[2] for call in report.call_args_list] == [False, True]


def test_http_client_never_replays_ambiguous_connection_reset(monkeypatch):
    from lib.http_transport import client as http_client
    from lib import proxy

    first, second = _route('p1'), _route('p2')
    monkeypatch.setattr(
        proxy, 'network_route_candidates',
        lambda _url, **kw: [first, second])
    request = mock.Mock(side_effect=requests.exceptions.ConnectionError(
        'reset after request body'))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', request)

    with pytest.raises(requests.exceptions.ConnectionError):
        http_client.http_post(
            'https://chatgpt.com/backend-api/codex/responses', json={})
    assert request.call_count == 1


def test_exhausted_route_error_does_not_expose_proxy_credentials(monkeypatch):
    from lib.http_transport import client as http_client
    from lib import proxy

    route = Route('pool:secret', 'proxy secret', 'proxy',
                  proxy_url='http://user:password@gw.invalid:8080')
    monkeypatch.setattr(
        proxy, 'network_route_candidates', lambda _url, **kw: [route])
    monkeypatch.setattr(proxy, 'report_network_route', mock.Mock())
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter, 'send',
        mock.Mock(side_effect=requests.exceptions.ProxyError(
            'failed via http://user:password@gw.invalid:8080')))

    with pytest.raises(requests.exceptions.ConnectionError) as caught:
        http_client.http_post(
            'https://chatgpt.com/backend-api/codex/responses', json={})
    assert 'user' not in str(caught.value)
    assert 'password' not in str(caught.value)


def test_async_stream_connect_failover_uses_same_route_plan(monkeypatch):
    import httpx
    from lib.llm import astream
    from lib.http_transport.outbound_async import managed_async_client

    first, second = _route('p1'), _route('p2')
    monkeypatch.setattr('lib.proxy.network_route_candidates', lambda *a, **k: [first, second])
    report = mock.Mock()
    monkeypatch.setattr('lib.proxy.report_network_route', report)
    calls = []
    async def send(_transport, request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError('down')
        return httpx.Response(200, content=b'ok')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    plan = SimpleNamespace(url='https://chatgpt.com/backend-api/codex/responses',
                           hdrs={}, body={}, network_scope='subscription')
    async def run():
        async with managed_async_client() as client:
            monkeypatch.setattr(astream, 'get_async_client', lambda **_: client)
            async with astream._open_server_stream(plan) as (opened, route):
                assert await opened.aread() == b'ok'
                assert route is second
                assert opened.extensions['tofu_network_route']['routeId'] == 'p2'
    asyncio.run(run())
    assert len(calls) == 2
    assert calls[0] is calls[1]
    assert [call.args[2] for call in report.call_args_list] == [False, True]
