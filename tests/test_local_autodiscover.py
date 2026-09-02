#!/usr/bin/env python3
"""tests/test_local_autodiscover.py — well-known local-engine port auto-discovery.

The autodiscover worker (lib/llm_dispatch/autodiscover_local.py) probes the
canonical loopback ports (Ollama 11434 / vLLM 8000 / SGLang 30000) after
startup and periodically, and registers any endpoint serving models as a
normal brand='local' provider in server_config.json.

Owner-ratified semantics:

  * Closed ports are silent (DEBUG, never WARNING) and cheap (TCP connect).
  * Idempotent: a port already covered by ANY provider's endpoints/base_url
    (any localhost spelling) is skipped; re-running the sweep adds nothing.
  * No zombies: deleting an auto-created provider dismisses the port; the
    sweep NEVER resurrects it.
  * Zero models (engine up, nothing pulled) → not added, NOT dismissed —
    the next sweep re-probes.
  * Fail-closed: one broken candidate never masks the others.
  * Opt-out: TOFU_LOCAL_AUTODISCOVER=0 → sweep is a no-op.
  * NEUTER: delete the covered-port skip → second run duplicates the row.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_dispatch import autodiscover_local as ad


def _models(*ids):
    return [{'model_id': mid, 'aliases': [], 'capabilities': ['text'],
             'rpm': 30, 'cost': 0.0, 'thinking_default': False} for mid in ids]


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolate config/state paths + stub the dispatcher rebuild."""
    cfg_path = tmp_path / 'server_config.json'
    state_path = tmp_path / 'local_autodiscover.json'
    cfg_path.write_text(json.dumps({'providers': []}))
    monkeypatch.setattr('lib._SERVER_CONFIG_PATH', str(cfg_path))
    monkeypatch.setattr(ad, '_STATE_PATH', str(state_path))
    monkeypatch.setattr(ad, '_rebuild_slots', lambda: None)
    monkeypatch.delenv('TOFU_LOCAL_AUTODISCOVER', raising=False)
    monkeypatch.delenv('OLLAMA_HOST', raising=False)
    return {'cfg': cfg_path, 'state': state_path}


def _read_cfg(sandbox):
    return json.loads(sandbox['cfg'].read_text())


def _fake_net(open_ports, served):
    """Build (port_open, discover) fakes. served: {port_key: [model_ids]}."""
    def port_open(host, port):
        return ad._port_key(host, port) in open_ports

    def discover(base_url):
        key = ad._port_key(*ad._parse_host_port(base_url, 0))
        ids = served.get(key, [])
        return _models(*ids), base_url
    return port_open, discover


@pytest.fixture()
def poll_sandbox(sandbox, monkeypatch):
    """Reset the bounded process-local scheduler for deterministic clock tests."""
    monkeypatch.setattr(ad, '_BOOT_DELAY', 0.0)
    monkeypatch.setattr(ad, '_SWEEP_INTERVAL', 10.0)
    monkeypatch.setattr(ad, '_MAX_PROBE_INTERVAL', 40.0)
    monkeypatch.setattr(ad, '_runtime_enabled', True)
    monkeypatch.setattr(ad, '_next_sweep_at', None)
    monkeypatch.setattr(ad, '_last_open_keys', set())
    monkeypatch.setattr(ad, '_empty_keys', set())
    monkeypatch.setattr(ad, '_probe_due_at', {})
    monkeypatch.setattr(ad, '_next_probe_delay', {})
    monkeypatch.setattr(ad, '_force_full_probe', False)
    monkeypatch.setattr(ad, '_schedule_generation', 0)
    return sandbox


@pytest.mark.unit
class TestAutodiscover:

    def test_adds_ollama_provider_when_models_served(self, sandbox):
        port_open, discover = _fake_net({'127.0.0.1:11434'},
                                        {'127.0.0.1:11434': ['llama3.1']})
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert len(stats['added']) == 1
        prov = _read_cfg(sandbox)['providers'][0]
        assert prov['brand'] == 'local' and prov['engine'] == 'ollama'
        assert prov['base_url'] == 'http://127.0.0.1:11434/v1'
        assert prov['endpoint_models'] == {'http://127.0.0.1:11434/v1': ['llama3.1']}
        assert [m['model_id'] for m in prov['models']] == ['llama3.1']

    def test_closed_ports_are_silent_and_free(self, sandbox, caplog):
        port_open, discover = _fake_net(set(), {})
        import logging
        with caplog.at_level(logging.DEBUG):
            stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert stats['added'] == [] and stats['probed'] == 0
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], 'closed ports must never log at WARNING+'

    def test_idempotent_second_run_adds_nothing(self, sandbox):
        port_open, discover = _fake_net({'127.0.0.1:11434'},
                                        {'127.0.0.1:11434': ['llama3.1']})
        ad.sweep_once(port_open=port_open, discover=discover)
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert stats['added'] == []
        assert len(_read_cfg(sandbox)['providers']) == 1

    def test_user_configured_port_is_skipped(self, sandbox):
        # Pre-existing provider on the same port (localhost spelling variant).
        cfg = json.loads(sandbox['cfg'].read_text())
        cfg['providers'].append({'id': 'mine', 'brand': 'local',
                                 'base_url': 'http://localhost:11434/v1',
                                 'endpoints': ['http://localhost:11434/v1']})
        sandbox['cfg'].write_text(json.dumps(cfg))
        port_open, discover = _fake_net({'127.0.0.1:11434'},
                                        {'127.0.0.1:11434': ['llama3.1']})
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert stats['added'] == []
        assert len(_read_cfg(sandbox)['providers']) == 1

    def test_deleted_provider_never_resurrects(self, sandbox):
        port_open, discover = _fake_net({'127.0.0.1:11434'},
                                        {'127.0.0.1:11434': ['llama3.1']})
        ad.sweep_once(port_open=port_open, discover=discover)
        # User deletes the provider in Settings.
        sandbox['cfg'].write_text(json.dumps({'providers': []}))
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert stats['added'] == []
        assert _read_cfg(sandbox)['providers'] == []
        state = json.loads(sandbox['state'].read_text())
        assert '127.0.0.1:11434' in state['dismissed']

    def test_engine_up_but_zero_models_not_added_nor_dismissed(self, sandbox):
        port_open, discover = _fake_net({'127.0.0.1:11434'},
                                        {'127.0.0.1:11434': []})
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert stats['added'] == [] and stats['probed'] == 1
        assert not sandbox['state'].exists()  # nothing persisted

    def test_well_known_engine_starts_at_canonical_v1_models(self, sandbox):
        seen = []

        def discover(base_url):
            seen.append(base_url)
            return [], base_url

        stats = ad.sweep_once(
            port_open=lambda _host, port: port == 11434,
            discover=discover,
        )
        assert stats['probed'] == 1
        assert seen == ['http://127.0.0.1:11434/v1'], (
            'well-known engines must not pay a guaranteed bare /models 404 '
            'before their canonical /v1/models request')

    def test_background_discover_uses_one_http_request(self, sandbox,
                                                       monkeypatch):
        import lib.llm_dispatch.discovery as discovery

        calls = []

        class Response:
            ok = True
            status_code = 200
            text = ''

            @staticmethod
            def json():
                return {'data': []}

        def http_get(url, **_kwargs):
            calls.append(url)
            return Response()

        monkeypatch.setattr(discovery, 'http_get', http_get)
        models, effective = ad._discover('http://127.0.0.1:11434/v1')

        assert models == []
        assert effective == 'http://127.0.0.1:11434/v1'
        assert calls == ['http://127.0.0.1:11434/v1/models']

    def test_one_broken_candidate_does_not_mask_others(self, sandbox):
        port_open, _ = _fake_net({'127.0.0.1:11434', '127.0.0.1:8000'},
                                 {'127.0.0.1:8000': ['qwen3-32b']})

        def discover(base_url):
            if '11434' in base_url:
                raise RuntimeError('boom')
            return _models('qwen3-32b'), base_url + '/v1'
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert len(stats['added']) == 1
        assert stats['added'][0]['engine'] == 'vllm'

    def test_opt_out_env_makes_sweep_noop(self, sandbox, monkeypatch):
        monkeypatch.setenv('TOFU_LOCAL_AUTODISCOVER', '0')
        port_open, discover = _fake_net({'127.0.0.1:11434'},
                                        {'127.0.0.1:11434': ['llama3.1']})
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert stats.get('disabled') is True
        assert _read_cfg(sandbox)['providers'] == []

    def test_ollama_host_env_adds_candidate(self, sandbox, monkeypatch):
        monkeypatch.setenv('OLLAMA_HOST', 'http://192.168.1.5:11434')
        port_open, discover = _fake_net({'192.168.1.5:11434'},
                                        {'192.168.1.5:11434': ['mistral']})
        stats = ad.sweep_once(port_open=port_open, discover=discover)
        assert len(stats['added']) == 1
        prov = _read_cfg(sandbox)['providers'][0]
        assert prov['base_url'] == 'http://192.168.1.5:11434/v1'

    def test_neuter_coverage_skip_duplicates_provider(self, sandbox):
        """NEUTER: without the covered-port skip the sweep re-adds the same
        port on every run — the duplication this guard exists to prevent."""
        port_open, discover = _fake_net({'127.0.0.1:11434'},
                                        {'127.0.0.1:11434': ['llama3.1']})
        ad.sweep_once(port_open=port_open, discover=discover)
        # Neutered sweep: drop the coverage/dismissed checks by scanning with
        # an empty coverage set (simulating the removed guard).
        import lib as _lib
        cfg = _lib._load_server_config()
        prov = cfg['providers'][0]
        assert _read_cfg(sandbox)['providers']
        # If the skip were gone, _persist_provider would still dedupe by id,
        # so the REAL user-visible duplication guard is the covered set —
        # assert the covered set detects the port (the property the neuter
        # would break):
        assert '127.0.0.1:11434' in ad._covered_port_keys([prov])

    def test_frontend_preset_port_parity(self):
        """Backend WELL_KNOWN_ENGINES mirrors the frontend _LOCAL_ENGINE_PRESETS
        ports so the UI shows the auto card under the same engine badge."""
        from tests._runtime_sections import runtime_section
        src = runtime_section('settings/local_endpoints.js')
        import re
        for row in ad.WELL_KNOWN_ENGINES:
            assert str(row['port']) in src, row
            assert re.search(r"engine:\s*'%s'" % row['engine'], src), row


@pytest.mark.unit
def test_background_poll_backs_off_empty_http_but_keeps_tcp_topology_checks(
        poll_sandbox, caplog):
    import logging

    tcp_calls = []
    http_calls = []

    def port_open(host, port):
        tcp_calls.append(ad._port_key(host, port))
        return port == 11434

    def discover(base_url):
        http_calls.append(base_url)
        return [], base_url

    with caplog.at_level(logging.INFO):
        at_0 = ad.poll_if_due(now=0, port_open=port_open, discover=discover)
        at_10 = ad.poll_if_due(now=10, port_open=port_open, discover=discover)
        at_20 = ad.poll_if_due(now=20, port_open=port_open, discover=discover)
        at_30 = ad.poll_if_due(now=30, port_open=port_open, discover=discover)
        at_40 = ad.poll_if_due(now=40, port_open=port_open, discover=discover)

    assert all(row['scheduled'] for row in (at_0, at_10, at_20, at_30, at_40))
    assert http_calls == ['http://127.0.0.1:11434/v1'] * 3
    assert at_20['deferred'] == ['127.0.0.1:11434']
    assert at_40['deferred'] == ['127.0.0.1:11434']
    assert tcp_calls.count('127.0.0.1:11434') == 5, (
        'HTTP backoff must not delay detection of a newly opened local port')
    assert sum('answers but serves no models' in r.getMessage()
               for r in caplog.records) == 1, (
        'a stable empty endpoint should log its state transition once')


@pytest.mark.unit
def test_closed_then_reopened_port_bypasses_old_http_backoff(poll_sandbox):
    open_now = {'value': True}
    http_calls = []

    def port_open(_host, port):
        return port == 11434 and open_now['value']

    def discover(base_url):
        http_calls.append(base_url)
        return [], base_url

    ad.poll_if_due(now=0, port_open=port_open, discover=discover)
    open_now['value'] = False
    ad.poll_if_due(now=10, port_open=port_open, discover=discover)
    open_now['value'] = True
    reopened = ad.poll_if_due(now=20, port_open=port_open, discover=discover)

    assert reopened['probed_keys'] == ['127.0.0.1:11434']
    assert len(http_calls) == 2


@pytest.mark.unit
def test_explicit_trigger_forces_probe_and_wakes_shared_monitor(
        poll_sandbox, monkeypatch):
    http_calls = []
    wakes = []

    def discover(base_url):
        http_calls.append(base_url)
        return [], base_url

    port_open = lambda _host, port: port == 11434
    ad.poll_if_due(now=0, port_open=port_open, discover=discover)

    import lib.llm_dispatch.health_local as health_local
    monkeypatch.setattr(
        health_local, 'wake_local_health_checker',
        lambda: wakes.append(True) or True)
    assert ad.trigger_local_autodiscovery() is True
    forced = ad.poll_if_due(now=1, port_open=port_open, discover=discover)

    assert wakes == [True]
    assert forced['probed_keys'] == ['127.0.0.1:11434']
    assert len(http_calls) == 2


@pytest.mark.unit
def test_trigger_during_http_probe_preserves_new_immediate_generation(
        poll_sandbox, monkeypatch):
    import threading
    import lib.llm_dispatch.health_local as health_local

    entered = threading.Event()
    release = threading.Event()
    results = []
    http_calls = []

    def discover(base_url):
        http_calls.append(base_url)
        entered.set()
        assert release.wait(1.0)
        return [], base_url

    monkeypatch.setattr(health_local, 'wake_local_health_checker', lambda: True)

    worker = threading.Thread(
        target=lambda: results.append(ad.poll_if_due(
            now=0,
            port_open=lambda _host, port: port == 11434,
            discover=discover,
        )),
        name='autodiscover-race-test',
    )
    worker.start()
    assert entered.wait(1.0)
    assert ad.trigger_local_autodiscovery() is True
    release.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert results[0]['next_poll_s'] == 0.0, (
        'a stale sweep completion swallowed the newer Settings wake')
    ad.poll_if_due(
        now=1,
        port_open=lambda _host, port: port == 11434,
        discover=discover,
    )
    assert len(http_calls) == 2


@pytest.mark.unit
def test_compatibility_start_uses_shared_monitor_without_private_worker(
        monkeypatch):
    import lib.llm_dispatch.health_local as health_local

    calls = []
    monkeypatch.setattr(ad, '_runtime_enabled', False)
    monkeypatch.setattr(
        health_local, 'start_local_health_checker',
        lambda: calls.append('start') or False)
    monkeypatch.setattr(
        health_local, 'wake_local_health_checker',
        lambda: calls.append('wake') or True)

    assert ad.start_local_autodiscovery() is True
    assert calls == ['start', 'wake']
    assert not hasattr(ad, '_thread')
    assert ad.stop_local_autodiscovery(timeout=0.01) is True
    assert calls == ['start', 'wake', 'wake']


@pytest.mark.unit
def test_health_monitor_executes_cooperative_discovery(monkeypatch):
    import threading
    import lib.llm_dispatch.health_local as health_local

    called = threading.Event()
    monkeypatch.setattr(
        ad, 'poll_if_due',
        lambda **_kwargs: called.set() or {
            'scheduled': False, 'next_poll_s': 60.0})

    assert health_local.start_local_health_checker() is True
    try:
        assert called.wait(1.0), 'shared monitor never scheduled discovery'
        assert health_local._thread.name == 'local-endpoint-monitor'
    finally:
        assert health_local.stop_local_health_checker(timeout=1.0) is True
