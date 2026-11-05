"""Provider-pinned route groups: cooldown readiness before compiler rank order.

A request-scoped v2 route group (``provider_pin``) orders its slots by the
compiler-owned ``Slot.route_rank``; slot scores never reorder that preference.
Cooldown, however, is not a candidate filter: ``Slot.score()`` reports it as
``inf`` and only the score ordering, the sticky branch and the strict check
read it. The rank ordering returned the top-ranked slot even while it cooled,
so a strict pick returned nothing (the loop polled the cooldown out) and a
non-strict pick sent to the cooling slot; ready lower-ranked routes were never
tried (route-unavailable investigation, 2026-10-25).

Contract pinned here:
  * among ready routes the lowest ``route_rank`` wins whatever the scores;
  * a cooling route never shadows a ready one, strict or not;
  * with every route cooling, strict picks wait (``None``) and non-strict
    picks keep rank order;
  * a ready warm (affinity) route still wins over rank, a cooling one rebinds
    to the best ready rank, and the pinned warm-key hold still waits out a
    short cooldown first;
  * a no-channel route hold plus a cooling sibling serves the next ready route
    without waiting.
"""
from __future__ import annotations

import threading
import time

import pytest

import lib.llm_dispatch.api as api
from lib.llm_dispatch.provider_pin import provider_pin
from lib.llm_dispatch.slot import Slot

pytestmark = pytest.mark.unit

_PIN = 'model-route:9:readiness'
_MODEL = 'alpha'
_MESSAGES = [{'role': 'user', 'content': 'hi'}]
_OK = ('ok', 'stop', {'prompt_tokens': 3, 'completion_tokens': 2})


def _route(rank, *, latency=100.0):
    """One route of a minted group; each rank is its own provider endpoint."""
    return Slot(
        key_name=f'ephemeral_rank{rank}', api_key=f'sk-rank{rank}',
        model='wire-alpha', logical_model=_MODEL, capabilities={'text'},
        provider_id=_PIN, request_scoped=True,
        routing_provider_id=f'provider-{rank}', routing_owner_user_id=1,
        route_offering_id='offering-alpha', route_connection_id=f'conn-{rank}',
        route_credential_id=f'credential-{rank}',
        route_deployment_id=f'deployment-{rank}', route_rank=rank,
        latency_ema=latency)


def _cool(slot, seconds=1000.0):
    slot.cooldown_until = time.time() + seconds


@pytest.fixture
def group(monkeypatch):
    """Install a real dispatcher holding exactly the given pinned routes."""
    from lib.llm_dispatch import conv_affinity
    from lib.llm_dispatch.dispatcher import LLMDispatcher

    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    monkeypatch.setenv('TOFU_CONV_STICKY_ROUTING', '1')
    monkeypatch.delenv('TOFU_CONV_STICKY_HOLD_MS', raising=False)
    monkeypatch.delenv('TOFU_CONV_STICKY_HOLD_MAX_MS', raising=False)
    monkeypatch.delenv('TOFU_GATEWAY_OUTAGE_BUDGET_S', raising=False)
    monkeypatch.delenv('TOFU_429_SATURATION_SECS', raising=False)
    conv_affinity.clear_conv_affinity()
    conv_affinity._conv_keys.clear()

    def install(*slots):
        dispatcher = LLMDispatcher()
        dispatcher.slots = list(slots)
        dispatcher._initialized = True
        monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
        return dispatcher

    yield install
    conv_affinity.clear_conv_affinity()
    conv_affinity._conv_keys.clear()


def _pick(dispatcher, **kwargs):
    with provider_pin(_PIN):
        return dispatcher.pick_slot(
            capability='text', prefer_model=_MODEL, **kwargs)


# ── pick: readiness first, then compiler rank order ─────────────────

class TestPinnedPickReadiness:

    @pytest.mark.parametrize('strict', [True, False])
    def test_cooling_top_rank_never_shadows_ready_sibling(self, group, strict):
        top = _route(0)
        second = _route(1, latency=900.0)
        third = _route(2, latency=1.0)
        dispatcher = group(top, second, third)
        _cool(top)

        # The next READY rank, not the best-scoring route and never the
        # cooling top rank (strict used to get None, non-strict the cooling
        # slot itself).
        assert _pick(dispatcher, strict_model=strict) is second

    @pytest.mark.parametrize('strict', [True, False])
    def test_rank_order_beats_score_among_ready_routes(self, group, strict):
        top = _route(0, latency=5000.0)
        top.inflight = 3
        second = _route(1, latency=1.0)
        dispatcher = group(top, second)
        assert top.score() > second.score()

        assert _pick(dispatcher, strict_model=strict) is top

    def test_every_route_cooling_strict_waits_non_strict_keeps_rank(self, group):
        top, second = _route(0), _route(1, latency=1.0)
        dispatcher = group(second, top)
        _cool(top, 50.0)
        _cool(second, 5.0)

        assert _pick(dispatcher, strict_model=True) is None
        assert _pick(dispatcher, strict_model=False) is top

    def test_cooling_warm_route_rebinds_to_best_ready_rank(self, group):
        from lib.llm_dispatch import conv_affinity

        top, second, third = _route(0), _route(1), _route(2, latency=1.0)
        dispatcher = group(top, second, third)
        conv_affinity.record_conv_key(
            'conv-warm-top', top.affinity_key(), route_key=_MODEL)
        _cool(top)

        conv_affinity.set_conv_affinity('conv-warm-top')
        try:
            assert _pick(dispatcher, strict_model=True) is second
            assert conv_affinity.get_preferred_key(
                'conv-warm-top', route_key=_MODEL) == second.affinity_key()
            # Once rebound, the ready warm route keeps winning over rank so
            # the conversation's prompt cache stays on it.
            top.cooldown_until = 0.0
            assert _pick(dispatcher, strict_model=True) is second
        finally:
            conv_affinity.clear_conv_affinity()


# ── dispatch: holds and waits around the pinned pick ─────────────────

class _Clock:
    """Wall and monotonic time that only advance when dispatch sleeps.

    Only the test's own (dispatching) thread reads, moves or records this
    clock; other threads keep real time and sleeps, so a background sleeper
    can neither spin nor add a sleep the assertions would attribute to
    dispatch.
    """

    def __init__(self):
        self.now = time.time()
        self.sleeps = []
        self._thread = threading.get_ident()
        self._real_time = time.time
        self._real_monotonic = time.monotonic
        self._real_sleep = time.sleep

    def time(self):
        if threading.get_ident() != self._thread:
            return self._real_time()
        return self.now

    def monotonic(self):
        if threading.get_ident() != self._thread:
            return self._real_monotonic()
        return self.now

    def sleep(self, seconds, *args, **kwargs):
        if threading.get_ident() != self._thread:
            return self._real_sleep(seconds)
        self.sleeps.append(round(float(seconds), 3))
        self.now += max(0.0, float(seconds))


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(time, 'time', clock.time)
    monkeypatch.setattr(time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    return clock


def _transport(monkeypatch, decide=lambda api_key: _OK):
    calls = []

    def _stream(body, **kwargs):
        calls.append(kwargs.get('api_key'))
        return decide(kwargs.get('api_key'))

    monkeypatch.setattr('lib.llm.stream_chat', _stream)
    return calls


def _dispatch(conv_id=None):
    from lib.llm_dispatch import conv_affinity

    if conv_id:
        conv_affinity.set_conv_affinity(conv_id)
    try:
        with provider_pin(_PIN):
            return api.dispatch_stream(
                _MESSAGES, prefer_model=_MODEL, strict_model=True,
                log_prefix='[T]', max_retries=2)
    finally:
        if conv_id:
            conv_affinity.clear_conv_affinity()


class TestPinnedDispatchReadiness:

    def test_short_cooldown_on_warm_top_rank_is_held_then_served(
            self, group, clock, monkeypatch):
        from lib.llm_dispatch import conv_affinity

        monkeypatch.setenv('TOFU_CONV_STICKY_HOLD', '1')
        top, second = _route(0), _route(1, latency=1.0)
        group(top, second)
        conv_affinity.record_conv_key(
            'conv-hold', top.affinity_key(), route_key=_MODEL)
        top.record_error(is_rate_limit=True, error='HTTP 429')
        assert 0 < top.cooldown_until - time.time() <= 1.0
        calls = _transport(monkeypatch)

        assert _dispatch('conv-hold')[0] == 'ok'

        # The pinned-scope warm-key hold waits once for the warm route and
        # the pick lands back on it (the prompt cache stays warm).
        assert calls == ['sk-rank0']
        assert len(clock.sleeps) == 1 and 0 < clock.sleeps[0] <= 1.5

    def test_hold_disabled_serves_ready_rank_without_polling(
            self, group, clock, monkeypatch):
        from lib.llm_dispatch import conv_affinity

        monkeypatch.setenv('TOFU_CONV_STICKY_HOLD', '0')
        top, second = _route(0), _route(1)
        group(top, second)
        conv_affinity.record_conv_key(
            'conv-no-hold', top.affinity_key(), route_key=_MODEL)
        top.record_error(is_rate_limit=True, error='HTTP 429')
        calls = _transport(monkeypatch)

        assert _dispatch('conv-no-hold')[0] == 'ok'

        # No implicit hold: the cooling warm route is not polled out.
        assert calls == ['sk-rank1']
        assert clock.sleeps == []
        assert conv_affinity.get_preferred_key(
            'conv-no-hold', route_key=_MODEL) == second.affinity_key()

    def test_no_channel_hold_and_cooling_sibling_serve_next_ready_route(
            self, group, clock, monkeypatch):
        from lib.llm.llm_errors import _classify_http_error

        monkeypatch.setenv('TOFU_CONV_STICKY_HOLD', '0')
        dead, cooling, ready = _route(0), _route(1), _route(2)
        group(dead, cooling, ready)
        _cool(cooling, 30.0)
        no_channel = (
            '{"error":{"code":"model_not_found","message":"分组 g 下模型 '
            'wire-alpha 无可用渠道（distributor）"}}')

        def decide(api_key):
            if api_key == 'sk-rank0':
                _classify_http_error(503, no_channel, 'wire-alpha', '[T]')
            return _OK

        calls = _transport(monkeypatch, decide)

        assert _dispatch()[0] == 'ok'

        # rank 0 is held (no channel), rank 1 cools: rank 2 serves at once.
        assert calls == ['sk-rank0', 'sk-rank2']
        assert clock.sleeps == []
        assert cooling.inflight == 0 and dead.inflight == 0
