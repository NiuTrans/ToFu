"""Offline tests for adaptive per-engine proxy-mode dual-attempt + learning.

No network: ``search_session.get`` is monkeypatched to simulate a proxy path
that is blocked/dead and a direct path that works (and vice versa). Proves:
  * no proxy configured → exactly ONE direct attempt, no ``proxies=`` kwarg
    (byte-identical to the historical behaviour);
  * proxy configured + dual-attempt → a connect failure / block / soft-block on
    the first path transparently retries the OTHER path and succeeds;
  * the winning path becomes sticky (tried first next call);
  * a read-timeout does NOT trigger a second full attempt (budget guard);
  * NC: with dual-attempt DISABLED the same first-path failure yields 0 results.
"""

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
    detect_proxy_url,
    proxy_mode_manager,
)


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
    """Clean circuit breaker + proxy prefs + config per test."""
    monkeypatch.setattr(common, "engine_circuit", common._EngineCircuit())
    _reset_proxy_mode_manager()
    yield
    _reset_proxy_mode_manager()


def _set_proxy(monkeypatch, url="http://proxy:8080", dual=True):
    cfg = _config.SearchConfig(proxy_url=url, proxy_dual_attempt=dual)
    monkeypatch.setattr(_config, "_global_config", cfg)


# ── detect_proxy_url ──────────────────────────────────────────────

def test_detect_prefers_explicit_over_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://env:1")
    cfg = _config.SearchConfig(proxy_url="http://explicit:2")
    assert detect_proxy_url(cfg) == "http://explicit:2"


def test_detect_falls_back_to_env(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("http_proxy", "http://env:9")
    assert detect_proxy_url(_config.SearchConfig()) == "http://env:9"


def test_detect_none_when_unset(monkeypatch):
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    assert detect_proxy_url(_config.SearchConfig()) == ""


# ── attempt_plan ──────────────────────────────────────────────────

def test_plan_single_direct_when_no_proxy(monkeypatch):
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    mgr = ProxyModeManager()
    plan = mgr.attempt_plan("Bing", _config.SearchConfig())
    assert plan == [(DIRECT, None)]


def test_plan_dual_proxy_first_then_direct():
    mgr = ProxyModeManager()
    cfg = _config.SearchConfig(proxy_url="http://p:1")
    plan = mgr.attempt_plan("Bing", cfg)
    assert [m for m, _ in plan] == [PROXY, DIRECT]
    # Explicit proxy_url → forced proxies dict on the PROXY attempt.
    assert plan[0][1] == {"http": "http://p:1", "https": "http://p:1"}
    # DIRECT attempt uses the reliable env bypass marker.
    assert plan[1][1] == {"no_proxy": "*"}


def test_plan_single_proxy_when_dual_disabled():
    mgr = ProxyModeManager()
    cfg = _config.SearchConfig(proxy_url="http://p:1", proxy_dual_attempt=False)
    plan = mgr.attempt_plan("Bing", cfg)
    assert [m for m, _ in plan] == [PROXY]


def test_plan_sticky_direct_reorders():
    mgr = ProxyModeManager()
    cfg = _config.SearchConfig(proxy_url="http://p:1")
    mgr.record_success("Bing", DIRECT)
    plan = mgr.attempt_plan("Bing", cfg)
    assert [m for m, _ in plan] == [DIRECT, PROXY]


# ── http_search_get integration: no proxy = one attempt, no proxies kwarg ──

def test_no_proxy_single_attempt_no_kwarg(monkeypatch):
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(_config, "_global_config", _config.SearchConfig())
    calls = []

    def fake_get(url, **kw):
        calls.append(kw)
        return FakeResp(_OK_HTML, 200)

    monkeypatch.setattr(common.search_session, "get", fake_get)
    out = http_search_get(name="T", url="https://t/", params={}, query="q",
                          parser=_parser_one)
    assert len(out) == 1
    assert len(calls) == 1
    assert "proxies" not in calls[0]   # byte-identical to historical path


# ── dual-attempt: proxy path dead → direct path works, and learns DIRECT ──

def test_proxy_connect_failure_falls_back_to_direct(monkeypatch):
    _set_proxy(monkeypatch)
    seen = []

    def fake_get(url, **kw):
        proxies = kw.get("proxies")
        seen.append(proxies)
        if proxies == {"http": "http://proxy:8080", "https": "http://proxy:8080"}:
            raise requests.exceptions.ProxyError("proxy dead")
        return FakeResp(_OK_HTML, 200)   # direct path (no_proxy bypass) works

    monkeypatch.setattr(common.search_session, "get", fake_get)
    out = http_search_get(name="Bing", url="https://t/", params={}, query="q",
                          parser=_parser_one)
    assert len(out) == 1
    assert len(seen) == 2                     # tried proxy, then direct
    assert proxy_mode_manager._preferred("Bing") == DIRECT   # learned


def test_direct_path_403_block_falls_back_to_proxy(monkeypatch):
    """A datacenter-IP block (403) on the direct path retries via proxy."""
    _set_proxy(monkeypatch)
    # Force DIRECT-first ordering (as if learned), so the 403 hits direct.
    proxy_mode_manager.record_success("Bing", DIRECT)
    seen = []

    def fake_get(url, **kw):
        proxies = kw.get("proxies")
        seen.append(proxies)
        if proxies == {"no_proxy": "*"}:          # direct path
            return FakeResp("blocked", 403)
        return FakeResp(_OK_HTML, 200)            # proxy path

    monkeypatch.setattr(common.search_session, "get", fake_get)
    out = http_search_get(name="Bing", url="https://t/", params={}, query="q",
                          parser=_parser_one)
    assert len(out) == 1
    assert len(seen) == 2
    assert proxy_mode_manager._preferred("Bing") == PROXY


def test_soft_block_200_zero_results_retries_other_path(monkeypatch):
    """A big 200 body that parses to 0 results = soft block → retry."""
    _set_proxy(monkeypatch)
    seen = []

    def fake_get(url, **kw):
        seen.append(kw.get("proxies"))
        if len(seen) == 1:
            return FakeResp("x" * 30_000, 200)   # substantial consent wall
        return FakeResp(_OK_HTML, 200)

    def parser(resp):
        # 0 results for the big consent body, 1 for the real one.
        return [] if len(resp.text) > 25_000 else [make_result("a", "b", "https://a.com", "T")]

    monkeypatch.setattr(common.search_session, "get", fake_get)
    out = http_search_get(name="Brave", url="https://t/", params={}, query="q",
                          parser=parser)
    assert len(out) == 1
    assert len(seen) == 2


def test_read_timeout_does_not_double_attempt(monkeypatch):
    """A read-timeout is NOT a connect failure → no second full attempt."""
    _set_proxy(monkeypatch)
    seen = []

    def fake_get(url, **kw):
        seen.append(kw.get("proxies"))
        raise requests.exceptions.ReadTimeout("slow endpoint")

    monkeypatch.setattr(common.search_session, "get", fake_get)
    with pytest.raises(requests.exceptions.ReadTimeout):
        http_search_get(name="Bing", url="https://t/", params={}, query="q",
                        parser=_parser_one)
    assert len(seen) == 1     # only the first path was tried


def test_both_paths_fail_raises_and_trips_breaker(monkeypatch):
    _set_proxy(monkeypatch)

    def fake_get(url, **kw):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr(common.search_session, "get", fake_get)
    for _ in range(common.engine_circuit.FAIL_THRESHOLD):
        with pytest.raises(requests.exceptions.ConnectionError):
            http_search_get(name="Bing", url="https://t/", params={},
                            query="q", parser=_parser_one)
    assert common.engine_circuit.is_open("Bing") is True


# ── NC bite: with dual-attempt DISABLED, the proxy-path failure is fatal ──

def test_NC_dual_disabled_first_failure_is_fatal(monkeypatch):
    _set_proxy(monkeypatch, dual=False)
    seen = []

    def fake_get(url, **kw):
        seen.append(kw.get("proxies"))
        raise requests.exceptions.ProxyError("proxy dead")

    monkeypatch.setattr(common.search_session, "get", fake_get)
    with pytest.raises(requests.exceptions.ProxyError):
        http_search_get(name="Bing", url="https://t/", params={}, query="q",
                        parser=_parser_one)
    assert len(seen) == 1     # only the proxy path was attempted
