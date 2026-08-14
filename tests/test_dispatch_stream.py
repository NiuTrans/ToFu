"""Behavioural tests for the SYNC ``dispatch_stream`` (lib/llm_dispatch/api.py).

The async sibling (``async_dispatch_stream``) already has a behavioural suite in
``test_async_dispatch_stream.py``; the sync path only had a signature test.  This
file pins the sync retry/exclusion state machine so the upcoming
``_StreamRetryState`` extraction (shared between the sync + async loops) is
covered on BOTH sides:

  - success returns (msg, finish, usage), records slot success, injects
    ``usage['_dispatch']`` metadata;
  - a 429 (RateLimitError, non-quota) is retried for FREE (does not count
    toward max_retries) then succeeds on the next slot;
  - a quota-exhausted 429 (is_quota=True) excludes the KEY and counts as a
    hard attempt;
  - a PermissionError excludes the (key, model) PAIR (not the whole model),
    then fails over;
  - an EndpointUnreachableError cools the slot + excludes the pair, then
    fails over to a live slot;
  - AbortedError propagates immediately (no retry) and releases the slot.

Run:  pytest tests/test_dispatch_stream.py -m unit
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_slot(model='qwen-plus', key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'})


class _FakeDispatcher:
    """Hands out a queued list of slots; None entries simulate 'no slot'.

    Exposes ``slots`` (the sync PermissionError handler inspects
    ``dispatcher.slots`` to decide whether to escalate a pair-exclusion to a
    key-exclusion) — default empty so that escalation does NOT fire and we
    test the common pair-exclusion path.
    """

    def __init__(self, slots, all_slots=None):
        self._slots = list(slots)
        self.slots = list(all_slots or [])
        self.picks = 0

    def pick_and_reserve(self, **kwargs):
        self.picks += 1
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        if slot is not None:
            slot.record_request()  # mirror real pick_and_reserve(reserve=True)
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # 429 retries call time.sleep(0.3) — make them instant so tests are fast.
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


@pytest.mark.unit
class TestDispatchStreamSuccess:
    def test_success_returns_tuple_and_records_slot(self, monkeypatch):
        from lib.llm_dispatch import api

        slot = _make_slot()
        disp = _FakeDispatcher([slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            kwargs['on_content']('hello ')
            kwargs['on_content']('world')
            return 'hello world', 'stop', {'completion_tokens': 7}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        chunks = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}],
            on_content=chunks.append, log_prefix='[t]')

        assert msg == 'hello world'
        assert finish == 'stop'
        assert chunks == ['hello ', 'world']
        assert slot.inflight == 0
        assert slot.last_success_time > 0
        assert slot.consecutive_errors == 0
        assert usage['_dispatch']['model'] == 'qwen-plus'
        assert usage['_dispatch']['key'] == 'k0'
        assert usage['_dispatch']['429_retries'] == 0


@pytest.mark.unit
class TestDispatchStreamRetry:
    def test_429_then_success_is_free(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        # The non-quota 429 path probes is_key_enabled — keep the key enabled
        # so the routine-backpressure branch (free retry) is exercised.
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('429 too many requests')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]', max_retries=1)

        # max_retries=1 yet a 429 still succeeded → the 429 retry was FREE
        # (didn't count toward hard_attempts).
        assert msg == 'ok'
        assert calls['n'] == 2
        assert usage['_dispatch']['429_retries'] >= 1
        assert slot1.total_errors >= 1
        assert slot2.last_success_time > 0

    def test_quota_429_excludes_key_and_counts_hard(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                e = RateLimitError('quota exhausted')
                e.is_quota = True
                raise e
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        retries = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            on_retry=lambda **kw: retries.append(kw))

        assert msg == 'ok'
        assert calls['n'] == 2
        # Quota exhaustion is a HARD attempt with a 'Key balance exhausted'
        # retry notice (distinct from the free 429 path).
        assert any('balance' in (r.get('reason') or '').lower() for r in retries)


@pytest.mark.unit
class TestDispatchStreamPermissionPairExclusion:
    def test_permission_excludes_pair_then_fails_over(self, monkeypatch):
        """401/403 on (key, model) must exclude only that PAIR, letting another
        key serving the same model still be tried (the pair-exclusion fix)."""
        from lib.llm_dispatch import api
        from lib.llm import PermissionError_

        # Two keys, SAME model — pair-exclusion must route to the 2nd key.
        denied = _make_slot(model='gpt-4o', key='kA')
        allowed = _make_slot(model='gpt-4o', key='kB')
        disp = _FakeDispatcher([denied, allowed])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError_('403 forbidden')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2
        assert denied.total_errors >= 1
        assert allowed.last_success_time > 0


@pytest.mark.unit
class TestDispatchStreamUnreachableFailover:
    def test_unreachable_cools_slot_and_fails_over(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import EndpointUnreachableError

        dead = _make_slot(key='dead')
        live = _make_slot(key='live')
        disp = _FakeDispatcher([dead, live])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise EndpointUnreachableError(
                    'endpoint unreachable: connect timeout',
                    base_url='http://10.0.0.1:8080/v1')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2
        assert dead.cooldown_until > time.time()
        assert dead.total_errors >= 1
        assert live.last_success_time > 0


@pytest.mark.unit
class TestDispatchStreamAllUnreachable:
    def test_all_unreachable_raises_friendly_error(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import EndpointUnreachableError

        dead = _make_slot(model='glm5.1-FP8', key='dead')
        disp = _FakeDispatcher([dead])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            raise EndpointUnreachableError(
                'endpoint unreachable: connect timeout',
                base_url='http://10.0.0.1:8080/v1')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        with pytest.raises(EndpointUnreachableError) as ei:
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='glm5.1-FP8', strict_model=True,
                max_retries=3, log_prefix='[t]')
        msg = str(ei.value)
        assert 'unreachable' in msg.lower()
        assert 'glm5.1-FP8' in msg


@pytest.mark.unit
class TestFinalizeStreamSuccessHelper:
    """The success-path bookkeeping shared by the sync + async dispatch loops
    was extracted into ``_finalize_stream_success``. Pin its contract directly
    so a drift in WHICH usage fields get stamped fails here (both loops call
    it identically, so this covers both)."""

    def _state(self, hard=0, r429=0):
        from lib.llm_dispatch.api import _StreamRetryState
        st = _StreamRetryState()
        st.hard_attempts = hard
        st._429_count = r429
        return st

    def test_stamps_dispatch_metadata_and_records_success(self):
        from lib.llm_dispatch.api import _finalize_stream_success
        slot = _make_slot(model='gpt-4o', key='kZ')
        usage = {'completion_tokens': 11}
        st = self._state(hard=1, r429=2)
        _finalize_stream_success(slot, usage, latency=123.4, ttft=45.6,
                                 state=st, cache_conv_id='', tag='[t]')
        assert usage['_dispatch']['model'] == 'gpt-4o'
        assert usage['_dispatch']['key'] == 'kZ'
        assert usage['_dispatch']['latency_ms'] == 123
        assert usage['_dispatch']['attempt'] == 2      # hard_attempts + 1
        assert usage['_dispatch']['429_retries'] == 2
        assert slot.last_success_time > 0
        assert slot.consecutive_errors == 0

    def test_non_dict_usage_is_tolerated(self):
        from lib.llm_dispatch.api import _finalize_stream_success
        slot = _make_slot()
        st = self._state()
        # Some providers return None usage — must not raise, must still record.
        _finalize_stream_success(slot, None, latency=10.0, ttft=None,
                                 state=st, cache_conv_id='', tag='[t]')
        assert slot.last_success_time > 0

    def test_output_tokens_from_output_tokens_key(self):
        from lib.llm_dispatch.api import _finalize_stream_success
        slot = _make_slot()
        # Anthropic-shape usage uses output_tokens, not completion_tokens.
        usage = {'output_tokens': 9}
        _finalize_stream_success(slot, usage, latency=5.0, ttft=None,
                                 state=self._state(), cache_conv_id='', tag='[t]')
        assert usage['_dispatch']['model'] == slot.model

    def test_codex_success_arms_unmetered_write_visibility(self, monkeypatch):
        from lib.llm_dispatch import cache_settle
        from lib.llm_dispatch.api import _finalize_stream_success

        observed = []
        recorded = []
        monkeypatch.setattr(
            cache_settle, 'observe_codex_cache',
            lambda conv_id, usage: observed.append((conv_id, usage)))
        monkeypatch.setattr(
            cache_settle, 'record_stream_end',
            lambda conv_id, **kwargs: recorded.append((conv_id, kwargs)))

        slot = _make_slot(model='gpt-5.6-luna', key='codex-key')
        slot.oauth = 'codex'
        usage = {
            'prompt_tokens': 5066,
            'prompt_tokens_details': {'cached_tokens': 0},
        }
        _finalize_stream_success(
            slot, usage, latency=5.0, ttft=None,
            state=self._state(), cache_conv_id='conv-codex', tag='[t]')

        assert observed == [('conv-codex', usage)]
        assert recorded[0][0] == 'conv-codex'
        assert recorded[0][1]['cache_profile'] == 'codex'
        assert recorded[0][1]['pending_write'] is True


@pytest.mark.unit
class TestDispatchStreamAbort:
    def test_abort_propagates_and_releases_slot(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm import AbortedError

        slot = _make_slot()
        disp = _FakeDispatcher([slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            raise AbortedError('user aborted')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        with pytest.raises(AbortedError):
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
        assert slot.inflight == 0


@pytest.mark.unit
class TestDispatchStreamQuotaScope:
    """End-to-end at the dispatch seam: RateLimitError.status_code decides
    the key_stats stop granularity (owner 2026-07-29: HTTP 402 = the
    gateway ACCOUNT's credit pool is dead → key-wide stop; a 429-quota
    body stays vendor-ambiguous → per-model stop)."""

    @pytest.fixture
    def isolated_key_stats(self, monkeypatch, tmp_path):
        import lib.key_stats as ks
        snapshot = {k: ks._cache[k] for k in
                    ('day', 'stats', 'overrides', 'loaded')}
        monkeypatch.setattr(ks, '_STATS_PATH',
                            str(tmp_path / 'key_stats.json'))
        monkeypatch.setattr(ks, '_list_siblings',
                            lambda pid: ['default::k1', 'default::k2'])
        ks._cache.update(day='', stats={}, overrides={}, loaded=False)
        yield ks
        ks._cache.update(snapshot)

    def _run_quota_dispatch(self, monkeypatch, status_code):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('quota exhausted',
                                     is_quota=True,
                                     status_code=status_code)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
        assert msg == 'ok' and calls['n'] == 2

    def test_402_quota_stops_entire_key(self, monkeypatch,
                                        isolated_key_stats):
        ks = isolated_key_stats
        self._run_quota_dispatch(monkeypatch, status_code=402)
        row = ks.get_today_stats('default', 'k1')
        assert row['exhausted'] is True, (
            'a 402 quota error must flip the KEY-WIDE exhausted flag — '
            'the account credit pool is dead for EVERY model on the key')
        assert row['exhausted_models'] == {}
        assert ks.is_key_enabled('default', 'k1') is False
        assert ks.is_key_enabled('default', 'k1', model='qwen-plus') is False

    def test_429_quota_stops_only_the_model(self, monkeypatch,
                                            isolated_key_stats):
        ks = isolated_key_stats
        self._run_quota_dispatch(monkeypatch, status_code=429)
        row = ks.get_today_stats('default', 'k1')
        assert row['exhausted'] is False, (
            'a 429-quota body is vendor-ambiguous and must NOT key-wide '
            'stop the key (2026-07-28 aggregating-gateway contract)')
        assert set(row['exhausted_models']) == {'qwen-plus'}
        assert ks.is_key_enabled('default', 'k1', model='other-model') is True


@pytest.mark.unit
class TestQuotaScopeCallSites:
    """Every RateLimitError handler in the dispatch layer must wire the
    402-vs-429 distinction into record_error — a call site that forgets
    `is_account_quota=` silently degrades account-level 402s back to
    per-model convergence (the 2026-07-29 key_2 incident shape)."""

    def test_every_quota_record_error_passes_account_scope(self):
        from tests._source_scan import strip_comments

        with open('lib/llm_dispatch/api.py', encoding='utf-8') as f:
            live = strip_comments(f.read(), lang='python')

        calls = []
        idx = 0
        while True:
            j = live.find('record_error(', idx)
            if j < 0:
                break
            depth = 0
            k = j + len('record_error')
            for k in range(k, len(live)):
                ch = live[k]
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        break
            calls.append(live[j:k + 1])
            idx = k + 1

        quota_sites = [c for c in calls if 'is_quota_exhausted=' in c]
        assert len(quota_sites) >= 3, (
            f'expected >=3 quota record_error call sites in api.py, found '
            f'{len(quota_sites)} — the scan must stay non-vacuous')
        for c in quota_sites:
            assert 'is_account_quota=' in c, (
                'a quota record_error call site does not pass '
                'is_account_quota= — the 402 key-wide rule is unwired there')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
if __name__ == '__main__':
    pytest.main([__file__, '-v'])
