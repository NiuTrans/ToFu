"""tests/test_probe_persistence.py — Background cell-probe persistence.

Covers the server-owned probe task in ``routes.config``:

* ``_run_cell_probe_task`` fans out cells, fills the task, marks it done.
* The task is persisted to disk (``_probe_cache_path``) as a secret-free
  snapshot — so closing Settings / restarting the server doesn't lose it.
* ``_public_probe_snapshot`` never leaks API keys.

We patch ``_probe_one_cell`` so no network is touched and point the probe
cache at a temp dir.
"""

import os
import tempfile
import unittest
from unittest import mock


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
