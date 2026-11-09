#!/usr/bin/env python3
"""tests/llm_dispatch/test_billing_stop_no_last_resort.py — a recorded billing
stop is never re-chosen by the provider "last-resort" guard.

Observed 2026-11-06 during the forced-operation edit benchmark
(lane 4cbf7e4a): most DeepSeek-V4-Pro credentials answered HTTP 402 "Credit
exhausted" and the rest were rate-limited, yet Tofu spent one live 402 on an
already-exhausted credential on EVERY subsequent request (121 distinct
ephemeral slot ids in ~12 minutes).

Mechanism being pinned:

* ``Slot.record_error`` parks the slot for an hour and records a durable
  key-wide (HTTP 402) or per-model (429 + insufficient_quota) billing stop in
  ``lib.key_stats``.
* The next request mints a fresh request-scoped route group, so the slot-local
  cooldown is gone; only the durable stop survives.
* ``lib.key_stats._enable._pick_last_resort_unlocked`` then promoted ONE
  exhausted key back to enabled whenever every sibling under the Provider
  namespace was raw-disabled, so the dispatcher's ``_KeyAdmission`` admitted it
  again — one provider request per turn, forever, on a key the provider has
  already told us is out of credit.

Contract after the fix (no new knob; AGENTS.md principle 5):

  1. A key-wide or matching per-model billing stop is never last-resort
     promoted, whether or not a manual override exists.
  2. A credential-health (success-rate) disable keeps its historical
     last-resort promotion — a sick key is not a dead balance, and the owner
     ruling that an error surfaces only when every key is unavailable stands.
  3. A manual ON override still wins ("I topped up"), including over a stop
     recorded afterwards.
  4. Provider-defined recovery still works untouched: the subscription reset
     time, day rollover and owner re-enable are covered by the neighbouring
     suites, and a key with NO recorded stop is always pickable.

Run:  pytest tests/llm_dispatch/test_billing_stop_no_last_resort.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'owner:1:billing-stop-test'
KEY = 'cred_dead'
OTHER = 'cred_also_dead'
PK = f'{PROV}::{KEY}'


@pytest.fixture
def stats(monkeypatch, tmp_path):
    """Real lib.key_stats on an isolated store; both keys are its siblings."""
    import lib.key_stats as ks

    snapshot = dict(ks._cache)
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    monkeypatch.setattr(ks, '_list_siblings',
                        lambda pid: [PK, f'{PROV}::{OTHER}'])
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    yield ks
    ks._cache.clear()
    ks._cache.update(snapshot)


def test_keywide_billing_stop_is_not_last_resort_promoted(stats):
    """Every sibling billing-dead ⇒ NO key is promoted back to enabled."""
    ks = stats
    ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 Credit exhausted')
    ks.mark_key_exhausted(PROV, OTHER, reason='HTTP 402 Credit exhausted')

    assert ks.is_key_enabled(PROV, KEY, model='deepseek-v4-pro') is False
    assert ks.is_key_enabled(PROV, OTHER, model='deepseek-v4-pro') is False
    assert ks.get_today_stats(PROV, KEY)['last_resort'] is False
    assert ks.get_today_stats(PROV, KEY)['enabled'] is False


def test_per_model_billing_stop_is_not_promoted_for_that_model(stats):
    """A 429 insufficient_quota stop stays scoped to its model even when the
    whole Provider is otherwise billing-dead; sibling models stay free."""
    ks = stats
    ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota',
                          model='deepseek-v4-pro')
    ks.mark_key_exhausted(PROV, OTHER, reason='insufficient_quota',
                          model='deepseek-v4-pro')

    assert ks.is_key_enabled(PROV, KEY, model='deepseek-v4-pro') is False
    assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True, (
        'a per-model stop must not poison a sibling model on the same key')


def test_credential_health_disable_keeps_last_resort(stats):
    """Unchanged: a success-rate disable is not a billing stop, so the sole
    remaining key of a Provider is still promoted as last resort."""
    ks = stats
    for _ in range(ks.MIN_ATTEMPTS):
        ks.record_outcome(PROV, KEY, success=False,
                          error='HTTP 401 invalid_api_key', credential_failure=True)
    for _ in range(ks.MIN_ATTEMPTS):
        ks.record_outcome(PROV, OTHER, success=False,
                          error='HTTP 401 invalid_api_key', credential_failure=True)

    enabled = [ks.is_key_enabled(PROV, KEY), ks.is_key_enabled(PROV, OTHER)]
    assert sorted(enabled) == [False, True], (
        'exactly one credential-health-disabled key stays as last resort')
    rows = [ks.get_today_stats(PROV, KEY), ks.get_today_stats(PROV, OTHER)]
    assert sorted(row['last_resort'] for row in rows) == [False, True]


def test_manual_override_still_recovers_a_stopped_key(stats):
    """Owner action ("I topped up") is the intended way back, not a retry."""
    ks = stats
    ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 Credit exhausted')
    ks.mark_key_exhausted(PROV, OTHER, reason='HTTP 402 Credit exhausted')

    assert ks.set_key_override(PROV, KEY, True)['enabled'] is True
    assert ks.is_key_enabled(PROV, KEY, model='deepseek-v4-pro') is True
    # A stop recorded AFTER the manual ON is still user-supremacy overridden.
    ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 Credit exhausted again')
    assert ks.is_key_enabled(PROV, KEY, model='deepseek-v4-pro') is True
    # …and the other, never re-enabled key stays refused.
    assert ks.is_key_enabled(PROV, OTHER, model='deepseek-v4-pro') is False


def test_day_rollover_recovers_a_billing_stop(stats, monkeypatch):
    """Provider-defined recovery: a new day clears the recorded stop."""
    ks = stats
    ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 Credit exhausted')
    ks.mark_key_exhausted(PROV, OTHER, reason='HTTP 402 Credit exhausted')
    assert ks.is_key_enabled(PROV, KEY) is False

    monkeypatch.setattr(ks, '_today', lambda: '2099-01-02')
    assert ks.is_key_enabled(PROV, KEY, model='deepseek-v4-pro') is True


def _dispatcher(slots):
    """Real picker over a real SlotPool, so the key-health credential index
    the selection batch reads is the one the pool maintains."""
    import threading
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.slot_pool import SlotPool

    disp = object.__new__(LLMDispatcher)
    disp._lock = threading.Lock()
    disp._pool = SlotPool()
    disp._pool.publish(list(slots))
    disp._contention_strikes = {}
    disp._logical_index = {}
    disp._direct_models = set()
    disp.initialize = lambda: None
    return disp


def _slot(model='deepseek-v4-pro', key=KEY):
    from lib.llm_dispatch.slot import Slot

    return Slot(key_name=key, api_key='synthetic-key', model=model,
                capabilities={'text'}, provider_id=PROV,
                routing_owner_user_id=1,
                routing_provider_id=PROV.split(':', 2)[2],
                route_credential_id=key)


def test_picker_does_not_choose_a_billing_dead_credential(stats):
    """The observed failure at the real selection seam: with every credential
    of the model billing-stopped, no slot is picked and no 402 is spent."""
    ks = stats
    ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 Credit exhausted')
    ks.mark_key_exhausted(PROV, OTHER, reason='HTTP 402 Credit exhausted')
    disp = _dispatcher([_slot(key=KEY), _slot(key=OTHER)])

    assert disp._pick('text', 'deepseek-v4-pro', None, None,
                      strict_model=True) is None


def test_all_billing_dead_pool_is_not_healable_capacity(stats):
    """A retry loop must not poll a pool that only policy can unblock."""
    ks = stats
    ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 Credit exhausted')
    ks.mark_key_exhausted(PROV, OTHER, reason='HTTP 402 Credit exhausted')
    disp = _dispatcher([_slot(key=KEY), _slot(key=OTHER)])

    from lib.llm_dispatch.provider_pin import provider_pin
    with provider_pin(PROV):
        assert disp.has_capable_slots(
            'text', prefer_model='deepseek-v4-pro') is False


def test_billing_dead_pool_surfaces_the_quota_error_without_a_new_request(
        stats, monkeypatch):
    """End-to-end: the caller gets the recorded billing failure, not an
    endless cooldown cycle and not another live 402."""
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch import api

    ks = stats
    ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 Credit exhausted')
    disp = _dispatcher([_slot(key=KEY)])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
    monkeypatch.setattr(api, '_audit_severity_downgrade', lambda: None)
    sleeps = []
    monkeypatch.setattr(api.time, 'sleep', lambda seconds: sleeps.append(seconds))

    def must_not_send(*args, **kwargs):
        raise AssertionError('a billing-stopped credential was dispatched again')

    monkeypatch.setattr('lib.llm.chat', must_not_send)
    from lib.llm_dispatch.provider_pin import provider_pin
    with provider_pin(PROV):
        with pytest.raises((RateLimitError, RuntimeError)) as excinfo:
            api.dispatch_chat([{'role': 'user', 'content': 'synthetic'}],
                              capability='text', max_retries=2)
    assert 'no upstream request was sent' in str(excinfo.value) or isinstance(
        excinfo.value, RateLimitError)
    assert sleeps == [], 'a policy-refused pool must not be polled as cooldown'
