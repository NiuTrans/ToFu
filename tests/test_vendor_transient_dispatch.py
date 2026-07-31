#!/usr/bin/env python3
"""tests/test_vendor_transient_dispatch.py — slot-health + HUD-label guards
for the 2026-07-26 yuju claude-opus-5 vendor-4xx storm (pt_48f29db9).

The incident: the vendor outage surfaced as HTTP 400/403 "请求失败,请稍后
(再)尝试" (ext.error.source=UPSTREAM_VENDOR). Three poisoning behaviours are
pinned FIXED here:

  1. A transient 4xx re-classified as RateLimitError(is_gateway=True) must
     NOT feed the consecutive-429 auto-exhaust streak in key_stats (a sick
     upstream auto-disabling HEALTHY keys for the day) — but MUST still
     land in record_outcome (the dead-key safety net: daily failure stats).
  2. A deterministic HTTP 400 (BadRequestError) is PAYLOAD-level: dispatch
     releases the slot (no consecutive_errors → no 300s lockout) and only
     pair-excludes, so sibling keys each get one try.
  3. The retry HUD tells the truth: gateway-class errors surface as
     'Upstream error' (+ the real status), and an all-slots-cooling wait
     labels itself by the ACTUAL cooldown cause — never the hardcoded
     '限流排队' that masked hard-error backoff.

Run:  pytest tests/test_vendor_transient_dispatch.py -m unit
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


def _make_slot(model='qwen-plus', key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'})


@pytest.fixture
def key_stats_recorders(monkeypatch):
    """Capture key_stats calls; the real ones persist to disk."""
    rec = {'rate_limit': [], 'outcome': [], 'exhausted': []}
    monkeypatch.setattr('lib.key_stats.record_rate_limit',
                        lambda p, k, reason='': rec['rate_limit'].append((p, k, reason)) or False)
    monkeypatch.setattr('lib.key_stats.record_outcome',
                        lambda p, k, success, error='': rec['outcome'].append((p, k, success, error)))
    monkeypatch.setattr('lib.key_stats.mark_key_exhausted',
                        lambda p, k, reason='', model='': rec['exhausted'].append((p, k, reason, model)))
    return rec


# ══════════════════════════════════════════════════════════
#  Slot.record_error — is_gateway accounting + cooldown_reason
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSlotGatewayAccounting:

    def test_gateway_error_skips_429_streak_but_keeps_failure_stats(
            self, key_stats_recorders):
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, is_gateway=True,
                          error='HTTP 503: upstream')
        assert key_stats_recorders['rate_limit'] == [], (
            'a sick upstream must NOT feed the consecutive-429 auto-exhaust '
            'streak — that auto-disabled HEALTHY keys for the day')
        assert len(key_stats_recorders['outcome']) == 1, (
            'the dead-key safety net (daily failure stats) must keep working')
        assert key_stats_recorders['outcome'][0][2] is False
        assert slot.cooldown_reason == 'upstream'
        assert 0 < slot.cooldown_until - time.time() <= 1.0, (
            'gateway-class cooldown stays the 0.5s slot-rotation nudge')

    def test_plain_429_still_feeds_the_streak(self, key_stats_recorders):
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, error='HTTP 429')
        assert len(key_stats_recorders['rate_limit']) == 1
        assert key_stats_recorders['outcome'] == []
        assert slot.cooldown_reason == 'rate_limit'

    def test_generic_errors_backoff_marks_error_reason(self, key_stats_recorders):
        slot = _make_slot()
        for _ in range(3):
            slot.record_request()
            slot.record_error(is_rate_limit=False, error='boom')
        assert slot.cooldown_reason == 'error'
        assert slot.cooldown_until - time.time() >= 4.0, (
            'consecutive-error backoff is the multi-second kind (5s..300s)')

    def test_quota_marks_quota_reason(self, key_stats_recorders):
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, is_quota_exhausted=True,
                          error='insufficient_quota')
        assert slot.cooldown_reason == 'quota'
        assert len(key_stats_recorders['exhausted']) == 1

    def test_truncation_marks_error_reason(self, key_stats_recorders):
        slot = _make_slot()
        for _ in range(3):
            slot.record_truncation('premature close')
        assert slot.cooldown_reason == 'error'


# ══════════════════════════════════════════════════════════
#  Dispatcher.cooling_cause_summary
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCoolingCauseSummary:

    def _dispatcher(self, slots):
        from lib.llm_dispatch.dispatcher import LLMDispatcher
        disp = object.__new__(LLMDispatcher)
        disp._lock = threading.Lock()
        disp.slots = list(slots)
        disp.initialize = lambda: None
        return disp

    def test_reports_actual_causes(self):
        s1 = _make_slot(key='k1')
        s1.cooldown_until = time.time() + 0.5
        s1.cooldown_reason = 'rate_limit'
        s2 = _make_slot(key='k2')
        s2.cooldown_until = time.time() + 300
        s2.cooldown_reason = 'error'
        s3 = _make_slot(key='k3')  # not cooling — invisible
        disp = self._dispatcher([s1, s2, s3])
        assert disp.cooling_cause_summary('text') == {'rate_limit', 'error'}

    def test_empty_when_nothing_cooling(self):
        disp = self._dispatcher([_make_slot(key='k1')])
        assert disp.cooling_cause_summary('text') == set()

    def test_exclusions_are_respected(self):
        s1 = _make_slot(key='k1', model='m1')
        s1.cooldown_until = time.time() + 300
        s1.cooldown_reason = 'error'
        disp = self._dispatcher([s1])
        assert disp.cooling_cause_summary(
            'text', exclude_pairs={('k1', 'm1')}) == set()
        assert disp.cooling_cause_summary(
            'text', exclude_models={'m1'}) == set()

    def test_legacy_blank_reason_buckets_as_error(self):
        s1 = _make_slot(key='k1')
        s1.cooldown_until = time.time() + 10
        s1.cooldown_reason = ''  # pre-deploy stamp
        disp = self._dispatcher([s1])
        # '' must NOT masquerade as rate_limit — bucketing as error keeps the
        # honest label for one cooldown lifetime.
        assert disp.cooling_cause_summary('text') == {'error'}


# ══════════════════════════════════════════════════════════
#  dispatch_stream loop — release-vs-poison + honest on_retry
# ══════════════════════════════════════════════════════════

class _FakeDispatcher:
    """Queued slot handout (mirrors tests/test_dispatch_stream.py)."""

    def __init__(self, slots, all_slots=None, causes=()):
        self._slots = list(slots)
        self.slots = list(all_slots or [])
        self._causes = set(causes)

    def pick_and_reserve(self, **kwargs):
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        if slot is not None:
            slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return True

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'

    def cooling_cause_summary(self, *a, **kw):
        return set(self._causes)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


@pytest.mark.unit
class TestDispatchStreamVendorStorm:

    def test_bad_request_releases_slot_and_pair_excludes(
            self, monkeypatch, key_stats_recorders):
        """Deterministic 400: NO consecutive_errors feed (no 300s lockout),
        NO key_stats call — the sibling key still gets its one try."""
        from lib.llm_dispatch import api
        from lib.llm_errors import BadRequestError

        sick = _make_slot(model='opus-x', key='kA')
        healthy = _make_slot(model='opus-x', key='kB')
        disp = _FakeDispatcher([sick, healthy])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise BadRequestError('API HTTP 400: signature: Field required')
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]', max_retries=2)

        assert msg == 'ok'
        assert calls['n'] == 2
        assert sick.consecutive_errors == 0, (
            'a deterministic 400 is PAYLOAD-level — feeding slot health is '
            'the 300s-lockout poisoning from the incident')
        assert sick.inflight == 0, 'release() must return the inflight reservation'
        assert key_stats_recorders['rate_limit'] == []
        _failures = [c for c in key_stats_recorders['outcome'] if not c[2]]
        assert _failures == [], (
            'release() — not record_error — is the ContentFilter precedent '
            '(success bookkeeping for kB is legitimate and unrelated)')
        # pair-exclusion is proven by the failover itself: kB got tried.

    def test_gateway_ratelimit_reports_upstream_error_not_429(
            self, monkeypatch, key_stats_recorders):
        """is_gateway RateLimitError → HUD hears 'Upstream error' + the REAL
        status, and the 429 auto-exhaust streak is NOT fed."""
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('API HTTP 403: 请求失败，请稍后再尝试',
                                     is_gateway=True, status_code=403)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        retries = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            on_retry=lambda **kw: retries.append(kw))

        assert msg == 'ok'
        upstream = [r for r in retries if r.get('reason') == 'Upstream error']
        assert upstream, f'HUD must hear Upstream error, got: {retries}'
        assert upstream[0].get('status_code') == 403, (
            'the REAL status rides along — not a fake 429')
        assert not any(r.get('status_code') == 429 for r in retries)
        assert key_stats_recorders['rate_limit'] == []
        _failures = [c for c in key_stats_recorders['outcome'] if not c[2]]
        assert len(_failures) == 1 and _failures[0][1] == 'k1', (
            'the upstream outage still lands in the daily failure stats '
            '(dead-key safety net); the k2 success is unrelated')

    def test_plain_429_still_reports_rate_limited(
            self, monkeypatch, key_stats_recorders):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('429 too many requests', status_code=429)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        retries = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            on_retry=lambda **kw: retries.append(kw))

        assert msg == 'ok'
        assert any(r.get('reason') == 'Rate limited (429)'
                   and r.get('status_code') == 429 for r in retries)
        assert len(key_stats_recorders['rate_limit']) == 1

    def test_cooldown_wait_labels_itself_by_actual_cause(
            self, monkeypatch, key_stats_recorders):
        """All-slots-cooling with an ERROR-backoff cause must NOT claim
        限流排队 — the incident's fake rate-limit label."""
        from lib.llm_dispatch import api

        good = _make_slot(key='k9')
        # First pick returns None (everything cooling), then the good slot.
        disp = _FakeDispatcher([None, good], causes={'error'})
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        retries = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            on_retry=lambda **kw: retries.append(kw))

        assert msg == 'ok'
        assert retries, 'the cooldown wait must surface a phase'
        assert retries[0].get('reason') == 'Waiting for model (retry backoff)'
        assert retries[0].get('status_code') == 0

    def test_cooldown_wait_with_rate_limit_cause_keeps_legacy_label(
            self, monkeypatch, key_stats_recorders):
        from lib.llm_dispatch import api

        good = _make_slot(key='k9')
        disp = _FakeDispatcher([None, good], causes={'rate_limit'})
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        def _fake_stream(body, **kwargs):
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        retries = []
        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            on_retry=lambda **kw: retries.append(kw))

        assert msg == 'ok'
        assert retries[0].get('reason') == 'Waiting for model (rate-limited)'
        assert retries[0].get('status_code') == 429


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
