"""Unreachable network endpoints: a connect-phase outage is waitable.

Incident 2026-11-04 (task ea45335a, conv mvxvjudbaclqza): the gateway host's
DNS lookup failed for about 30 s (``gaierror``). Dispatch cooled and excluded
each Credential x Deployment pair on that host and spent a hard attempt per
key, so three keys of claude-opus-5.5 failed within one second. The task then
fell back to kimi-k3.1-pro on the same host (three more instant failures),
pool rescue ended on a protocol-incompatible candidate, and a 30-minute turn
failed while sibling conversations recovered at 13:34:03.

Contract pinned here:
  * the failure holds every slot whose ``base_url`` names the same host and
    port; slots on another endpoint (same model or, non-strict, another
    model) serve at once with no waiting;
  * when only held endpoints can serve, dispatch probes again on the fixed
    schedule shared with no-channel holds: no hard attempt, no slot health
    penalty or cooldown, recovery on the first served probe, abortable;
  * budgeted callers (explicit ``max_429_attempts``) keep a hard attempt per
    observation and surface the typed endpoint error; the opt-in outage
    budget bounds the wait like a gateway 5xx storm;
  * a non-strict pick passes over a route whose protocol cannot represent the
    request's native continuity instead of ending the dispatch on it.
"""
import asyncio
import threading
import time

import pytest

import lib.llm_dispatch.api as api
from lib.llm.conversion import ProtocolConversionError
from lib.llm.llm_errors import AbortedError, EndpointUnreachableError
from lib.llm_dispatch._endpoint_outage import network_endpoint
from lib.llm_dispatch.provider_pin import provider_pin

pytestmark = pytest.mark.unit

_PIN = 'model-route:9:test'
_LOGICAL = 'claude-opus-5.5'
_GATEWAY = 'https://aigc.example/v1/anthropic'
_OTHER = 'https://api.other.example/v1'
_MESSAGES = [{'role': 'user', 'content': 'hi'}]
_OK = ('ok', 'stop', {'prompt_tokens': 3, 'completion_tokens': 2})
_REASON = 'Endpoint unreachable'


class _Clock:
    """Monotonic time and sleeps that only the dispatching thread moves."""

    def __init__(self):
        self.now = 1_000_000.0
        self.sleeps = []
        self._thread = threading.get_ident()
        self._real_monotonic = time.monotonic
        self._real_sleep = time.sleep

    def monotonic(self):
        if threading.get_ident() != self._thread:
            return self._real_monotonic()
        return self.now

    def sleep(self, seconds, *args, **kwargs):
        if threading.get_ident() != self._thread:
            return self._real_sleep(seconds)
        self.sleeps.append(round(float(seconds), 3))
        self.now += float(seconds)


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    monkeypatch.delenv('TOFU_GATEWAY_OUTAGE_BUDGET_S', raising=False)
    monkeypatch.delenv('TOFU_429_SATURATION_SECS', raising=False)
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    yield clock


def _slot(key, model, *, rank, base_url=_GATEWAY, logical=_LOGICAL):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-' + key, model=model,
                logical_model=logical, provider_id=_PIN,
                routing_provider_id='gateway', route_rank=rank,
                base_url=base_url, capabilities={'text'})


def _plain_slot(key, model, base_url):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-' + key, model=model,
                base_url=base_url, capabilities={'text'})


def _install(monkeypatch, *slots):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    dispatcher = LLMDispatcher()
    dispatcher.slots = list(slots)
    dispatcher._initialized = True
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    return dispatcher


def _unreachable(url):
    raise EndpointUnreachableError(
        'Endpoint unreachable during TCP/TLS connection setup (gaierror)',
        base_url=url)


def _stream_transport(monkeypatch, decide):
    calls = []

    def _stream(body, **kwargs):
        calls.append((kwargs.get('api_key'), kwargs.get('base_url')))
        return decide(len(calls), kwargs.get('base_url') or '')

    monkeypatch.setattr('lib.llm.stream_chat', _stream)
    return calls


def _gateway_down_until(n_failures):
    def decide(call_no, base_url):
        if base_url == _GATEWAY and call_no <= n_failures:
            _unreachable(base_url)
        return _OK
    return decide


def _schedule():
    from lib.llm_dispatch._route_unavailable import ROUTE_UNAVAILABLE_BACKOFF_S
    return list(ROUTE_UNAVAILABLE_BACKOFF_S)


def _untouched(slot):
    return (slot.consecutive_errors, slot.total_errors, slot.gateway_errors,
            slot.cooldown_until, slot.inflight) == (0, 0, 0, 0.0, 0)


class TestNetworkEndpointIdentity:

    @pytest.mark.parametrize('first, second', [
        ('https://aigc.example/v1/anthropic', 'https://aigc.example/v1/openai/native'),
        ('https://AIGC.example/v1', 'https://aigc.example:443/other'),
        ('http://10.0.0.1/v1', 'http://10.0.0.1:80/'),
    ])
    def test_paths_and_default_ports_share_one_endpoint(self, first, second):
        assert network_endpoint(first) == network_endpoint(second) is not None

    @pytest.mark.parametrize('first, second', [
        ('http://10.0.0.1:8080/v1', 'http://10.0.0.1:8081/v1'),
        ('https://aigc.example/v1', 'https://api.other.example/v1'),
        ('http://aigc.example/v1', 'https://aigc.example/v1'),
    ])
    def test_hosts_and_ports_are_distinct_endpoints(self, first, second):
        assert network_endpoint(first) != network_endpoint(second)

    @pytest.mark.parametrize('url', ['', None, 'not a url', 'http://h:99999/'])
    def test_unnamed_endpoints_have_no_identity(self, url):
        assert network_endpoint(url) is None

    def test_ipv6_literal(self):
        assert network_endpoint('http://[::1]:8000/v1') == ('::1', 8000)


class TestSharedEndpointWaitsAndRecovers:

    def test_dns_outage_probes_on_schedule_and_never_fails_the_turn(
            self, clock, monkeypatch):
        slots = [_slot(f'k{i}', 'kunlun.claude', rank=i) for i in range(3)]
        _install(monkeypatch, *slots)
        calls = _stream_transport(monkeypatch, _gateway_down_until(4))
        retries = []

        with provider_pin(_PIN):
            msg, finish, _usage = api.dispatch_stream(
                _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                max_retries=1, log_prefix='[T]',
                on_retry=lambda **kw: retries.append(kw))

        assert (msg, finish) == ('ok', 'stop')
        # One request per probe: the other keys behind the same dead host are
        # never tried back to back, and no hard attempt is spent.
        assert calls == [('sk-test-k0', _GATEWAY)] * 5
        assert clock.sleeps == _schedule()[:4]
        assert [r['reason'] for r in retries] == [_REASON] * 4
        assert [r['attempt'] for r in retries] == [1, 2, 3, 4]
        assert {r['status_code'] for r in retries} == {0}
        # The network path failed, not the keys: no health or cooldown.
        assert all(_untouched(slot) for slot in slots[1:])
        assert (slots[0].consecutive_errors, slots[0].total_errors,
                slots[0].inflight) == (0, 0, 0)
        assert slots[0].last_success_time > 0

    def test_backoff_is_capped(self, clock, monkeypatch):
        _install(monkeypatch, _slot('k0', 'kunlun.claude', rank=0))
        schedule = _schedule()
        _stream_transport(monkeypatch, _gateway_down_until(len(schedule) + 2))

        with provider_pin(_PIN):
            msg, _finish, _usage = api.dispatch_stream(
                _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                log_prefix='[T]')

        assert msg == 'ok'
        assert clock.sleeps == schedule + [schedule[-1]] * 2

    def test_wait_honours_abort(self, clock, monkeypatch):
        slot = _slot('k0', 'kunlun.claude', rank=0)
        _install(monkeypatch, slot)
        calls = _stream_transport(monkeypatch, _gateway_down_until(10 ** 6))
        stop = []

        with provider_pin(_PIN), pytest.raises(AbortedError):
            api.dispatch_stream(
                _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                log_prefix='[T]', abort_check=lambda: bool(stop),
                on_retry=lambda **kw: stop.append(kw))

        assert len(calls) == 1
        assert slot.inflight == 0

    def test_budgeted_caller_spends_a_hard_attempt_per_probe(
            self, clock, monkeypatch):
        _install(monkeypatch, _slot('k0', 'kunlun.claude', rank=0),
                 _slot('k1', 'kunlun.claude', rank=1))
        calls = _stream_transport(monkeypatch, _gateway_down_until(10 ** 6))

        with provider_pin(_PIN), pytest.raises(EndpointUnreachableError) as caught:
            api.dispatch_stream(
                _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                max_retries=3, max_429_attempts=5, log_prefix='[T]')

        assert len(calls) == 3
        assert clock.sleeps == _schedule()[:2]
        message = str(caught.value)
        assert 'Endpoint unreachable during connection setup' in message
        assert _LOGICAL in message

    def test_opt_in_outage_budget_bounds_the_wait(self, clock, monkeypatch):
        monkeypatch.setenv('TOFU_GATEWAY_OUTAGE_BUDGET_S', '10')
        _install(monkeypatch, _slot('k0', 'kunlun.claude', rank=0))
        calls = _stream_transport(monkeypatch, _gateway_down_until(10 ** 6))

        with provider_pin(_PIN), pytest.raises(EndpointUnreachableError):
            api.dispatch_stream(
                _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                max_retries=1, log_prefix='[T]')

        assert len(calls) == 3
        assert clock.sleeps == _schedule()[:3]


class TestOtherEndpointsServeWithoutWaiting:

    def test_strict_group_moves_to_a_route_on_another_endpoint(
            self, clock, monkeypatch):
        dead = _slot('k0', 'kunlun.claude', rank=0)
        dead_sibling = _slot('k1', 'kunlun.claude', rank=1)
        other = _slot('k2', 'claude-direct', rank=2, base_url=_OTHER)
        _install(monkeypatch, dead, dead_sibling, other)

        def decide(call_no, base_url):
            if base_url == _GATEWAY:
                if call_no > 1:
                    raise AssertionError('re-tried a held endpoint')
                _unreachable(base_url)
            return _OK

        calls = _stream_transport(monkeypatch, decide)
        with provider_pin(_PIN):
            msg, _finish, _usage = api.dispatch_stream(
                _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                log_prefix='[T]')

        assert msg == 'ok'
        assert calls == [('sk-test-k0', _GATEWAY), ('sk-test-k2', _OTHER)]
        assert clock.sleeps == []
        assert _untouched(dead_sibling)

    def test_non_strict_dispatch_moves_to_a_model_on_another_endpoint(
            self, clock, monkeypatch):
        dead = _plain_slot('k0', 'dead-wire', _GATEWAY)
        good = _plain_slot('k1', 'good-wire', _OTHER)
        _install(monkeypatch, dead, good)
        calls = _stream_transport(monkeypatch, _gateway_down_until(10 ** 6))

        msg, _finish, _usage = api.dispatch_stream(
            _MESSAGES, prefer_model='dead-wire', log_prefix='[T]')

        assert msg == 'ok'
        assert calls == [('sk-test-k0', _GATEWAY), ('sk-test-k1', _OTHER)]
        assert clock.sleeps == []
        assert _untouched(dead)

    def test_race_releases_an_unreachable_slot_neutrally(
            self, clock, monkeypatch):
        dead = _plain_slot('k0', 'dead-wire', _GATEWAY)
        live = _plain_slot('k1', 'live-wire', _OTHER)
        dispatcher = _install(monkeypatch, dead, live)

        def reserve(**_kwargs):
            for slot in (dead, live):
                slot.record_request()
            return [dead, live]

        def _chat(**kwargs):
            if kwargs.get('base_url') == _GATEWAY:
                _unreachable(_GATEWAY)
            return 'ok', {'completion_tokens': 2}

        monkeypatch.setattr(dispatcher, 'pick_top_n', reserve)
        monkeypatch.setattr('lib.llm.chat', _chat)

        text, _usage = api.dispatch_fastest(_MESSAGES, log_prefix='[T]')

        assert text == 'ok'
        assert _untouched(dead)
        assert live.inflight == 0


class TestSiblingDispatchLoops:

    def test_async_stream_waits_and_recovers(self, clock, monkeypatch):
        _install(monkeypatch, _slot('k0', 'kunlun.claude', rank=0),
                 _slot('k1', 'kunlun.claude', rank=1))
        calls = []

        async def _astream(body, **kwargs):
            calls.append(kwargs.get('base_url'))
            if len(calls) <= 2:
                _unreachable(kwargs.get('base_url'))
            return _OK

        async def _sleep(seconds, abort_check=None, interval=0.5):
            clock.sleep(seconds)

        monkeypatch.setattr('lib.llm.astream.async_stream_chat', _astream)
        monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', _sleep)

        async def _run():
            with provider_pin(_PIN):
                return await api.async_dispatch_stream(
                    _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                    max_retries=1, log_prefix='[T]')

        msg, _finish, _usage = asyncio.run(_run())
        assert msg == 'ok'
        assert calls == [_GATEWAY] * 3
        assert clock.sleeps == _schedule()[:2]

    def test_non_stream_chat_waits_and_recovers(self, clock, monkeypatch):
        slots = (_slot('k0', 'kunlun.claude', rank=0),
                 _slot('k1', 'kunlun.claude', rank=1))
        _install(monkeypatch, *slots)
        calls = []

        def _chat(*args, **kwargs):
            calls.append(kwargs.get('base_url'))
            if len(calls) <= 2:
                _unreachable(kwargs.get('base_url'))
            return 'ok', {'prompt_tokens': 3, 'completion_tokens': 2}

        monkeypatch.setattr('lib.llm.chat', _chat)
        with provider_pin(_PIN):
            content, _usage = api.dispatch_chat(
                _MESSAGES, prefer_model=_LOGICAL, strict_model=True,
                max_retries=1, log_prefix='[T]')

        assert content == 'ok'
        assert calls == [_GATEWAY] * 3
        assert clock.sleeps == _schedule()[:2]
        assert _untouched(slots[1])


class TestUnrepresentableRoutes:
    """Pool rescue is non-strict; one incompatible candidate must not end it."""

    _RESPONSES = 'https://responses.example/v1'

    def _install_routes(self, monkeypatch):
        blocked = (_plain_slot('k0', 'responses-wire', self._RESPONSES),
                   _plain_slot('k1', 'responses-wire', self._RESPONSES))
        carrier = _plain_slot('k2', 'messages-wire', _OTHER)
        _install(monkeypatch, *blocked, carrier)
        return blocked, carrier

    @staticmethod
    def _transport(monkeypatch, unrepresentable):
        def decide(_call_no, base_url):
            if base_url in unrepresentable:
                raise ProtocolConversionError('mandatory_continuity_unrepresentable')
            return _OK
        return _stream_transport(monkeypatch, decide)

    def test_non_strict_pick_passes_over_the_route(self, clock, monkeypatch):
        blocked, _carrier = self._install_routes(monkeypatch)
        calls = self._transport(monkeypatch, {self._RESPONSES})

        msg, _finish, _usage = api.dispatch_stream(
            _MESSAGES, prefer_model='responses-wire', log_prefix='[T]')

        assert msg == 'ok'
        # Conversion is identical for every key on that route: one try.
        assert [url for _key, url in calls] == [self._RESPONSES, _OTHER]
        assert clock.sleeps == []
        assert all(_untouched(slot) for slot in blocked)

    def test_strict_pick_surfaces_the_conversion_error(self, clock, monkeypatch):
        self._install_routes(monkeypatch)
        calls = self._transport(monkeypatch, {self._RESPONSES})

        with pytest.raises(ProtocolConversionError):
            api.dispatch_stream(
                _MESSAGES, prefer_model='responses-wire', strict_model=True,
                log_prefix='[T]')

        assert len(calls) == 1

    def test_no_carrier_surfaces_the_conversion_error(self, clock, monkeypatch):
        self._install_routes(monkeypatch)
        calls = self._transport(monkeypatch, {self._RESPONSES, _OTHER})

        with pytest.raises(ProtocolConversionError):
            api.dispatch_stream(
                _MESSAGES, prefer_model='responses-wire', log_prefix='[T]')

        assert sorted(url for _key, url in calls) == sorted(
            [self._RESPONSES, _OTHER])
