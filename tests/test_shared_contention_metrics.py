#!/usr/bin/env python3
"""tests/test_shared_contention_metrics.py — shared-project 429 contention
must not pollute model health metrics (epic pt_47594accfe654410).

The incident (2026-07-28): the sankuai gateway's shared Moonshot project
(50M TPM) was saturated by OTHER tenants (local traffic measured ~2M/min
≈ 4% of the pipe). 782 contention 429s in ~80 minutes were each recorded
as a model error, crushing the kimi-k3 card's success rate to 24% — while
the genuine outcome rate was 9,122 successes / 116 real failures. The same
429s also fed the consecutive-429 auto-exhaust streak, one step from
disabling HEALTHY keys for the day.

Pinned here:

  1. The narrow classifier: a 429 body naming a project-level TPM limit
     (``reached project`` + ``tpm rate limit``), or an app/model-level RPM
     limit (``App:`` + model scope + the explicit per-minute request limit)
     → is_shared_contention. Generic 429s and quota 429s are NOT laundered
     into contention (quota precedence is asserted with matching bodies).
  2. Slot accounting: contention increments ``contention_errors`` (not
     ``total_errors``) and compensates the attempt out of
     ``total_requests`` — the success-rate column reflects genuine
     outcomes only; slot-local error scoring/cooldown remains untouched so a
     warm prompt-cache key is not displaced by project-wide contention.
  3. Contention feeds NEITHER key_stats path (no consecutive-429 streak,
     no failure stats) — the dead-key safety nets stay reserved for
     genuine key health.
  4. Plain 429 behaviour is UNCHANGED (still feeds the streak + error
     count) — fixing the metric must not launder real per-key throttling.
  5. The model-health fold carries contention_errors to the card.

Run:  pytest tests/test_shared_contention_metrics.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

MOONSHOT_BODY = (
    'API HTTP 429: Your account org-ad4041fa / proj-330e2da '
    '<ak-etdrwtdnxeh111d51wz1> request reached project (kimi-k3) TPM rate '
    'limit, current: 50019215, limit: 50000000, see '
    'https://platform.moonshot.cn/docs/pricing/limits'
)

APP_MODEL_RPM_BODY = (
    'API HTTP 429: {"status":429,"message":"App:**62518在模型:kimi-k3'
    '每分钟请求次数超过限制","data":null,"ext":{"error":{"source":"AIGC",'
    '"service":"aigc","stage":"validation"}}}'
)


def _classify(err_msg):
    from lib.llm_errors import _classify_http_error, RateLimitError
    with pytest.raises(RateLimitError) as ei:
        _classify_http_error(429, err_msg, 'kimi-k3', '[t]')
    return ei.value


@pytest.mark.unit
class TestContentionClassification:

    def test_project_level_body_is_contention(self):
        e = _classify(MOONSHOT_BODY)
        assert e.is_shared_contention is True
        assert e.is_quota is False
        assert e.is_gateway is False

    def test_app_model_rpm_body_is_contention(self):
        e = _classify(APP_MODEL_RPM_BODY)
        assert e.is_shared_contention is True
        assert e.is_quota is False
        assert e.is_gateway is False

    def test_generic_429_is_not_contention(self):
        e = _classify('API HTTP 429: rate limit exceeded, slow down')
        assert e.is_shared_contention is False
        assert e.is_quota is False

    def test_quota_precedence_over_contention(self):
        """A body matching BOTH pattern families must stay a billing-stop —
        the narrow contention match must never launder a quota error."""
        e = _classify(MOONSHOT_BODY + ' insufficient_quota')
        assert e.is_quota is True
        assert e.is_shared_contention is False

        app_error = _classify(APP_MODEL_RPM_BODY + ' insufficient_quota')
        assert app_error.is_quota is True
        assert app_error.is_shared_contention is False

    def test_single_pattern_alone_is_not_contention(self):
        """Narrowness: ONE of the two phrases is not enough."""
        e = _classify('API HTTP 429: request reached project quota')
        assert e.is_shared_contention is False
        e2 = _classify('API HTTP 429: account TPM rate limit hit')
        assert e2.is_shared_contention is False

    @pytest.mark.parametrize('body', [
        'API HTTP 429: 在模型:kimi-k3每分钟请求次数超过限制',
        'API HTTP 429: App:**62518每分钟请求次数超过限制',
        'API HTTP 429: App:**62518在模型:kimi-k3请求过快',
    ])
    def test_app_model_limit_requires_all_scope_markers(self, body):
        assert _classify(body).is_shared_contention is False


def _make_slot():
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name='k0', api_key='sk-test', model='kimi-k3',
                capabilities={'text'})


@pytest.fixture
def key_stats_recorders(monkeypatch):
    rec = {'rate_limit': [], 'outcome': [], 'exhausted': []}
    monkeypatch.setattr(
        'lib.key_stats.record_rate_limit',
        lambda p, k, reason='': rec['rate_limit'].append((p, k)) or False)
    monkeypatch.setattr(
        'lib.key_stats.record_outcome',
        lambda p, k, success, error='': rec['outcome'].append((p, k, success)))
    monkeypatch.setattr(
        'lib.key_stats.mark_key_exhausted',
        lambda p, k, reason='', model='': rec['exhausted'].append((p, k)))
    return rec


@pytest.mark.unit
class TestContentionAccounting:

    def test_contention_is_not_a_health_error(self, key_stats_recorders):
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, is_shared_contention=True,
                          error='reached project TPM rate limit')
        assert slot.contention_errors == 1
        assert slot.total_errors == 0, (
            'contention must stay OUT of the model health success-rate')
        assert slot.total_requests == 0, (
            'the contention attempt is compensated out of total_requests — '
            'it is neither a success nor a failure of THIS key')
        assert slot.consecutive_errors == 0, (
            'shared contention must not penalize this key in slot scoring')
        assert slot.cooldown_until == 0
        assert slot.cooldown_reason == ''
        assert key_stats_recorders['rate_limit'] == [], (
            'contention must NOT feed the consecutive-429 auto-exhaust '
            'streak — a saturated shared project must not disable a '
            'healthy key for the day')
        assert key_stats_recorders['outcome'] == []
        assert key_stats_recorders['exhausted'] == []

    def test_success_rate_reflects_genuine_outcomes_only(
            self, key_stats_recorders):
        """The incident's shape: 9 contention 429s + 1 real error + 2
        successes → the card must show 67%, not 18%. (3+ genuine attempts
        so the cold-start "assume good" convention doesn't kick in.)"""
        slot = _make_slot()
        for _ in range(9):
            slot.record_request()
            slot.record_error(is_rate_limit=True, is_shared_contention=True)
        slot.record_request()
        slot.record_error(is_rate_limit=False, error='boom')
        for _ in range(2):
            slot.record_request()
            slot.record_success(100)
        assert slot.contention_errors == 9
        assert slot.total_requests == 3
        assert slot.total_errors == 1
        assert abs(slot.success_rate - (1 - 1 / 3)) < 1e-9

    def test_plain_429_accounting_unchanged(self, key_stats_recorders):
        """Complement: the fix must NOT launder real per-key throttling."""
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, error='HTTP 429 generic')
        assert slot.contention_errors == 0
        assert slot.total_errors == 1
        assert slot.total_requests == 1
        assert len(key_stats_recorders['rate_limit']) == 1, (
            'a genuine per-key 429 still feeds the consecutive-429 streak')


@pytest.mark.unit
class TestModelHealthFold:

    def test_fold_carries_contention(self):
        from lib.dispatch_stats import aggregate_model_health
        out = aggregate_model_health([
            {'provider_id': 'sankuai', 'model': 'kimi-k3',
             'total_requests': 2, 'total_errors': 1, 'contention_errors': 9},
            {'provider_id': 'sankuai', 'model': 'kimi-k3',
             'total_requests': 3, 'total_errors': 0, 'contention_errors': 5},
        ])
        row = out['providers']['sankuai']['kimi-k3']
        assert row['contention_errors'] == 14
        assert row['total_errors'] == 1
        assert row['success_rate'] == 0.8


@pytest.mark.unit
class TestDispatchLoopIntegration:

    def test_contention_429_then_success_feeds_nothing(
            self, monkeypatch, key_stats_recorders):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_vendor_transient_dispatch import (
            _FakeDispatcher, _make_slot)

        slot1 = _make_slot(model='kimi-k3', key='k1')
        slot2 = _make_slot(model='kimi-k3', key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
        monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                            lambda *_a, **_k: None)

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
        assert calls['n'] == 2
        assert slot1.contention_errors == 1
        assert slot1.total_errors == 0
        assert key_stats_recorders['rate_limit'] == []
        failures = [c for c in key_stats_recorders['outcome'] if not c[2]]
        assert failures == [], (
            'external contention must not land in the daily failure stats '
            '(the k2 success is legitimate and unrelated)')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
