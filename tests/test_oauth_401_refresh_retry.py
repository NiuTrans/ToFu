"""Fruit 2 (E2): a 401 on an OAuth-subscription slot forces ONE token
refresh (bypassing the near-expiry check — the provider's refresh function
is called directly) and ONE retry with the new token before normal
failover applies. Non-OAuth (API-key) slots keep the current behavior
(pair exclusion + failover, no refresh call — 2026-08-03 403 pool
fallback must not regress).

Run:  pytest tests/test_oauth_401_refresh_retry.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_slot(model='claude-sonnet-4', key='k0', oauth='claude'):
    from lib.llm_dispatch.slot import Slot
    s = Slot(key_name=key, api_key='stale-token', model=model,
             capabilities={'text'})
    s.oauth = oauth
    return s


class _FakeDispatcher:
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
            slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


@pytest.mark.unit
class TestOAuth401RefreshRetry:
    def test_401_on_oauth_slot_refreshes_and_retries_once(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import PermissionError_

        slot = _make_slot()
        # Picker re-picks the SAME slot after release (it has no cooldown).
        disp = _FakeDispatcher([slot, slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError_('API HTTP 401: unauthorized')
            return 'ok', 'stop', {'completion_tokens': 1}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        refresh = {'n': 0}

        def _fake_refresh(*a, **k):
            refresh['n'] += 1
            return {'access_token': 'fresh-token'}

        import lib.oauth.claude as claude_mod
        monkeypatch.setattr(claude_mod, 'claude_refresh_token', _fake_refresh)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2, 'first attempt 401 → refresh → one retry'
        assert refresh['n'] == 1, 'exactly one forced refresh'
        # The 401 was NOT treated as a slot-health failure:
        assert slot.consecutive_errors == 0
        assert slot.cooldown_until == 0

    def test_refresh_failure_falls_through_to_normal_failover(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import PermissionError_

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError_('API HTTP 401: unauthorized')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        import lib.oauth.claude as claude_mod
        monkeypatch.setattr(claude_mod, 'claude_refresh_token',
                            lambda *a, **k: None)  # refresh fails

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2, 'no same-slot retry when refresh fails'
        # Normal failover bookkeeping applied to the 401 slot:
        assert slot1.consecutive_errors == 1
        # (Removed a vacuous `... or True` line — it could never fail; the
        # failover bookkeeping contract is covered by the two asserts above.)

    def test_non_oauth_slot_never_refreshes(self, monkeypatch):
        """Guard: plain API-key slots keep today's behavior — immediate pair
        exclusion, no refresh call (the 2026-08-03 all-keys-403 pool
        fallback path must not regress)."""
        from lib.llm_dispatch import api
        from lib.llm_errors import PermissionError_

        slot1 = _make_slot(key='k1', oauth='')   # plain API-key slot
        slot2 = _make_slot(key='k2', oauth='')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError_('API HTTP 403: forbidden')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        refresh = {'n': 0}
        import lib.oauth.claude as claude_mod
        monkeypatch.setattr(
            claude_mod, 'claude_refresh_token',
            lambda *a, **k: refresh.update(n=refresh['n'] + 1))

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2
        assert refresh['n'] == 0, 'no refresh for non-oauth slots'
        assert slot1.consecutive_errors == 1, 'normal failover bookkeeping'

    def test_refresh_retry_happens_at_most_once_per_request(self, monkeypatch):
        """The retried request 401s AGAIN → no second refresh, normal
        failover bookkeeping kicks in (no loops)."""
        from lib.llm_dispatch import api
        from lib.llm_errors import PermissionError_

        slot = _make_slot()
        disp = _FakeDispatcher([slot, slot, slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            raise PermissionError_('API HTTP 401: unauthorized')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        refresh = {'n': 0}

        def _fake_refresh(*a, **k):
            refresh['n'] += 1
            return {'access_token': 'fresh-token'}

        import lib.oauth.claude as claude_mod
        monkeypatch.setattr(claude_mod, 'claude_refresh_token', _fake_refresh)

        with pytest.raises(PermissionError_):
            api.dispatch_stream([{'role': 'user', 'content': 'hi'}],
                                max_retries=2, log_prefix='[t]')

        assert refresh['n'] == 1, 'max one forced refresh per request'
        # The post-refresh 401 took the normal path (health signal).
        assert slot.consecutive_errors >= 1
