#!/usr/bin/env python3
"""Per-model runtime health — aggregation + cooldown export + nested pricing.

WHY
---
The Settings model cards now show the dispatcher's error-rate throttling
directly: success rate, consecutive-error streaks, and ACTIVE cooldowns
(``cooldown_until`` / ``cooldown_reason`` in lib/llm_dispatch/slot.py).
That state is per-SLOT; the card needs a per-(provider, wire-model) fold
(``lib/dispatch_stats.aggregate_model_health``) served by
``GET /api/v1/dispatch/model-health``.

Two failure modes this file pins:
  1. The fold is only as good as the export — if ``get_slots_info`` stops
     carrying the cooldown fields, every card silently shows "healthy"
     while the dispatcher is actively throttling. (NEUTER 1 verifies the
     chain goes red when the export is dropped.)
  2. The pricing-tier evaluator must read the SAME nested ``pricing`` dict
     the edit dialog writes, or a user-set price never updates the 'cheap'
     tag even though cost accounting honors it. (NEUTER 3.)

WHAT IS GUARDED (results, not implementation — charter 2026-07-27)
------------------------------------------------------------------
  * aggregate_model_health folds totals / success rate / max streak /
    max-remaining cooldown + its reason per (provider, wire model).
  * Expired cooldowns and is_available=False are handled correctly.
  * success_rate is None until >= 3 lifetime requests (noise floor).
  * get_slots_info exports cooldown_until + cooldown_reason from a REAL
    Slot in a REAL Dispatcher (the exact chain the route consumes).
  * reevaluate_pricing_tags honors the nested m['pricing'] override both
    ways (add + remove), and top-level input_price still wins.

NEUTERS (verified by hand at authoring time; shipped files restored):
  * NEUTER 1: delete the two cooldown lines from get_slots_info →
    test_slots_info_exports_cooldown_state +
    test_end_to_end_chain_carries_cooldown go red.
  * NEUTER 2: drop the cooldown merge in aggregate_model_health →
    test_cooldown_* go red.
  * NEUTER 3: revert reevaluate_pricing_tags to m.get('input_price') only →
    test_nested_pricing_* go red.
"""

from __future__ import annotations

import time

import pytest

from lib.dispatch_stats import aggregate_model_health
from lib.llm_dispatch.config._pricing import reevaluate_pricing_tags
from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.slot import Slot

pytestmark = pytest.mark.unit

NOW = time.time()


def _slot_dict(**kw):
    base = {
        'provider_id': 'prov1', 'model': 'wire-a',
        'total_requests': 0, 'total_errors': 0, 'consecutive_errors': 0,
        'inflight': 0, 'available': True,
        'cooldown_until': 0.0, 'cooldown_reason': '',
        'last_error_time': 0.0, 'last_error_msg': '',
    }
    base.update(kw)
    return base


# ══════════════════════════════════════════════════════════════════════
#  aggregate_model_health
# ══════════════════════════════════════════════════════════════════════

def test_fold_totals_and_success_rate():
    out = aggregate_model_health([
        _slot_dict(total_requests=10, total_errors=1),
        _slot_dict(total_requests=5, total_errors=4),
    ])
    e = out['providers']['prov1']['wire-a']
    assert e['slots'] == 2
    assert e['total_requests'] == 15
    assert e['total_errors'] == 5
    assert e['success_rate'] == pytest.approx(round(1 - 5 / 15, 3))


def test_success_rate_none_below_noise_floor():
    out = aggregate_model_health([_slot_dict(total_requests=2, total_errors=0)])
    assert out['providers']['prov1']['wire-a']['success_rate'] is None


def test_providers_and_models_keyed_separately():
    out = aggregate_model_health([
        _slot_dict(provider_id='p1', model='a', total_requests=5),
        _slot_dict(provider_id='p2', model='a', total_requests=7),
        _slot_dict(provider_id='p1', model='b', total_requests=3),
    ])
    assert set(out['providers']) == {'p1', 'p2'}
    assert set(out['providers']['p1']) == {'a', 'b'}
    assert out['providers']['p2']['a']['total_requests'] == 7


def test_cooldown_max_remaining_and_reason():
    out = aggregate_model_health([
        _slot_dict(cooldown_until=NOW + 12, cooldown_reason='rate_limit'),
        _slot_dict(cooldown_until=NOW + 44, cooldown_reason='error',
                   consecutive_errors=3, last_error_time=NOW - 1,
                   last_error_msg='boom'),
    ])
    e = out['providers']['prov1']['wire-a']
    assert e['cooldown_remaining_s'] > 40
    assert e['cooldown_reason'] == 'error'
    assert e['consecutive_errors'] == 3
    assert e['last_error_msg'] == 'boom'
    # Neither slot is currently usable: both cooling.
    assert e['available_slots'] == 0


def test_expired_cooldown_not_counted():
    out = aggregate_model_health([
        _slot_dict(cooldown_until=NOW - 5, cooldown_reason='error'),
    ])
    e = out['providers']['prov1']['wire-a']
    assert e['cooldown_remaining_s'] == 0
    assert e['available_slots'] == 1


def test_unavailable_slot_not_available_even_without_cooldown():
    out = aggregate_model_health([_slot_dict(available=False)])
    e = out['providers']['prov1']['wire-a']
    assert e['available_slots'] == 0
    assert e['slots'] == 1


def test_missing_cooldown_keys_degrade_to_zero():
    """Robustness: slot dicts lacking the cooldown keys (the pre-feature
    export shape) must not crash the fold — they just report no cooldown."""
    s = _slot_dict()
    del s['cooldown_until']
    del s['cooldown_reason']
    out = aggregate_model_health([s])
    e = out['providers']['prov1']['wire-a']
    assert e['cooldown_remaining_s'] == 0
    assert e['cooldown_reason'] == ''


# ══════════════════════════════════════════════════════════════════════
#  get_slots_info cooldown export (the chain's source)
# ══════════════════════════════════════════════════════════════════════

def _dispatcher_with(slots):
    d = LLMDispatcher()
    d._initialized = True          # initialize() returns early
    d.slots = slots
    return d


def test_slots_info_exports_cooldown_state():
    s = Slot(key_name='key_0', api_key='k', model='wire-x',
             capabilities={'text'}, provider_id='prov1')
    s.cooldown_until = time.time() + 60
    s.cooldown_reason = 'upstream'
    info = _dispatcher_with([s]).get_slots_info()
    assert len(info) == 1
    assert info[0]['cooldown_reason'] == 'upstream'
    assert info[0]['cooldown_until'] > time.time() + 50


def test_end_to_end_chain_carries_cooldown():
    """The exact route chain: real Slots → get_slots_info → aggregate."""
    hot = Slot(key_name='key_0', api_key='k', model='wire-x',
               capabilities={'text'}, provider_id='prov1')
    hot.cooldown_until = time.time() + 30
    hot.cooldown_reason = 'error'
    cold = Slot(key_name='key_1', api_key='k', model='wire-x',
                capabilities={'text'}, provider_id='prov1')
    slots_info = _dispatcher_with([hot, cold]).get_slots_info()
    e = aggregate_model_health(slots_info)['providers']['prov1']['wire-x']
    assert e['cooldown_remaining_s'] > 25
    assert e['cooldown_reason'] == 'error'
    assert e['available_slots'] == 1


# ══════════════════════════════════════════════════════════════════════
#  reevaluate_pricing_tags reads the nested pricing override
# ══════════════════════════════════════════════════════════════════════

def test_nested_pricing_earns_cheap_tag():
    models = [{'model_id': 'm1', 'capabilities': ['text'], 'cost': 50.0,
               'pricing': {'input': 0.5, 'output': 2.0}}]
    reevaluate_pricing_tags(models)
    assert 'cheap' in models[0]['capabilities']


def test_nested_pricing_strips_stale_cheap_tag():
    # A cheap blended `cost` would keep the tag alive via the fallback —
    # the nested REAL prices must win and strip it.
    models = [{'model_id': 'm2', 'capabilities': ['text', 'cheap'], 'cost': 0.001,
               'pricing': {'input': 100.0, 'output': 200.0}}]
    reevaluate_pricing_tags(models)
    assert 'cheap' not in models[0]['capabilities']


def test_top_level_input_price_still_wins_over_nested():
    models = [{'model_id': 'm3', 'capabilities': ['text'],
               'input_price': 0.5, 'output_price': 2.0,
               'pricing': {'input': 100.0, 'output': 200.0}}]
    reevaluate_pricing_tags(models)
    assert 'cheap' in models[0]['capabilities']


def test_non_chat_models_never_get_tier_tags():
    models = [{'model_id': 'm4', 'capabilities': ['embedding'],
               'pricing': {'input': 0.1, 'output': 0.1}}]
    reevaluate_pricing_tags(models)
    assert models[0]['capabilities'] == ['embedding']


# ══════════════════════════════════════════════════════════════════════
#  Route shape — /api/v1/dispatch/model-health over the real app
# ══════════════════════════════════════════════════════════════════════

def test_route_serves_health_envelope(flask_client, monkeypatch):
    """The endpoint must return the providers envelope and never 5xx when
    the dispatcher is unavailable (a cold boot has no slot pool yet)."""
    class _StubDispatcher:
        def get_slots_info(self):
            return [{
                'key': 'key_0', 'model': 'wire-x', 'provider_id': 'prov1',
                'total_requests': 10, 'total_errors': 2, 'consecutive_errors': 3,
                'inflight': 1, 'available': True,
                'cooldown_until': time.time() + 33, 'cooldown_reason': 'error',
                'last_error_time': time.time(), 'last_error_msg': 'boom',
            }]

    monkeypatch.setattr('lib.llm_dispatch.get_dispatcher',
                        lambda: _StubDispatcher())
    resp = flask_client.get('/api/v1/dispatch/model-health')
    assert resp.status_code == 200
    body = resp.get_json()
    e = body['providers']['prov1']['wire-x']
    assert e['success_rate'] == pytest.approx(0.8)
    assert e['cooldown_remaining_s'] > 30
    assert e['cooldown_reason'] == 'error'
    assert e['available_slots'] == 0

    def _boom():
        raise RuntimeError('no dispatcher yet')
    monkeypatch.setattr('lib.llm_dispatch.get_dispatcher', _boom)
    resp2 = flask_client.get('/api/v1/dispatch/model-health')
    assert resp2.status_code == 200
    assert resp2.get_json()['providers'] == {}


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
