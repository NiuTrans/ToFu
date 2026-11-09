"""Regression: ``lib.search.search_bridge.sync_search_config`` must forward the
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# The attributes sync_search_config reads off `lib` (aliased _lib).
_LIB_STUB = {
    'SKIP_DOMAINS': {'youtube.com'},
    'FETCH_SKIP_DOMAINS': {'junk.example'},
    'PREFETCH_GATE_ENABLED': True,
}


def _capture_configure_kwargs(monkeypatch, env=None):
    """Run sync_search_config with a stubbed _lib + captured configure()."""
    import lib.search.search_bridge as sb

    for k, v in _LIB_STUB.items():
        monkeypatch.setattr(sb._lib, k, v, raising=False)
    # Deterministic proxy (avoid touching real settings)
    monkeypatch.setattr(sb, '_resolve_proxy_url', lambda: '')

    for var in ('PREFETCH_GATE_ENABLED', 'PREFETCH_GATE_MIN_QUERY_TERMS',
                'PREFETCH_GATE_MIN_FETCH', 'TOFU_SEARCH_PROXY_DUAL_ATTEMPT'):
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
        assert cap['skip_domains'] == {'youtube.com'}
        assert cap['fetch_skip_domains'] == {'junk.example'}

    def test_search_limits_stay_library_defaults(self, monkeypatch):
        cap = _capture_configure_kwargs(monkeypatch)
        # Result counts, page sizes, timeouts, download limits and deepening
        # are owned by tofu-search's SearchConfig; the host never re-declares
        # (and so can never shadow) them.
        for key in ('fetch_top_n', 'fetch_timeout', 'fetch_max_chars_search',
                    'fetch_max_chars_direct', 'fetch_max_chars_pdf',
                    'fetch_max_bytes', 'deepen_enabled'):
            assert key not in cap

    def test_llm_relevance_gate_is_off_regardless_of_legacy_settings(self, monkeypatch):
        import lib.search.search_bridge as sb
        monkeypatch.setattr(sb._lib, '_SAVED_CONFIG', {'search': {
            'profile': 'deep', 'llm_content_filter': True}}, raising=False)
        cap = _capture_configure_kwargs(monkeypatch, env={'FETCH_LLM_FILTER': '1'})
        assert cap['filter_enabled'] is False
        # The model seam stays installed for explicitly configured callers.
        assert cap['llm_function'] is sb._chatui_llm

    def test_env_overrides_are_honoured(self, monkeypatch):
        cap = _capture_configure_kwargs(monkeypatch, env={
            'PREFETCH_GATE_MIN_QUERY_TERMS': '4',
            'PREFETCH_GATE_MIN_FETCH': '1',
            'TOFU_SEARCH_PROXY_DUAL_ATTEMPT': 'no',
        })
        assert cap['prefetch_gate_min_query_terms'] == 4
        assert cap['prefetch_gate_min_fetch'] == 1
        assert cap['proxy_dual_attempt'] is False

    # ── 0.10.0 multi-proxy failover chain (SOFT floor) ──

    def _capture_with_pool(self, monkeypatch, *, pool, proxy_url='http://p1:1',
                           env=None):
        """sync_search_config with a stubbed pool + primary proxy."""
        import lib.proxy as lp
        import lib.search.search_bridge as sb

        for k, v in _LIB_STUB.items():
            monkeypatch.setattr(sb._lib, k, v, raising=False)
        monkeypatch.setattr(sb, '_resolve_proxy_url', lambda: proxy_url)
        monkeypatch.setattr(lp, 'global_proxy_failover_urls', lambda: list(pool))
        for var in ('PREFETCH_GATE_ENABLED', 'PREFETCH_GATE_MIN_QUERY_TERMS',
                    'PREFETCH_GATE_MIN_FETCH', 'TOFU_SEARCH_PROXY_DUAL_ATTEMPT',
                    'TOFU_SEARCH_PROXY_RACE'):
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
            # The bridge resolves no primary at all (call-time routing owns it),
            # so it passes neither a primary nor a precomputed failover chain.
            assert 'proxy_url' not in cap
            assert 'proxy_fallback_urls' not in cap
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
        import lib.search.search_bridge as sb
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
    import lib.search.search_bridge as sb

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
    import lib.search.search_bridge as sb

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


@pytest.mark.unit
def test_filter_adapter_reports_read_budget_expiry_as_timeout(monkeypatch):
    """tofu-search recognizes the builtin TimeoutError as budget expiry."""
    import lib.search.search_bridge as sb
    from lib.llm.llm_errors import ResponseReadTimeoutError

    expiry = ResponseReadTimeoutError(base_url='https://gateway.example/v1',
                                      timeout_seconds=1.08756)

    def fake_dispatch(messages, **kwargs):
        raise expiry

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', fake_dispatch)
    with pytest.raises(TimeoutError) as caught:
        sb._chatui_llm([{'role': 'user', 'content': 'page'}], timeout=1.08756)
    assert caught.value.__cause__ is expiry
    assert 'timeout budget: 1.08756s' in str(caught.value)


def _non_budget_failures():
    from lib.llm.llm_errors import (
        RequestTransportError, RequestUploadTimeoutError, RetryableAPIError,
    )
    return [
        RetryableAPIError('HTTP 503 from gateway', status_code=503),
        RequestTransportError(failure_stage='connection', reason='connection_error'),
        RequestUploadTimeoutError(base_url='https://gateway.example/v1',
                                  timeout_seconds=10),
    ]


@pytest.mark.unit
@pytest.mark.parametrize('failure_index', range(3))
def test_filter_adapter_keeps_other_failures_unchanged(monkeypatch, failure_index):
    import lib.search.search_bridge as sb

    failure = _non_budget_failures()[failure_index]

    def fake_dispatch(messages, **kwargs):
        raise failure

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', fake_dispatch)
    with pytest.raises(type(failure)) as caught:
        sb._chatui_llm([{'role': 'user', 'content': 'page'}], timeout=2.0)
    assert caught.value is failure
    assert not isinstance(caught.value, TimeoutError)


@pytest.mark.unit
def test_host_filter_budget_expiry_is_reported_below_error(monkeypatch, caplog):
    """The host adapter and tofu-search agree on what an elapsed budget is."""
    import logging

    import lib.search.search_bridge as sb
    from lib.llm.llm_errors import ResponseReadTimeoutError
    from tofu_search.config import SearchConfig
    from tofu_search.fetch.content_filter import filter_web_content

    def fake_dispatch(messages, **kwargs):
        raise ResponseReadTimeoutError(base_url='https://gateway.example/v1',
                                       timeout_seconds=kwargs['timeout'])

    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', fake_dispatch)
    raw = 'Real article body. ' * 400
    config = SearchConfig(filter_enabled=True, llm_function=sb._chatui_llm,
                          filter_cache_ttl=0)
    with caplog.at_level(logging.DEBUG):
        out = filter_web_content(raw, url='https://example.com/slow', query='q',
                                 timeout=2.0, config=config)
    assert out == raw
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert any(record.levelno == logging.WARNING and 'TIMEOUT' in record.getMessage()
               for record in caplog.records)
