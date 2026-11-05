"""Engine waves never wait on probes unless nothing else runs.

An engine whose most recent request failed (a single failure, or a tripped
breaker that cooled down) still gets called so it can come back, but an
unreachable one must not hold every search for its full network timeout.
All engines and fetches are stubbed; no network is used.
"""

import threading
import time

import pytest

from tofu_search.config import get_config
from tofu_search.search import _common as common
from tofu_search.search import orchestrator
from tofu_search.search._common import _EngineCircuit

pytestmark = pytest.mark.unit

_ENGINES = ('search_ddg_html', 'search_brave', 'search_bing', 'search_ddg_api',
            'search_searxng', 'search_marginalia')


def _row(url, title):
    return {'url': url, 'title': title, 'snippet': f'{title} details', 'source': 'stub'}


@pytest.fixture
def probing_circuit(monkeypatch):
    """A breaker whose tripped engines are half-open immediately."""
    circuit = _EngineCircuit()
    circuit.COOLDOWN = -1
    monkeypatch.setattr(common, 'engine_circuit', circuit)

    def half_open(name):
        for _ in range(circuit.FAIL_THRESHOLD):
            circuit.record_failure(name)
        assert not circuit.is_open(name) and circuit.last_call_failed(name)
    return half_open


@pytest.fixture
def engines(monkeypatch):
    def install(**overrides):
        for name in _ENGINES:
            monkeypatch.setattr(orchestrator, name, overrides.get(
                name, lambda q, n=6, freshness='': []))
    monkeypatch.setattr(orchestrator, 'xhs_search_available', lambda: False)
    monkeypatch.setattr(orchestrator, 'is_deepen_enabled', lambda: False)
    monkeypatch.setattr(orchestrator, 'fetch_page_content',
                        lambda url, **kw: f'Quasar latency facts from {url}. ' * 30)
    return install


def _search(**kw):
    cfg = get_config().copy(prefetch_gate_enabled=False, search_deadline_secs=10)
    return orchestrator.perform_web_search(
        'quasar latency', max_results=2, filter_pages=False, rerank=False,
        config=cfg, **kw)


def test_unreachable_recovery_probe_does_not_delay_search(probing_circuit, engines):
    release = threading.Event()

    def unreachable(q, n=6, freshness=''):
        release.wait(5)
        raise ConnectionError('name resolution failed')

    rows = [_row(f'https://docs{i}.example/a', f'Quasar latency guide {i}') for i in range(3)]
    engines(search_ddg_html=lambda q, n=6, freshness='': rows,
            search_marginalia=unreachable)
    probing_circuit('Marginalia')
    try:
        started = time.monotonic()
        out = _search()
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert len(out) == 2
    assert elapsed < 2.0, f'search waited for the recovery probe: {elapsed:.2f}s'
    assert out._search_diag['engine_unavailable'].get('Marginalia') == 'circuit_open'


def test_recovery_probe_that_answers_in_time_contributes(probing_circuit, engines):
    def slow_primary(q, n=6, freshness=''):
        time.sleep(0.3)
        return [_row('https://docs.example/a', 'Quasar latency guide')]

    engines(search_ddg_html=slow_primary,
            search_marginalia=lambda q, n=6, freshness='': [
                _row('https://probe.example/a', 'Quasar latency notes')])
    probing_circuit('Marginalia')

    out = _search()

    assert 'https://probe.example/a' in {r['url'] for r in out}
    assert 'Marginalia' not in out._search_diag['engine_unavailable']


def test_wave_of_only_probing_engines_waits_for_them(probing_circuit, engines):
    def recovering(q, n=6, freshness=''):
        time.sleep(0.2)
        return [_row('https://docs.example/a', 'Quasar latency guide')]

    engines(search_ddg_html=recovering)
    probing_circuit('DDG-HTML')

    out = _search(engines=['DDG-HTML'])

    assert [r['url'] for r in out] == ['https://docs.example/a']
    assert 'DDG-HTML' not in out._search_diag['engine_unavailable']


def test_engine_that_failed_once_is_not_awaited_before_it_trips(monkeypatch, engines):
    """Cold start: the second search must not repeat the first one's dead-engine wait."""
    circuit = _EngineCircuit()
    monkeypatch.setattr(common, 'engine_circuit', circuit)
    release = threading.Event()

    def unreachable(q, n=6, freshness=''):
        release.wait(5)
        raise ConnectionError('name resolution failed')

    rows = [_row(f'https://docs{i}.example/a', f'Quasar latency guide {i}') for i in range(3)]
    engines(search_ddg_html=lambda q, n=6, freshness='': rows,
            search_marginalia=unreachable)
    circuit.record_failure('Marginalia')  # the first search found it unreachable
    assert not circuit.is_open('Marginalia')
    try:
        started = time.monotonic()
        out = _search()
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert len(out) == 2
    assert elapsed < 2.0, f'search waited for an engine whose last call failed: {elapsed:.2f}s'
