"""Offline tests for the multi-proxy failover chain + parallel path racing.

No network: ``search_session.get`` is monkeypatched. Proves:
  * ``proxy_fallback_urls`` extends the attempt plan into an ordered chain
    [PROXY, proxy#1, …, DIRECT] with stable, credential-free labels;
  * the sticky learning can pin ANY chain entry (incl. a fallback proxy);
  * dual-attempt DISABLED → fallbacks are ignored (single primary attempt);
  * after the first path fails, TWO-or-more remaining alternates are RACED
    concurrently (structural proof via a rendezvous event, no wall-clock
    assertions), first genuine success wins and becomes the learned path;
  * ``proxy_race=False`` keeps the chain strictly sequential;
  * NC: all racers fail → the call raises (never misreported as "no matches").
"""

import threading

import pytest
import requests

import tofu_search.config as _config
from tofu_search.search import _common as common
from tofu_search.search._common import http_search_get, make_result
from tofu_search.search.proxy_mode import (
    DIRECT,
    PROXY,
    ProxyModeManager,
    _reset_proxy_mode_manager,
    detect_proxy_urls,
    proxy_mode_manager,
)

FALLBACK = 'proxy#1'

_PROXY1 = {'http': 'http://p1:1', 'https': 'http://p1:1'}
_PROXY2 = {'http': 'http://p2:2', 'https': 'http://p2:2'}
_DIRECT = {'no_proxy': '*'}


class FakeResp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    @property
    def ok(self):
        return 200 <= self.status_code < 300


_OK_HTML = "<article class='result'>x</article>"


def _parser_one(resp):
    return [make_result("a", "b", "https://a.com", "T")]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Clean circuit breaker + proxy prefs per test (config: shared conftest)."""
    monkeypatch.setattr(common, "engine_circuit", common._EngineCircuit())
    _reset_proxy_mode_manager()
    yield
    _reset_proxy_mode_manager()


def _set_chain(monkeypatch, fallbacks=("http://p2:2",), race=True, dual=True):
    cfg = _config.SearchConfig(
        proxy_url="http://p1:1",
        proxy_fallback_urls=list(fallbacks),
        proxy_dual_attempt=dual,
        proxy_race=race,
    )
    monkeypatch.setattr(_config, "_global_config", cfg)
    return cfg


# ── detect_proxy_urls ─────────────────────────────────────────────

def test_detect_chain_order_and_dedup():
    cfg = _config.SearchConfig(
        proxy_url="http://p1:1",
        proxy_fallback_urls=["http://p2:2", " http://p3:3 ", "http://p2:2", ""],
    )
    assert detect_proxy_urls(cfg) == ["http://p1:1", "http://p2:2", "http://p3:3"]


def test_detect_chain_falls_back_to_env_primary(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("http_proxy", "http://env:9")
    cfg = _config.SearchConfig(proxy_fallback_urls=["http://p2:2"])
    assert detect_proxy_urls(cfg) == ["http://env:9", "http://p2:2"]


# ── attempt_plan shape ────────────────────────────────────────────

def test_plan_chain_labels_and_kwargs():
    mgr = ProxyModeManager()
    cfg = _config.SearchConfig(proxy_url="http://p1:1",
                               proxy_fallback_urls=["http://p2:2"])
    plan = mgr.attempt_plan("Bing", cfg)
    assert [m for m, _ in plan] == [PROXY, FALLBACK, DIRECT]
    assert plan[0][1] == _PROXY1            # explicit primary → forced dict
    assert plan[1][1] == _PROXY2            # fallback → forced dict
    assert plan[2][1] == _DIRECT


def test_plan_sticky_can_pin_a_fallback_proxy():
    mgr = ProxyModeManager()
    cfg = _config.SearchConfig(proxy_url="http://p1:1",
                               proxy_fallback_urls=["http://p2:2"])
    mgr.record_success("Bing", FALLBACK)
    plan = mgr.attempt_plan("Bing", cfg)
    assert [m for m, _ in plan] == [FALLBACK, PROXY, DIRECT]


def test_NC_plan_dual_disabled_ignores_fallbacks():
    """dual_attempt=False = exactly ONE proxied attempt — the failover chain
    must NOT silently widen it."""
    mgr = ProxyModeManager()
    cfg = _config.SearchConfig(proxy_url="http://p1:1",
                               proxy_fallback_urls=["http://p2:2"],
                               proxy_dual_attempt=False)
    plan = mgr.attempt_plan("Bing", cfg)
    assert [m for m, _ in plan] == [PROXY]


# ── racing: first path dead → alternates run CONCURRENTLY ─────────

def test_race_fallback_proxy_wins_and_is_learned(monkeypatch):
    """Primary dead (fast ProxyError, the tunnel-403 shape) → the fallback
    proxy and the direct path are raced IN PARALLEL; the fallback wins.

    Parallelism is proven STRUCTURALLY: the fallback's fake blocks until the
    direct path's fake has STARTED (rendezvous). Run sequentially, the
    fallback attempt would time out the rendezvous and return 403 instead,
    failing the assertions below.
    """
    _set_chain(monkeypatch)
    direct_started = threading.Event()
    seen = []
    seen_lock = threading.Lock()

    def fake_get(url, **kw):
        proxies = kw.get("proxies")
        with seen_lock:
            seen.append(proxies)
        if proxies == _PROXY1:
            raise requests.exceptions.ProxyError("primary dead")
        if proxies == _PROXY2:
            # Rendezvous: only proceeds once the direct racer has started.
            if direct_started.wait(timeout=2.0):
                return FakeResp(_OK_HTML, 200)
            return FakeResp("no rendezvous — ran sequentially", 403)
        direct_started.set()                     # direct path
        raise requests.exceptions.ConnectionError("no direct egress")

    monkeypatch.setattr(common.search_session, "get", fake_get)
    out = http_search_get(name="Bing", url="https://t/", params={}, query="q",
                          parser=_parser_one)
    assert len(out) == 1
    assert proxy_mode_manager._preferred("Bing") == FALLBACK   # learned
    assert _PROXY2 in seen and _DIRECT in seen   # both alternates ran


def test_race_all_paths_fail_raises_never_empty(monkeypatch):
    """Every racer fails → the call RAISES (an outage must never be
    misreported as "no matches")."""
    _set_chain(monkeypatch)

    def fake_get(url, **kw):
        proxies = kw.get("proxies")
        if proxies == _PROXY1:
            raise requests.exceptions.ProxyError("primary dead")
        if proxies == _PROXY2:
            return FakeResp("blocked", 403)
        raise requests.exceptions.ConnectionError("no direct egress")

    monkeypatch.setattr(common.search_session, "get", fake_get)
    with pytest.raises(requests.exceptions.ConnectionError):
        http_search_get(name="Bing", url="https://t/", params={}, query="q",
                        parser=_parser_one)


def test_race_disabled_keeps_chain_sequential(monkeypatch):
    """proxy_race=False: the fallback chain still fails over, one at a time."""
    _set_chain(monkeypatch, race=False)
    seen = []

    def fake_get(url, **kw):
        proxies = kw.get("proxies")
        seen.append(proxies)
        if proxies == _PROXY1:
            raise requests.exceptions.ProxyError("primary dead")
        if proxies == _PROXY2:
            return FakeResp(_OK_HTML, 200)
        raise AssertionError("direct path must not run before the chain "
                             "is exhausted sequentially")

    monkeypatch.setattr(common.search_session, "get", fake_get)
    out = http_search_get(name="Bing", url="https://t/", params={}, query="q",
                          parser=_parser_one)
    assert len(out) == 1
    assert seen == [_PROXY1, _PROXY2]            # strict order, no race
    assert proxy_mode_manager._preferred("Bing") == FALLBACK


def test_race_soft_block_loses_to_healthy_path(monkeypatch):
    """A fallback serving a consent wall (big 200 body, 0 results) loses the
    race to the direct path's genuine results."""
    _set_chain(monkeypatch)
    gate = threading.Event()
    seen = []
    seen_lock = threading.Lock()

    def fake_get(url, **kw):
        proxies = kw.get("proxies")
        with seen_lock:
            seen.append(proxies)
        if proxies == _PROXY1:
            raise requests.exceptions.ProxyError("primary dead")
        if proxies == _PROXY2:
            gate.set()
            return FakeResp("x" * 30_000, 200)   # consent wall
        gate.wait(timeout=2.0)                   # keep the race honest
        return FakeResp(_OK_HTML, 200)           # direct path wins

    monkeypatch.setattr(common.search_session, "get", fake_get)

    def parser(resp):
        return [] if len(resp.text) > 25_000 else [make_result("a", "b", "https://a.com", "T")]

    out = http_search_get(name="Brave", url="https://t/", params={}, query="q",
                          parser=parser)
    assert len(out) == 1
    assert proxy_mode_manager._preferred("Brave") == DIRECT
    assert _PROXY2 in seen and _DIRECT in seen
