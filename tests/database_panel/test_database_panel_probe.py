"""tests/database_panel/test_database_panel_probe.py — path discovery + preferences.

The probe ranks a recommended faster path only from writable, persistent,
non-volatile candidates with enough free space; the current data root is
never recommended.  Preferences round-trip through the atomic JSON store
with defaults intact.
"""



from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

import unittest
from unittest.mock import patch

from lib.database_panel import config_store, probe


class _FakeReport:
    def __init__(self, *, storage_class='local-block',
                 persistence='persistent', writable=True,
                 free_bytes=10 * 1024 ** 3, latency=1.0,
                 filesystem_type='ext4'):
        self.storage_class = storage_class
        self.persistence = persistence
        self.writable = writable
        self.free_bytes = free_bytes
        self.probe_latency_ms = latency
        self.filesystem_type = filesystem_type
        self.limitations = ()


class DiscoverPathsTest(unittest.TestCase):

    def setUp(self):
        probe._probe_cache.update({'at': 0.0, 'result': None})

    def _run_discovery(self, reports):
        paths = [f'/fake/{name}' for name in reports]

        def fake_probe(candidate):
            return reports[str(candidate).rsplit('/', 1)[-1]]

        with patch.object(probe, '_candidate_paths',
                          return_value=[__import__('pathlib').Path(p)
                                        for p in paths]), \
                patch.object(probe, 'data_root',
                             return_value=__import__('pathlib').Path('/fake/root')), \
                patch('lib.storage_authority_sql.storage_capabilities.probe_storage_path',
                      side_effect=fake_probe):
            return probe.discover_paths(refresh=True)

    def test_recommends_lowest_latency_persistent_path(self):
        result = self._run_discovery({
            'root': _FakeReport(latency=50.0),
            'fast': _FakeReport(latency=0.5),
            'slow': _FakeReport(latency=5.0),
        })
        self.assertEqual(result['recommendedPath'], '/fake/fast')
        flags = {e['path']: e['recommended'] for e in result['candidates']}
        self.assertEqual(flags, {'/fake/root': False, '/fake/fast': True,
                                 '/fake/slow': False})

    def test_current_root_and_volatile_never_recommended(self):
        result = self._run_discovery({
            'root': _FakeReport(latency=0.1),   # fastest but current
            'tmp': _FakeReport(storage_class='memory-filesystem',
                               latency=0.2),
            'other': _FakeReport(latency=3.0),
        })
        self.assertEqual(result['recommendedPath'], '/fake/other')
        by_path = {e['path']: e for e in result['candidates']}
        self.assertTrue(by_path['/fake/tmp']['volatile'])

    def test_no_eligible_candidate_means_no_recommendation(self):
        result = self._run_discovery({
            'root': _FakeReport(latency=1.0),
            'full': _FakeReport(free_bytes=1024),
            'ro': _FakeReport(writable=False),
        })
        self.assertEqual(result['recommendedPath'], '')

    def test_cache_serves_second_call(self):
        first = self._run_discovery({'root': _FakeReport(),
                                     'fast': _FakeReport(latency=0.5)})
        with patch('lib.storage_authority_sql.storage_capabilities.probe_storage_path',
                   side_effect=AssertionError('must not re-probe')):
            second = probe.discover_paths()
        self.assertEqual(first, second)


class DirBytesBoundedTest(unittest.TestCase):
    """The status walk must never hold the request thread open."""

    def setUp(self):
        probe._dir_bytes_cache.clear()
        probe._dir_bytes_inflight.clear()

    def tearDown(self):
        probe._dir_bytes_cache.clear()
        probe._dir_bytes_inflight.clear()

    def test_request_path_serves_cache_and_refreshes_in_background(self):
        import threading
        from pathlib import Path

        # A walk that blocks until released simulates a slow FUSE tree.
        release = threading.Event()
        walk_started = threading.Event()

        def slow_walk(root):
            walk_started.set()
            release.wait(5)
            return 12345

        with patch.object(probe, '_walk_dir_bytes', side_effect=slow_walk):
            # Cold start: no total yet, returns instantly (None) not 20s.
            self.assertIsNone(probe._dir_bytes_bounded(Path('/fake/auth')))
            self.assertTrue(walk_started.wait(5))
            # While the walk is still running, callers are served instantly
            # and no second walk piles onto IO.
            self.assertIsNone(probe._dir_bytes_bounded(Path('/fake/auth')))
            release.set()

        # The background walk fills the cache; the next caller is served
        # the completed total without any walk.
        for _ in range(100):
            with probe._cache_lock:
                done = not probe._dir_bytes_inflight
            if done:
                break
            threading.Event().wait(0.05)
        with patch.object(probe, '_walk_dir_bytes',
                          side_effect=AssertionError('must not re-walk')):
            self.assertEqual(
                probe._dir_bytes_bounded(Path('/fake/auth')), 12345)

    def test_stale_total_is_served_while_refresh_runs(self):
        import threading
        import time
        from pathlib import Path

        # An expired cache entry must be served (stale) rather than blocking.
        probe._dir_bytes_cache['/fake/auth'] = {
            'at': time.monotonic() - probe._DIR_BYTES_CACHE_TTL_S - 1,
            'total': 777}
        release = threading.Event()

        def slow_walk(root):
            release.wait(5)
            return 999

        with patch.object(probe, '_walk_dir_bytes', side_effect=slow_walk):
            self.assertEqual(
                probe._dir_bytes_bounded(Path('/fake/auth')), 777)
            release.set()


class FastpathStateTest(unittest.TestCase):

    def test_native_opt_in_defaults_off_and_unknown_until_ready(self):
        with patch.dict('os.environ', {}, clear=True):
            state = probe.fastpath_state('tofudb', {'ready': False})
        self.assertTrue(state['supported'])
        self.assertEqual(state['kind'], 'native')
        self.assertFalse(state['enabled'])
        self.assertEqual(state['mode'], 'off')
        self.assertIsNone(state['active'])

    def test_sqlite_reads_environment(self):
        with patch.dict('os.environ', {
                'TOFU_STORAGE_FASTPATH': 'auto',
                'TOFU_STORAGE_FASTPATH_DIR': '/fast'}):
            state = probe.fastpath_state('sqlite')
        self.assertTrue(state['supported'])
        self.assertTrue(state['enabled'])
        self.assertEqual(state['directory'], '/fast')


class AuthorityWitnessTest(unittest.TestCase):

    def test_empty_init_is_not_data(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            authority = Path(tmp) / 'auth'
            (authority / 'blocks').mkdir(parents=True)
            (authority / 'payload-segments').mkdir()
            (authority / 'active.wal').write_bytes(b'')
            (authority / 'CONTROL').write_bytes(b'\0' * 8192)
            (authority / 'LOCK').write_bytes(b'')
            self.assertFalse(probe.tofudb_authority_has_data(authority))

    def test_wal_content_is_data(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            authority = Path(tmp) / 'auth'
            (authority / 'blocks').mkdir(parents=True)
            (authority / 'payload-segments').mkdir()
            (authority / 'active.wal').write_bytes(b'\1' * 64)
            self.assertTrue(probe.tofudb_authority_has_data(authority))

    def test_block_content_is_data(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            authority = Path(tmp) / 'auth'
            (authority / 'blocks').mkdir(parents=True)
            (authority / 'payload-segments').mkdir()
            (authority / 'active.wal').write_bytes(b'')
            (authority / 'blocks' / 'seg-1').write_bytes(b'\1' * 64)
            self.assertTrue(probe.tofudb_authority_has_data(authority))

    def test_missing_dir_is_not_data(self):
        from pathlib import Path

        self.assertFalse(
            probe.tofudb_authority_has_data(Path('/nonexistent/auth')))


class PreferencesTest(unittest.TestCase):

    def test_round_trip_with_defaults(self):
        writes = {}

        def fake_update(path, mutator, **kwargs):
            current = writes.get('cfg')
            writes['cfg'] = mutator(current)
            return writes['cfg']

        with patch.object(config_store, 'config_path',
                          return_value='/tmp/nonexistent-panel.json'), \
                patch('lib.database_panel.config_store.update_json_atomic',
                      side_effect=fake_update), \
                patch('lib.database_panel.config_store.read_json',
                      side_effect=lambda path, default=None: writes.get('cfg', default)):
            defaults = config_store.load_preferences()
            self.assertFalse(defaults['backup']['enabled'])
            saved = config_store.save_preferences(
                backup_enabled=True,
                backup_directory='/stable')
        self.assertEqual(saved['backup'],
                         {'enabled': True, 'directory': '/stable'})


if __name__ == '__main__':
    unittest.main()


def test_engine_status_reports_the_actual_native_front_from_the_ready_launch(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir()
    front = tmp_path / 'local-authority'
    front.mkdir()
    witness = {'durableAuthority': str(root / 'tofudb-authority'),
               'localAuthority': str(front), 'shadowDirectory': str(root / 'shadow'),
               'syncIntervalMs': 1000}
    monkeypatch.setattr(probe, 'configured_backend', lambda: 'tofudb')
    monkeypatch.setattr(probe, 'data_root', lambda: str(root))
    monkeypatch.setattr(probe, '_fs_report', lambda path: {})
    monkeypatch.setattr(probe, '_dir_bytes_bounded', lambda path: 0)
    monkeypatch.setattr(probe, 'resolve_tofudb_binary', lambda: None)
    monkeypatch.setattr('lib.storage.service.storage_status', lambda: {
        'ready': True, 'state': 'ready', 'native_fastpath': witness})
    state = probe.engine_status()
    assert state['authorityPath'] == str(front)
    assert state['dataRoot'] == str(root)
    assert state['nativeFastpath'] == witness


def test_engine_status_uses_ready_backend_not_pending_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'configured_backend', lambda: 'tofudb')
    monkeypatch.setattr(probe, 'data_root', lambda: str(tmp_path))
    monkeypatch.setattr(probe, '_fs_report', lambda path: {})
    monkeypatch.setattr(probe, 'resolve_tofudb_binary', lambda: None)
    monkeypatch.setattr('lib.storage.service.storage_status', lambda: {
        'ready': True, 'state': 'ready', 'backend': 'sqlite'})
    state = probe.engine_status()
    assert state['backend'] == 'sqlite'
    assert state['nativeFastpath'] is None
    assert state['authorityPath'] == str(tmp_path / 'tofu.db')
