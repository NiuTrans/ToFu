"""DDG's anonymous endpoints share one budget, query and engine policy."""

from types import SimpleNamespace

import pytest
import requests

from tofu_search import SearchConfig, perform_web_search
from tofu_search.config import bind_call_config
from tofu_search.search import _common
from tofu_search.search.engines import ddg
from tofu_search.search.proxy_mode import ProxyModeManager

pytestmark = pytest.mark.unit
QUERY = 'site:docs.python.org "TaskGroup" cancellation'
HTML_URL = 'https://html.duckduckgo.com/html/'
LITE_URL = 'https://lite.duckduckgo.com/lite/'
LITE = """
<table>
<tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio-task.html">TaskGroup cancellation</a></td></tr>
<tr><td class="result-snippet">Cancellation propagates between tasks.</td></tr>
<tr><td><span class="link-text">docs.python.org</span></td></tr>
</table>
"""


def response(text=LITE, status=200):
    value = requests.Response()
    value.status_code = status
    value._content = text.encode()
    value.encoding = 'utf-8'
    return value


@pytest.fixture
def wire(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(_common, 'time', SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(_common, 'engine_circuit', _common._EngineCircuit())
    monkeypatch.setattr(_common, 'proxy_mode_manager', ProxyModeManager())
    monkeypatch.setattr(_common.host_throttle, 'wait', lambda *a, **kw: 0)
    seen = SimpleNamespace(calls=[], outcomes=[], clock=clock)
    def get(url, **kwargs):
        seen.calls.append({'url': url, **kwargs})
        outcome = seen.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(ddg, '_search_http2_get', get)
    return seen


def config(**kwargs):
    return SearchConfig(proxy_url='http://chosen.invalid:8123',
                        proxy_dual_attempt=False, min_request_interval_ms=0, **kwargs)


@pytest.mark.parametrize('first', [
    requests.ReadTimeout('slow origin'),
    response(status=202), response(status=403), response(status=429),
    response(status=503), response('<form id="challenge-form"></form>'),
])
def test_lite_recovers_an_unavailable_html_endpoint_without_changing_scope(wire, first):
    wire.outcomes = [first, response()]
    rows = perform_web_search(
        QUERY, engines=['DDG-HTML'], freshness='month', fetch_pages=False,
        filter_pages=False, config=config(search_deadline_secs=5))
    assert rows and rows[0]['url'] == 'https://docs.python.org/3/library/asyncio-task.html'
    assert rows[0]['snippet'] == 'Cancellation propagates between tasks.'
    assert [call['url'] for call in wire.calls] == [HTML_URL, LITE_URL]
    assert all(call['params'] == {'q': QUERY, 'df': 'm'} for call in wire.calls)
    assert len({call['deadline'] for call in wire.calls}) == 1
    assert all(call['proxies'] == {'http': 'http://chosen.invalid:8123',
                                   'https': 'http://chosen.invalid:8123'} for call in wire.calls)
    assert rows._search_diag['engine_ok'] == ['DDG-HTML']
    assert not rows._search_diag['engine_errors']


def test_recovery_spends_only_the_original_remaining_budget(wire):
    def slow_origin():
        wire.clock.now += 4
        return requests.ReadTimeout()
    wire.outcomes = [slow_origin, response()]
    with bind_call_config(config(search_deadline_secs=5)):
        assert ddg.search_ddg_html(QUERY)
    assert wire.calls[1]['timeout'] == 1


def test_exhausted_budget_cannot_start_a_lite_request(wire):
    def slow_origin():
        wire.clock.now += 5
        return requests.ReadTimeout()
    wire.outcomes = [slow_origin, response()]
    with bind_call_config(config(search_deadline_secs=5)):
        with pytest.raises(requests.Timeout):
            ddg.search_ddg_html(QUERY)
    assert len(wire.calls) == 1
    assert _common.engine_circuit._state['DDG-HTML']['fails'] == 1


def test_repeated_challenges_are_one_failed_engine_observation(wire):
    wire.outcomes = [response(status=202), response(status=202)]
    with bind_call_config(config()):
        with pytest.raises(requests.HTTPError):
            ddg.search_ddg_html(QUERY)
    assert [call['url'] for call in wire.calls] == [HTML_URL, LITE_URL]
    assert _common.engine_circuit._state['DDG-HTML']['fails'] == 1


@pytest.mark.parametrize('first', [
    requests.exceptions.ProxyError('unreachable proxy'),
    response(status=407), response(status=400),
    requests.RequestException('invalid transport configuration'),
])
def test_proxy_and_permanent_errors_do_not_repeat_on_another_endpoint(wire, first):
    wire.outcomes = [first]
    with bind_call_config(config()):
        with pytest.raises(requests.RequestException):
            ddg.search_ddg_html(QUERY)
    assert len(wire.calls) == 1


@pytest.mark.parametrize('text', [
    '<div class="result"><a class="result__a" href="https://docs.python.org/">Python</a></div>',
    '<div class="no-results">No results found</div>',
])
def test_completed_primary_result_or_empty_does_not_repeat(wire, text):
    wire.outcomes = [response(text)]
    with bind_call_config(config()):
        ddg.search_ddg_html(QUERY)
    assert len(wire.calls) == 1


def test_lite_snippets_never_cross_the_next_result_and_ads_are_excluded():
    html = """<table>
    <tr><td><a class="result-link" href="https://duckduckgo.com/y.js?ad_provider=test">Ad</a></td></tr>
    <tr><td class="result-snippet">Sponsored claim.</td></tr>
    <tr><td><a class="result-link" href="https://example.org/one">One</a></td></tr>
    <tr><td><span class="link-text">example.org/one</span></td></tr>
    <tr><td><a class="result-link" href="https://example.org/two">Two</a></td></tr>
    <tr><td class="result-snippet">Only the second source says this.</td></tr>
    </table>"""
    rows = ddg.parse_ddg_html_text(html, source='DuckDuckGo (via browser)')
    assert [(r['title'], r['snippet']) for r in rows] == [
        ('One', ''), ('Two', 'Only the second source says this.')]
    assert all(r['source'] == 'DuckDuckGo (via browser)' for r in rows)
    assert len(ddg.parse_ddg_html_text(html, max_results=1)) == 1


@pytest.mark.parametrize('css', ['result__a', 'result-link'])
def test_ddg_redirect_decode_preserves_destination_escapes_and_ordinary_parameters(css):
    html = f"""<a class="{css}" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2F%252F%3Fq%3Da%2526b">Encoded</a>
    <a class="{css}" href="https://example.org/?uddg=ordinary">Direct</a>
    <a class="{css}" href="http-junk:example.org">Invalid</a>"""
    rows = ddg.parse_ddg_html_text(html)
    assert [r['url'] for r in rows] == [
        'https://example.org/%2F?q=a%26b', 'https://example.org/?uddg=ordinary']
