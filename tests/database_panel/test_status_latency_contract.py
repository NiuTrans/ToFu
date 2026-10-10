"""Settings entry cannot run launch admission or write-capability probes."""
from pathlib import Path
from unittest.mock import patch
import pytest
from lib.database_panel import probe, switch

pytestmark = pytest.mark.unit


def test_cold_status_avoids_launch_and_write_probes(tmp_path):
    root = tmp_path / 'data'
    authority = root / 'tofudb-authority'
    authority.mkdir(parents=True)
    (authority / 'active.wal').write_bytes(b'content')
    runtime = {'backend': 'tofudb', 'ready': True, 'state': 'ready'}
    probe._fs_cache.update(at=0, path='', report=None)
    forbidden = AssertionError('Settings entry must not perform heavy admission')
    with patch.object(probe, 'data_root', return_value=root), \
         patch.object(probe, 'configured_backend', return_value='tofudb'), \
         patch.object(probe, 'deployment_mode', return_value='personal'), \
         patch.object(probe, '_dir_bytes_bounded', return_value=None), \
         patch.object(probe, 'resolve_tofudb_binary', side_effect=forbidden), \
         patch.object(probe, 'tofudb_authority_has_data', side_effect=forbidden), \
         patch('lib.storage_sidecar.storage_capabilities.probe_storage_path', side_effect=forbidden), \
         patch('serverctl_pkg.native_engine.source_fingerprint', side_effect=forbidden), \
         patch('serverctl_pkg.native_engine.build_identity', side_effect=forbidden):
        state = probe.engine_status(runtime)
        described = switch.describe(state)
    assert state['ready']
    assert state['engines']['tofudb']['available'] is True
    assert state['engines']['tofudb']['availabilityState'] == 'running'
    assert state['engines']['tofudb']['launchVerification'] == 'not-checked'
    assert state['filesystem']['evidence'] == 'read-only'
    assert described['tofudb']['hasData'] is True


def test_stopped_engine_is_unknown_not_uninstalled(tmp_path):
    with patch.object(probe, 'data_root', return_value=tmp_path), \
         patch.object(probe, 'configured_backend', return_value='tofudb'), \
         patch.object(probe, 'deployment_mode', return_value='personal'), \
         patch.object(probe, '_fs_report', return_value={}):
        state = probe.engine_status({'backend': 'tofudb', 'ready': False})
    assert state['engines']['tofudb']['available'] is None
    assert state['engines']['tofudb']['availabilityState'] == 'unknown'


def test_discovery_concurrent_request_does_not_duplicate_probe():
    probe._discovery_lock.acquire()
    try:
        with patch('lib.storage_sidecar.storage_capabilities.probe_storage_path', side_effect=AssertionError('duplicate scan')):
            state = probe.discover_paths(refresh=True)
        assert state['inProgress'] is True
    finally:
        probe._discovery_lock.release()
