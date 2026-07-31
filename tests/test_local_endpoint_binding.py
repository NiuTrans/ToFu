#!/usr/bin/env python3
"""tests/test_local_endpoint_binding.py — per-endpoint model binding (endpoint_models).

Self-hosted engines (vLLM / SGLang) serve exactly one model per URL; Ollama
serves a handful. The dispatcher must place a model's slots ONLY on the
endpoints that actually declared serving it (``provider.endpoint_models``),
instead of fanning every model out to every endpoint (the homogeneous-fleet
assumption — which actively misroutes heterogeneous fleets into upstream
404s).

Binding semantics (owner-ratified 2026-07-25):

  * endpoint ABSENT from the map (or mapped to a falsy list) → "no probe
    data" → legacy union fan-out (backwards compatible; zero migration).
  * non-empty entry → that endpoint serves ONLY the listed ROOT model_ids.
  * Aliases follow their ROOT model's endpoints — /v1/models never lists
    aliases, so checking the alias id against the binding would wrongly
    orphan every alias slot.
  * /v1 fallback: a bare-origin URL that 404s on /models is retried under
    /v1 (Ollama's default :11434 habit), and the EFFECTIVE base URL
    propagates to probe results so the stored endpoint actually works.

FAILING-FIRST proven: every SlotBindingTest / V1FallbackTest case is RED on
the pre-fix tree (union fan-out / no fallback), GREEN after. The two
preservation guards (legacy union) are GREEN both before and after.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

A = 'http://10.0.0.5:8000/v1'
B = 'http://10.0.0.6:8000/v1'


def _build(providers):
    """Build a throwaway dispatcher's slots from a provider list."""
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers(providers)
    return d.slots


def _local_provider(models, binding='ABSENT', endpoints=(A, B)):
    p = {
        'id': 'local_test',
        'brand': 'local',
        'base_url': endpoints[0],
        'endpoints': list(endpoints),
        'api_keys': [],          # local keyless → one blank-key slot pool
        'enabled': True,
        'models': models,
    }
    if binding != 'ABSENT':
        p['endpoint_models'] = binding
    return [p]


def _eps_of(slots, model):
    return {s.base_url for s in slots if s.model == model}


# ══════════════════════════════════════════════════════
#  Slot placement under endpoint_models binding
# ══════════════════════════════════════════════════════

@pytest.mark.unit
def test_binding_places_model_only_on_declared_endpoint():
    slots = _build(_local_provider(
        [{'model_id': 'qwen', 'capabilities': ['text']},
         {'model_id': 'llama', 'capabilities': ['text']}],
        binding={A: ['qwen'], B: ['llama']},
    ))
    assert _eps_of(slots, 'qwen') == {A}, \
        'qwen must live ONLY on A (the endpoint that declared it); fan-out misroutes'
    assert _eps_of(slots, 'llama') == {B}, \
        'llama must live ONLY on B'


@pytest.mark.unit
def test_alias_follows_root_endpoint_not_alias_lookup():
    slots = _build(_local_provider(
        [{'model_id': 'qwen', 'capabilities': ['text'], 'aliases': ['qwen-fast']}],
        binding={A: ['qwen'], B: ['llama']},
    ))
    # /v1/models never lists 'qwen-fast' — if the code checked the ALIAS id
    # against the binding the alias would get zero slots; if it fans out it
    # gets {A, B}. Correct is exactly {A} (where the root lives).
    assert _eps_of(slots, 'qwen-fast') == {A}, \
        'alias must follow its ROOT model to A only'


@pytest.mark.unit
def test_empty_binding_entry_means_no_probe_info():
    # A=[] is indistinguishable from "never probed" → union fallback for A.
    slots = _build(_local_provider(
        [{'model_id': 'qwen', 'capabilities': ['text']},
         {'model_id': 'llama', 'capabilities': ['text']}],
        binding={A: [], B: ['llama']},
    ))
    assert _eps_of(slots, 'qwen') == {A}, \
        'B declared only llama — qwen must not be placed on B'
    assert _eps_of(slots, 'llama') == {A, B}, \
        'empty binding entry must fall back to union (serve all)'


@pytest.mark.unit
def test_model_served_by_no_bound_endpoint_gets_no_slots():
    slots = _build(_local_provider(
        [{'model_id': 'qwen', 'capabilities': ['text']},
         {'model_id': 'ghost', 'capabilities': ['text']}],
        binding={A: ['qwen'], B: ['llama']},
    ))
    assert _eps_of(slots, 'ghost') == set(), \
        'a model no bound endpoint serves must not be placed anywhere (honest absence beats 404s)'
    # …while the declared model keeps its single home.
    assert _eps_of(slots, 'qwen') == {A}


# ── Preservation guards (GREEN before AND after) ──

@pytest.mark.unit
def test_absent_binding_is_legacy_union():
    slots = _build(_local_provider(
        [{'model_id': 'qwen', 'capabilities': ['text']}],
    ))
    assert _eps_of(slots, 'qwen') == {A, B}, \
        'no endpoint_models at all → legacy union fan-out must be preserved'


@pytest.mark.unit
def test_multikey_binding_interacts_per_key():
    slots = _build(_local_provider(
        [{'model_id': 'qwen', 'capabilities': ['text']}],
        binding={A: ['qwen'], B: ['llama']},
    ))
    # one blank local key, two endpoints → qwen lands on A only; B keeps no
    # qwen slot even though slot naming is per-endpoint.
    key_names = {s.key_name for s in slots if s.model == 'qwen'}
    assert all(kn.endswith('_ep0') or '_ep' not in kn for kn in key_names), \
        'qwen slots must only use the A-endpoint suffix'


# ══════════════════════════════════════════════════════
#  /v1 fallback for bare-origin (Ollama-style) URLs
# ══════════════════════════════════════════════════════

class _Resp:
    def __init__(self, ok, status, payload=None):
        self.ok = ok
        self.status_code = status
        self._payload = payload or {}
        self.text = '' if ok else 'not found'

    def json(self):
        return self._payload


def _fake_http_get():
    calls = []

    def fake(url, headers=None, timeout=None, **kw):
        calls.append(url)
        if url.endswith('/v1/models'):
            return _Resp(True, 200, {'data': [{'id': 'qwen3'}]})
        return _Resp(False, 404)

    return fake, calls


@pytest.mark.unit
def test_discover_retries_bare_origin_under_v1(monkeypatch):
    import lib.llm_dispatch.discovery as disco_pkg
    fake, calls = _fake_http_get()
    monkeypatch.setattr(disco_pkg, 'http_get', fake, raising=False)
    from lib.llm_dispatch.discovery import discover_models
    models = discover_models('http://10.0.0.5:11434', '')
    assert [m['model_id'] for m in models] == ['qwen3'], \
        'bare-origin /models 404 must be retried under /v1 (ollama habit)'
    assert calls[0] == 'http://10.0.0.5:11434/models', \
        'the direct URL must be tried FIRST'
    assert 'http://10.0.0.5:11434/v1/models' in calls, \
        'fallback must append /v1 exactly once'


@pytest.mark.unit
def test_discover_reports_effective_base_url(monkeypatch):
    import lib.llm_dispatch.discovery as disco_pkg
    fake, _ = _fake_http_get()
    monkeypatch.setattr(disco_pkg, 'http_get', fake, raising=False)
    from lib.llm_dispatch.discovery import discover_models
    models, effective = discover_models('http://10.0.0.5:11434', '',
                                        return_effective=True)
    assert [m['model_id'] for m in models] == ['qwen3']
    assert effective == 'http://10.0.0.5:11434/v1', \
        'caller must learn the WORKING base URL so the stored endpoint is usable'


@pytest.mark.unit
def test_probe_result_carries_effective_v1_base_url(monkeypatch):
    import lib.llm_dispatch.discovery as disco_pkg
    fake, _ = _fake_http_get()
    monkeypatch.setattr(disco_pkg, 'http_get', fake, raising=False)
    from lib.llm_dispatch.discovery import probe_provider
    res = probe_provider('http://10.0.0.5:11434', '', force_local=True)
    assert res.get('ok') is True, 'probe of a bare ollama origin must succeed via /v1'
    assert res.get('base_url') == 'http://10.0.0.5:11434/v1', \
        'probe must return the effective /v1 base URL (chat calls would 404 on the bare origin)'
    assert [m['model_id'] for m in res.get('models', [])] == ['qwen3']


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
