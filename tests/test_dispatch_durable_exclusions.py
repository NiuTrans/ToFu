"""Durable exclusion + strict-model vacancy tests (2026-08-03, owner directive).

Incident: kimi-k3's ONLY key (key_access pins the model to one cell) got a
durable 401 ("invalid AppId"). Three compounding mechanics turned that into
a ~2-minute spin followed by a dead turn:

  B. the 60s hard-error exclusion reset (designed for TRANSIENT 502/timeouts)
     resurrected the permission-dead pair every minute, each resurrection
     burning one of the 3 hard attempts on a guaranteed re-failure;
  C. ``has_capable_slots`` ignored ``strict_model``/``prefer_model`` — the
     pool's healthy opus/glm slots answered "slots exist", so the strict
     kimi-k3 loop kept cycling instead of declaring the pinned model vacant
     and escalating to the caller's pool rescue immediately.

Pins:
  * permission pairs / quota keys are DURABLE for the dispatch call
    (survive the reset); transient pairs (502/timeout/unreachable) are not;
  * the whole-key permission escalation lands in the durable key set;
  * ``has_capable_slots(prefer_model=…)`` narrows to the alias group;
  * the strict-model slot-None branch passes prefer_model through.

Run:  pytest tests/test_dispatch_durable_exclusions.py -m unit
"""
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_slot(model='gpt-4o', key='k0', api_key=None):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key=api_key or f'sk-{key}-1234',
                model=model, capabilities={'text'})


def _force_reset(st):
    """Make the next maybe_reset_exclusions fire: 429 cycling, 61s stale."""
    st._429_count = max(st._429_count, 2)
    st._last_exclusion_reset -= 61


# ── B1: _StreamRetryState durable sets ───────────────────────────────

@pytest.mark.unit
class TestStreamRetryStateDurable:
    def test_permission_pair_survives_reset_transient_cleared(self):
        from lib.llm_dispatch.api import _StreamRetryState
        st = _StreamRetryState()
        st.note_permission_pair(SimpleNamespace(key_name='k1', model='m1'),
                                SimpleNamespace(slots=[]), 'text', '[t]')
        st.exclude_pairs.add(('k2', 'm2'))  # transient (timeout/502 class)
        _force_reset(st)
        st.maybe_reset_exclusions('[t]', 'dispatch_stream')
        survivors = st.eff_exclude_pairs() or set()
        assert ('k1', 'm1') in survivors, (
            'a 401/403 pair must NOT be resurrected by the 60s reset — '
            'an auth rejection cannot heal inside one dispatch call')
        assert ('k2', 'm2') not in survivors, (
            'transient pairs keep the reset second-chance semantics')

    def test_quota_key_survives_reset(self):
        from lib.llm_dispatch.api import _StreamRetryState
        st = _StreamRetryState()
        st.note_quota_key(SimpleNamespace(key_name='kQ', model='m1'))
        st.exclude_keys.add('kT')  # transient key exclusion
        _force_reset(st)
        st.maybe_reset_exclusions('[t]', 'dispatch_stream')
        survivors = st.eff_exclude_keys() or set()
        assert 'kQ' in survivors, (
            'a quota-dead key must stay excluded for the whole dispatch call')
        assert 'kT' not in survivors

    def test_whole_key_permission_escalation_is_durable(self):
        from lib.llm_dispatch.api import _StreamRetryState
        s1 = _make_slot(model='m1', key='k1')
        s2 = _make_slot(model='m2', key='k1')
        disp = SimpleNamespace(slots=[s1, s2])
        st = _StreamRetryState()
        st.note_permission_pair(s1, disp, 'text', '[t]')
        st.note_permission_pair(s2, disp, 'text', '[t]')
        assert 'k1' in st.exclude_keys_durable, (
            '401 on EVERY model of a key must exclude the whole key durably')
        _force_reset(st)
        st.maybe_reset_exclusions('[t]', 'dispatch_stream')
        assert 'k1' in (st.eff_exclude_keys() or set())

    def test_eff_unions_only_after_first_attempt(self):
        from lib.llm_dispatch.api import _StreamRetryState
        st = _StreamRetryState()
        st.exclude_pairs_durable.add(('k1', 'm1'))
        assert st.eff_exclude_pairs() is None, (
            'attempt-1 pick must see no exclusions (legacy gating)')
        st.hard_attempts = 1
        assert ('k1', 'm1') in (st.eff_exclude_pairs() or set())


# ── B2: end-to-end — no resurrection of the dead pair during 429 cycling ──

class _PairRouter:
    """Picks the kA slot while it is not excluded, else kB.

    Records the exclude_pairs kwarg of every pick so the test can assert
    the dead pair stays excluded across the 60s reset.
    """

    def __init__(self, ka, kb):
        self.slots = [ka, kb]
        self._ka, self._kb = ka, kb
        self.seen_pairs = []

    def pick_and_reserve(self, **kw):
        pairs = set(kw.get('exclude_pairs') or set())
        self.seen_pairs.append(pairs)
        slot = (self._kb if (self._ka.key_name, self._ka.model) in pairs
                else self._ka)
        slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return True

    def summarize_slots(self, *a, **kw):
        return 'pair-router'


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, secs):
        # One sleep jumps past the 60s reset interval so the reset fires.
        self.now += max(secs, 61.0)


@pytest.mark.unit
class TestNoResurrectionDuringCycling:
    def test_dead_permission_pair_not_retried_after_reset(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm import PermissionError_, RateLimitError

        ka = _make_slot(model='gpt-4o', key='kA')
        kb = _make_slot(model='gpt-4o', key='kB')
        router = _PairRouter(ka, kb)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: router)
        clock = _FakeClock()
        monkeypatch.setattr(api, 'time', clock)

        calls = {'kA': 0, 'kB': 0}

        def _fake_stream(body, api_key=None, **kwargs):
            if api_key == ka.api_key:
                calls['kA'] += 1
                raise PermissionError_('401 invalid appid')
            calls['kB'] += 1
            if calls['kB'] == 1:
                raise RateLimitError('429 slow down', status_code=429)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['kA'] == 1, (
            'the 401-dead pair was resurrected and re-tried after the 60s '
            f'reset (kA calls={calls["kA"]})')
        assert calls['kB'] == 2
        assert clock.now >= 61, 'test did not actually cross the reset interval'
        assert router.seen_pairs, 'dispatcher never consulted exclusions'
        assert all((ka.key_name, ka.model) in pairs
                   for pairs in router.seen_pairs[1:]), (
            'every pick after the 401 must keep the dead pair excluded, '
            f'included past the reset: {router.seen_pairs}')


# ── C1: has_capable_slots prefer_model narrowing (real dispatcher) ────

def _real_dispatcher(slots):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher.__new__(LLMDispatcher)
    d.slots = list(slots)
    d._initialized = True
    d._lock = threading.Lock()
    d._alias_index = {}
    d._contention_strikes = {}
    d.face_refusals = []
    return d


@pytest.mark.unit
class TestHasCapableSlotsPreferModel:
    def test_strict_group_vacant_despite_healthy_other_models(self):
        kimi = _make_slot(model='kimi-k3', key='k1')
        glm = _make_slot(model='glm-5.1', key='k2')
        d = _real_dispatcher([kimi, glm])
        excluded = {('k1', 'kimi-k3')}
        assert d.has_capable_slots(
            'text', exclude_pairs=excluded,
            prefer_model='kimi-k3') is False, (
            'under strict_model the ONLY question is whether the pinned '
            "model's group still has a slot — healthy strangers are "
            'irrelevant')
        assert d.has_capable_slots(
            'text', exclude_pairs=excluded) is True, (
            'without prefer_model the pool-wide answer must still see glm')

    def test_strict_group_present_when_slot_not_excluded(self):
        kimi = _make_slot(model='kimi-k3', key='k1')
        d = _real_dispatcher([kimi])
        # Cooldown must NOT count as vacant (429-equivalent → keep cycling).
        kimi.cooldown_until = time.time() + 60
        assert d.has_capable_slots('text', prefer_model='kimi-k3') is True


# ── C2: the strict slot-None branch passes prefer_model through ───────

class _CapSpy:
    def __init__(self, has=False):
        self.slots = []
        self.kw = None
        self._has = has

    def pick_and_reserve(self, **kw):
        return None

    def has_capable_slots(self, *a, **kw):
        self.kw = kw
        return self._has

    def summarize_slots(self, *a, **kw):
        return 'cap-spy'


@pytest.mark.unit
class TestStrictBranchPassesPreferModel:
    def test_strict_call_narrows_vacancy_probe(self, monkeypatch):
        from lib.llm_dispatch import api
        spy = _CapSpy(has=False)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: spy)
        with pytest.raises(Exception):
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='kimi-k3', strict_model=True, log_prefix='[t]')
        assert spy.kw is not None, 'slot-None branch never probed vacancy'
        assert spy.kw.get('prefer_model') == 'kimi-k3', (
            'strict_model vacancy probe must be narrowed to the pinned model')

    def test_non_strict_call_leaves_probe_pool_wide(self, monkeypatch):
        from lib.llm_dispatch import api
        spy = _CapSpy(has=False)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: spy)
        with pytest.raises(Exception):
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='kimi-k3', strict_model=False, log_prefix='[t]')
        assert spy.kw is not None
        assert spy.kw.get('prefer_model') is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
