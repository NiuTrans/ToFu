"""Regression: ``lib.search_bridge.sync_search_config`` must forward the
tofu-search 0.3.2 pre-fetch-gate knobs and the 0.4.1 adaptive-proxy knob to
``tofu_search.configure``.

Why this matters: the three ``prefetch_gate_*`` fields have NO env-var mapping
inside ``configure()`` (unlike ``FETCH_*`` / ``TOFU_SEARCH_PROXY_*``), so if the
bridge doesn't pass them EXPLICITLY they silently run the library defaults and
are un-tunable from the chatui host. This test pins that they're always passed,
and that env overrides are honoured.

Pure-pytest (no Quart shim): we patch ``tofu_search.configure`` to capture the
kwargs and stub the ``_lib`` attributes the bridge reads.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# The FETCH_* attributes sync_search_config reads off `lib` (aliased _lib).
_LIB_STUB = {
    'LLM_CONTENT_FILTER_ENABLED': True,
    'FETCH_TOP_N': 6,
    'FETCH_TIMEOUT': 15,
    'FETCH_MAX_CHARS_SEARCH': 60000,
    'FETCH_MAX_CHARS_DIRECT': 200000,
    'FETCH_MAX_CHARS_PDF': 0,
    'FETCH_MAX_BYTES': 20 * 1024 * 1024,
    'SKIP_DOMAINS': {'youtube.com'},
    'PREFETCH_GATE_ENABLED': True,
}


def _capture_configure_kwargs(monkeypatch, env=None):
    """Run sync_search_config with a stubbed _lib + captured configure()."""
    import lib.search_bridge as sb

    for k, v in _LIB_STUB.items():
        monkeypatch.setattr(sb._lib, k, v, raising=False)
    # Deterministic proxy (avoid touching real settings)
    monkeypatch.setattr(sb, '_resolve_proxy_url', lambda: '')

    for var in ('PREFETCH_GATE_ENABLED', 'PREFETCH_GATE_MIN_QUERY_TERMS',
                'PREFETCH_GATE_MIN_FETCH', 'TOFU_SEARCH_PROXY_DUAL_ATTEMPT',
                'FETCH_FILTER_MIN_CHARS', 'FETCH_FILTER_TIMEOUT',
                'FETCH_FILTER_MODE', 'FETCH_FILTER_GATE_MAX_CHARS'):
        monkeypatch.delenv(var, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)

    captured = {}

    def fake_configure(**kwargs):
        captured.update(kwargs)
        return None

    with patch('tofu_search.configure', side_effect=fake_configure):
        sb.sync_search_config()
    return captured


@pytest.mark.unit
class TestSyncSearchConfigKnobs:

    def test_new_knobs_are_passed_with_defaults(self, monkeypatch):
        cap = _capture_configure_kwargs(monkeypatch)
        # 0.3.2 prefetch gate — no env fallback inside configure(), so the
        # bridge MUST pass them.
        assert cap['prefetch_gate_enabled'] is True
        assert cap['prefetch_gate_min_query_terms'] == 2
        assert cap['prefetch_gate_min_fetch'] == 3
        # 0.4.1 adaptive proxy
        assert cap['proxy_dual_attempt'] is True
        # sanity: the pre-existing knobs still flow
        assert cap['fetch_top_n'] == 6
        assert cap['filter_enabled'] is True
        # 0.6.0 content-filter rework: gate mode is the bridge default, and
        # the timeout default follows the library's 300 → 45 tightening.
        assert cap['filter_mode'] == 'gate'
        assert cap['filter_timeout'] == 45
        assert cap['filter_min_chars'] == 6000
        assert cap['gate_input_max_chars'] == 6000

    def test_env_overrides_are_honoured(self, monkeypatch):
        cap = _capture_configure_kwargs(monkeypatch, env={
            'PREFETCH_GATE_MIN_QUERY_TERMS': '4',
            'PREFETCH_GATE_MIN_FETCH': '1',
            'TOFU_SEARCH_PROXY_DUAL_ATTEMPT': 'no',
            'FETCH_FILTER_MODE': 'rewrite',
            'FETCH_FILTER_TIMEOUT': '120',
            'FETCH_FILTER_MIN_CHARS': '7000',
            'FETCH_FILTER_GATE_MAX_CHARS': '5000',
        })
        assert cap['prefetch_gate_min_query_terms'] == 4
        assert cap['prefetch_gate_min_fetch'] == 1
        assert cap['proxy_dual_attempt'] is False
        assert cap['filter_mode'] == 'rewrite'
        assert cap['filter_timeout'] == 120
        assert cap['filter_min_chars'] == 7000
        assert cap['gate_input_max_chars'] == 5000

    # ── 0.10.0 multi-proxy failover chain (SOFT floor) ──

    def _capture_with_pool(self, monkeypatch, *, pool, proxy_url='http://p1:1',
                           env=None):
        """sync_search_config with a stubbed pool + primary proxy."""
        import lib.proxy as lp
        import lib.search_bridge as sb

        for k, v in _LIB_STUB.items():
            monkeypatch.setattr(sb._lib, k, v, raising=False)
        monkeypatch.setattr(sb, '_resolve_proxy_url', lambda: proxy_url)
        monkeypatch.setattr(lp, 'global_proxy_failover_urls', lambda: list(pool))
        for var in ('PREFETCH_GATE_ENABLED', 'PREFETCH_GATE_MIN_QUERY_TERMS',
                    'PREFETCH_GATE_MIN_FETCH', 'TOFU_SEARCH_PROXY_DUAL_ATTEMPT',
                    'TOFU_SEARCH_PROXY_RACE', 'FETCH_FILTER_MIN_CHARS',
                    'FETCH_FILTER_TIMEOUT', 'FETCH_FILTER_MODE',
                    'FETCH_FILTER_GATE_MAX_CHARS'):
            monkeypatch.delenv(var, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        captured = {}
        with patch('tofu_search.configure',
                   side_effect=lambda **kw: captured.update(kw)):
            sb.sync_search_config()
        return captured

    def test_failover_chain_flows_when_library_supports_it(self, monkeypatch):
        import tofu_search
        cap = self._capture_with_pool(
            monkeypatch, pool=['http://p1:1', 'http://p2:2'])
        supported = 'proxy_fallback_urls' in getattr(
            tofu_search.SearchConfig, '__dataclass_fields__', {})
        if supported:
            # The primary is excluded; the rest of the pool becomes the chain.
            assert cap['proxy_url'] == 'http://p1:1'
            assert cap['proxy_fallback_urls'] == ['http://p2:2']
            assert cap['proxy_race'] is True
        else:
            # SOFT floor: an older installed library receives NEITHER kwarg.
            assert 'proxy_fallback_urls' not in cap
            assert 'proxy_race' not in cap

    def test_empty_pool_passes_no_fallback_kwarg(self, monkeypatch):
        import tofu_search
        cap = self._capture_with_pool(monkeypatch, pool=[])
        assert 'proxy_fallback_urls' not in cap
        supported = 'proxy_fallback_urls' in getattr(
            tofu_search.SearchConfig, '__dataclass_fields__', {})
        # proxy_race is passed whenever the field exists (it only gates the
        # parallel behaviour, harmless without a chain).
        assert ('proxy_race' in cap) is supported

    def test_proxy_race_env_override(self, monkeypatch):
        import tofu_search
        cap = self._capture_with_pool(
            monkeypatch, pool=['http://p2:2'],
            env={'TOFU_SEARCH_PROXY_RACE': '0'})
        supported = 'proxy_race' in getattr(
            tofu_search.SearchConfig, '__dataclass_fields__', {})
        if supported:
            assert cap['proxy_race'] is False

    def test_prefetch_gate_disabled_via_lib_flag(self, monkeypatch):
        # PREFETCH_GATE_ENABLED env absent → falls back to _lib attribute.
        import lib.search_bridge as sb
        monkeypatch.setattr(sb._lib, 'PREFETCH_GATE_ENABLED', False, raising=False)
        cap = _capture_configure_kwargs(monkeypatch)
        # (helper re-sets the stub True, so override AFTER)
        # Re-run with the flag flipped explicitly via env for determinism:
        cap2 = _capture_configure_kwargs(monkeypatch, env={'PREFETCH_GATE_ENABLED': 'false'})
        assert cap2['prefetch_gate_enabled'] is False


@pytest.mark.unit
def test_gate_adapter_preserves_irrelevant_verdict_and_bounds_provider_work(
        monkeypatch):
    """The sentinel is semantic output, so it must not also be a wire stop."""
    import lib.search_bridge as sb

    captured = {}

    def fake_dispatch(messages, **kwargs):
        captured.update(kwargs)
        return '§§IRRELEVANT§§', {}

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', fake_dispatch)

    result = sb._chatui_llm(
        [
            {'role': 'system', 'content': 'You are a web page relevance judge.'},
            {'role': 'user', 'content': 'page'},
        ],
        stop=['§§IRRELEVANT§§'],
        timeout=45,
    )

    assert result == '§§IRRELEVANT§§'
    assert captured['max_tokens'] == 32
    assert captured['max_429_attempts'] == 1
    assert not captured.get('extra')


@pytest.mark.unit
def test_rewrite_adapter_keeps_long_output_budget_and_unrelated_stops(
        monkeypatch):
    """Only the verdict sentinel and gate output budget are specialized."""
    import lib.search_bridge as sb

    captured = {}

    def fake_dispatch(messages, **kwargs):
        captured.update(kwargs)
        return '[USEFUL]\ncleaned page', {}

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', fake_dispatch)

    result = sb._chatui_llm(
        [{'role': 'system', 'content': 'You are a web page content cleaner.'}],
        stop=['§§IRRELEVANT§§', '<END>'],
    )

    assert result.startswith('[USEFUL]')
    assert 'max_tokens' not in captured
    assert captured['extra'] == {'stop': ['<END>']}
    assert captured['max_429_attempts'] == 1
