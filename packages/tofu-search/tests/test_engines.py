"""Offline tests for the bs4-based engine parsers and the shared HTTP envelope.

No network: parsers run against representative HTML fixtures, and
http_search_get is exercised with a monkeypatched session.get.
"""

import base64

import pytest
import requests

from tofu_search.search import _common as common
from tofu_search.search._common import _EngineCircuit, http_search_get, make_result
from tofu_search.search.engines.bing import _bing_decode_url, _parse_bing
from tofu_search.search.engines.brave import _parse_brave
from tofu_search.search.engines.ddg import _parse_ddg_html
from tofu_search.search.engines.searxng import _searxng_parse_html, _searxng_parse_json


class FakeResp:
    """Minimal stand-in for requests.Response."""
    def __init__(self, text="", status_code=200, json_data=None, headers=None):
        self.text = text
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


# ── make_result ──

def test_make_result_cleans_and_caps():
    r = make_result("  <b>Title</b>  ", "<p>snip&amp;pet</p>", "https://x.com", "Eng")
    assert r["title"] == "Title"
    assert r["snippet"] == "snip&pet"
    assert r["url"] == "https://x.com"
    assert r["source"] == "Eng"


def test_make_result_length_caps():
    r = make_result("t" * 500, "s" * 1000, "https://x.com", "Eng")
    assert len(r["title"]) == 200
    assert len(r["snippet"]) == 500


# ── Bing parser ──

BING_HTML = """
<html><body>
<ol id="b_results">
  <li class="b_algo"><h2><a href="https://example.com/a">First <b>Result</b></a></h2>
      <div class="b_caption"><p>Snippet one about things.</p></div></li>
  <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&u=a1aHR0cHM6Ly9kZWNvZGVkLmNvbS9w">Wrapped</a></h2>
      <div class="b_caption"><p>Second snippet.</p></div></li>
</ol></body></html>
"""


def test_bing_parser_extracts_results():
    out = _parse_bing(FakeResp(BING_HTML))
    assert len(out) == 2
    assert out[0]["title"] == "First Result"
    assert out[0]["url"] == "https://example.com/a"
    assert out[0]["snippet"] == "Snippet one about things."
    assert all(r["source"] == "Bing" for r in out)


def test_bing_decodes_ck_redirect():
    target = "https://decoded.com/page"
    payload = base64.b64encode(target.encode()).decode().rstrip("=")
    raw = f"https://www.bing.com/ck/a?!&&u=a1{payload}"
    assert _bing_decode_url(raw) == target


def test_bing_drops_undecodable_redirect():
    assert _bing_decode_url("https://www.bing.com/ck/a?u=garbage") is None


def test_bing_parser_empty_on_no_blocks():
    assert _parse_bing(FakeResp("<html><body>no results</body></html>")) == []


# ── Brave parser ──

BRAVE_HTML = """
<html><body>
<div data-pos="1">
  <a href="https://brave-result.com/x" class="h svelte-abc">
    <div class="title search-snippet-title svelte-abc" title="Brave Title One"></div></a>
  <div class="snippet-content svelte-x"><div class="content svelte-y">2 days ago - A useful snippet.</div></div>
</div>
<div data-pos="2">
  <a href="https://search.brave.com/internal" class="svelte-z">Ad</a>
</div>
</body></html>
"""


def test_brave_parser_extracts_and_skips_internal():
    out = _parse_brave(FakeResp(BRAVE_HTML))
    assert len(out) == 1
    assert out[0]["url"] == "https://brave-result.com/x"
    assert out[0]["title"] == "Brave Title One"
    assert "2 days ago" not in out[0]["snippet"]
    assert "useful snippet" in out[0]["snippet"]


# ── DDG HTML parser ──

DDG_HTML = """
<html><body>
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.com%2Fpage&rut=abc">Real Title</a>
  <a class="result__snippet">A descriptive snippet here.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/y.js?ad_provider=x">Ad result</a>
</div>
</body></html>
"""


def test_ddg_parser_decodes_uddg_and_skips_ads():
    out = _parse_ddg_html(FakeResp(DDG_HTML))
    assert len(out) == 1
    assert out[0]["url"] == "https://real.com/page"
    assert out[0]["title"] == "Real Title"
    assert out[0]["snippet"] == "A descriptive snippet here."


# ── SearXNG parsers ──

SEARXNG_HTML = """
<html><body>
<article class="result result-default">
  <h3><a href="https://sx.com/one">SearXNG One</a></h3>
  <p class="content">Content for one.</p>
</article>
<article class="result result-default">
  <h3><a href="https://sx.com/two">SearXNG Two</a></h3>
  <p class="content">Content for two.</p>
</article>
</body></html>
"""


def test_searxng_html_parser():
    out = _searxng_parse_html(SEARXNG_HTML, max_results=6)
    assert len(out) == 2
    assert out[0]["url"] == "https://sx.com/one"
    assert out[1]["title"] == "SearXNG Two"


def test_searxng_html_respects_max_results():
    out = _searxng_parse_html(SEARXNG_HTML, max_results=1)
    assert len(out) == 1


def test_searxng_json_parser():
    data = {"results": [
        {"url": "https://j.com/a", "title": "JA", "content": "ca",
         "engines": ["mwmbl", "brave"]},
        {"url": "", "title": "skip-no-url", "content": "x"},
    ]}
    out = _searxng_parse_json(data, max_results=6)
    assert len(out) == 1
    assert out[0]["url"] == "https://j.com/a"
    assert out[0]["upstream_engines"] == ["mwmbl", "brave"]
    assert out[0]["engine_count"] == 2


# ── Engine circuit breaker ──

def test_circuit_trips_after_threshold():
    cb = _EngineCircuit()
    assert cb.is_open("Bing") is False
    for _ in range(cb.FAIL_THRESHOLD):
        cb.record_failure("Bing")
    assert cb.is_open("Bing") is True


def test_circuit_success_resets():
    cb = _EngineCircuit()
    cb.record_failure("Brave")
    cb.record_failure("Brave")
    cb.record_success("Brave")
    cb.record_failure("Brave")
    assert cb.is_open("Brave") is False  # counter was reset


def test_circuit_cooldown_expiry(monkeypatch):
    cb = _EngineCircuit()
    for _ in range(cb.FAIL_THRESHOLD):
        cb.record_failure("DDG")
    assert cb.is_open("DDG") is True
    # Fast-forward past the cooldown window.
    real_time = common.time.time()
    monkeypatch.setattr(common.time, "time", lambda: real_time + cb.COOLDOWN + 1)
    assert cb.is_open("DDG") is False


# ── http_search_get envelope ──

@pytest.fixture(autouse=True)
def _fresh_engine_circuit(monkeypatch):
    """Each test gets a clean breaker so cross-test state doesn't leak."""
    monkeypatch.setattr(common, "engine_circuit", _EngineCircuit())


def test_http_search_get_happy_path(monkeypatch):
    def fake_get(url, **kw):
        return FakeResp("<article class='result'>x</article>", 200)
    monkeypatch.setattr(common.search_session, "get", fake_get)

    out = http_search_get(
        name="T", url="https://t/", params={}, query="q",
        parser=lambda resp: [make_result("a", "b", "https://a.com", "T")],
    )
    assert len(out) == 1
    assert common.engine_circuit.is_open("T") is False


def test_http_search_get_trips_circuit_on_repeated_failure(monkeypatch):
    def boom(url, **kw):
        raise requests.Timeout("slow")
    monkeypatch.setattr(common.search_session, "get", boom)

    for _ in range(common.engine_circuit.FAIL_THRESHOLD):
        with pytest.raises(requests.Timeout):
            http_search_get(name="T", url="https://t/", params={},
                            query="q", parser=lambda r: [])
    assert common.engine_circuit.is_open("T") is True

    # Now that it's open, the engine is skipped without calling get().
    called = []
    monkeypatch.setattr(common.search_session, "get",
                        lambda url, **kw: called.append(1) or FakeResp("", 200))
    assert http_search_get(name="T", url="https://t/", params={},
                           query="q", parser=lambda r: []) == []
    assert called == []


def test_http_search_get_http_error_counts_as_failure(monkeypatch):
    monkeypatch.setattr(common.search_session, "get",
                        lambda url, **kw: FakeResp("", 503))
    with pytest.raises(requests.exceptions.HTTPError):
        http_search_get(name="T", url="https://t/", params={}, query="q",
                        parser=lambda r: [])
    # One 503 recorded a failure (not yet tripped at threshold 3).
    assert common.engine_circuit._state["T"]["fails"] == 1


def test_engine_transport_failure_classified_as_network_error(monkeypatch):
    """Every engine dying at the transport level must surface as
    ``network_error`` — never ``no_matches``.

    Regression: a dead proxy + no direct egress made all engines raise
    ConnectionError, but the exceptions were swallowed into ``[]`` and the
    diag reported 'no_matches', so the model told the user "nothing found"
    (and the UI showed a fake single result) instead of "search is broken".
    """
    from tofu_search.search import orchestrator as orch

    def boom(*a, **kw):
        raise requests.exceptions.ConnectionError("dns dead")

    for name in ('search_ddg_html', 'search_brave', 'search_bing',
                 'search_ddg_api', 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(orch, name, boom)

    res = orch.perform_web_search('anything', fetch_pages=False,
                                  filter_pages=False, rerank=False)
    assert list(res) == []
    diag = getattr(res, '_search_diag', None)
    assert diag is not None, 'expected a search diag on total failure'
    assert diag['reason'] == 'network_error', diag
    assert diag['engine_empty'] == [], diag
    assert {'DDG-HTML', 'Brave', 'Bing', 'DDG-API', 'SearXNG',
            'Marginalia'} <= set(diag['engine_errors']), diag
