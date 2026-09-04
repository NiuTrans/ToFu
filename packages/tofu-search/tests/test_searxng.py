"""SearXNG engine: blockage vs genuine-no-match classification (offline).

Incident 2026-08-21: every default public instance was unusable from the
proxy egress (429 rate-limits, SSL error, connect timeout, Anubis PoW
bot-wall served as 200) — yet the engine returned [] for ALL of it, so the
orchestrator classified SearXNG as engine_empty ("no matches") instead of
engine_errors. The harness web_search summary showed "the rest returned no
matches" — a dead engine masquerading as a survivor.

Contract (mirrors _common.http_search_get): [] ONLY for a genuine no-match
or a circuit-breaker skip; total blockage RAISES requests.RequestException.
"""

import pytest
import requests

import tofu_search.config as _config
from tofu_search.search._common import _EngineCircuit
from tofu_search.search.engines import searxng


class FakeResp:
    def __init__(self, status_code=200, text='', content_type='text/html',
                 json_data=None):
        self.status_code = status_code
        self.text = text
        self.headers = {'content-type': content_type}
        self._json_data = json_data

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json_data or {}


_ANUBIS = ('<html><head><title>Making sure you&#39;re not a bot!</title></head>'
           '<body>challenge</body></html>')
_REAL_EMPTY_PAGE = '<html><body>%s</body></html>' % ('no results here ' * 60)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(_config, '_global_config', _config.SearchConfig(
        searxng_url='', searxng_instances=['https://a.invalid', 'https://b.invalid']))
    monkeypatch.setattr(searxng, 'engine_circuit', _EngineCircuit())


def test_all_429_raises_not_empty(monkeypatch):
    def fake_get(url, **kw):
        return FakeResp(429, 'rate limited')

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    with pytest.raises(requests.RequestException, match='blocked'):
        searxng.search_searxng('q')


def test_bot_wall_200_counts_as_blocked(monkeypatch):
    def fake_get(url, **kw):
        return FakeResp(200, _ANUBIS)

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    with pytest.raises(requests.RequestException, match='bot-wall'):
        searxng.search_searxng('q')


def test_genuine_empty_page_returns_empty_list(monkeypatch):
    def fake_get(url, **kw):
        return FakeResp(200, _REAL_EMPTY_PAGE)

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    assert searxng.search_searxng('q') == []
    # A genuine answer resets the circuit counter (no failure recorded).
    assert searxng.engine_circuit._state == {}


def test_json_results_returned_and_reset_circuit(monkeypatch):
    payload = {'results': [
        {'url': 'https://hit.example/a', 'title': 'A', 'content': 'x'},
        {'url': 'https://hit.example/b', 'title': 'B', 'content': 'y'},
    ]}

    def fake_get(url, **kw):
        return FakeResp(200, '', 'application/json', payload)

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    out = searxng.search_searxng('q')
    assert [r['url'] for r in out] == ['https://hit.example/a',
                                       'https://hit.example/b']


def test_circuit_trips_after_repeated_total_blockage(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResp(429, 'rate limited')

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    for _ in range(searxng.engine_circuit.FAIL_THRESHOLD):
        with pytest.raises(requests.RequestException):
            searxng.search_searxng('q')
    assert searxng.engine_circuit.is_open('SearXNG') is True
    n = len(calls)
    # Benched: skipped BEFORE any network, returns [] like other engines.
    assert searxng.search_searxng('q') == []
    assert len(calls) == n


def test_unconfigured_engine_returns_empty(monkeypatch):
    monkeypatch.setattr(_config, '_global_config', _config.SearchConfig(
        searxng_url='', searxng_instances=[]))
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return FakeResp(200, _REAL_EMPTY_PAGE)

    monkeypatch.setattr(searxng.search_session, 'get', fake_get)
    assert searxng.search_searxng('q') == []
    assert calls == []
