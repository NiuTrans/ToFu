"""Tests for lib/netpath.py — adaptive direct-vs-proxy path selection.

Covers:
  * scorer mechanics (EWMA latency contest, consecutive-failure failover,
    hysteresis anti-flap, both-bad fallback, no-proxy deployments)
  * the ``lib.proxy.proxies_for`` integration (explicit rules win over
    learned decisions; a 'direct' pin bypasses the proxy)
  * passive outcome attribution via ``lib.proxy.report_outcome``
  * persistence round-trip (save → wipe → load)
  * the active prober against a real local HTTP server (direct ok, dead
    proxy fails, working "proxy" measured)

Run:  pytest tests/test_netpath.py -v
"""
from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import lib.proxy as lib_proxy
import lib.netpath as netpath

PROXY_ENV_VARS = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')


@pytest.fixture(autouse=True)
def _clean_netpath(monkeypatch, tmp_path):
    """Isolate every test from learned state and the prober thread."""
    # conftest pins TOFU_NETPATH=off suite-wide so importing server never
    # spawns the prober in test processes; these tests exercise netpath
    # itself, so turn the switch back on for this module only.
    monkeypatch.setenv('TOFU_NETPATH', 'on')
    # Pin the proxy configuration EXPLICITLY: the scorer's proxy path only
    # exists when an env proxy is set (``netpath._proxy_url()``), and several
    # tests assert 'proxy' wins / is the effective default. The dev box has
    # ambient http_proxy/https_proxy; CI has none — without this pin the
    # whole module is env-dependent ('assert direct == proxy' on CI).
    # Tests that exercise the no-proxy deployment delete these themselves.
    for var in PROXY_ENV_VARS:
        monkeypatch.setenv(var, 'http://netpath-test-proxy.invalid:3128')
    # report_outcome() saves learned state unthrottled on the first call —
    # redirect the store so test hosts never reach the production
    # data/config/netpath.json the server loads at boot.
    monkeypatch.setattr(netpath, '_STORE_PATH', str(tmp_path / 'netpath.json'))
    netpath.reset_for_test()
    yield
    netpath.reset_for_test()
    lib_proxy.set_bypass_domains([])


def _note(host: str) -> str:
    url = 'https://%s/' % host
    netpath.note_url(url)
    return url


def _feed(host: str, path: str, ok: bool = True, lat: float = 100.0, n: int = 1):
    url = 'https://%s/' % host
    for _ in range(n):
        netpath.report_outcome(url, ok, lat if ok else None, path=path)


def _decision(host: str) -> str:
    return netpath.decide(host) or 'default'


# ═══════════════════════════════════════════════════════════
#  Scorer mechanics
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScorer:
    def test_undecided_before_any_measurement(self):
        _note('a.example.com')
        assert netpath.decide('a.example.com') is None
        # Undecided → lib.proxy falls back to env behaviour (empty dict).
        assert lib_proxy.proxies_for('https://a.example.com/x') == {}

    def test_direct_faster_pins_direct(self):
        _note('fast-direct.example.com')
        _feed('fast-direct.example.com', 'direct', lat=100, n=2)
        _feed('fast-direct.example.com', 'proxy', lat=300, n=2)
        assert _decision('fast-direct.example.com') == 'direct'
        assert lib_proxy.proxies_for('https://fast-direct.example.com/x') == {
            'no_proxy': '*'}

    def test_proxy_faster_keeps_proxy(self):
        _note('fast-proxy.example.com')
        _feed('fast-proxy.example.com', 'direct', lat=300, n=2)
        # Initially direct is the only measured path → pinned.
        assert _decision('fast-proxy.example.com') == 'direct'
        _feed('fast-proxy.example.com', 'proxy', lat=100, n=2)
        assert _decision('fast-proxy.example.com') == 'proxy'
        assert lib_proxy.proxies_for('https://fast-proxy.example.com/x') == {}

    def test_consecutive_failures_fail_over(self):
        _note('flaky.example.com')
        _feed('flaky.example.com', 'direct', lat=100, n=2)
        _feed('flaky.example.com', 'proxy', lat=300, n=2)
        assert _decision('flaky.example.com') == 'direct'
        _feed('flaky.example.com', 'direct', ok=False, n=2)
        assert _decision('flaky.example.com') == 'proxy'

    def test_single_failure_does_not_flip(self):
        _note('one-flap.example.com')
        _feed('one-flap.example.com', 'direct', lat=100, n=2)
        _feed('one-flap.example.com', 'proxy', lat=300, n=2)
        _feed('one-flap.example.com', 'direct', ok=False, n=1)
        assert _decision('one-flap.example.com') == 'direct'

    def test_healed_path_is_rediscovered(self):
        _note('heal.example.com')
        _feed('heal.example.com', 'direct', lat=100, n=2)
        _feed('heal.example.com', 'proxy', lat=300, n=2)
        _feed('heal.example.com', 'direct', ok=False, n=2)
        assert _decision('heal.example.com') == 'proxy'
        # Direct recovers — after fresh measurements it wins the contest.
        _feed('heal.example.com', 'direct', lat=100, n=2)
        assert _decision('heal.example.com') == 'direct'

    def test_hysteresis_prevents_flapping(self):
        _note('hyst.example.com')
        _feed('hyst.example.com', 'direct', lat=100, n=2)
        # Proxy is only 10% faster — NOT enough to unseat the incumbent.
        _feed('hyst.example.com', 'proxy', lat=90, n=2)
        assert _decision('hyst.example.com') == 'direct'
        # 50ms EWMA after two samples: 78 then 69.6 < 75 → switch.
        _feed('hyst.example.com', 'proxy', lat=50, n=2)
        assert _decision('hyst.example.com') == 'proxy'

    def test_both_paths_bad_falls_back_to_default(self):
        _note('both-bad.example.com')
        _feed('both-bad.example.com', 'direct', lat=100, n=2)
        _feed('both-bad.example.com', 'proxy', lat=300, n=2)
        assert _decision('both-bad.example.com') == 'direct'
        _feed('both-bad.example.com', 'direct', ok=False, n=2)
        assert _decision('both-bad.example.com') == 'proxy'
        _feed('both-bad.example.com', 'proxy', ok=False, n=2)
        assert netpath.decide('both-bad.example.com') is None

    def test_no_proxy_env_never_picks_proxy(self, monkeypatch):
        for var in PROXY_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        _note('no-proxy.example.com')
        _feed('no-proxy.example.com', 'direct', lat=100, n=2)
        assert _decision('no-proxy.example.com') == 'direct'
        # Direct goes bad with no proxy available → undecided, not 'proxy'.
        _feed('no-proxy.example.com', 'direct', ok=False, n=2)
        assert netpath.decide('no-proxy.example.com') is None

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv('TOFU_NETPATH', 'off')
        url = _note('off.example.com')
        netpath.report_outcome(url, True, 50.0, path='direct')
        assert netpath.decide('off.example.com') is None
        assert lib_proxy.proxies_for(url) == {}

    def test_lru_cap_evicts_stalest(self):
        for i in range(netpath._MAX_HOSTS + 1):
            _note('host-%03d.example.com' % i)
        assert len(netpath._states) == netpath._MAX_HOSTS
        assert 'host-000.example.com' not in netpath._states
        assert ('host-%03d.example.com' % netpath._MAX_HOSTS) in netpath._states

    def test_reset_proxy_stats(self):
        _note('reset.example.com')
        _feed('reset.example.com', 'direct', lat=300, n=2)
        _feed('reset.example.com', 'proxy', lat=100, n=2)
        assert _decision('reset.example.com') == 'proxy'
        netpath.reset_proxy_stats()
        st = netpath._states['reset.example.com']
        assert st['decision'] is None
        assert st['paths']['proxy']['ewma_ms'] is None
        assert st['paths']['direct']['ewma_ms'] == 300


# ═══════════════════════════════════════════════════════════
#  lib.proxy integration
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProxyIntegration:
    def test_explicit_bypass_domain_wins_over_learned_proxy(self):
        # Learned state says proxy is better…
        _note('bypass-me.example.com')
        _feed('bypass-me.example.com', 'direct', lat=300, n=2)
        _feed('bypass-me.example.com', 'proxy', lat=100, n=2)
        assert _decision('bypass-me.example.com') == 'proxy'
        # …but an explicit bypass-domain suffix still forces direct.
        lib_proxy.set_bypass_domains(['bypass-me.example.com'])
        assert lib_proxy.proxies_for('https://bypass-me.example.com/') == {
            'no_proxy': '*'}

    def test_registered_no_proxy_host_wins_over_learned_proxy(self):
        _note('registered.example.com')
        _feed('registered.example.com', 'direct', lat=300, n=2)
        _feed('registered.example.com', 'proxy', lat=100, n=2)
        assert _decision('registered.example.com') == 'proxy'
        lib_proxy.register_no_proxy_host('registered.example.com')
        try:
            assert lib_proxy.proxies_for('https://registered.example.com/') == {
                'no_proxy': '*'}
        finally:
            lib_proxy._registered_hosts.discard('registered.example.com')

    def test_passive_report_attributes_to_effective_path(self):
        # A real request routed by proxies_for (undecided → env default =
        # proxy in this test env) must be attributed to the proxy path.
        lib_proxy.proxies_for('https://attr.example.com/')
        lib_proxy.report_outcome('https://attr.example.com/', True, 42.0)
        summary = netpath.status_summary()['hosts']['attr.example.com']
        assert summary['proxy_ms'] == 42.0
        assert summary['direct_ms'] is None


# ═══════════════════════════════════════════════════════════
#  Persistence
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPersistence:
    def test_save_load_round_trip(self, tmp_path, monkeypatch):
        store = str(tmp_path / 'netpath.json')
        monkeypatch.setattr(netpath, '_STORE_PATH', store)
        _note('persist.example.com')
        _feed('persist.example.com', 'direct', lat=120, n=2)
        assert _decision('persist.example.com') == 'direct'
        netpath._save()

        netpath.reset_for_test()
        assert netpath.status_summary()['hosts'] == {}

        netpath._load()
        st = netpath._states.get('persist.example.com')
        assert st is not None
        assert st['decision'] == 'direct'
        assert st['paths']['direct']['ewma_ms'] == 120

    def test_load_ignores_wrong_version(self, tmp_path, monkeypatch):
        import json
        store = tmp_path / 'netpath.json'
        store.write_text(json.dumps({'version': 999, 'hosts': [
            {'host': 'old.example.com'}]}))
        monkeypatch.setattr(netpath, '_STORE_PATH', str(store))
        netpath._load()
        assert 'old.example.com' not in netpath._states

    def test_failed_save_stays_dirty_and_retries_without_fixed_tmp(
            self, monkeypatch):
        _note('retry-save.example.com')
        # Keep report_outcome from performing the first automatic save.
        monkeypatch.setattr(netpath, '_last_save', netpath.time.time())
        _feed('retry-save.example.com', 'direct', lat=80, n=1)
        assert netpath._dirty is True
        real_write = netpath.write_json_atomic
        monkeypatch.setattr(
            netpath, 'write_json_atomic',
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError('injected disk failure')))

        netpath._save()

        assert netpath._dirty is True
        assert not os.path.exists(netpath._STORE_PATH + '.tmp')
        monkeypatch.setattr(netpath, 'write_json_atomic', real_write)
        netpath._save()
        assert netpath._dirty is False
        assert os.path.isfile(netpath._STORE_PATH)

    def test_proxy_reset_is_persisted_before_restart(self):
        _note('proxy-reset.example.com')
        _feed('proxy-reset.example.com', 'proxy', lat=20, n=2)
        assert netpath._states['proxy-reset.example.com']['paths'][
            'proxy']['ewma_ms'] == 20

        netpath.reset_proxy_stats()
        netpath.reset_for_test()
        netpath._load()

        restored = netpath._states['proxy-reset.example.com']
        assert restored['paths']['proxy']['ewma_ms'] is None
        assert restored['paths']['proxy']['samples'] == 0

    def test_load_never_probes_a_sample_url_for_a_different_host(
            self, tmp_path, monkeypatch):
        import json
        store = tmp_path / 'netpath.json'
        store.write_text(json.dumps({
            'version': netpath._STORE_VERSION,
            'hosts': [{
                'host': 'api.example.org',
                'sample_url': 'http://127.0.0.1/private',
                'decision': 'sideways',
                'paths': {},
            }],
        }))
        monkeypatch.setattr(netpath, '_STORE_PATH', str(store))

        netpath._load()

        restored = netpath._states['api.example.org']
        assert restored['sample_url'] == 'https://api.example.org/'
        assert restored['decision'] is None

    def test_load_preserves_stale_last_use_instead_of_rearming_host(
            self, tmp_path, monkeypatch):
        import json

        now = 1_000_000.0
        stale_last_seen = now - netpath._HOST_TTL - 1
        store = tmp_path / 'netpath.json'
        store.write_text(json.dumps({
            'version': netpath._STORE_VERSION,
            'hosts': [{
                'host': 'stale.example.org',
                'sample_url': 'https://stale.example.org/',
                'last_seen': stale_last_seen,
                'decision': 'proxy',
                'paths': {
                    'direct': {'fails': 8},
                    'proxy': {'samples': 3, 'ewma_ms': 40},
                },
            }],
        }))
        monkeypatch.setattr(netpath, '_STORE_PATH', str(store))
        monkeypatch.setattr(netpath.time, 'time', lambda: now)

        netpath._load()

        restored = netpath._states['stale.example.org']
        assert restored['last_seen'] == stale_last_seen
        assert restored['paths']['direct']['probe_failures'] == 8
        assert netpath._eligible_probe_paths(now) == []


# ═══════════════════════════════════════════════════════════
#  Exempt hosts: localhost & IP literals are never tracked
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestExemptHosts:
    def test_is_ip_literal(self):
        assert netpath._is_ip_literal('127.0.0.1')
        assert netpath._is_ip_literal('0.0.0.0')
        assert netpath._is_ip_literal('::1')
        assert netpath._is_ip_literal('203.0.113.9')
        assert not netpath._is_ip_literal('localhost')
        assert not netpath._is_ip_literal('your-llm-gateway.example.com')

    def test_note_url_ignores_ip_literals(self, tmp_path):
        for url in ('http://127.0.0.1:8000/v1', 'https://10.0.0.9/',
                    'http://[::1]:11434/v1', 'https://203.0.113.9/'):
            netpath.note_url(url)
        assert netpath._states == {}
        # Nothing registered → nothing to persist.
        assert not (tmp_path / 'netpath.json').exists()

    def test_note_url_ignores_localhost(self):
        netpath.note_url('http://localhost:11434/v1')
        assert 'localhost' not in netpath._states

    def test_decide_returns_none_for_exempt(self):
        assert netpath.decide('127.0.0.1') is None
        assert netpath.decide('::1') is None
        assert netpath.decide('localhost') is None

    def test_report_outcome_noop_for_exempt(self):
        netpath.note_url('http://127.0.0.1:8000/')
        netpath.report_outcome('http://127.0.0.1:8000/', True, 12.0,
                               path='direct')
        assert netpath._states == {}

    def test_load_skips_exempt_hosts(self, tmp_path, monkeypatch):
        import json
        store = tmp_path / 'netpath.json'
        store.write_text(json.dumps({'version': netpath._STORE_VERSION,
                                     'hosts': [
                                         {'host': '127.0.0.1',
                                          'decision': 'proxy'},
                                         {'host': 'localhost',
                                          'decision': 'direct'},
                                         {'host': 'real.example.com',
                                          'decision': 'direct'},
                                     ]}))
        monkeypatch.setattr(netpath, '_STORE_PATH', str(store))
        netpath._load()
        assert '127.0.0.1' not in netpath._states
        assert 'localhost' not in netpath._states
        assert 'real.example.com' in netpath._states


# ═══════════════════════════════════════════════════════════
#  Active prober (real local HTTP server)
# ═══════════════════════════════════════════════════════════

class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'x'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def local_server():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), _OkHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


# netpath refuses to track IP literals by design, so the prober tests
# register a reserved-suffix host; this fixture rewrites the probe URL right
# before the real _probe_once performs the fetch, keeping the actual
# direct-vs-proxy network behaviour genuine.
_FAKE_HOST = 'probe-fake.test'


@pytest.fixture()
def fake_dns(monkeypatch):
    real_probe = netpath._probe_once

    def _swapped(url, use_proxy):
        return real_probe(url.replace(_FAKE_HOST, '127.0.0.1'), use_proxy)

    monkeypatch.setattr(netpath, '_probe_once', _swapped)


@pytest.mark.unit
class TestProber:
    def test_probe_intervals_have_a_hard_floor_and_ceiling(self):
        assert netpath._bounded_probe_interval(1) == 30
        assert netpath._bounded_probe_interval(float('inf')) == 180
        assert netpath._bounded_probe_interval(99_999) == 6 * 3600
        assert netpath._failed_probe_delay(180, 1) == 180
        assert netpath._failed_probe_delay(180, 2) == 360
        assert netpath._failed_probe_delay(180, 99) == 3600

    def test_probe_round_backs_off_failed_and_stable_paths(
            self, monkeypatch):
        now = [1_000.0]
        calls = []
        monkeypatch.setattr(netpath.time, 'time', lambda: now[0])
        monkeypatch.setattr(netpath, '_PROBE_INTERVAL', 180.0)
        monkeypatch.setattr(netpath, '_PROBE_MAX_INTERVAL', 3600.0)
        monkeypatch.setattr(
            netpath,
            '_probe_once',
            lambda _url, use_proxy: (
                calls.append('proxy' if use_proxy else 'direct')
                or ((True, 40.0) if use_proxy else (False, None))),
        )
        netpath._prober_stop.clear()
        _note('adaptive.example.com')

        assert netpath._probe_round(period=180) == pytest.approx(180)
        assert calls == ['direct', 'proxy']
        state = netpath._states['adaptive.example.com']['paths']
        assert state['direct']['next_probe'] == 1_180
        assert state['proxy']['next_probe'] == 4_600

        now[0] = 1_179
        assert netpath._probe_round(period=180) == pytest.approx(1)
        assert calls == ['direct', 'proxy']

        now[0] = 1_180
        assert netpath._probe_round(period=180) == pytest.approx(360)
        assert calls == ['direct', 'proxy', 'direct']
        assert state['direct']['next_probe'] == 1_540
        assert state['proxy']['next_probe'] == 4_600

    def test_stable_host_uses_at_most_55_active_requests_per_day(
            self, monkeypatch):
        now = [10_000.0]
        end = now[0] + 24 * 3600
        calls = []
        monkeypatch.setattr(netpath.time, 'time', lambda: now[0])
        monkeypatch.setattr(netpath, '_PROBE_INTERVAL', 180.0)
        monkeypatch.setattr(netpath, '_PROBE_MAX_INTERVAL', 3600.0)
        monkeypatch.setattr(netpath, '_save', lambda: None)
        monkeypatch.setattr(
            netpath,
            '_probe_once',
            lambda _url, use_proxy: (
                calls.append('proxy' if use_proxy else 'direct')
                or ((True, 40.0) if use_proxy else (False, None))),
        )
        netpath._prober_stop.clear()
        _note('daily-budget.example.com')

        while now[0] < end:
            delay = netpath._probe_round(period=180)
            assert delay >= 0.05
            now[0] += delay

        # The previous fixed three-minute dual-path loop issued 960 requests
        # for this host/day. Adaptive probing preserves hourly recovery checks
        # while cutting the deterministic steady-state budget by over 94%.
        assert len(calls) <= 55
        assert calls.count('proxy') <= 25

    def test_proxy_reset_wakes_even_without_old_proxy_measurements(self):
        _note('new-proxy.example.com')
        netpath._prober_wake.clear()

        netpath.reset_proxy_stats()

        assert netpath._prober_wake.is_set()

    def test_unexpected_probe_fault_waits_base_interval(self, monkeypatch):
        now = 7_000.0
        monkeypatch.setattr(netpath.time, 'time', lambda: now)
        monkeypatch.setattr(netpath, '_PROBE_INTERVAL', 180.0)
        monkeypatch.setattr(netpath, '_PROBE_MAX_INTERVAL', 3600.0)
        monkeypatch.setattr(
            netpath,
            '_probe_path',
            lambda *_args: (_ for _ in ()).throw(RuntimeError('injected')),
        )
        netpath._prober_stop.clear()
        _note('fault.example.com')

        assert netpath._probe_round(period=180) == pytest.approx(180)
        paths = netpath._states['fault.example.com']['paths']
        assert paths['direct']['next_probe'] == 7_180
        assert paths['proxy']['next_probe'] == 7_180

    def test_passive_failure_wakes_alternate_path_immediately(
            self, monkeypatch):
        now = 2_000.0
        monkeypatch.setattr(netpath.time, 'time', lambda: now)
        monkeypatch.setattr(netpath, '_PROBE_INTERVAL', 180.0)
        monkeypatch.setattr(netpath, '_PROBE_MAX_INTERVAL', 3600.0)
        url = _note('wake.example.com')
        assert netpath.decide('wake.example.com') is None
        netpath._prober_wake.clear()

        netpath.report_outcome(url, False)

        paths = netpath._states['wake.example.com']['paths']
        assert paths['proxy']['next_probe'] == 2_180
        assert paths['direct']['next_probe'] == 2_000
        assert netpath._prober_wake.is_set()

    def test_passive_success_postpones_redundant_probe(self, monkeypatch):
        now = 3_000.0
        monkeypatch.setattr(netpath.time, 'time', lambda: now)
        monkeypatch.setattr(netpath, '_PROBE_INTERVAL', 180.0)
        monkeypatch.setattr(netpath, '_PROBE_MAX_INTERVAL', 3600.0)
        url = _note('traffic.example.com')

        netpath.report_outcome(url, True, 30.0, path='proxy')

        proxy_path = netpath._states['traffic.example.com']['paths']['proxy']
        assert proxy_path['next_probe'] == 6_600
        assert ('traffic.example.com', 'proxy') not in (
            netpath._eligible_probe_paths(now))

    def test_probe_host_dead_proxy_marks_proxy_bad(
            self, local_server, monkeypatch, fake_dns):
        # Proxy points at a closed port → only the direct path can work.
        monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
        monkeypatch.setenv('https_proxy', 'http://127.0.0.1:1')
        url = 'http://%s:%d/' % (_FAKE_HOST, local_server.server_port)
        netpath.note_url(url)
        netpath.probe_host(_FAKE_HOST)
        summary = netpath.status_summary()['hosts'][_FAKE_HOST]
        assert summary['direct_ms'] is not None
        assert summary['proxy_fails'] == 1
        assert summary['decision'] == 'direct'

    def test_probe_host_working_proxy_measures_both(
            self, local_server, monkeypatch, fake_dns):
        # A "proxy" that answers (any HTTP response = path works).
        proxy = 'http://127.0.0.1:%d' % local_server.server_port
        monkeypatch.setenv('http_proxy', proxy)
        monkeypatch.setenv('https_proxy', proxy)
        url = 'http://%s:%d/' % (_FAKE_HOST, local_server.server_port)
        netpath.note_url(url)
        netpath.probe_host(_FAKE_HOST)
        summary = netpath.status_summary()['hosts'][_FAKE_HOST]
        assert summary['direct_ms'] is not None
        assert summary['proxy_ms'] is not None
        assert summary['decision'] in ('direct', 'proxy')

    def test_prober_thread_start_stop(self):
        assert netpath.start_prober(interval=60) is True
        # Idempotent — a second call does not spawn another thread.
        first = netpath._prober_thread
        assert netpath.start_prober(interval=60) is True
        assert netpath._prober_thread is first
        netpath.stop_prober()
        assert not first.is_alive()

    def test_timed_out_stop_retains_owner_and_blocks_duplicate(self):
        class StuckThread:
            def __init__(self):
                self.join_timeouts = []

            @staticmethod
            def is_alive():
                return True

            def join(self, timeout):
                self.join_timeouts.append(timeout)

        old_owner = StuckThread()
        netpath._prober_thread = old_owner
        netpath._prober_stop.clear()
        try:
            netpath.stop_prober()

            assert netpath._prober_thread is old_owner
            assert netpath._prober_stop.is_set()
            assert old_owner.join_timeouts[0] <= 15
            assert netpath.start_prober(interval=60) is False
            assert netpath._prober_thread is old_owner
        finally:
            netpath._prober_thread = None

    def test_prober_respects_off_switch(self, monkeypatch):
        monkeypatch.setenv('TOFU_NETPATH', 'off')
        assert netpath.start_prober(interval=60) is False
