#!/usr/bin/env python3
"""tests/test_shared_contention_backoff.py — shared-project 429 probe control.

History: 2026-07-28 (pt_1a72b708098d446f) introduced a jittered, escalating
family parking (2s → doubling → 60s cap) for contention 429s, on the theory
that rotating our own keys is futile when the whole upstream project is
saturated by other tenants. 2026-08-03 that policy was retired in favor of a
fixed 0.3s retry. Production evidence on 2026-08-26 then found 239 project TPM
rejections across 87 rounds. Fixed one-second coordination shipped next, but on
2026-08-27 one translation still spent 746 rejected calls in a 600-second
window before failing. The replacement keeps pre-request admission per
provider/model while adapting the probe interval to the observed streak.

Pinned here:

  1. The first strike arms the 0.3s baseline; repeated rejection adapts
     1→2→4→8→15s. Deep queues recheck in abortable three-second slices.
  2. Coordination NEVER cools a slot or contaminates health. Quiet recovery
     resets state; one lucky success does not, while sustained recovery does.
  3. Provider/model families coordinate independently.
  4. Log throttle: strikes 1-3, interval changes, and every 100th at INFO;
     wire classification is DEBUG, not one INFO line per rejected request.
  5. Sync chat/stream and native-async stream consume the coordinator delay;
     the wait remains abortable and is included in queue-wait accounting.
  6. The per-cycle 429 loop log is DEBUG after the first 3 cycles.
  7. Wait-label precedence + rpm-decay contracts UNCHANGED (the label
     function and the slot accounting are isolation-tested as before).

Run:  pytest tests/test_shared_contention_backoff.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import pytest

from tests._runtime_sections import ROOT as FRONTEND_ROOT

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'gw'


def _slot(model, key):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test', model=model,
                capabilities={'text'}, provider_id=PROV)


def _dispatcher(slots):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    disp = object.__new__(LLMDispatcher)
    disp._lock = threading.Lock()
    disp.slots = list(slots)
    disp.initialize = lambda: None
    disp._contention_strikes = {}
    disp._logical_index = {}
    disp._direct_models = {slot.model for slot in slots}
    return disp


@pytest.mark.unit
class TestProjectProbeCoordination:

    def test_first_strike_keeps_baseline_without_parking(self):
        s1, s2, other = (_slot('kimi-k3', 'k0'), _slot('kimi-k3', 'k1'),
                         _slot('qwen3.5-plus', 'k2'))
        disp = _dispatcher([s1, s2, other])
        assert disp.note_shared_contention(s1) == pytest.approx(0.3)
        for s in (s1, s2, other):
            assert s.cooldown_until == 0.0, (
                'probe coordination must never park ANY slot')
            assert s.cooldown_reason == ''

    def test_pre_request_admission_serializes_without_capped_herd(
            self, monkeypatch):
        """A deep queue rechecks after 3s; it is not admitted as one herd."""
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        now = [100.0]
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
        assert disp.note_shared_contention(s) == pytest.approx(0.3)
        decisions = [disp.reserve_shared_contention_probe(s)
                     for _ in range(4)]
        assert [decision.delay_s for decision in decisions] == pytest.approx(
            [0.3, 1.3, 2.3, 3.0])
        assert [decision.admitted for decision in decisions] == [
            True, True, True, False]
        assert disp._contention_strikes[(PROV, 'kimi-k3')].next_probe_at == \
            pytest.approx(103.3)

        now[0] = 103.0
        later = [disp.reserve_shared_contention_probe(s) for _ in range(2)]
        assert [decision.delay_s for decision in later] == pytest.approx(
            [0.3, 1.3])
        assert all(decision.admitted for decision in later)
        assert s.cooldown_until == 0.0
        assert s.cooldown_reason == ''

    def test_immediate_admission_defers_without_advancing_probe_clock(
            self, monkeypatch):
        """Optional work cannot reserve a future probe while yielding."""
        slot = _slot('kimi-k3', 'k0')
        disp = _dispatcher([slot])
        now = [100.0]
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
        disp.note_shared_contention(slot)
        family = (PROV, 'kimi-k3')
        before = disp._contention_strikes[family]

        blocked = disp.reserve_shared_contention_probe_now(slot)

        assert blocked.admitted is False
        assert blocked.delay_s == pytest.approx(0.3)
        assert disp._contention_strikes[family] == before

        now[0] = 100.3
        admitted = disp.reserve_shared_contention_probe_now(slot)
        assert admitted.admitted is True
        assert admitted.delay_s == 0.0
        assert disp._contention_strikes[family].next_probe_at == \
            pytest.approx(101.3)

    def test_families_coordinate_independently(self, monkeypatch):
        kimi = _slot('kimi-k3', 'k0')
        qwen = _slot('qwen3.5-plus', 'k1')
        disp = _dispatcher([kimi, qwen])
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: 100.0)
        assert disp.note_shared_contention(kimi) == pytest.approx(0.3)
        assert disp.note_shared_contention(qwen) == pytest.approx(0.3)
        assert disp.reserve_shared_contention_probe(kimi).delay_s == \
            pytest.approx(0.3)
        assert disp.reserve_shared_contention_probe(qwen).delay_s == \
            pytest.approx(0.3)

    def test_repeated_rejections_adapt_to_a_bounded_probe_interval(
            self, monkeypatch):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: 100.0)

        delays = [disp.note_shared_contention(s) for _ in range(10)]

        assert [
            disp._contention_probe_spacing_s(strike)
            for strike in range(1, 11)
        ] == [1.0, 1.0, 2.0, 2.0, 4.0, 4.0, 8.0, 8.0, 15.0, 15.0]
        assert delays == pytest.approx([
            0.3, 1.0, 2.0, 2.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0,
        ])
        state = disp._contention_strikes[(PROV, 'kimi-k3')]
        assert state.strikes == 10
        assert disp._contention_probe_spacing_s(state.strikes) == 15.0
        assert state.next_probe_at == pytest.approx(115.0)

    def test_ten_minute_storm_has_a_hard_probe_budget(self, monkeypatch):
        """Continuous rejection spends at most 50 starts per family/10 min."""
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        now = [100.0]
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
        deadline = now[0] + 600.0
        probes = 1  # the rejection that first establishes family contention
        disp.note_shared_contention(s)

        while now[0] < deadline:
            decision = disp.reserve_shared_contention_probe(s)
            now[0] += decision.delay_s
            if not decision.admitted:
                continue
            if now[0] >= deadline:
                break
            probes += 1
            disp.note_shared_contention(s)

        assert probes <= 50, (
            f'adaptive gate spent {probes} rejected starts in ten minutes')
        assert disp._contention_strikes[(PROV, 'kimi-k3')].strikes == probes

    def test_quiet_window_keeps_one_serialization_seed_then_resets(
            self, monkeypatch):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        now = [100.0]
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
        for _ in range(3):
            disp.note_shared_contention(s)
        key = (PROV, 'kimi-k3')
        assert disp._contention_strikes[key].strikes == 3
        now[0] += 31.0

        decisions = [disp.reserve_shared_contention_probe(s)
                     for _ in range(5)]
        assert [decision.delay_s for decision in decisions] == pytest.approx(
            [0.0, 1.0, 2.0, 3.0, 3.0])
        assert [decision.admitted for decision in decisions] == [
            True, True, True, True, False]
        assert disp._contention_strikes[key].strikes == 1

        now[0] = 221.0
        assert disp.note_shared_contention(s) == pytest.approx(0.3)
        assert disp._contention_strikes[key].strikes == 1

    def test_sustained_success_resets_probe_reservations(self, monkeypatch):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: 100.0)
        disp.note_shared_contention(s)
        assert disp.note_shared_success(s) is False
        state = disp._contention_strikes[(PROV, 'kimi-k3')]
        assert state.recovery_successes == 1
        assert disp.note_shared_success(s) is True
        assert (PROV, 'kimi-k3') not in disp._contention_strikes
        assert disp.note_shared_contention(s) == pytest.approx(0.3)

    def test_new_contention_cancels_partial_recovery(self, monkeypatch):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: 100.0)
        disp.note_shared_contention(s)
        assert disp.note_shared_success(s) is False
        disp.note_shared_contention(s)
        state = disp._contention_strikes[(PROV, 'kimi-k3')]
        assert state.recovery_successes == 0
        assert state.strikes == 2

    def test_family_state_table_has_hard_bound(self, monkeypatch):
        disp = _dispatcher([])
        disp._CONTENTION_MAX_FAMILIES = 3
        now = [100.0]
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
        for index in range(4):
            disp.note_shared_contention(_slot(f'model-{index}', f'k{index}'))
            now[0] += 0.1
        assert len(disp._contention_strikes) == 3
        assert (PROV, 'model-0') not in disp._contention_strikes
        assert (PROV, 'model-3') in disp._contention_strikes

    def test_log_is_throttled(self, monkeypatch):
        """INFO retains first/escalation/heartbeat records, not every wire."""
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        infos, debugs = [], []
        monkeypatch.setattr('lib.llm_dispatch.dispatcher.logger.info',
                            lambda *a, **k: infos.append(a))
        monkeypatch.setattr('lib.llm_dispatch.dispatcher.logger.debug',
                            lambda *a, **k: debugs.append(a))
        for _ in range(100):
            disp.note_shared_contention(s)
        assert len(infos) == 7, (
            f'first 3 + three spacing changes + strike 100, got {len(infos)}')
        assert len(debugs) == 93
        assert infos[-1][-2] == pytest.approx(15.0)

    def test_cooling_summary_has_no_contention_cause(self):
        """Nothing parks → the wait-label summary never sees 'contention'."""
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        disp.note_shared_contention(s)
        assert 'contention' not in disp.cooling_cause_summary('text')

    def test_picker_steers_automatic_work_to_a_ready_family(self):
        """Automatic work avoids a known wait without mutating slot health."""
        s1, other = _slot('kimi-k3', 'k0'), _slot('qwen3.5-plus', 'k1')
        other.latency_ema = 99999.0  # kimi wins on score deterministically
        disp = _dispatcher([s1, other])
        disp.note_shared_contention(s1)
        picked = disp._pick('text', None, None, None)
        assert picked is not None
        assert picked.model == 'qwen3.5-plus'
        assert s1.cooldown_until == 0.0
        assert s1.consecutive_errors == 0

    def test_explicit_model_never_crosses_its_candidate_boundary(self):
        selected = _slot('kimi-k3', 'k0')
        other = _slot('qwen3.5-plus', 'k1')
        disp = _dispatcher([selected, other])
        disp.note_shared_contention(selected)

        picked = disp._pick(
            'text', 'kimi-k3', None, None, strict_model=True)

        assert picked is selected

    def test_live_gate_state_is_bounded_and_observable(self, monkeypatch):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: 100.0)
        for _ in range(5):
            disp.note_shared_contention(s)

        assert disp.get_shared_contention_info() == [{
            'provider_id': PROV,
            'model': 'kimi-k3',
            'strikes': 5,
            'probe_spacing_s': 4.0,
            'next_probe_in_s': 4.0,
            'recovery_successes': 0,
        }]


@pytest.mark.unit
class TestWaitLabel:

    def _label(self, causes):
        from lib.llm_dispatch.retry_i18n import cooldown_wait_label
        return cooldown_wait_label(causes)

    def test_contention_wins(self):
        reason, status = self._label({'contention'})
        assert reason == 'Waiting for model (shared project limit)'
        assert status == 0, (
            'status 429 would swallow the token into the generic '
            'rate-limited detailKey — contention must ride the reason branch')

    def test_contention_beats_mixed_causes(self):
        reason, _ = self._label({'contention', 'error'})
        assert reason == 'Waiting for model (shared project limit)'

    def test_legacy_labels_unchanged(self):
        assert self._label({'rate_limit'}) == (
            'Waiting for model (rate-limited)', 429)
        assert self._label({'error'}) == (
            'Waiting for model (retry backoff)', 0)
        assert self._label(set()) == (
            'Waiting for model (rate-limited)', 429)

    def test_token_registered_with_i18n(self):
        from lib.llm_dispatch.retry_i18n import RETRY_REASON_KEYS
        key = RETRY_REASON_KEYS.get('Waiting for model (shared project limit)')
        assert key == 'stream.retryReason.waitingSharedProject'
        import json
        locales = FRONTEND_ROOT / 'frontend' / 'src' / 'i18n' / 'locales'
        packs = [json.loads((locales / f'{lang}.json').read_text(encoding='utf-8'))
                 for lang in ('en', 'zh')]
        assert all('stream.retryReason.waitingSharedProject' in pack
                   for pack in packs), (
            'missing i18n strings — the missing-translation tripwire would '
            'fire in production')

    def test_reasonkey_survives_phase_fields(self):
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        f = retry_phase_fields(
            model='kimi-k3', attempt=1,
            reason='Waiting for model (shared project limit)', status_code=0)
        assert f['detailKey'] == 'stream.phase.retryCooldownWait'
        assert f['detailArgs']['reasonKey'] == \
            'stream.retryReason.waitingSharedProject'
        assert 'attempt' not in f['detailArgs']


@pytest.mark.unit
class TestRpmLimitNotDecayed:

    def test_contention_skips_rpm_decay(self):
        s = _slot('kimi-k3', 'k0')
        before = s.rpm_limit
        s.record_request()
        s.record_error(is_rate_limit=True, is_shared_contention=True)
        assert s.rpm_limit == before, (
            'external saturation teaches the scorer nothing about this '
            "key's capacity — decaying rpm_limit is a false lesson")

    def test_plain_429_still_decays(self):
        s = _slot('kimi-k3', 'k0')
        before = s.rpm_limit
        s.record_request()
        s.record_error(is_rate_limit=True)
        assert s.rpm_limit < before, (
            'complement: a genuine per-key 429 still decays the estimate')


@pytest.mark.unit
class TestDispatchIntegration:

    @pytest.mark.parametrize('operation', ['chat', 'stream'])
    def test_optional_dispatch_yields_before_transport_and_releases_slot(
            self, monkeypatch, operation):
        from lib.llm_dispatch import api

        slot = _slot('kimi-k3', 'k0')
        disp = _dispatcher([slot])
        now = [100.0]
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
        disp.note_shared_contention(slot)
        family = (PROV, 'kimi-k3')
        before = disp._contention_strikes[family]
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled',
                            lambda *a, **k: True)
        monkeypatch.setattr(
            'lib.llm_dispatch.cache_settle.settle_before_send',
            lambda *a, **k: 0.0,
        )
        transports = []
        monkeypatch.setattr(
            'lib.llm.chat',
            lambda *a, **k: transports.append('chat'),
        )
        monkeypatch.setattr(
            'lib.llm.stream_chat',
            lambda *a, **k: transports.append('stream'),
        )
        dispatch = api.dispatch_chat if operation == 'chat' \
            else api.dispatch_stream

        with pytest.raises(api.DispatchSharedContentionDeferred) as raised:
            dispatch(
                [{'role': 'user', 'content': 'optional'}],
                log_prefix='[optional]',
                defer_on_shared_contention=True,
            )

        assert raised.value.request_not_dispatched is True
        assert raised.value.retry_after_s == pytest.approx(0.3)
        assert transports == []
        assert slot.inflight == 0
        assert slot.total_errors == 0
        assert disp._contention_strikes[family] == before

    def test_optional_async_dispatch_yields_before_transport(
            self, monkeypatch):
        from lib.llm_dispatch import api

        slot = _slot('kimi-k3', 'k0')
        disp = _dispatcher([slot])
        now = [100.0]
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
        disp.note_shared_contention(slot)
        family = (PROV, 'kimi-k3')
        before = disp._contention_strikes[family]
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        transports = []

        async def _transport(*args, **kwargs):
            transports.append('async-stream')

        monkeypatch.setattr('lib.llm.astream.async_stream_chat', _transport)

        with pytest.raises(api.DispatchSharedContentionDeferred):
            asyncio.run(api.async_dispatch_stream(
                [{'role': 'user', 'content': 'optional'}],
                log_prefix='[optional-async]',
                defer_on_shared_contention=True,
            ))

        assert transports == []
        assert slot.inflight == 0
        assert slot.total_errors == 0
        assert disp._contention_strikes[family] == before

    def test_optional_dispatch_uses_ready_alternate_family(
            self, monkeypatch):
        from lib.llm_dispatch import api

        gated = _slot('kimi-k3', 'k0')
        ready = _slot('qwen3.5-plus', 'k1')
        ready.latency_ema = 99999.0
        disp = _dispatcher([gated, ready])
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.time.monotonic', lambda: 100.0)
        disp.note_shared_contention(gated)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled',
                            lambda *a, **k: True)
        monkeypatch.setattr('lib.key_stats.record_outcome',
                            lambda *a, **k: None)
        sent_models = []

        def _chat(*args, **kwargs):
            sent_models.append(kwargs['model'])
            return 'ok', {}

        monkeypatch.setattr('lib.llm.chat', _chat)

        content, _usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'optional'}],
            defer_on_shared_contention=True,
        )

        assert content == 'ok'
        assert sent_models == ['qwen3.5-plus']
        assert gated.inflight == 0
        assert gated.total_errors == 0

    def test_smart_chat_does_not_resurrect_deferred_optional_work(
            self, monkeypatch):
        from lib.llm_dispatch import api

        deferred = api.DispatchSharedContentionDeferred(retry_after_s=2.0)
        direct_calls = []
        monkeypatch.setattr(
            'lib.llm_dispatch._api_multi.dispatch_chat',
            lambda *a, **k: (_ for _ in ()).throw(deferred),
        )
        monkeypatch.setattr(
            'lib.llm.chat',
            lambda *a, **k: direct_calls.append((a, k)),
        )

        with pytest.raises(api.DispatchSharedContentionDeferred) as raised:
            api.smart_chat(
                [{'role': 'user', 'content': 'optional'}],
                defer_on_shared_contention=True,
            )

        assert raised.value is deferred
        assert direct_calls == []

    def test_first_contention_uses_baseline_without_parking(
            self, monkeypatch):
        """One contention 429 keeps the existing 0.3s retry contract."""
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_vendor_transient_dispatch import _FakeDispatcher

        s1, other = _slot('kimi-k3', 'k0'), _slot('kimi-k3', 'k1')
        disp = _FakeDispatcher([s1, other])
        # The fake hands out queued slots; give it a real registry so the
        # loop's note_shared_contention call has somewhere to land.
        real = _dispatcher([s1, other])
        disp.note_shared_contention = real.note_shared_contention
        disp.reserve_shared_contention_probe = \
            real.reserve_shared_contention_probe
        recoveries = []

        def _note_success(slot):
            recoveries.append(slot)
            return real.note_shared_success(slot)

        disp.note_shared_success = _note_success
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
        sleeps = []
        monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                            lambda seconds: sleeps.append(seconds))
        monkeypatch.setattr('lib.key_stats.record_outcome',
                            lambda *a, **k: None)
        monkeypatch.setattr('lib.key_stats.record_rate_limit',
                            lambda *a, **k: False)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError(
                    'API HTTP 429: reached project TPM rate limit',
                    status_code=429, is_shared_contention=True)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert sleeps == pytest.approx([0.3], abs=0.02)
        assert recoveries == [other]
        assert s1.cooldown_reason == ''
        assert s1.cooldown_until == 0, (
            'family admission owns shared contention; slot cooling would '
            'evict the conversation from its warm cache key')

    def test_shared_contention_preserves_conversation_warm_key(self):
        """A project-wide rejection must not force a cold namespace switch."""
        from lib.llm_dispatch import conv_affinity

        warm = _slot('kimi-k3', 'warm-key')
        cold = _slot('kimi-k3', 'cold-key')
        warm.latency_ema = 100.0
        cold.latency_ema = 1.0
        disp = _dispatcher([warm, cold])
        conversation_id = 'shared-contention-warm-cache'
        conv_affinity.record_conv_key(
            conversation_id, warm.key_name, route_key='cap:text')

        warm.record_request()
        warm.record_error(is_rate_limit=True, is_shared_contention=True)

        with conv_affinity.conv_affinity(conversation_id):
            chosen = disp._pick(
                'text', None, None, None,
                reserve=False, strict_model=False,
            )

        assert chosen is warm, (
            'shared contention displaced the prompt-cache-warm key')
        assert warm.consecutive_errors == 0
        assert warm.cooldown_until == 0

    def test_dispatch_chat_consumes_coordinator_delay(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_vendor_transient_dispatch import _FakeDispatcher

        first, second = _slot('kimi-k3', 'k0'), _slot('kimi-k3', 'k1')
        disp = _FakeDispatcher([first, second], all_slots=[first, second])
        real = _dispatcher([first, second])
        disp.note_shared_contention = real.note_shared_contention
        disp.reserve_shared_contention_probe = \
            real.reserve_shared_contention_probe
        recoveries = []

        def _note_success(slot):
            recoveries.append(slot)
            return real.note_shared_success(slot)

        disp.note_shared_success = _note_success
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
        monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
        monkeypatch.setattr('lib.key_stats.record_rate_limit',
                            lambda *a, **k: False)
        sleeps = []
        monkeypatch.setattr(api.time, 'sleep', sleeps.append)
        calls = {'n': 0}

        def _fake_chat(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError(
                    'reached project TPM rate limit', status_code=429,
                    is_shared_contention=True)
            return 'ok', {}

        monkeypatch.setattr('lib.llm.chat', _fake_chat)
        content, _usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert content == 'ok'
        assert sleeps == pytest.approx([0.3], abs=0.02)
        assert recoveries == [second]

    def test_existing_family_gate_precedes_first_network_call(
            self, monkeypatch):
        """A different dispatch sees contention before spending one request."""
        from lib.llm_dispatch import api
        from tests.test_vendor_transient_dispatch import _FakeDispatcher

        slot = _slot('kimi-k3', 'k0')
        real = _dispatcher([slot])
        real.note_shared_contention(slot)
        disp = _FakeDispatcher([slot], all_slots=[slot])
        disp.reserve_shared_contention_probe = \
            real.reserve_shared_contention_probe
        disp.note_shared_success = real.note_shared_success
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
        monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
        sleeps = []
        monkeypatch.setattr(api.time, 'sleep', sleeps.append)
        sent_after_wait = []

        def _fake_chat(*args, **kwargs):
            sent_after_wait.append(bool(sleeps))
            return 'ok', {}

        monkeypatch.setattr('lib.llm.chat', _fake_chat)
        content, _usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[new-task]')

        assert content == 'ok'
        assert sleeps == pytest.approx([0.3], abs=0.02)
        assert sent_after_wait == [True]

    def test_local_cache_gate_precedes_project_probe(self, monkeypatch):
        """A probe reservation must describe an actual request start."""
        from lib.llm_dispatch import api
        from tests.test_vendor_transient_dispatch import _FakeDispatcher

        slot = _slot('kimi-k3', 'k0')
        real = _dispatcher([slot])
        real.note_shared_contention(slot)
        disp = _FakeDispatcher([slot], all_slots=[slot])
        order = []

        def _reserve(candidate):
            order.append('project-admission')
            return real.reserve_shared_contention_probe(candidate)

        disp.reserve_shared_contention_probe = _reserve
        disp.note_shared_success = real.note_shared_success
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
        monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
        monkeypatch.setattr(api.time, 'sleep', lambda _seconds: None)
        monkeypatch.setattr(
            'lib.llm_dispatch.cache_settle.settle_before_send',
            lambda *a, **k: order.append('cache-settle'),
        )

        def _fake_stream(*args, **kwargs):
            order.append('network')
            return 'ok', 'stop', {}

        monkeypatch.setattr('lib.llm.stream_chat', _fake_stream)
        result = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[ordering]')

        assert result[0] == 'ok'
        assert order[:3] == ['cache-settle', 'project-admission', 'network']

    def test_abort_during_project_wait_releases_reserved_slot(
            self, monkeypatch):
        from lib.llm import AbortedError
        from lib.llm_dispatch import api
        from tests.test_vendor_transient_dispatch import _FakeDispatcher

        slot = _slot('kimi-k3', 'k0')
        real = _dispatcher([slot])
        real.note_shared_contention(slot)
        disp = _FakeDispatcher([slot], all_slots=[slot])
        disp.reserve_shared_contention_probe = \
            real.reserve_shared_contention_probe
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr(
            'lib.llm_dispatch.cache_settle.settle_before_send',
            lambda *a, **k: 0.0,
        )
        checks = iter([False, True])

        with pytest.raises(AbortedError):
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                abort_check=lambda: next(checks, True),
                log_prefix='[abort-admission]',
            )

        assert slot.inflight == 0
        assert slot.total_errors == 0

    def test_async_stream_consumes_coordinator_delay(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_async_dispatch_stream import _FakeDispatcher

        first, second = _slot('kimi-k3', 'k0'), _slot('kimi-k3', 'k1')
        disp = _FakeDispatcher([first, second])
        real = _dispatcher([first, second])
        disp.note_shared_contention = real.note_shared_contention
        disp.reserve_shared_contention_probe = \
            real.reserve_shared_contention_probe
        recoveries = []

        def _note_success(slot):
            recoveries.append(slot)
            return real.note_shared_success(slot)

        disp.note_shared_success = _note_success
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        sleeps = []

        async def _fake_sleep(seconds, abort_check=None):
            sleeps.append(seconds)

        async def _fake_stream(*args, **kwargs):
            if not sleeps:
                raise RateLimitError(
                    'reached project TPM rate limit', status_code=429,
                    is_shared_contention=True)
            return 'ok', 'stop', {}

        monkeypatch.setattr(
            'lib.llm._transport.async_abortable_sleep', _fake_sleep)
        monkeypatch.setattr('lib.llm.astream.async_stream_chat', _fake_stream)
        result = asyncio.run(api.async_dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]'))

        assert result[0] == 'ok'
        assert sleeps == pytest.approx([0.3], abs=0.02)
        assert recoveries == [second]

    def test_per_cycle_429_log_throttled_after_first_three(
            self, monkeypatch):
        """Log-bloat guard for a sustained retry era: cycles 1-3 log at
        INFO, cycles 4+ at DEBUG (every 100th still surfaces at INFO)."""
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_429_saturation_escalation import (
            _FakeClock, _FakeDispatcher, _FakeSlot)

        slot = _FakeSlot()
        clock = _FakeClock()
        disp = _FakeDispatcher(slot)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr(api, 'time', clock)
        monkeypatch.setenv('TOFU_429_SATURATION_SECS', '0')

        calls = {'n': 0}

        def _chat(*a, **kw):
            calls['n'] += 1
            if calls['n'] <= 6:
                raise RateLimitError('slow down', status_code=429)
            return ('ok-text', {'completion_tokens': 2})

        monkeypatch.setattr('lib.llm.chat', _chat)
        infos, debugs = [], []
        monkeypatch.setattr(api.logger, 'info',
                            lambda *a, **k: infos.append(a))
        monkeypatch.setattr(api.logger, 'debug',
                            lambda *a, **k: debugs.append(a))

        content, usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='m1', strict_model=True, log_prefix='[T]')

        assert content == 'ok-text'
        rate_infos = [a for a in infos if '429 rate-limited on' in str(a[0])]
        rate_debugs = [a for a in debugs if '429 rate-limited on' in str(a[0])]
        assert len(rate_infos) == 3, (
            f'cycles 1-3 at INFO, got {len(rate_infos)}')
        assert len(rate_debugs) == 3, (
            f'cycles 4-6 at DEBUG, got {len(rate_debugs)}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
