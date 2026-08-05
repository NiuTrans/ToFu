"""Fruit 1 (E2): Codex subscription-quota signal → RATE-LIMIT class with a
TIMED cooldown parsed from ``resets_at`` / ``resets_in_seconds``.

The ChatGPT Codex upstream signals subscription-quota exhaustion with
``error.type == "usage_limit_reached"`` (+ ``resets_at`` unix ts and/or
``resets_in_seconds``), and capacity pressure with the body text
"selected model is at capacity". These must:

  - classify as RATE-LIMIT-class (RateLimitError), NOT billing-quota
    (``is_quota`` stays False — the key is healthy, the subscription window
    resets), and NOT shared-project contention;
  - carry the parsed reset duration (``retry_after_s``) so the dispatcher
    cools the slot for the explicit duration instead of the generic 0.5s
    429 steering cooldown;
  - leave plain transient 429 handling (retry-until-success, 0.5s cooldown)
    untouched (regression guard for commit 80431312).

Run:  pytest tests/test_subscription_quota_cooldown.py -m unit
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _classify(status, body):
    from lib.llm_errors import _classify_http_error
    _classify_http_error(status, body, 'gpt-5-codex', '[t]')


@pytest.mark.unit
class TestSubscriptionQuotaClassification:
    def test_usage_limit_reached_with_resets_in_seconds(self):
        from lib.llm_errors import RateLimitError
        body = ('{"error":{"type":"usage_limit_reached",'
                '"message":"You have hit your usage limit",'
                '"resets_in_seconds":300}}')
        with pytest.raises(RateLimitError) as ei:
            _classify(429, body)
        e = ei.value
        assert e.is_subscription_quota is True
        assert e.retry_after_s == pytest.approx(300, abs=2)
        # NOT billing exhaustion, NOT shared-project contention
        assert e.is_quota is False
        assert e.is_shared_contention is False
        assert e.status_code == 429

    def test_usage_limit_reached_with_resets_at_unix_ts(self):
        from lib.llm_errors import RateLimitError
        future = int(time.time()) + 600
        body = ('{"error":{"type":"usage_limit_reached",'
                '"message":"usage limit reached","resets_at":%d}}' % future)
        with pytest.raises(RateLimitError) as ei:
            _classify(429, body)
        assert ei.value.is_subscription_quota is True
        assert ei.value.retry_after_s == pytest.approx(600, abs=5)

    def test_usage_limit_reached_without_reset_falls_back(self):
        """No resets_* fields → still rate-limit class, but NO explicit
        duration (the dispatcher then uses the generic 429 policy)."""
        from lib.llm_errors import RateLimitError
        body = '{"error":{"type":"usage_limit_reached","message":"limit"}}'
        with pytest.raises(RateLimitError) as ei:
            _classify(429, body)
        assert ei.value.is_subscription_quota is True
        assert ei.value.retry_after_s is None

    def test_selected_model_at_capacity_is_rate_limit_class(self):
        from lib.llm_errors import RateLimitError
        body = '{"error":{"message":"selected model is at capacity"}}'
        with pytest.raises(RateLimitError) as ei:
            _classify(429, body)
        assert ei.value.is_subscription_quota is True

    def test_plain_429_not_subscription_quota(self):
        """Regression guard: a transient per-key 429 keeps the old shape."""
        from lib.llm_errors import RateLimitError
        with pytest.raises(RateLimitError) as ei:
            _classify(429, '{"error":{"message":"Rate limit reached"}}')
        e = ei.value
        assert e.is_subscription_quota is False
        assert e.retry_after_s is None
        assert e.is_quota is False

    def test_billing_quota_429_not_subscription_quota(self):
        """insufficient_quota stays on the BILLING channel (is_quota)."""
        from lib.llm_errors import RateLimitError
        body = '{"error":{"code":"insufficient_quota","message":"x"}}'
        with pytest.raises(RateLimitError) as ei:
            _classify(429, body)
        assert ei.value.is_quota is True
        assert ei.value.is_subscription_quota is False

    def test_rate_limit_error_auto_detects_from_message_text(self):
        """SSE raise sites outside llm_errors only carry the raw body text in
        the message — the exception itself must still surface the parsed
        reset duration so the dispatcher can wire the timed cooldown."""
        from lib.llm_errors import RateLimitError
        e = RateLimitError('SSE error: usage_limit_reached: slow down '
                           '"resets_in_seconds": 120')
        assert e.is_subscription_quota is True
        assert e.retry_after_s == pytest.approx(120, abs=1)


@pytest.mark.unit
class TestSlotExplicitCooldown:
    def _slot(self):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name='k0', api_key='sk-x', model='gpt-5-codex',
                    capabilities={'text'})

    def test_record_error_uses_explicit_cooldown(self):
        slot = self._slot()
        slot.record_request()
        before = time.time()
        slot.record_error(is_rate_limit=True, cooldown_s=600,
                          error='usage_limit_reached')
        remaining = slot.cooldown_until - before
        assert remaining == pytest.approx(600, abs=5)
        assert slot.cooldown_reason == 'quota'
        assert slot.inflight == 0

    def test_default_429_cooldown_unchanged(self):
        """Guard (mutation check): no explicit duration → the 0.5s steering
        cooldown + 'rate_limit' reason from the 80431312 policy is intact."""
        slot = self._slot()
        slot.record_request()
        before = time.time()
        slot.record_error(is_rate_limit=True)
        remaining = slot.cooldown_until - before
        assert remaining == pytest.approx(0.5, abs=0.4)
        assert slot.cooldown_reason == 'rate_limit'


@pytest.mark.unit
class TestDispatchWiresRetryAfter:
    """dispatch_chat must hand the parsed reset duration to the slot."""

    def _slot(self, key):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name=key, api_key='sk-x', model='gpt-5-codex',
                    capabilities={'text'})

    def test_dispatch_chat_cools_slot_for_reset_duration(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = self._slot('k1')
        slot2 = self._slot('k2')

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

        calls = {'n': 0}

        def _fake_chat(**kw):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError(
                    'usage_limit_reached "resets_in_seconds": 900',
                    status_code=429)
            return 'ok', {'completion_tokens': 1}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'chat', _fake_chat)

        content, usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert content == 'ok'
        assert calls['n'] == 2
        # The failed slot was parked for the RESET duration, not 0.5s.
        assert slot1.cooldown_until - time.time() > 800
        assert slot1.cooldown_reason == 'quota'
        assert usage['_dispatch']['429_retries'] == 1
