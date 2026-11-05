"""Gateway 5xx route unavailability: New-API's distributor ``model_not_found``.

Incident 2026-10-25 (task e39cc4d9): every attempt on a provider-pinned,
strict wire model returned HTTP 503 ``{"error":{"code":"model_not_found",
"message":"分组 azure 下模型 gpt-6.1-sol 无可用渠道（distributor） ..."}}`` — no
enabled gateway channel serves that model in the key's group. Dispatch filed
it as a whole-gateway outage, logged it as "429 rate-limited", re-probed the
dead route every 0.5 s and never tried the group's other route for the model.

Contract pinned here:
  * a 5xx with that structured code or the exact distributor phrase is typed
    route-unavailability evidence (still waitable gateway class outside
    dispatch), distinct from other 5xx and from the broad 400 markers;
  * another eligible route or model serves at once (no waiting, no cycling);
  * a sole eligible route waits on the fixed backoff schedule with an
    accurate retry reason, never fails the turn, honours abort, and recovers
    when the gateway serves again;
  * the evidence is Deployment health, never key health.
"""
import asyncio
import logging
import threading
import time

import pytest

import lib.llm_dispatch.api as api
from lib.llm.llm_errors import (
    AbortedError,
    ModelRouteMissingError,
    RateLimitError,
    _classify_http_error,
)
from lib.llm_dispatch.provider_pin import provider_pin

pytestmark = pytest.mark.unit

_NO_CHANNEL_BODY = (
    '{"error":{"code":"model_not_found","message":"分组 azure 下模型 '
    'gpt-6.1-sol 无可用渠道（distributor） (request id: 20261025180033abc)",'
    '"type":"new_api_error"}}')
_GATEWAY_MESSAGE = '分组 azure 下模型 gpt-6.1-sol 无可用渠道（distributor）'
_REASON = 'Model route unavailable: ' + _GATEWAY_MESSAGE
_PIN = 'model-route:7:test'
_MESSAGES = [{'role': 'user', 'content': 'hi'}]
_OK = ('ok', 'stop', {'prompt_tokens': 3, 'completion_tokens': 2})


def _raise_no_channel(model):
    _classify_http_error(503, _NO_CHANNEL_BODY, model, '[T]')


# ── classification ──────────────────────────────────────────────────

class TestRouteUnavailableClassification:

    @pytest.mark.parametrize('status', [500, 502, 503, 504])
    def test_structured_model_not_found_5xx_is_route_unavailable(self, status):
        from lib.llm.llm_errors import ModelRouteUnavailableError
        with pytest.raises(ModelRouteUnavailableError) as caught:
            _classify_http_error(status, _NO_CHANNEL_BODY, 'kunlun.gpt', '[T]')
        error = caught.value
        # Outside dispatch the owner's waitable 5xx policy is unchanged.
        assert isinstance(error, RateLimitError) and error.is_gateway
        assert error.status_code == status
        assert error.error_code == 'model_not_found'
        assert error.model == 'kunlun.gpt'
        # Bounded and stable: the per-request id is not part of the reason.
        assert error.gateway_message == _GATEWAY_MESSAGE

    def test_distributor_phrase_without_code_is_route_unavailable(self):
        from lib.llm.llm_errors import ModelRouteUnavailableError
        body = ('{"error":{"message":"分组 default 下模型 m 无可用渠道(distributor)",'
                '"type":"one_api_error"}}')
        with pytest.raises(ModelRouteUnavailableError) as caught:
            _classify_http_error(503, body, 'm', '[T]')
        assert caught.value.error_code == 'distributor_no_channel'

    @pytest.mark.parametrize('body', [
        'upstream down',
        '<html>503 Service Temporarily Unavailable</html>',
        # The broad HTTP 400 route-missing markers never type a 5xx.
        '{"error":{"message":"The model `m` does not exist"}}',
        '{"error":{"code":"server_error","message":"无可用渠道"}}',
    ])
    def test_other_5xx_bodies_stay_whole_gateway_outage(self, body):
        with pytest.raises(RateLimitError) as caught:
            _classify_http_error(503, body, 'm', '[T]')
        assert type(caught.value) is RateLimitError
        assert caught.value.is_gateway

    def test_400_route_missing_is_unchanged(self):
        with pytest.raises(ModelRouteMissingError):
            _classify_http_error(400, _NO_CHANNEL_BODY, 'm', '[T]')


# ── dispatch harness: real dispatcher, real slots, fake clock ─────────

class _Clock:
    """Monotonic time that only the test's own (dispatching) thread moves.

    Other threads keep the real clock and real sleeps: a daemon such as the
    storage client's liveness monitor (``storage-supervisor``, a 1 s poll)
    would otherwise spin on the fake sleep and append its sleeps to the
    dispatch record.
    """

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
    from lib.model_routing.health import reset_owner_health_registries
    reset_owner_health_registries()
    yield clock
    reset_owner_health_registries()


def _slot(key, model, *, provider, rank, logical='gpt-6.1-sol'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-' + key, model=model,
                logical_model=logical, provider_id=_PIN,
                routing_provider_id=provider, route_rank=rank,
                capabilities={'text'})


def _install(monkeypatch, *slots):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    dispatcher = LLMDispatcher()
    dispatcher.slots = list(slots)
    dispatcher._initialized = True
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    return dispatcher


def _stream_transport(monkeypatch, decide):
    calls = []

    def _stream(body, **kwargs):
        calls.append((kwargs.get('api_key'), body.get('model')))
        return decide(len(calls), body.get('model'))

    monkeypatch.setattr('lib.llm.stream_chat', _stream)
    return calls


def _unavailable_until(n_failures, model='kunlun.gpt'):
    def decide(call_no, wire_model):
        if wire_model == model and call_no <= n_failures:
            _raise_no_channel(wire_model)
        return _OK
    return decide


def _schedule():
    from lib.llm_dispatch._route_unavailable import ROUTE_UNAVAILABLE_BACKOFF_S
    return list(ROUTE_UNAVAILABLE_BACKOFF_S)


# ── sole eligible route: wait on the fixed schedule, then recover ────

class TestSoleRouteWaitsAndRecovers:

    def test_strict_pinned_route_backs_off_and_recovers(
            self, clock, monkeypatch, caplog):
        route = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        _install(monkeypatch, route)
        calls = _stream_transport(monkeypatch, _unavailable_until(4))
        retries = []

        with caplog.at_level(logging.INFO, logger='lib.llm_dispatch.api'):
            with provider_pin(_PIN):
                msg, finish, _usage = api.dispatch_stream(
                    _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                    log_prefix='[T]', on_retry=lambda **kw: retries.append(kw))

        assert (msg, finish) == ('ok', 'stop')
        assert calls == [('sk-test-k0', 'kunlun.gpt')] * 5
        assert [r['reason'] for r in retries] == [_REASON] * 4
        assert {r['status_code'] for r in retries} == {503}
        assert [r['attempt'] for r in retries] == [1, 2, 3, 4]
        messages = [r.getMessage() for r in caplog.records]
        assert not any('429 rate-limited' in m for m in messages)
        assert any('Model route unavailable' in m for m in messages)
        # Route evidence is not key health.
        assert (route.consecutive_errors, route.total_errors,
                route.gateway_errors) == (0, 0, 0)
        assert route.inflight == 0
        # One wait per verdict on the fixed schedule — no 0.3/0.5 s cycling.
        assert len(clock.sleeps) == 4 and min(clock.sleeps) >= 1.0
        assert clock.sleeps == _schedule()[:4]

    def test_backoff_is_capped_and_never_fails_the_turn(
            self, clock, monkeypatch):
        route = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        _install(monkeypatch, route)
        schedule = _schedule()
        failures = len(schedule) + 3
        _stream_transport(monkeypatch, _unavailable_until(failures))

        with provider_pin(_PIN):
            msg, _finish, _usage = api.dispatch_stream(
                _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                max_retries=1, log_prefix='[T]')

        assert msg == 'ok'
        assert clock.sleeps == schedule + [schedule[-1]] * 3

    def test_wait_honours_abort(self, clock, monkeypatch):
        route = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        _install(monkeypatch, route)
        calls = _stream_transport(monkeypatch, _unavailable_until(10 ** 6))
        stop = []

        with provider_pin(_PIN), pytest.raises(AbortedError):
            api.dispatch_stream(
                _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                log_prefix='[T]', abort_check=lambda: bool(stop),
                on_retry=lambda **kw: stop.append(kw))

        assert len(calls) == 1
        assert route.inflight == 0

    def test_long_backoff_is_abortable_mid_wait(self, clock, monkeypatch):
        route = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        _install(monkeypatch, route)
        calls = _stream_transport(monkeypatch, _unavailable_until(10 ** 6))

        with provider_pin(_PIN), pytest.raises(AbortedError):
            api.dispatch_stream(
                _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                log_prefix='[T]', abort_check=lambda: len(clock.sleeps) >= 2)

        assert len(calls) == 1
        assert sum(clock.sleeps) < _schedule()[0]
        assert route.inflight == 0

    def test_evidence_is_deployment_health_and_success_clears_it(
            self, clock, monkeypatch):
        from lib.model_routing.health import (
            HealthTarget, existing_owner_health_registry)
        route = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        route.routing_owner_user_id = 7
        route.route_deployment_id = 'dep-kunlun'
        route.route_credential_id = 'cred-kunlun'
        _install(monkeypatch, route)
        _stream_transport(monkeypatch, _unavailable_until(1))
        target = HealthTarget('deployment', 'dep-kunlun')
        observed = []

        def _on_retry(**kw):
            registry = existing_owner_health_registry(7, '')
            observed.append(registry is not None
                            and registry.target_unavailable(target))

        with provider_pin(_PIN):
            api.dispatch_stream(
                _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                log_prefix='[T]', owner_user_id=7, on_retry=_on_retry)

        assert observed == [True]
        registry = existing_owner_health_registry(7, '')
        assert not registry.target_unavailable(target)
        assert not registry.target_unavailable(
            HealthTarget('credential', 'cred-kunlun'))


# ── another eligible route or model: fail over without waiting ───────

class TestFailoverWithoutWaiting:

    def test_strict_group_moves_to_the_models_other_route(
            self, clock, monkeypatch):
        dead = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        dead_sibling = _slot('k1', 'kunlun.gpt', provider='kunlun', rank=1)
        other = _slot('k2', 'gpt-6.1-sol', provider='codex', rank=2)
        dead.base_url = dead_sibling.base_url = 'https://gw.example/v1'
        _install(monkeypatch, dead, dead_sibling, other)

        def decide(call_no, wire_model):
            if wire_model == 'kunlun.gpt':
                if call_no > 3:
                    raise AbortedError('re-probed an unavailable route')
                _raise_no_channel(wire_model)
            return _OK

        calls = _stream_transport(monkeypatch, decide)
        with provider_pin(_PIN):
            msg, _finish, _usage = api.dispatch_stream(
                _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                log_prefix='[T]')

        assert msg == 'ok'
        # One verdict holds the provider/wire model on every key.
        assert calls == [('sk-test-k0', 'kunlun.gpt'),
                         ('sk-test-k2', 'gpt-6.1-sol')]
        assert clock.sleeps == []

    def test_non_strict_dispatch_falls_over_to_another_model(
            self, clock, monkeypatch):
        from lib.llm_dispatch.slot import Slot
        dead = Slot(key_name='k0', api_key='sk-test-k0', model='dead-wire',
                    capabilities={'text'})
        good = Slot(key_name='k1', api_key='sk-test-k1', model='good-wire',
                    capabilities={'text'})
        _install(monkeypatch, dead, good)

        def decide(call_no, wire_model):
            if wire_model == 'dead-wire':
                if call_no > 3:
                    raise AbortedError('re-probed an unavailable route')
                _raise_no_channel(wire_model)
            return _OK

        calls = _stream_transport(monkeypatch, decide)
        msg, _finish, _usage = api.dispatch_stream(
            _MESSAGES, prefer_model='dead-wire', log_prefix='[T]')

        assert msg == 'ok'
        assert calls == [('sk-test-k0', 'dead-wire'),
                         ('sk-test-k1', 'good-wire')]
        assert clock.sleeps == []


# ── sibling loops share the same semantics ───────────────────────────

class TestSiblingDispatchLoops:

    def test_async_stream_backs_off_and_recovers(self, clock, monkeypatch):
        route = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        _install(monkeypatch, route)
        calls = []

        async def _astream(body, **kwargs):
            calls.append(body.get('model'))
            if len(calls) <= 2:
                _raise_no_channel(body.get('model'))
            return _OK

        async def _sleep(seconds, abort_check=None, interval=0.5):
            clock.sleep(seconds)

        monkeypatch.setattr('lib.llm.astream.async_stream_chat', _astream)
        monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', _sleep)
        retries = []

        async def _run():
            with provider_pin(_PIN):
                return await api.async_dispatch_stream(
                    _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                    log_prefix='[T]', on_retry=lambda **kw: retries.append(kw))

        msg, _finish, _usage = asyncio.run(_run())
        assert msg == 'ok'
        assert calls == ['kunlun.gpt'] * 3
        assert [r['reason'] for r in retries] == [_REASON] * 2
        assert len(clock.sleeps) == 2 and min(clock.sleeps) >= 1.0
        assert clock.sleeps == _schedule()[:2]

    def test_non_stream_chat_backs_off_and_recovers(self, clock, monkeypatch):
        route = _slot('k0', 'kunlun.gpt', provider='kunlun', rank=0)
        _install(monkeypatch, route)
        calls = []

        def _chat(*args, **kwargs):
            calls.append(kwargs.get('model'))
            if len(calls) <= 2:
                _raise_no_channel(kwargs.get('model'))
            return 'ok', {'prompt_tokens': 3, 'completion_tokens': 2}

        monkeypatch.setattr('lib.llm.chat', _chat)
        with provider_pin(_PIN):
            content, _usage = api.dispatch_chat(
                _MESSAGES, prefer_model='gpt-6.1-sol', strict_model=True,
                log_prefix='[T]')

        assert content == 'ok'
        assert calls == ['kunlun.gpt'] * 3
        assert len(clock.sleeps) == 2 and min(clock.sleeps) >= 1.0
        assert clock.sleeps == _schedule()[:2]


# ── retry notice: the HUD localizes the cause ───────────────────────

class TestRetryNoticeLocalization:
    """The verdict's retry reason maps to a dedicated localized HUD key.

    Through the generic ``stream.phase.retryReason`` copy the zh HUD read
    ``重试中…Model route unavailable: …``: ``{reason}`` is the dispatcher's
    English token. The gateway's own message is data and rides verbatim.
    """

    _KEY = 'stream.phase.retryRouteUnavailable'

    @staticmethod
    def _catalog(language):
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        path = root / 'frontend' / 'src' / 'i18n' / 'locales' / f'{language}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def test_dispatch_reason_maps_to_dedicated_key_with_gateway_message(self):
        from lib.llm.llm_errors import ModelRouteUnavailableError
        from lib.llm_dispatch._route_unavailable import route_unavailable_reason
        from lib.llm_dispatch.retry_i18n import retry_phase_fields

        with pytest.raises(ModelRouteUnavailableError) as caught:
            _raise_no_channel('kunlun.gpt')
        reason = route_unavailable_reason(caught.value)
        assert reason == _REASON

        fields = retry_phase_fields(
            model='gpt-6.1-sol', attempt=2, reason=reason, status_code=503,
            legacy_detail='legacy')

        assert fields['detail'] == 'legacy'
        assert fields['detailKey'] == self._KEY
        assert fields['detailArgs'] == {
            'model': 'gpt-6.1-sol', 'attempt': 2, 'message': _GATEWAY_MESSAGE}

    @pytest.mark.parametrize('language', ['zh', 'en'])
    def test_both_locales_render_the_cause(self, language):
        import re

        template = self._catalog(language)[self._KEY]
        assert set(re.findall(r'\{([A-Za-z0-9_]+)\}', template)) == {
            'message', 'model', 'attempt'}
        rendered = template.format(
            message=_GATEWAY_MESSAGE, model='gpt-6.1-sol', attempt=2)
        assert _GATEWAY_MESSAGE in rendered and 'gpt-6.1-sol' in rendered
        if language == 'zh':
            assert 'Model route unavailable' not in rendered
            assert '路由' in rendered
