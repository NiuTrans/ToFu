"""Free metasearch recovery preserves constraints, provenance and honest gaps."""

import json
from types import SimpleNamespace

import pytest
import requests

from tofu_search import SearchConfig, format_results, perform_web_search
from tofu_search.config import bind_call_config
from tofu_search.search import _common
from tofu_search.search.engines import searxng

pytestmark = pytest.mark.unit
QUERY = "Python asyncio TaskGroup cancellation"
GOOD = {"title": "Python TaskGroup cancellation", "url": "https://docs.python.org/taskgroup",
        "content": "Python asyncio TaskGroup propagates cancellation to siblings."}
WEAK = {"title": "Welcome to Python", "url": "https://www.python.org",
        "content": "Python is a programming language."}
EMPTY = ('<div id="results"><div id="urls"><div class="dialog-error-block" role="alert">'
         'No matching documents.</div></div></div>')


class Response:
    def __init__(self, payload=None, *, text="", status=200, headers=None, chunks=None):
        self.status_code = status
        self.text = json.dumps(payload) if payload is not None else text
        self.headers = {"content-type": "application/json" if payload is not None else "text/html",
                        **(headers or {})}
        self.closed = False
        self.chunks = chunks if chunks is not None else [self.text.encode()]
    @property
    def ok(self):
        return 200 <= self.status_code < 300
    def json(self):
        return json.loads(self.text)
    def iter_content(self, chunk_size):
        yield from self.chunks
    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(searxng, "engine_circuit", _common._EngineCircuit())
    monkeypatch.setattr(searxng.random, "shuffle", lambda rows: None)


def use_responses(monkeypatch, responses):
    calls = []
    queue = iter(responses)
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(queue)
    monkeypatch.setattr(searxng.search_session, "get", get)
    return calls


def search(instances=("https://first.invalid", "https://second.invalid"), query=QUERY, **kwargs):
    with bind_call_config(SearchConfig(searxng_url="", searxng_instances=list(instances), **kwargs)):
        return searxng.search_searxng(query)


def test_weak_first_instance_does_not_hide_later_evidence(monkeypatch):
    first, second = Response({"results": [WEAK]}), Response({"results": [GOOD]})
    calls = use_responses(monkeypatch, [first, second])
    rows = search()
    assert rows[0]["url"] == GOOD["url"]
    assert len(calls) == 2 and all(r.closed for r in (first, second))
    assert [call[1]["params"]["q"] for call in calls] == [QUERY, QUERY]


def test_uncertain_candidates_survive_when_no_better_source_is_available(monkeypatch):
    use_responses(monkeypatch, [Response({"results": [WEAK]}), Response(status=429)])
    rows = search()
    assert rows[0]["url"] == WEAK["url"]
    assert "instance_failures" in rows.coverage_warnings


def test_empty_json_does_not_repeat_same_instance_as_html(monkeypatch):
    calls = use_responses(monkeypatch, [Response({"results": []}), Response({"results": [GOOD]})])
    assert search()[0]["url"] == GOOD["url"]
    assert [url for url, _ in calls] == ["https://first.invalid/search",
                                       "https://second.invalid/search"]


@pytest.mark.parametrize("response", [
    Response({"results": [], "unresponsive_engines": [["bing", "timeout"]]}),
    Response({"error": "server is unavailable"}),
    Response({"results": "not a list"}),
    Response({"results": [{"url": "javascript:bad", "title": "broken"}]}),
    Response(text="<html>Maintenance</html>"),
    Response(text=EMPTY, headers={"cf-mitigated": "challenge"}),
    Response(text=EMPTY, status=202),
    Response(text=EMPTY + '<div id="engines_msg"><td class="response-error">timeout</td></div>'),
])
def test_unavailable_search_is_never_a_genuine_empty(monkeypatch, response):
    use_responses(monkeypatch, [response, response])
    with pytest.raises(requests.RequestException):
        search(instances=("https://first.invalid",))
    assert response.closed


def test_valid_html_empty_does_not_depend_on_language_or_page_size(monkeypatch):
    response = Response(text=EMPTY.replace("No matching documents.", "没有匹配的结果。"))
    calls = use_responses(monkeypatch, [response])
    assert search(instances=("https://first.invalid",)) == []
    assert len(calls) == 1 and response.closed


def test_html_topic_about_bot_walls_is_valid_evidence(monkeypatch):
    html = ('<div id="results"><article class="result"><h3>'
            '<a href="https://example.org/challenge">Cloudflare just a moment diagnosis</a>'
            '</h3><p class="content">Why making sure you are human blocks searches</p>'
            '</article></div>')
    use_responses(monkeypatch, [Response(text=html)])
    rows = search(instances=("https://first.invalid",), query="Cloudflare challenge diagnosis")
    assert rows[0]["url"] == "https://example.org/challenge"


def test_partial_upstream_failure_reaches_public_coverage_output(monkeypatch):
    use_responses(monkeypatch, [Response({"results": [GOOD],
        "unresponsive_engines": [["bing", "private upstream response"]], "query": QUERY})])
    rows = perform_web_search(
        QUERY, engines=["SearXNG"], fetch_pages=False,
        config=SearchConfig(searxng_url="", searxng_instances=["https://first.invalid"]))
    assert rows and rows._search_diag["engine_warnings"]["SearXNG"] == ["upstream_unavailable"]
    text = format_results(rows, query=QUERY)
    assert "upstream_unavailable" in text
    assert "private upstream response" not in text


def test_failover_respects_site_scope_before_accepting_instance(monkeypatch):
    outside = {**GOOD, "url": "https://outside.invalid/taskgroup"}
    calls = use_responses(monkeypatch, [Response({"results": [outside]}), Response({"results": [GOOD]})])
    rows = search(query=QUERY + " site:docs.python.org")
    assert rows[0]["url"] == GOOD["url"] and len(calls) == 2


def test_oversize_response_is_closed_and_other_instance_can_recover(monkeypatch):
    oversized = Response(chunks=[b"x" * (2 * 1024 * 1024 + 1)])
    calls = use_responses(monkeypatch, [oversized, Response({"results": [GOOD]})])
    assert search()[0]["url"] == GOOD["url"]
    assert len(calls) == 2 and oversized.closed


def test_remaining_deadline_prevents_another_instance_or_format_attempt(monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(searxng, "time", SimpleNamespace(monotonic=lambda: clock.now))
    calls = []
    def timeout(url, **kwargs):
        calls.append(kwargs["timeout"])
        clock.now += kwargs["timeout"]
        raise requests.Timeout()
    monkeypatch.setattr(searxng.search_session, "get", timeout)
    with pytest.raises(requests.RequestException):
        search(search_deadline_secs=0.25)
    assert calls == [0.25]


def test_existing_freshness_and_engine_policy_is_preserved_on_failover(monkeypatch):
    calls = use_responses(monkeypatch, [Response(status=403), Response(text=EMPTY),
                                       Response({"results": [GOOD]})])
    with bind_call_config(SearchConfig(searxng_url="", searxng_instances=[
            "https://first.invalid", "https://second.invalid"], searxng_engines="google,brave")):
        searxng.search_searxng(QUERY, freshness="month")
    assert all(call[1]["params"]["time_range"] == "month" for call in calls)
    assert all(call[1]["params"]["engines"] == "google,brave" for call in calls)
