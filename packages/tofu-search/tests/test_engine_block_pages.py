"""A blocked search endpoint is unavailable evidence, never a no-match."""

import pytest
import requests

from tofu_search.config import get_config
from tofu_search.search import _common as common
from tofu_search.search.engines.ddg import _parse_ddg_html
from tofu_search.search.proxy_mode import _reset_proxy_mode_manager

pytestmark = pytest.mark.unit

CHALLENGE = '<html><form id="challenge-form" action="/anomaly.js">Verify</form></html>'
RESULT = ('<div class="result"><a class="result__a" href="https://python.org">'
          'Python CAPTCHA example</a><span class="result__snippet">captcha tutorial</span></div>')


def response(text="", status=200, headers=None):
    value = requests.Response()
    value.status_code = status
    value._content = text.encode()
    value.headers.update(headers or {})
    return value


@pytest.fixture(autouse=True)
def isolated_transport_state(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(common, "engine_circuit", common._EngineCircuit())
    monkeypatch.setattr(common.host_throttle, "wait", lambda *a, **kw: 0)
    monkeypatch.setattr(common.time, "sleep", lambda *a: None)
    _reset_proxy_mode_manager()
    yield
    _reset_proxy_mode_manager()


def search(parser=_parse_ddg_html):
    return common.http_search_get(
        name="DDG-HTML", url="https://html.duckduckgo.com/html/",
        params={"q": "Python"}, query="Python", parser=parser, on_ratelimit_retry=True)


def test_repeated_accepted_response_is_failure_after_bounded_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(common.search_session, "get",
                        lambda *a, **kw: calls.append(1) or response(CHALLENGE, 202))
    with pytest.raises(requests.RequestException):
        search()
    assert len(calls) == 2
    assert common.engine_circuit._state["DDG-HTML"]["fails"] == 1


def test_accepted_then_real_results_recovers(monkeypatch):
    replies = iter([response(CHALLENGE, 202), response(RESULT)])
    monkeypatch.setattr(common.search_session, "get", lambda *a, **kw: next(replies))
    assert search()[0]["url"] == "https://python.org"
    assert not common.engine_circuit.is_open("DDG-HTML")


def test_small_ddg_challenge_is_not_a_successful_empty_search(monkeypatch):
    monkeypatch.setattr(common.search_session, "get", lambda *a, **kw: response(CHALLENGE))
    with pytest.raises(requests.RequestException):
        search()


def test_explicit_challenge_header_cannot_be_overridden_by_parser_output(monkeypatch):
    monkeypatch.setattr(common.search_session, "get",
                        lambda *a, **kw: response(RESULT, headers={"cf-mitigated": "challenge"}))
    with pytest.raises(requests.RequestException):
        search()


def test_genuine_empty_and_results_about_captcha_remain_valid(monkeypatch):
    replies = iter([response("<p>No matches for CAPTCHA examples.</p>"), response(RESULT)])
    monkeypatch.setattr(common.search_session, "get", lambda *a, **kw: next(replies))
    assert search() == []
    assert search()[0]["title"] == "Python CAPTCHA example"


def test_small_challenge_reaches_an_already_configured_alternate(monkeypatch):
    import tofu_search.config as config

    monkeypatch.setattr(config, "_global_config", get_config().copy(
        proxy_url="http://proxy.example:8080", proxy_dual_attempt=True))
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs.get("proxies"))
        return response(CHALLENGE if len(calls) == 1 else RESULT)

    monkeypatch.setattr(common.search_session, "get", get)
    assert search()[0]["url"] == "https://python.org"
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_orchestrator_reports_challenge_as_failed_coverage(monkeypatch):
    from tofu_search import perform_web_search

    monkeypatch.setattr(common.search_session, "get", lambda *a, **kw: response(CHALLENGE))
    results = perform_web_search(
        "Python", engines=["DDG-HTML"], fetch_pages=False, filter_pages=False,
        config=get_config().copy(search_deadline_secs=1))
    assert not results
    assert "DDG-HTML" in results._search_diag["engine_errors"]
    assert "DDG-HTML" not in results._search_diag["engine_empty"]
