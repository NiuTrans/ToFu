"""Subscription-quota retry/wait HUD surfacing.

The Codex OAuth subscription quota signal (``usage_limit_reached`` with a
``resets_at`` window hours/days away) must NOT be labelled a transient 429
"queued retry" — the slot is parked for the whole window, so the HUD copy
must say quota-exhausted, not rate-limited. These tests pin the three hops
where that classification used to be dropped:

  1. ``retry_phase_fields`` — the 'Subscription quota reached' reason wins
     over a 429 status (status branch must not swallow it into
     retryRateLimited) and maps to a dedicated detailKey + typed reasonKey.
  2. ``cooldown_wait_label`` — a 'quota' cooldown cause outranks contention
     and rate-limit when labelling an all-slots-cooling wait.
  3. ``dispatcher.dominant_cooldown_cause`` — 'quota' beats the other causes.
  4. Dispatcher RateLimitError path — a subscription-quota 429 notifies
     on_retry with reason='Subscription quota reached', not
     'Rate limited (429)'.
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit


class TestSubscriptionQuotaPhaseFields(unittest.TestCase):
    def _fields(self, **kw):
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        return retry_phase_fields(**kw)

    def test_subscription_reason_beats_429_status(self):
        f = self._fields(model='gpt-6-astra', attempt=7,
                         reason='Subscription quota reached', status_code=429,
                         legacy_detail='X')
        self.assertEqual(f['detailKey'], 'stream.phase.subscriptionQuotaReached')
        self.assertEqual(f['detailArgs'],
                         {'model': 'gpt-6-astra', 'attempt': 7})
        self.assertEqual(f['detail'], 'X')

    def test_reason_token_maps_to_typed_key(self):
        from lib.llm_dispatch.retry_i18n import RETRY_REASON_KEYS
        self.assertEqual(RETRY_REASON_KEYS['Subscription quota reached'],
                         'stream.retryReason.subscriptionQuotaReached')


class TestSubscriptionQuotaWaitLabel(unittest.TestCase):
    def test_quota_outranks_contention_and_rate_limit(self):
        from lib.llm_dispatch.retry_i18n import cooldown_wait_label
        reason, status = cooldown_wait_label({'quota', 'contention'})
        self.assertEqual(reason, 'Subscription quota reached')
        self.assertEqual(status, 429)

    def test_quota_alone(self):
        from lib.llm_dispatch.retry_i18n import cooldown_wait_label
        reason, status = cooldown_wait_label({'quota'})
        self.assertEqual(reason, 'Subscription quota reached')
        self.assertEqual(status, 429)


def _make_slot(key):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-x', model='gpt-6-astra',
                capabilities={'text'})


def test_dispatch_on_retry_gets_subscription_reason(monkeypatch):
    """dispatch_stream: a subscription-quota 429 must notify on_retry with
    reason='Subscription quota reached' (NOT the generic 'Rate limited (429)'),
    so the HUD shows a quota banner instead of 'queued retry'."""
    import time as _t
    from lib.llm_dispatch import api
    from lib.llm.llm_errors import RateLimitError

    slot1, slot2 = _make_slot('k1'), _make_slot('k2')

    class _Disp:
        def __init__(self):
            self._q = [slot1, slot2]
            self.slots = [slot1, slot2]
        def pick_and_reserve(self, **kw):
            s = self._q.pop(0)
            if s is not None:
                s.record_request()
            return s
        def has_capable_slots(self, *a, **k):
            return bool(self._q)
        def summarize_slots(self, *a, **k):
            return 'fake'
        def note_shared_contention(self, slot):
            pass

    disp = _Disp()
    monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                        lambda *_a, **_k: None)
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
    monkeypatch.setattr('lib.key_stats.record_rate_limit',
                        lambda *a, **k: False)

    retries = []
    calls = {'n': 0}

    def _fake_stream(body, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RateLimitError(
                'usage_limit_reached "resets_in_seconds": 900',
                status_code=429)
        return 'ok', 'stop', {}

    import lib.llm as llm_mod
    monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

    msg, finish, _usage = api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        on_retry=lambda **kw: retries.append(kw))

    assert msg == 'ok'
    assert calls['n'] == 2
    assert retries, 'on_retry was never called'
    assert retries[0]['reason'] == 'Subscription quota reached'
    assert retries[0]['status_code'] == 429
    assert slot1.cooldown_reason == 'quota'
    assert slot1.cooldown_until - _t.time() > 800


class TestDominantCooldownCause(unittest.TestCase):
    def _dispatcher_with_causes(self, causes):
        from lib.llm_dispatch.dispatcher import LLMDispatcher
        d = LLMDispatcher.__new__(LLMDispatcher)
        d.cooling_cause_summary = lambda *a, **k: set(causes)
        return d

    def test_quota_wins(self):
        d = self._dispatcher_with_causes({'quota', 'rate_limit'})
        self.assertEqual(d.dominant_cooldown_cause('text'), 'quota')

    def test_empty_when_nothing_cooling(self):
        d = self._dispatcher_with_causes(set())
        self.assertEqual(d.dominant_cooldown_cause('text'), '')


if __name__ == '__main__':
    unittest.main()
