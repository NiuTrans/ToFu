#!/usr/bin/env python3
"""tests/test_key_stats_no_429_auto_disable.py — owner policy 2026-07-29:

    连续 429 永不再自动禁用 key;只有「没钱」(HTTP 402 / quota-exhausted)
    才自动禁用。

The 2026-07-29 incident this policy answers: an upstream-vendor outage on the
sankuai anthropic face wrapped itself in 429s/403s for ~90 minutes. The
consecutive-429 streak heuristic read that storm as "key is dead" and
auto-exhausted ALL THREE Opus-5 keys for the day — a transient upstream
incident became a total model outage that needed a human to hand-re-enable
keys at 22:20. Meanwhile the system behaved exactly as designed: the design
itself was wrong. 429 means "backpressure, slow down", never "this key is
dead"; the only honest kill signal is the provider saying so (402/quota).

Pinned here (pairs — each REMOVED behavior next to the KEPT behavior it
must not take down with it):

  1. record_rate_limit × 500 → the key is NEVER exhausted and stays
     enabled — while the counters (rate_limited / consecutive_429) keep
     counting for the UI, and a prior real last_error is not clobbered.
  2. A key fresh off a 429 storm is still picked by the dispatcher
     (picker-level, the seam the incident actually broke).
  3. Slot.record_error(is_rate_limit=True) cooldown stays the brief
     steering cooldown ('rate_limit', ~0.5s) no matter how long the
     streak runs — it must never escalate to the hourly 'quota' park.
  4. The KEPT half: a genuine billing-stop (mark_key_exhausted, the 402
     path) still disables the key today. Removing the 429 heuristic must
     not neuter the money signal.
  5. Structural: no auto-exhaust code path survives anywhere
     (dispatcher probes, retry-reason token, stats writer, i18n label).
     Comment-stripped scans per charter #24 — a comment may neither
     satisfy nor violate these.

Run:  pytest tests/test_key_stats_no_429_auto_disable.py -m unit
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

PROV = 'gw429'
KEY = 'gw429_key_0'
PK = f'{PROV}::{KEY}'


@pytest.fixture
def fresh_stats(monkeypatch, tmp_path):
    """Isolate lib.key_stats onto a tmp stats file + known siblings."""
    import lib.key_stats as ks

    snapshot = {
        'day': ks._cache['day'],
        'stats': ks._cache['stats'],
        'overrides': ks._cache['overrides'],
        'loaded': ks._cache['loaded'],
    }
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    monkeypatch.setattr(ks, '_list_siblings', lambda pid: [PK])
    ks._cache['day'] = ''
    ks._cache['stats'] = {}
    ks._cache['overrides'] = {}
    ks._cache['loaded'] = False
    yield ks
    ks._cache['day'] = snapshot['day']
    ks._cache['stats'] = snapshot['stats']
    ks._cache['overrides'] = snapshot['overrides']
    ks._cache['loaded'] = snapshot['loaded']


@pytest.mark.unit
class TestConsecutive429NeverDisables:

    def test_500_streak_never_exhausts_and_stays_enabled(self, fresh_stats):
        """The core of the new policy: 429 is backpressure, not death.

        500 in a row — five times the old heuristic's kill threshold — and
        the key must be exactly as dispatchable as before the storm."""
        ks = fresh_stats
        for _ in range(500):
            ks.record_rate_limit(PROV, KEY, reason='HTTP 429 slow down')

        row = ks.get_today_stats(PROV, KEY)
        assert row['exhausted'] is False, (
            'a 429 streak must never flip the exhausted flag — only a '
            'billing-stop (402) may do that (owner policy 2026-07-29)')
        assert ks.is_key_enabled(PROV, KEY) is True
        assert ks.is_key_enabled(PROV, KEY, model='claude-opus-5') is True
        # The counters are the UI's telemetry — they keep counting.
        assert row['rate_limited'] == 500
        assert row['consecutive_429'] == 500

    def test_streak_does_not_clobber_real_last_error(self, fresh_stats):
        """A 429 body is ambiguous ('slow down' vs 'dead key') — it must
        never overwrite the last REAL failure the UI shows the user."""
        ks = fresh_stats
        ks.record_outcome(PROV, KEY, success=False,
                          error='HTTP 500 real upstream boom')
        for _ in range(150):
            ks.record_rate_limit(PROV, KEY, reason='HTTP 429 ambiguous')
        assert ks.get_today_stats(PROV, KEY)['last_error'] == \
            'HTTP 500 real upstream boom'

    def test_key_still_picked_after_storm(self, fresh_stats, monkeypatch):
        """Picker-level: the incident's actual failure was the dispatcher
        filtering the stormed key out of every conversation for the day.

        A HEALTHY SIBLING is required in this fixture: with a single
        configured key the last-resort guard would keep even a streak-killed
        key enabled and this test would pass for the wrong reason. With a
        healthy sibling, last-resort stays out and only the policy itself
        keeps the stormed key dispatchable."""
        from lib.llm_dispatch.dispatcher import LLMDispatcher
        from lib.llm_dispatch.slot import Slot

        ks = fresh_stats
        sibling_pk = f'{PROV}::gw429_key_1'
        monkeypatch.setattr(ks, '_list_siblings', lambda pid: [PK, sibling_pk])
        ks.record_outcome(PROV, 'gw429_key_1', success=True)
        for _ in range(200):
            ks.record_rate_limit(PROV, KEY, reason='HTTP 429')

        disp = object.__new__(LLMDispatcher)
        disp._lock = threading.Lock()
        disp.slots = [Slot(key_name=KEY, api_key='sk-test',
                           model='claude-opus-5', capabilities={'text'},
                           provider_id=PROV)]
        disp.initialize = lambda: None
        picked = disp._pick('text', None, None, None)
        assert picked is not None, (
            'a key coming off a 429 storm must stay dispatchable — the '
            '2026-07-29 total-outage was this filter firing on a transient '
            'upstream incident')

    def test_slot_429_cooldown_never_escalates_to_quota_park(self,
                                                             fresh_stats):
        """Slot-level: the 0.5s steering cooldown is the whole answer to a
        429 — never the 3600s 'quota' park the streak used to trigger."""
        from lib.llm_dispatch.slot import Slot
        fresh_stats  # key_stats isolated on tmp file

        slot = Slot(key_name=KEY, api_key='sk-test', model='claude-opus-5',
                    capabilities={'text'}, provider_id=PROV)
        for _ in range(200):
            slot.record_error(is_rate_limit=True, error='HTTP 429')
            assert slot.cooldown_reason == 'rate_limit', (
                f'after a long 429 streak the cooldown reason must stay '
                f'"rate_limit", got {slot.cooldown_reason!r}')
            remaining = slot.cooldown_until - time.time()
            assert remaining < 2.0, (
                f'429 cooldown must stay a brief steering nudge, got '
                f'{remaining:.0f}s — the hourly quota park is reserved for '
                f'genuine billing-stops')

    def test_billing_stop_still_disables(self, fresh_stats):
        """The KEPT half of the policy: 402/quota still kills the key for
        the day. Removing the 429 heuristic must not soften the money
        signal — they are different error classes on purpose."""
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='您的Credit已耗尽',
                              model='claude-opus-5')
        assert ks.is_key_enabled(PROV, KEY, model='claude-opus-5') is False
        # …and a sibling model on the same key stays up (aggregating-gateway
        # isolation — unchanged behavior, pinned next door already).
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True


@pytest.mark.unit
class TestNoAutoExhaustPathSurvives:
    """Structural guards (charter #24): stripped-source scans, both
    directions — the removed mechanism must be GONE, and these scans must
    not be satisfiable by comments."""

    def _stripped(self, relpath, lang):
        from tests._source_scan import strip_comments
        with open(os.path.join(ROOT, relpath), encoding='utf-8') as f:
            return strip_comments(f.read(), lang=lang)

    def test_record_rate_limit_has_no_exhaust_write(self):
        """The stats writer itself: no code path inside record_rate_limit
        may flip the exhausted flag."""
        import lib.key_stats as ks
        from tests._source_scan import strip_comments
        src = strip_comments(inspect.getsource(ks.record_rate_limit),
                             lang='python')
        assert "'exhausted'] = True" not in src, (
            'record_rate_limit must never write the exhausted flag — the '
            'consecutive-429 kill switch is removed by policy')

    def test_dispatcher_has_no_429_exclusion_probe(self):
        """Both dispatch loops (stream + non-stream) had a post-429 probe
        that excluded the freshly-exhausted key mid-loop. Gone."""
        src = self._stripped('lib/llm_dispatch/api.py', 'python')
        assert 'note_auto_exhausted_key' not in src
        assert 'auto-exhausted after' not in src

    def test_retry_reason_token_gone(self):
        """The HUD token 'Key auto-exhausted (consecutive 429s)' has no
        producer anymore — its i18n mapping and label must not linger."""
        src = self._stripped('lib/llm_dispatch/retry_i18n.py', 'python')
        assert 'keyAutoExhausted' not in src
        js = self._stripped('static/js/i18n.js', 'js')
        assert 'keyAutoExhausted' not in js

    def test_threshold_constant_gone(self):
        """MAX_CONSECUTIVE_429 was the kill threshold — with the policy
        gone, the constant and its UI plumbing must not survive to imply
        a limit that no longer exists."""
        for relpath in ('lib/key_stats/_state.py', 'lib/key_stats/_record.py',
                        'lib/key_stats/_query.py', 'lib/key_stats/__init__.py',
                        'lib/dispatch_stats.py'):
            src = self._stripped(relpath, 'python')
            assert 'MAX_CONSECUTIVE_429' not in src, (
                f'{relpath} still references the removed kill threshold')
        js = self._stripped('static/js/settings/key_stats.js', 'js')
        assert 'max_consecutive_429' not in js


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
