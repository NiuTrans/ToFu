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
    d._logical_index = {}
    d._direct_models = set()
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


@pytest.mark.unit
def test_dispatch_chat_excludes_stream_only_model_on_first_pick(monkeypatch):
    """A Responses subscription slot must never receive stream=false.

    The permanent stream-only exclusion used to be assembled before attempt
    one but not passed to ``pick_and_reserve`` until a prior failure existed,
    causing a deterministic upstream ``Stream must be set to true`` 400.
    """
    from lib.llm_dispatch import api

    slot = _make_slot(model='gpt-5.4', key='codex')
    slot.stream_only = True

    class _StreamOnlySpy:
        slots = [slot]

        def __init__(self):
            self.seen = []

        def pick_and_reserve(self, **kw):
            excluded = set(kw.get('exclude_models') or set())
            self.seen.append(excluded)
            return None if slot.model in excluded else slot

        def has_capable_slots(self, *args, **kw):
            return slot.model not in set(kw.get('exclude_models') or set())

        def summarize_slots(self, *args, **kw):
            return 'stream-only-spy'

    spy = _StreamOnlySpy()
    monkeypatch.setattr(api, 'get_dispatcher', lambda: spy)
    with pytest.raises(Exception):
        api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='gpt-5.4', strict_model=True, max_retries=1)
    assert spy.seen == [{'gpt-5.4'}]


# ── D: a durable-dead pool must EXIT the cooldown cycle, not spin ──────

@pytest.mark.unit
class TestDurableDeadPoolCycleTerminates:
    def test_all_keys_permission_dead_exits_not_spins(self, monkeypatch):
        """The cooldown cycle must TERMINATE when every capable slot is dead
        on a non-healable class (permission/quota). The old exit condition
        ``_429_count > 0 or _slots_exist`` was self-sustaining:
        note_cooldown_cycle() increments _429_count, so after ANY single
        cooldown cycle the disjunct stayed true forever and the loop spun on
        a durable-excluded pool — the 2026-08-05 CI hang (233daa6 serial
        lane, insight second-pass burned 600s at the test timeout).

        Shape: one transient cooldown cycle (slots exist → keep cycling),
        then the only key goes 401 → durable exclusion → the next pick finds
        nothing AND the healable pool is empty → the loop must raise, not
        sleep again."""
        from lib.llm import PermissionError_
        from lib.llm_dispatch import api

        ka = _make_slot(model='gpt-4o', key='kA')

        class _OneKeyRouter:
            def __init__(self):
                self.slots = [ka]
                self.picks = 0
                self._cool_once = True

            def pick_and_reserve(self, **kw):
                self.picks += 1
                if self.picks > 60:
                    raise RuntimeError('cycle did not terminate')
                keys = set(kw.get('exclude_keys') or set())
                pairs = set(kw.get('exclude_pairs') or set())
                if ka.key_name in keys or (ka.key_name, ka.model) in pairs:
                    return None
                if self._cool_once:
                    # 'Cooling from another request's 429': pick finds nothing
                    # even though a capable slot exists — the first cycle.
                    self._cool_once = False
                    return None
                ka.record_request()
                return ka

            def has_capable_slots(self, *a, **kw):
                keys = set(kw.get('exclude_keys') or set())
                pairs = set(kw.get('exclude_pairs') or set())
                return not (ka.key_name in keys
                            or (ka.key_name, ka.model) in pairs)

            def summarize_slots(self, *a, **kw):
                return 'one-key'

        router = _OneKeyRouter()
        monkeypatch.setattr(api, 'get_dispatcher', lambda: router)
        clock = _FakeClock()
        monkeypatch.setattr(api, 'time', clock)

        def _fake_stream(body, api_key=None, **kwargs):
            raise PermissionError_('401 invalid appid')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        with pytest.raises(Exception) as exc_info:
            api.dispatch_stream([{'role': 'user', 'content': 'hi'}],
                                log_prefix='[t]')
        assert 'cycle did not terminate' not in str(exc_info.value), (
            'dispatch_stream spun on a durable-dead pool — the cooldown '
            'cycle never exits (the CI-hang regression)')
        assert router.picks < 60, (
            f'loop did not exit promptly after the durable exclusion '
            f'(picks={router.picks})')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
