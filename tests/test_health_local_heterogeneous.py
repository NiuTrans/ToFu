#!/usr/bin/env python3
"""tests/test_health_local_heterogeneous.py — health checker on heterogeneous fleets.

The local-endpoint health checker used to rebuild the model list from
``live_endpoints[0]`` ONLY and filter it by the union of served sets
(health_local.py). On a heterogeneous fleet (box A serves qwen, box B serves
llama) that dropped every model not on the first box — the model picker
flapped on every periodic resync — and a temporarily-down box lost its
private models outright.

Owner-ratified semantics (2026-07-25):

  * Discovery runs PER live endpoint; each model's metadata comes from the
    endpoint that actually serves it.
  * The served-model placement is persisted as ``provider.endpoint_models``
    ({normalized_url: [root_ids…]}) — the dispatcher reads it to place slots.
  * A DOWN endpoint keeps its previous binding and its models (transient
    restart must not wipe the picker).
  * No drift → NO rewrite (no slot-pool rebuild churn).
  * ``_check_endpoint`` on a bare-origin URL 404 retries under /v1 and
    reports the effective URL (ollama habit), so binding keys match the
    dispatcher's normalized endpoints.

FAILING-FIRST proven: the two fleet tests are RED pre-fix (llama dropped),
GREEN post-fix.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

A = 'http://10.0.0.5:8000/v1'
B = 'http://10.0.0.6:8000/v1'


def _model(mid):
    return {'model_id': mid, 'aliases': [], 'capabilities': ['text'],
            'rpm': 30, 'cost': 0.0, 'thinking_default': False}


def _provider(binding=None):
    p = {
        'id': 'local_test',
        'brand': 'local',
        'enabled': True,
        'endpoints': [A, B],
        'base_url': A,
        'api_keys': [],
        'models': [_model('qwen'), _model('llama')],
    }
    if binding:
        p['endpoint_models'] = binding
    return p


def _wire(monkeypatch, tmp_path, provider, check_results, discover_results):
    """Point health_local at a throwaway server_config + fake network."""
    import lib
    import lib.llm_dispatch.health_local as hl

    cfg_path = str(tmp_path / 'server_config.json')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump({'providers': [provider]}, f)

    monkeypatch.setattr(lib, '_SERVER_CONFIG_PATH', cfg_path, raising=False)
    monkeypatch.setattr(lib, '_load_server_config',
                        lambda: json.load(open(cfg_path, encoding='utf-8')),
                        raising=False)
    monkeypatch.setattr(hl, '_get_dispatcher', lambda: None)
    monkeypatch.setattr(hl, '_check_endpoint',
                        lambda url, key: dict(check_results[url]))
    monkeypatch.setattr(hl, 'discover_models',
                        lambda url, key: [_model(m['model_id'])
                                          for m in discover_results[url]])
    monkeypatch.setattr(hl, '_rebuild_dispatcher_slots', lambda: None)
    hl._success_streak.clear()
    return hl, cfg_path


def _read_provider(cfg_path):
    with open(cfg_path, encoding='utf-8') as f:
        return json.load(f)['providers'][0]


def _model_ids(cfg_path):
    return {m['model_id'] for m in _read_provider(cfg_path)['models']}


# ══════════════════════════════════════════════════════
#  Heterogeneous fleet — no churn, binding persisted
# ══════════════════════════════════════════════════════

@pytest.mark.unit
def test_periodic_resync_keeps_models_not_on_first_endpoint(monkeypatch, tmp_path):
    check = {
        A: {'ok': True, 'status': 'ok', 'served_models': {'qwen'}},
        B: {'ok': True, 'status': 'ok', 'served_models': {'llama'}},
    }
    discover = {A: [_model('qwen')], B: [_model('llama')]}
    hl, cfg_path = _wire(monkeypatch, tmp_path, _provider(), check, discover)
    # Force the periodic full-resync path every cycle.
    monkeypatch.setattr(hl, 'RESYNC_EVERY', 1)

    hl.check_once()
    assert _model_ids(cfg_path) == {'qwen', 'llama'}, (
        'periodic resync dropped llama — it is only served by box B, and the '
        'rebuild only queried the FIRST live endpoint (the flap bug)')

    prov = _read_provider(cfg_path)
    binding = prov.get('endpoint_models') or {}
    assert binding.get(A) == ['qwen'], 'binding for A must list qwen only'
    assert binding.get(B) == ['llama'], 'binding for B must list llama only'

    # Second cycle with zero drift → NO rewrite (prevents slot-pool churn).
    stats2 = hl.check_once()
    assert stats2['resynced'] == 0, 'unchanged fleet must not be re-persisted'
    assert _model_ids(cfg_path) == {'qwen', 'llama'}


@pytest.mark.unit
def test_down_endpoint_keeps_its_models_via_binding(monkeypatch, tmp_path):
    prov = _provider(binding={A: ['qwen'], B: ['llama']})
    check = {
        A: {'ok': True, 'status': 'ok', 'served_models': {'qwen'}},
        B: {'ok': False, 'status': 'timeout'},
    }
    discover = {A: [_model('qwen')]}
    hl, cfg_path = _wire(monkeypatch, tmp_path, prov, check, discover)

    hl.check_once()
    assert _model_ids(cfg_path) == {'qwen', 'llama'}, (
        'a transiently-down box must not lose its models — llama vanished '
        'from the picker while B was merely restarting')

    binding = _read_provider(cfg_path).get('endpoint_models') or {}
    assert binding.get(B) == ['llama'], \
        'down endpoint must keep its previous binding entry'


@pytest.mark.unit
def test_model_moved_between_endpoints_rebinds(monkeypatch, tmp_path):
    # llama was on B per old binding, but now A serves BOTH (B re-purposed
    # but still up, serving nothing relevant).
    prov = _provider(binding={A: ['qwen'], B: ['llama']})
    check = {
        A: {'ok': True, 'status': 'ok', 'served_models': {'qwen', 'llama'}},
        B: {'ok': True, 'status': 'ok', 'served_models': set()},
    }
    discover = {A: [_model('qwen'), _model('llama')], B: []}
    hl, cfg_path = _wire(monkeypatch, tmp_path, prov, check, discover)

    stats = hl.check_once()
    binding = _read_provider(cfg_path).get('endpoint_models') or {}
    assert binding.get(A) == ['llama', 'qwen'], \
        'placement drift (llama moved B→A) must trigger a re-bind'
    assert binding.get(B) == [], 'B now serves nothing — binding must say so'
    assert _model_ids(cfg_path) == {'qwen', 'llama'}


# ══════════════════════════════════════════════════════
#  _check_endpoint /v1 fallback (bare-origin ollama URLs)
# ══════════════════════════════════════════════════════

class _Resp:
    def __init__(self, ok, status, payload=None):
        self.ok = ok
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.mark.unit
def test_check_endpoint_falls_back_to_v1_and_reports_effective(monkeypatch):
    import lib.llm_dispatch.health_local as hl

    def fake_get(url, headers=None, timeout=None, **kw):
        if url.endswith('/v1/models'):
            return _Resp(True, 200, {'data': [{'id': 'qwen3'}]})
        return _Resp(False, 404)

    monkeypatch.setattr(hl, 'http_get', fake_get)
    res = hl._check_endpoint('http://10.0.0.5:11434', '')
    assert res['ok'] is True, 'bare-origin ollama URL must fall back to /v1'
    assert res['served_models'] == {'qwen3'}
    assert res.get('effective_url') == 'http://10.0.0.5:11434/v1', (
        'the effective URL must flow back so the binding key matches the '
        "dispatcher's normalized endpoint")


@pytest.mark.unit
def test_check_endpoint_no_fallback_on_timeout(monkeypatch):
    import lib.llm_dispatch.health_local as hl
    import requests

    calls = []

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append(url)
        raise requests.Timeout('boom')

    monkeypatch.setattr(hl, 'http_get', fake_get)
    res = hl._check_endpoint('http://10.0.0.5:11434', '')
    assert res['ok'] is False and res['status'] == 'timeout'
    assert calls == ['http://10.0.0.5:11434/models'], \
        'a timeout means the box is down — retrying /v1 would double the wait'


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
