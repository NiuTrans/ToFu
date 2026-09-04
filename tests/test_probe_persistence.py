"""tests/test_probe_persistence.py — Background cell-probe persistence.

Covers the server-owned probe task in ``lib.provider_probe``:

* ``run_cell_probe_task`` fans out cells, fills the task, marks it done.
* The task is persisted to disk (``probe_cache_path``) as a secret-free
  snapshot — so closing Settings / restarting the server doesn't lose it.
* ``public_probe_snapshot`` never leaks API keys.

We patch ``probe_one_cell`` so no network is touched and point the probe
cache at a temp dir.
"""

import os
import tempfile
import unittest
from unittest import mock

import pytest


pytestmark = pytest.mark.unit


class ProbePersistenceTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # The cell-probe engine moved to lib/provider_probe.py (2026-06);
        # run_cell_probe_task resolves probe_one_cell / probe_cache_path
        # through that module, so patch/redirect there.
        import lib.provider_probe as pp
        self.cfg = pp
        # Redirect the probe-cache path into the temp dir.
        self._orig_cache_path = pp.probe_cache_path
        pp.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')

    def tearDown(self):
        self.cfg.probe_cache_path = self._orig_cache_path
        self._tmp.cleanup()

    def _fake_probe(self, base_url, api_key, model_id, extra_headers, timeout,
                    protocol='openai', oauth='', adapter=None):
        # mx-dead is unreachable for everyone; everything else is ok.
        if model_id == 'mx-dead':
            return 'not_found', 'HTTP 404'
        return 'ok', 'HTTP 200'

    def test_background_task_runs_persists_and_recommends(self):
        cfg = self.cfg
        task = {
            'provider_id': 'mt',
            'status': 'running',
            'started_at': 0, 'finished_at': None,
            'total': 4, 'done_count': 0,
            'cells': {}, 'summary': {'ok': 0, 'disable': 0},
            'error': None, '_abort': False,
            '_base_url': 'https://gw.example.com/v1',
            '_extra_headers': {},
        }
        # 2 keys × (root + 1 alias 'mx-dead') = 4 cells.
        work = [
            (0, 'sk-aaa', 'modelX', 'modelX'),
            (0, 'sk-aaa', 'modelX', 'mx-dead'),
            (1, 'sk-bbb', 'modelX', 'modelX'),
            (1, 'sk-bbb', 'modelX', 'mx-dead'),
        ]
        with cfg.CELL_PROBE_LOCK:
            cfg.CELL_PROBE_TASKS['mt'] = task
        with mock.patch.object(cfg, 'probe_one_cell', side_effect=self._fake_probe):
            cfg.run_cell_probe_task(task, work, timeout=5)

        self.assertEqual(task['status'], 'done')
        self.assertEqual(task['done_count'], 4)
        # Two mx-dead cells should be flagged for disable.
        self.assertEqual(task['summary']['disable'], 2)
        self.assertEqual(task['summary']['ok'], 2)

        # The mx-dead cells are recommend_disable; the modelX cells are not.
        dead0 = task['cells'][cfg.probe_cell_key(0, 'mx-dead')]
        self.assertTrue(dead0['recommend_disable'])
        self.assertEqual(dead0['root_model_id'], 'modelX')
        ok0 = task['cells'][cfg.probe_cell_key(0, 'modelX')]
        self.assertFalse(ok0['recommend_disable'])

        # Persisted snapshot exists on disk and matches.
        from lib.json_store import read_json
        disk = read_json(cfg.probe_cache_path('mt'), default=None)
        self.assertIsInstance(disk, dict)
        self.assertEqual(disk['status'], 'done')
        self.assertEqual(len(disk['cells']), 4)
        with cfg.CELL_PROBE_LOCK:
            self.assertNotIn(
                'mt', cfg.CELL_PROBE_TASKS,
                'durable terminal probes must release private in-memory state',
            )

    def test_failed_terminal_persist_keeps_only_bounded_public_fallback(self):
        cfg = self.cfg
        task = {
            'provider_id': 'persist-failed',
            'status': 'running',
            'started_at': 0, 'finished_at': None,
            'total': 1, 'done_count': 0,
            'cells': {}, 'summary': {'ok': 0, 'disable': 0},
            'error': None, 'attempts': 1, '_abort': False,
            '_base_url': 'https://gw.example.com/v1',
            '_extra_headers': {'X-Secret': 'shh'},
        }
        work = [(0, 'sk-secret', 'modelX', 'modelX')]
        with cfg.CELL_PROBE_LOCK:
            cfg.CELL_PROBE_TASKS['persist-failed'] = task

        def cleanup_registry():
            with cfg.CELL_PROBE_LOCK:
                cfg.CELL_PROBE_TASKS.pop('persist-failed', None)

        self.addCleanup(cleanup_registry)

        with mock.patch.object(
            cfg, 'write_json_atomic', side_effect=OSError('disk full')
        ), mock.patch.object(
            cfg, 'probe_one_cell', side_effect=self._fake_probe
        ):
            cfg.run_cell_probe_task(task, work, timeout=5)

        with cfg.CELL_PROBE_LOCK:
            fallback = cfg.CELL_PROBE_TASKS['persist-failed']
        self.assertEqual(fallback['status'], 'done')
        self.assertNotIn('_base_url', fallback)
        self.assertNotIn('_extra_headers', fallback)
        self.assertNotIn('_abort', fallback)
        fallback_capacity = cfg.provider_probe_runtime_snapshot()[
            'terminalFallbackCapacity']
        self.assertGreaterEqual(fallback_capacity, 4)
        self.assertLessEqual(fallback_capacity, 32)

    def test_lane_releases_owned_credentials_before_idle_worker_retires(self):
        cfg = self.cfg
        task = {
            'provider_id': 'lane-scrub',
            'status': 'running',
            'started_at': 0, 'finished_at': None,
            'total': 1, 'done_count': 0, 'cells': {},
            'summary': {'ok': 0, 'disable': 0}, 'error': None,
            '_abort': False, '_base_url': 'https://gw.example.com/v1',
            '_extra_headers': {'X-Secret': 'header-secret'},
        }
        caller_work = [(0, 'sk-secret', 'modelX', 'modelX')]
        captured = {}

        def runner(owned_task, owned_work, timeout):
            captured['work'] = owned_work
            owned_task['status'] = 'done'
            owned_task['finished_at'] = 2

        with cfg.CELL_PROBE_LOCK:
            cfg.CELL_PROBE_TASKS['lane-scrub'] = task
        with mock.patch.object(cfg, 'persist_probe_task', return_value=True):
            future = cfg.submit_provider_probe_task(
                task, caller_work, 5, runner=runner)
            future.result(timeout=5)

        self.assertEqual(captured['work'], [])
        self.assertEqual(caller_work[0][1], 'sk-secret')
        self.assertFalse(any(str(key).startswith('_') for key in task))
        with cfg.CELL_PROBE_LOCK:
            self.assertNotIn('lane-scrub', cfg.CELL_PROBE_TASKS)

    def test_lane_runner_crash_is_terminal_and_preserves_original_error(self):
        cfg = self.cfg
        task = {
            'provider_id': 'lane-crash',
            'status': 'running',
            'started_at': 0, 'finished_at': None,
            'total': 1, 'done_count': 0, 'cells': {},
            'summary': {'ok': 0, 'disable': 0}, 'error': None,
            '_abort': False, '_base_url': 'https://gw.example.com/v1',
            '_extra_headers': {'X-Secret': 'header-secret'},
        }

        def runner(_task, _work, _timeout):
            raise RuntimeError('probe runner crashed')

        with cfg.CELL_PROBE_LOCK:
            cfg.CELL_PROBE_TASKS['lane-crash'] = task
        with mock.patch.object(cfg, 'persist_probe_task', return_value=True):
            future = cfg.submit_provider_probe_task(
                task, [(0, 'sk-secret', 'modelX', 'modelX')], 5,
                runner=runner)
            with self.assertRaisesRegex(RuntimeError, 'probe runner crashed'):
                future.result(timeout=5)

        self.assertEqual(task['status'], 'error')
        self.assertIn('probe runner crashed', str(task['error']))
        self.assertFalse(any(str(key).startswith('_') for key in task))
        with cfg.CELL_PROBE_LOCK:
            self.assertNotIn('lane-crash', cfg.CELL_PROBE_TASKS)

    def test_public_snapshot_has_no_secrets(self):
        cfg = self.cfg
        task = {
            'provider_id': 'mt', 'status': 'running',
            'started_at': 1, 'finished_at': None,
            'total': 1, 'done_count': 0,
            'cells': {}, 'summary': {'ok': 0, 'disable': 0}, 'error': None,
            '_abort': False, '_base_url': 'https://gw/v1',
            '_extra_headers': {'X-Secret': 'shh'},
        }
        snap = cfg.public_probe_snapshot(task)
        # No private (underscore) fields leak into the public snapshot.
        self.assertNotIn('_base_url', snap)
        self.assertNotIn('_extra_headers', snap)
        self.assertNotIn('_abort', snap)
        self.assertEqual(snap['provider_id'], 'mt')
        self.assertEqual(snap['probe_schema_version'], 2)


if __name__ == '__main__':
    unittest.main()
