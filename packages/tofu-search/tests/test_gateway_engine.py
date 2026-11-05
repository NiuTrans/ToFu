"""Offline specification of the keyed search-gateway engine and its pipeline policy.

The wire fixtures mirror observed gateway envelopes: failures are HTTP 200 with a
nonzero ``data.code``, the echoed ``searchParameters`` carries the caller key,
and Bing-backed rows wrap their links in ``/ck/a`` redirects.
"""

import base64
import json
import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from tofu_search.config import get_config
from tofu_search.search import _common as common
from tofu_search.search import orchestrator
from tofu_search.search.engines import gateway
from tofu_search.search.engines.gateway import GatewayError, _parse_gateway_payload

pytestmark = pytest.mark.unit

KEY = 'secret-billing-key-1234'
URL = 'https://gateway.invalid/search'


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    gateway._cooldown_until.clear()
    common.engine_circuit._state.clear()
    yield
    gateway._cooldown_until.clear()
    common.engine_circuit._state.clear()


def _envelope(results=None, code=0, msg='成功', **extra):
    data = {'searchParameters': {'appId': 'Bearer ' + KEY}, 'code': code, 'msg': msg,
            'results': results or []}
    data.update(extra)
    return {'status': 0, 'message': '成功', 'data': data}


def _row(url, title='Alpha title', snippet='alpha snippet', **extra):
    row = {'url': url, 'title': title, 'snippet': snippet, 'content': snippet,
           'content_filled': False, 'source': 'alpha-search'}
    row.update(extra)
    return row


def _parse(payload, **kwargs):
    kwargs.setdefault('source_id', 'alpha-search')
    kwargs.setdefault('tag', 'API:alpha-search')
    kwargs.setdefault('api_key', KEY)
    kwargs.setdefault('full_content', False)
    return _parse_gateway_payload(payload, **kwargs)


# ── Envelope and row parsing ──

def test_rows_become_tagged_results_with_normalized_dates():
    rows = [
        _row('https://a.example/1', publish_time='2025-04-07'),
        _row('https://a.example/2', publish_time='2026/04/02 00:00:00'),
        _row('https://a.example/3', publish_time='2017-03-13T03:13:00.0000000'),
        _row('https://a.example/4', publish_time='2026-02-30'),
        _row('https://a.example/5'),
    ]
    out = _parse(_envelope(rows))
    assert [r['source'] for r in out] == ['API:alpha-search'] * 5
    assert [r.get('published_date') for r in out] == [
        '2025-04-07', '2026-04-02', '2017-03-13', None, None]
    assert out[0]['title'] == 'Alpha title' and out[0]['snippet'] == 'alpha snippet'


def test_bing_tracking_links_are_unwrapped_and_non_http_rows_dropped():
    target = 'https://docs.example/page'
    payload = base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
    wrapped = 'https://www.bing.com/ck/a?!&&p=abc&u=a1' + payload + '&ntb=1'
    out = _parse(_envelope([_row(wrapped), _row('ftp://x.example/'), _row('')]))
    assert [r['url'] for r in out] == [target]


def test_bing_redirects_rehosted_by_the_gateway_are_unwrapped():
    target = 'https://peps.example/pep-0779/'
    payload = base64.urlsafe_b64encode(target.encode()).decode().rstrip('=')
    rehosted = 'https://api.redirect.example/ck/a?!&&p=abc&u=a1' + payload + '&ntb=1'
    plain_page = 'https://news.example/ck/a?id=7'
    out = _parse(_envelope([_row(rehosted), _row(plain_page)]))
    assert [r['url'] for r in out] == [target, plain_page]


def test_fast_mode_never_admits_provider_content():
    body = 'page body ' * 100
    out = _parse(_envelope([_row('https://a.example/1', content=body, content_filled=True)]))
    assert 'full_content' not in out[0]


def test_full_mode_admits_only_filled_page_sized_bodies():
    body = 'page body ' * 100
    rows = [
        _row('https://a.example/1', content=body, content_filled=True),
        _row('https://a.example/2', content=body, content_filled=False),
        _row('https://a.example/3', content='x' * 65, content_filled=True),
    ]
    out = _parse(_envelope(rows), full_content=True)
    assert out[0]['full_content'] == body.strip()
    assert 'full_content' not in out[1] and 'full_content' not in out[2]


def test_freshness_drops_known_old_rows_and_keeps_undated_rows():
    rows = [
        _row('https://a.example/new', publish_time='2026-10-20'),
        _row('https://a.example/old', publish_time='2026-01-01'),
        _row('https://a.example/undated'),
    ]
    out = _parse(_envelope(rows), freshness='week', today=date(2026, 10, 27))
    assert [r['url'] for r in out] == ['https://a.example/new', 'https://a.example/undated']


def test_rate_limit_code_is_reported_as_none():
    assert _parse(_envelope(code=1029, msg='RPM limit')) is None


def test_failures_raise_redacted_bounded_messages():
    payload = _envelope(code=500, msg='internal failed for ' + KEY,
                        internal_err_map={'[Main]alpha-search': 'java.io.InterruptedIOException: timeout'})
    with pytest.raises(GatewayError) as info:
        _parse(payload)
    message = str(info.value)
    assert KEY not in message
    assert 'gateway code 500' in message and 'InterruptedIOException' in message
    with pytest.raises(GatewayError):
        _parse({'status': 1, 'message': 'down'})
    with pytest.raises(GatewayError):
        _parse(['not', 'an', 'object'])


# ── Request lifecycle ──

class _FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, raw=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._raw = raw if raw is not None else json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._raw), chunk_size):
            yield self._raw[start:start + chunk_size]

    def close(self):
        self.closed = True


def _configured(**overrides):
    values = dict(gateway_url=URL, gateway_api_key=KEY, gateway_sources=['alpha-search'],
                  gateway_full_content=False, gateway_timeout_secs=10, search_deadline_secs=45)
    values.update(overrides)
    return get_config().copy(**values)


def test_unconfigured_gateway_is_unavailable_not_empty(monkeypatch):
    monkeypatch.setattr(gateway, 'get_config', lambda: _configured(gateway_api_key=''))
    out = gateway.search_gateway_source('alpha-search', 'q')
    assert out == [] and out.unavailable_reason == 'not_configured'


def test_one_source_per_request_with_bounded_budget(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None, stream=None, headers=None):
        calls.append({'url': url, 'json': json, 'timeout': timeout, 'headers': headers,
                      'stream': stream})
        return _FakeResponse(_envelope([_row('https://a.example/1')]))

    monkeypatch.setattr(gateway, 'http_post', fake_post)
    monkeypatch.setattr(gateway, 'get_config',
                        lambda: _configured(gateway_full_content=True, search_deadline_secs=6))
    out = gateway.search_gateway_source('alpha-search', 'query', max_results=200)
    assert len(out) == 1
    call = calls[0]
    assert call['url'] == URL
    assert call['headers']['Authorization'] == 'Bearer ' + KEY
    assert call['json'] == {'query': 'query', 'isFast': False, 'timeout': 4000,
                            'sources': ['alpha-search'], 'topK': 50}
    assert call['stream'] is True and call['timeout'] == (5, 7)


def test_rate_limit_starts_a_cooldown_without_further_requests(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return _FakeResponse(_envelope(code=1029, msg='RPM limit'))

    monkeypatch.setattr(gateway, 'http_post', fake_post)
    monkeypatch.setattr(gateway, 'get_config', _configured)
    first = gateway.search_gateway_source('alpha-search', 'q')
    second = gateway.search_gateway_source('alpha-search', 'q')
    assert first.unavailable_reason == second.unavailable_reason == 'rate_limited'
    assert len(calls) == 1


def test_expired_cooldowns_are_pruned(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(gateway, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    gateway._start_cooldown(('url', 'key', 'old-source'))
    now[0] += gateway._RATE_LIMIT_COOLDOWN_SECS + 1
    gateway._start_cooldown(('url', 'key', 'new-source'))
    assert set(gateway._cooldown_until) == {('url', 'key', 'new-source')}


def test_transport_failures_are_safe_and_trip_the_engine_circuit(monkeypatch):
    def fake_post(*args, **kwargs):
        raise requests.ConnectionError('connect to ' + URL + ' failed')

    monkeypatch.setattr(gateway, 'http_post', fake_post)
    monkeypatch.setattr(gateway, 'get_config', _configured)
    for _ in range(common._EngineCircuit.FAIL_THRESHOLD):
        with pytest.raises(GatewayError) as info:
            gateway.search_gateway_source('alpha-search', 'q')
        assert str(info.value) == 'gateway request failed (ConnectionError)'
    out = gateway.search_gateway_source('alpha-search', 'q')
    assert out.unavailable_reason == 'circuit_open'


def test_response_bytes_are_bounded(monkeypatch):
    response = _FakeResponse(None, raw=b' ' * (gateway._RESPONSE_BYTE_LIMIT + 1))
    monkeypatch.setattr(gateway, 'http_post', lambda *a, **k: response)
    monkeypatch.setattr(gateway, 'get_config', _configured)
    with pytest.raises(GatewayError, match='byte budget'):
        gateway.search_gateway_source('alpha-search', 'q')
    assert response.closed


def test_config_repr_never_contains_the_gateway_key():
    assert KEY not in repr(_configured())


# ── Pipeline policy ──

QUERY = 'gateway fusion pipeline'


@pytest.fixture
def pipeline(monkeypatch):
    calls = {'gateway': [], 'free': [], 'fetch': [], 'browser': 0}

    def gateway_rows(source_id, q, n=10, fresh=''):
        calls['gateway'].append(source_id)
        return list(state['gateway_rows'].get(source_id, []))

    def free_engine(name):
        def _engine(q, n=6, freshness=''):
            calls['free'].append(name)
            return [{'title': f'{QUERY} {name} result', 'snippet': f'{QUERY} from {name}',
                     'url': f'https://free.example/{name}', 'source': name}]
        return _engine

    def fetch(url, **kwargs):
        calls['fetch'].append(url)
        return f'{QUERY} fetched article body for {url}. ' + 'detail ' * 40

    def browser(query, max_results):
        calls['browser'] += 1
        return []

    state = {'gateway_rows': {}}
    monkeypatch.setattr(orchestrator, 'search_gateway_source', gateway_rows)
    for name, attr in (('DDG-HTML', 'search_ddg_html'), ('Brave', 'search_brave'),
                       ('Bing', 'search_bing'), ('DDG-API', 'search_ddg_api'),
                       ('SearXNG', 'search_searxng'), ('Marginalia', 'search_marginalia')):
        monkeypatch.setattr(orchestrator, attr, free_engine(name))
    monkeypatch.setattr(orchestrator, 'fetch_page_content', fetch)
    monkeypatch.setattr(orchestrator, 'search_via_browser', browser)
    monkeypatch.setattr(orchestrator, 'is_deepen_enabled', lambda: False)
    monkeypatch.setattr(orchestrator.time, 'sleep', lambda _s: None)

    def run(free_engines='fallback', engines=None, **overrides):
        values = dict(gateway_url=URL, gateway_api_key=KEY,
                      gateway_sources=['alpha-search', 'beta-search'],
                      free_engines=free_engines, prefetch_gate_enabled=False,
                      search_deadline_secs=0)
        values.update(overrides)
        return orchestrator.perform_web_search(
            QUERY, max_results=3, filter_pages=False, rerank=False,
            engines=engines, config=get_config().copy(**values))

    return state, calls, run


_WORDS = ('amber', 'basalt', 'cedar', 'delta', 'ember', 'fjord', 'garnet', 'harbor',
          'indigo', 'juniper', 'kestrel', 'lagoon', 'meadow', 'nectar', 'onyx', 'prairie',
          'quartz', 'raven', 'sierra', 'tundra', 'umber', 'violet', 'willow', 'yarrow')


def _useful(prefix, count=3, **extra):
    """Query-relevant rows with distinct vocabularies, so none group as near-duplicates."""
    rows = []
    for i in range(count):
        words = ' '.join(_WORDS[(i * 3 + j) % len(_WORDS)] for j in range(3))
        rows.append({'title': f'{QUERY} {prefix} {words}',
                     'snippet': f'{QUERY} {words} {prefix}{i} notes',
                     'url': f'https://{prefix}.example/{i}', 'source': 'API:' + prefix, **extra})
    return rows


def test_fallback_skips_free_engines_when_gateway_evidence_is_useful(pipeline):
    state, calls, run = pipeline
    state['gateway_rows'] = {'alpha-search': _useful('alpha'), 'beta-search': _useful('beta')}
    out = run()
    assert sorted(calls['gateway']) == ['alpha-search', 'beta-search']
    assert calls['free'] == []
    assert out and all(any(s.startswith('API:') for s in r['sources']) for r in out)
    assert out._search_diag['fallback_attempts'] == []


def test_fallback_runs_free_engines_when_gateway_has_no_useful_evidence(pipeline):
    state, calls, run = pipeline
    state['gateway_rows'] = {'alpha-search': [{'title': 'unrelated', 'snippet': 'nothing here',
                                               'url': 'https://alpha.example/x',
                                               'source': 'API:alpha-search'}]}
    out = run()
    assert sorted(calls['free']) == sorted(orchestrator_free_names())
    assert 'free-engines' in out._search_diag['fallback_attempts']
    assert any('free.example' in r['url'] for r in out)


def test_parallel_runs_free_engines_alongside(pipeline):
    state, calls, run = pipeline
    state['gateway_rows'] = {'alpha-search': _useful('alpha')}
    run(free_engines='parallel')
    assert sorted(calls['free']) == sorted(orchestrator_free_names())


def test_off_never_runs_free_engines_or_their_recovery(pipeline):
    state, calls, run = pipeline
    out = run(free_engines='off')
    assert calls['free'] == [] and calls['browser'] == 0
    assert out == []
    assert out._search_diag['fallback_attempts'] == []
    assert out._search_diag['reason'] == 'no_matches'


def test_explicit_engine_selection_bypasses_gateway_policy(pipeline):
    state, calls, run = pipeline
    state['gateway_rows'] = {'alpha-search': _useful('alpha')}
    run(engines=['DDG-HTML'])
    assert calls['gateway'] == [] and calls['free'] == ['DDG-HTML']
    calls['free'].clear()
    run(engines=['API:beta-search'], free_engines='parallel')
    assert calls['gateway'] == ['beta-search'] and calls['free'] == []


def test_provider_bodies_replace_our_page_fetch(pipeline):
    state, calls, run = pipeline
    body = f'{QUERY} provider crawled article. ' + 'paragraph ' * 60
    state['gateway_rows'] = {
        'alpha-search': _useful('alpha', count=2, full_content=body),
        'beta-search': _useful('beta', count=2),
    }
    out = run()
    alpha_urls = {f'https://alpha.example/{i}' for i in range(2)}
    assert not alpha_urls & set(calls['fetch'])
    assert {f'https://beta.example/{i}' for i in range(2)} <= set(calls['fetch'])
    assert any(r['url'] in alpha_urls and r['full_content'] == body for r in out)


def test_provider_bodies_meeting_target_end_the_fetch_wait(pipeline, monkeypatch):
    state, calls, run = pipeline
    release = threading.Event()

    def slow_fetch(url, **kwargs):
        calls['fetch'].append(url)
        release.wait(10)
        return f'{QUERY} late body ' + 'detail ' * 40

    monkeypatch.setattr(orchestrator, 'fetch_page_content', slow_fetch)
    alpha = _useful('alpha', count=6)
    for i, row in enumerate(alpha):
        row['full_content'] = f'{QUERY} provider article {i}. ' + ' '.join(
            f'fact{i}x{part}' for part in range(80))
    state['gateway_rows'] = {'alpha-search': alpha, 'beta-search': _useful('beta', count=1)}
    started = time.monotonic()
    try:
        out = run()
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert calls['fetch'] == ['https://beta.example/0']
    assert len(out) == 3 and all('provider article' in r['full_content'] for r in out)


def test_snippet_only_calls_drop_provider_bodies(pipeline):
    state, calls, run = pipeline
    state['gateway_rows'] = {'alpha-search': _useful('alpha', full_content='x' * 400)}
    config = get_config().copy(gateway_url=URL, gateway_api_key=KEY,
                               gateway_sources=['alpha-search'], prefetch_gate_enabled=False,
                               search_deadline_secs=0)
    out = orchestrator.perform_web_search(QUERY, max_results=3, fetch_pages=False,
                                          filter_pages=False, rerank=False, config=config)
    assert out and not any(r.get('full_content') for r in out)


def test_without_gateway_sources_free_engines_stay_the_default(pipeline):
    state, calls, run = pipeline
    run(gateway_url='')
    assert calls['gateway'] == []
    assert sorted(calls['free']) == sorted(orchestrator_free_names())


def orchestrator_free_names():
    return ['DDG-HTML', 'Brave', 'Bing', 'DDG-API', 'SearXNG', 'Marginalia']
