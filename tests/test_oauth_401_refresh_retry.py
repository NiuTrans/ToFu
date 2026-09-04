"""Fruit 2 (E2): a 401 on an OAuth-subscription slot forces ONE token
refresh (bypassing the near-expiry check — the provider's refresh function
is called directly) and ONE retry with the new token before normal
failover applies. Non-OAuth (API-key) slots keep the current behavior
(pair exclusion + failover, no refresh call — 2026-08-03 403 pool
fallback must not regress).

Run:  pytest tests/test_oauth_401_refresh_retry.py -m unit
"""
from __future__ import annotations

import asyncio
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
        self.slots = list(all_slots if all_slots is not None else slots)
        self.picks = 0

    def pick_and_reserve(self, **kwargs):
        self.picks += 1
        excluded_models = set(kwargs.get('exclude_models') or ())
        excluded_keys = set(kwargs.get('exclude_keys') or ())
        excluded_pairs = set(kwargs.get('exclude_pairs') or ())
        while self._slots:
            slot = self._slots.pop(0)
            if slot is None:
                return None
            if slot.model in excluded_models or slot.key_name in excluded_keys:
                continue
            if (slot.key_name, slot.model) in excluded_pairs:
                continue
            slot.record_request()
            return slot
        return None

    def has_capable_slots(self, *a, **kw):
        excluded_models = set(kw.get('exclude_models') or ())
        excluded_keys = set(kw.get('exclude_keys') or ())
        excluded_pairs = set(kw.get('exclude_pairs') or ())
        return any(
            slot is not None
            and slot.model not in excluded_models
            and slot.key_name not in excluded_keys
            and (slot.key_name, slot.model) not in excluded_pairs
            for slot in self._slots
        )

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


@pytest.mark.unit
class TestOAuth401RefreshRetry:
    def test_http_classifier_preserves_credential_wide_401_status(self):
        from lib.llm_errors import PermissionError_, _classify_http_error

        with pytest.raises(PermissionError_) as raised:
            _classify_http_error(
                401, 'Provided authentication token is expired',
                'gpt-5.6-sol', '[t]')

        assert raised.value.status_code == 401

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

        refresh = {'n': 0, 'user_id': None}

        def _fake_refresh(*a, **k):
            refresh['n'] += 1
            refresh['user_id'] = k.get('user_id')
            return {'access_token': 'fresh-token'}

        import lib.oauth.claude as claude_mod
        monkeypatch.setattr(claude_mod, 'claude_refresh_token', _fake_refresh)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            owner_user_id=41)

        assert msg == 'ok'
        assert calls['n'] == 2, 'first attempt 401 → refresh → one retry'
        assert refresh['n'] == 1, 'exactly one forced refresh'
        assert refresh['user_id'] == '41'
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

    def test_oauth_http_401_excludes_sibling_models_on_same_key(
            self, monkeypatch):
        """One rejected bearer token is shared by all models on its row."""
        from lib.llm_dispatch import api
        from lib.llm_errors import PermissionError_

        rejected = _make_slot(model='gpt-5.6-sol', key='oauth-codex',
                              oauth='codex')
        same_credential = _make_slot(model='gpt-5.4-nano', key='oauth-codex',
                                     oauth='codex')
        healthy = _make_slot(model='kimi-k3', key='healthy-key', oauth='')
        disp = _FakeDispatcher([rejected, same_credential, healthy])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        attempted_models = []

        def _fake_stream(body, **kwargs):
            attempted_models.append(body['model'])
            if body['model'] == 'gpt-5.6-sol':
                raise PermissionError_(
                    'Provided authentication token is expired',
                    status_code=401)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)
        import lib.oauth.codex as codex_mod
        monkeypatch.setattr(codex_mod, 'codex_refresh_token',
                            lambda *a, **k: None)

        msg, _finish, _usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert attempted_models == ['gpt-5.6-sol', 'kimi-k3']
        assert same_credential.consecutive_errors == 0

    def test_oauth_http_401_key_exclusion_matches_non_stream_dispatch(
            self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import PermissionError_

        rejected = _make_slot(model='gpt-5.6-sol', key='oauth-codex',
                              oauth='codex')
        same_credential = _make_slot(model='gpt-5.4-nano', key='oauth-codex',
                                     oauth='codex')
        healthy = _make_slot(model='kimi-k3', key='healthy-key', oauth='')
        monkeypatch.setattr(
            api, 'get_dispatcher',
            lambda: _FakeDispatcher([rejected, same_credential, healthy]))
        attempted_models = []

        def _fake_chat(**kwargs):
            attempted_models.append(kwargs['model'])
            if kwargs['model'] == 'gpt-5.6-sol':
                raise PermissionError_('expired', status_code=401)
            return 'ok', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'chat', _fake_chat)
        import lib.oauth.codex as codex_mod
        monkeypatch.setattr(codex_mod, 'codex_refresh_token',
                            lambda *a, **k: None)

        content, _usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert content == 'ok'
        assert attempted_models == ['gpt-5.6-sol', 'kimi-k3']
        assert same_credential.consecutive_errors == 0

    def test_oauth_http_401_key_exclusion_matches_async_dispatch(
            self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import PermissionError_

        rejected = _make_slot(model='gpt-5.6-sol', key='oauth-codex',
                              oauth='codex')
        same_credential = _make_slot(model='gpt-5.4-nano', key='oauth-codex',
                                     oauth='codex')
        healthy = _make_slot(model='kimi-k3', key='healthy-key', oauth='')
        monkeypatch.setattr(
            api, 'get_dispatcher',
            lambda: _FakeDispatcher([rejected, same_credential, healthy]))
        attempted_models = []

        async def _fake_stream(body, **kwargs):
            attempted_models.append(body['model'])
            if body['model'] == 'gpt-5.6-sol':
                raise PermissionError_('expired', status_code=401)
            return 'ok', 'stop', {}

        monkeypatch.setattr('lib.llm.astream.async_stream_chat', _fake_stream)
        import lib.oauth.codex as codex_mod
        monkeypatch.setattr(codex_mod, 'codex_refresh_token',
                            lambda *a, **k: None)

        message, _finish, _usage = asyncio.run(api.async_dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]'))

        assert message == 'ok'
        assert attempted_models == ['gpt-5.6-sol', 'kimi-k3']
        assert same_credential.consecutive_errors == 0

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
