"""Regression: per-call config overrides must reach engine-layer get_config().

Incident 2026-08-21: ``search(query, proxy_url=...)`` /
``perform_web_search(config=...)`` built a per-call SearchConfig, but the
engine request path (``search/_common.py``) read the GLOBAL ``get_config()``
— the override (e.g. ``proxy_url``) was silently dropped and every engine
went direct. On a host with no direct egress this looked exactly like a
total search outage, and made per-proxy diagnosis impossible.

Anchor: debug/diag_proxy_search.py reproduction (chatui repo) — per-call
kwargs produced "request failed via direct" for every engine; global
configure() produced results through both pool proxies.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import tofu_search.config as _config
from tofu_search.config import bind_call_config, get_config
from tofu_search.providers import submit_with_provider_context
from tofu_search.search import orchestrator as orch


def test_bind_call_config_scopes_get_config():
    global_cfg = get_config()
    override = global_cfg.copy(proxy_url='http://per-call:1')
    assert get_config().proxy_url != 'http://per-call:1'
    with bind_call_config(override):
        assert get_config() is override
    assert get_config() is global_cfg          # no leak after the block


def test_override_propagates_to_worker_threads():
    """submit_with_provider_context copies ContextVars into the worker."""
    override = get_config().copy(proxy_url='http://per-call:2')
    with bind_call_config(override):
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = submit_with_provider_context(
                pool, lambda: get_config().proxy_url)
            assert fut.result() == 'http://per-call:2'
    assert get_config().proxy_url != 'http://per-call:2'


def test_per_call_config_reaches_engine_thread(monkeypatch):
    """Bite test: perform_web_search(config=...) is visible from inside the
    engine function running on the orchestrator's worker thread."""
    seen = {}

    def fake_ddg(query, max_results=6, freshness=''):
        seen['proxy_url'] = get_config().proxy_url
        return []

    def fake_brave(query, max_results=6, freshness=''):
        # The 0-results retry path calls this module-global directly.
        return []

    monkeypatch.setattr(orch, 'search_ddg_html', fake_ddg)
    monkeypatch.setattr(orch, 'search_brave', fake_brave)

    override = get_config().copy(proxy_url='http://per-call:3',
                                 proxy_dual_attempt=False)
    out = orch.perform_web_search(
        'q', max_results=3, engines=['DDG-HTML'], fetch_pages=False,
        filter_pages=False, rerank=False, config=override)

    assert isinstance(out, list)
    assert seen.get('proxy_url') == 'http://per-call:3'
    # Global config untouched and the override released after the call.
    assert get_config().proxy_url != 'http://per-call:3'


def test_concurrent_calls_keep_their_own_overrides():
    """Two bound calls in different threads must not stomp each other."""
    barrier = threading.Barrier(2)
    seen = {}

    def run(tag, url):
        with bind_call_config(get_config().copy(proxy_url=url)):
            barrier.wait(timeout=5)
            seen[tag] = get_config().proxy_url

    t1 = threading.Thread(target=run, args=('a', 'http://call-a:1'))
    t2 = threading.Thread(target=run, args=('b', 'http://call-b:2'))
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    assert seen == {'a': 'http://call-a:1', 'b': 'http://call-b:2'}
