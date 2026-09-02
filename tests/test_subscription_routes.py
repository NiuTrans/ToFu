"""Deterministic guards for subscription multi-route selection."""

from __future__ import annotations

import threading
import time
import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from lib.subscription_routes import (
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
        status = manager.status()['routes']['chatgpt.com']['direct']
        assert status['retry_in_s'] == 5.0
        assert manager.candidates(url, [route], wait_timeout=0) == []
        now[0] += 5.1
        assert manager.candidates(url, [route], wait_timeout=1) == [route]
        assert manager.status()['routes']['chatgpt.com']['direct']['healthy']
    finally:
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
    assert is_safe_connect_failure(requests.exceptions.SSLError('tls'))
    assert not is_safe_connect_failure(requests.exceptions.ConnectionError(
        'connection reset after send'))


def test_http_client_fails_over_only_on_safe_connect_error(monkeypatch):
    from lib import http_client

    first, second = _route('p1'), _route('p2')
    response = mock.Mock(status_code=200)
    monkeypatch.setattr(
        http_client, 'subscription_route_candidates',
        lambda _url: [first, second])
    report = mock.Mock()
    monkeypatch.setattr(http_client, 'report_subscription_route', report)
    request = mock.Mock(side_effect=[
        requests.exceptions.ProxyError('proxy unavailable'), response])
    monkeypatch.setattr(http_client, '_sync_request', request)

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
    from lib import http_client

    first, second = _route('p1'), _route('p2')
    monkeypatch.setattr(
        http_client, 'subscription_route_candidates',
        lambda _url: [first, second])
    request = mock.Mock(side_effect=requests.exceptions.ConnectionError(
        'reset after request body'))
    monkeypatch.setattr(http_client, '_sync_request', request)

    with pytest.raises(requests.exceptions.ConnectionError):
        http_client.http_post(
            'https://chatgpt.com/backend-api/codex/responses', json={})
    assert request.call_count == 1


def test_exhausted_route_error_does_not_expose_proxy_credentials(monkeypatch):
    from lib import http_client

    route = Route('pool:secret', 'proxy secret', 'proxy',
                  proxy_url='http://user:password@gw.invalid:8080')
    monkeypatch.setattr(
        http_client, 'subscription_route_candidates', lambda _url: [route])
    monkeypatch.setattr(http_client, 'report_subscription_route', mock.Mock())
    monkeypatch.setattr(
        http_client, '_sync_request',
        mock.Mock(side_effect=requests.exceptions.ProxyError(
            'failed via http://user:password@gw.invalid:8080')))

    with pytest.raises(requests.exceptions.ConnectionError) as caught:
        http_client.http_post(
            'https://chatgpt.com/backend-api/codex/responses', json={})
    assert 'user' not in str(caught.value)
    assert 'password' not in str(caught.value)


def test_async_stream_connect_failover_uses_same_route_plan(monkeypatch):
    from lib.llm import astream

    first, second = _route('p1'), _route('p2')
    response = SimpleNamespace(status_code=200)
    plans = mock.Mock(return_value=[first, second])
    monkeypatch.setattr('lib.proxy.subscription_route_candidates', plans)
    report = mock.Mock()
    monkeypatch.setattr('lib.proxy.report_subscription_route', report)

    class StreamContext:
        def __init__(self, result=None, error=None):
            self.result = result
            self.error = error

        async def __aenter__(self):
            if self.error:
                raise self.error
            return self.result

        async def __aexit__(self, *_args):
            return False

    clients = iter((
        SimpleNamespace(stream=lambda *_a, **_kw: StreamContext(
            error=__import__('httpx').ConnectError('down'))),
        SimpleNamespace(stream=lambda *_a, **_kw: StreamContext(
            result=response)),
    ))
    monkeypatch.setattr(astream, 'get_async_client', lambda _proxy: next(clients))
    plan = SimpleNamespace(
        url='https://chatgpt.com/backend-api/codex/responses',
        hdrs={}, body={})

    async def run():
        async with astream._open_server_stream(plan) as (opened, route):
            assert opened is response
            assert route is second

    asyncio.run(run())
    # Connect failure is immediate. Header receipt is not success anymore: the
    # transport reports the selected route only after the full stream settles.
    assert [call.args[2] for call in report.call_args_list] == [False]
    assert response.extensions['tofu_network_route']['routeId'] == 'p2'


def test_async_desktop_handoff_closes_abandoned_raw_dumper(monkeypatch):
    from lib.llm import astream, stream

    dumper = SimpleNamespace(enabled=True, _fh=object(), finish=mock.Mock())
    plan = SimpleNamespace(
        url='https://chatgpt.com/backend-api/codex/responses',
        raw_dumper=dumper)
    monkeypatch.setattr(astream, 'prepare_request', lambda *_a, **_kw: plan)
    monkeypatch.setattr('lib.desktop.egress.route_request',
                        lambda *_a, **_kw: 'agent-1')
    monkeypatch.setattr(stream, '_stream_chat_once',
                        lambda *_a, **_kw: ({'content': 'ok'}, 'stop', {}))

    result = asyncio.run(astream._async_stream_chat_once(
        {'model': 'gpt-test', 'messages': []}))
    assert result[1] == 'stop'
    dumper.finish.assert_called_once_with(error=True)
