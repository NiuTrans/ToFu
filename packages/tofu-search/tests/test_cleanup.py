"""Tests for the final dead-code sweep:
  - SearXNG instance list is config-driven (configure() overrides it).
  - format.py 0-result diagnostics (no unreachable 'exception' branch).
"""

import pytest

from tofu_search import configure, get_config
from tofu_search.search._common import engine_circuit
from tofu_search.search.engines import searxng
from tofu_search.search.format import format_search_for_tool_response as fmt

# ── SearXNG instances now come from config ──

def test_searxng_default_instances_present():
    insts = get_config().searxng_instances
    assert isinstance(insts, list) and len(insts) >= 1
    assert all(u.startswith('https://') for u in insts)


def test_searxng_uses_config_instances(monkeypatch):
    """search_searxng must read the instance list from config, not a constant."""
    configure(searxng_instances=['https://example.invalid'])
    # Reset the engine breaker so the call isn't skipped.
    engine_circuit.record_success('SearXNG')

    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        raise searxng.requests.Timeout('no real net')

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    # Every instance unreachable = total blockage → raises (engine_errors),
    # never a fake "no matches" [].
    with pytest.raises(searxng.requests.RequestException):
        searxng.search_searxng('test query', max_results=3)
    # Every attempted URL must be the configured instance, none of the old defaults.
    assert seen and all(u.startswith('https://example.invalid') for u in seen)


def test_searxng_prefers_self_host_and_respects_its_engine_config(monkeypatch):
    configure(searxng_url='https://local.example',
              searxng_instances=['https://public.invalid'],
              searxng_engines='')
    calls = []

    class Resp:
        ok = True
        status_code = 200
        headers = {'content-type': 'application/json'}
        text = '{}'

        @staticmethod
        def json():
            return {'results': [{'url': 'https://hit.example/a', 'title': 'hit'}]}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get('params', {})))
        return Resp()

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    out = searxng.search_searxng('topic')
    assert out
    assert calls[0][0].startswith('https://local.example/')
    assert 'engines' not in calls[0][1]


def test_searxng_configured_engines_survive_html_fallback(monkeypatch):
    configure(searxng_url='https://local.example',
              searxng_instances=[], searxng_engines='mwmbl,pubmed')
    calls = []

    class Resp:
        ok = False
        status_code = 403
        headers = {'content-type': 'text/html'}
        text = ''

        @staticmethod
        def json():
            return {}

    def fake_get(url, **kwargs):
        calls.append(kwargs.get('params', {}))
        return Resp()

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    engine_circuit.record_success('SearXNG')
    # 403 on both JSON and HTML attempts = blocked everywhere → raises.
    with pytest.raises(searxng.requests.RequestException):
        searxng.search_searxng('topic')
    assert calls[0]['engines'] == 'mwmbl,pubmed'
    assert calls[1]['engines'] == 'mwmbl,pubmed'


# ── format.py 0-result diagnostics ──

def test_format_empty_no_diag():
    assert fmt([]) == "No search results found."


def test_format_network_error():
    out = fmt([], {'reason': 'network_error', 'reason_detail': 'x'})
    assert 'network' in out.lower()


def test_format_partial_network_error():
    out = fmt([], {'reason': 'partial_network_error', 'reason_detail': '2/6 failed'})
    assert '2/6 failed' in out


def test_format_no_matches():
    out = fmt([], {'reason': 'no_matches', 'reason_detail': 'nothing'})
    assert 'rephras' in out.lower()


def test_format_unknown_reason_falls_through():
    # An unexpected reason must hit the generic 'no matches' fallback, not crash.
    out = fmt([], {'reason': 'something_new', 'reason_detail': 'd'})
    assert isinstance(out, str) and out


def test_format_with_results():
    results = [{'title': 'T', 'url': 'https://e.com', 'source': 'Bing',
                'full_content': 'body text here'}]
    out = fmt(results)
    assert 'T' in out and 'https://e.com' in out and 'body text here' in out
