"""Focused reads preserve source spans, transport authority and raw defaults."""
from types import SimpleNamespace

import pytest

import tofu_search
from tofu_search.fetch import core, utils
from tofu_search.fetch.http import HttpError

pytestmark = pytest.mark.unit


@pytest.fixture
def source(monkeypatch):
    fact = 'Quasar throughput is exactly 9137 records per second; this is not latency.'
    body = 'Manual introduction.\n\n' + 'Ordinary unrelated background.\n\n' * 2400 + fact
    calls = []

    def read(url, *_args, **_kwargs):
        calls.append(url)
        return SimpleNamespace(headers={'Content-Type': 'text/plain'}, encoding='utf-8'), body.encode()

    monkeypatch.setattr(core, '_do_request', read)
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    return body, fact, calls


@pytest.mark.parametrize('entry', ['fetch_page_content', 'fetch_url'])
def test_public_focused_fetch_finds_late_evidence_in_one_request(source, entry):
    body, fact, calls = source
    result = getattr(tofu_search, entry)(
        'https://example.com/manual', max_chars=900, query='quasar throughput')
    assert fact in result
    assert len(result) <= 900
    assert all(piece in body for piece in result.split('\n\n[... omitted ...]\n\n'))
    assert calls == ['https://example.com/manual']


def test_focused_fetch_bypasses_partial_cache_and_does_not_replace_raw_cache(source):
    _, fact, calls = source
    url = 'https://example.com/cached'
    core._fetch_cache.put(url, 'Previously cached source prefix only.')
    result = core.fetch_page_content(url, max_chars=500, query='quasar throughput')
    assert fact in result
    assert calls == [url]
    assert core._fetch_cache.get(url) == 'Previously cached source prefix only.'


@pytest.mark.parametrize('query', ['', '  ', None])
def test_empty_query_retains_default_cache_behavior(source, query):
    _, _, calls = source
    url = 'https://example.com/cached'
    cached = 'Complete cached content.'
    core._fetch_cache.put(url, cached)
    assert core.fetch_page_content(url, max_chars=100, query=query) == cached
    assert calls == []


def test_uncapped_query_read_retains_entire_original_source(source):
    body, _, calls = source
    assert core.fetch_page_content('https://example.com/manual', max_chars=0,
                                   query='quasar throughput') == body
    assert len(calls) == 1


def test_uncapped_config_default_retains_source_for_a_focused_read(source):
    from tofu_search.config import SearchConfig, bind_call_config
    body, _, calls = source
    with bind_call_config(SearchConfig(fetch_max_chars_search=0)):
        assert core.fetch_page_content('https://example.com/manual',
                                       query='quasar throughput') == body
    assert len(calls) == 1


def test_focused_read_reports_partial_output_and_next_read_clears_it(source):
    _, _, _calls = source
    diagnostics = {'caller_field': 'retained'}
    result = core.fetch_page_content('https://example.com/manual', max_chars=500,
                                    query='quasar throughput', diag=diagnostics)
    assert result and diagnostics['truncated'] is True
    core._fetch_cache.put('https://example.com/short', 'Short complete source.')
    assert core.fetch_page_content('https://example.com/short', max_chars=500,
                                   diag=diagnostics) == 'Short complete source.'
    assert 'truncated' not in diagnostics
    assert diagnostics['caller_field'] == 'retained'


def test_focused_read_keeps_http_failure_and_does_not_select_error_text(monkeypatch):
    calls = []

    def refused(url, *_args, **_kwargs):
        calls.append(url)
        raise HttpError(403, url)

    monkeypatch.setattr(core, '_do_request', refused)
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_try_browser_fetch', lambda *_a, **_kw: None)
    diagnostics = {}
    assert core.fetch_page_content('https://example.com/refused', max_chars=500,
                                   query='quasar', diag=diagnostics) is None
    assert diagnostics['reason'] == 'http_403'
    assert diagnostics['http_status'] == 403
    assert len(calls) == 1


@pytest.mark.parametrize('route', ['reader', 'browser'])
def test_focused_read_projects_successful_optional_source_routes(monkeypatch, route):
    fact = 'Quasar throughput is exactly 9137 records per second.'
    body = 'Unrelated background.\n\n' * 1500 + fact
    calls = []

    def observed_source(*_a, **kwargs):
        calls.append(kwargs)
        return body

    def refused(url, *_a, **_kw):
        raise HttpError(403, url)

    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    monkeypatch.setattr(core, '_get_reader', lambda _url: (
        SimpleNamespace(read=observed_source) if route == 'reader' else None))
    monkeypatch.setattr(core, '_do_request', refused)
    monkeypatch.setattr(core, '_try_browser_fetch', observed_source)
    diagnostics = {}
    result = core.fetch_page_content('https://example.com/optional', max_chars=500,
                                    query='quasar throughput', diag=diagnostics)
    assert fact in result and len(result) <= 500
    assert len(calls) == 1
    assert 'reason' not in diagnostics


def test_focused_read_stays_inside_the_requested_html_fragment(monkeypatch):
    html = ('<html><main><h1>Manual</h1><section id="selected"><h2>Selected</h2>'
            '<p>The selected interval is 17 seconds.</p></section>'
            '<h2>Quasar</h2><p>Quasar throughput is 9137.</p></main></html>')
    monkeypatch.setattr(core, '_do_request', lambda *_a, **_kw: (
        SimpleNamespace(headers={'Content-Type': 'text/html'}, encoding='utf-8'), html.encode()))
    monkeypatch.setattr(core, '_fetch_cache', utils._FetchCache())
    result = core.fetch_page_content('https://example.com/manual#selected',
                                    max_chars=500, query='quasar throughput')
    assert '17 seconds' in result
    assert '9137' not in result


def test_invalid_query_fails_before_transport(source):
    _, _, calls = source
    with pytest.raises(TypeError, match='query'):
        core.fetch_page_content('https://example.com/manual', query={'not': 'text'})
    assert calls == []
