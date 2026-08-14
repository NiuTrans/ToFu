#!/usr/bin/env python3
"""tests/test_probe_cells_scoped.py — scoped (row / column / cell) probes.

The access matrix gained per-scope probe triggers (2026-07-25): every key
header can probe its COLUMN, every model row its ROW, and every verdict pip
its own CELL. The frontend sends ``only={key_idxs?, model_ids?}`` to
``/api/v1/providers/probe-cells/start``.

Contract asserted here
----------------------
  * FILTER — the work list is restricted to exactly the requested cells
    (column = one key across all ids; row = one id across all keys; cell =
    one (key, id) pair). An empty intersection → HTTP 400.
  * MERGE — a scoped run SEEDS its task from the persisted disk snapshot so
    fresh verdicts merge into the full grid instead of wiping the rest:
    the pre-existing cells of OTHER rows/columns survive, are persisted,
    and appear in the returned snapshot.
  * PRUNE — seed cells whose (key_idx, model_id) no longer exists in the
    provider's current shape (model deleted / key removed since the cache
    was written) are dropped — no ghost pips.
  * PROGRESS — done_count counts only THIS RUN's completions; seeded cells
    never inflate it; total == the scoped work count.
  * SUMMARY — summary counts the MERGED set from the first poll (a seeded
    flagged cell shows up in ``disable`` immediately).
  * NO CACHE SHORT-CIRCUIT — a scoped start always launches a fresh run even
    when a disk snapshot exists (the whole point is to re-test those cells);
    the unscoped resume-from-disk path is unchanged (regression pin).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit

import lib.provider_probe as pp  # noqa: E402

try:
    import routes.config as cfg
    _CFG_IMPORT_ERROR = None
except ImportError as e:  # pragma: no cover - sibling-refactor guard
    cfg = None
    _CFG_IMPORT_ERROR = e


def _make_app():
    from quart import Quart
    app = Quart('probe-scoped')
    app.register_blueprint(cfg.config_bp)
    return app


_MODELS = [
    {'model_id': 'm1', 'aliases': ['m1-a'], 'capabilities': ['text']},
    {'model_id': 'm2', 'capabilities': ['text']},
]
_KEYS = ['sk-a', 'sk-b']

_BASE_BODY = {
    'provider_id': 't-scoped',
    'base_url': 'https://gw.example.com/v1',
    'api_keys': _KEYS,
    'models': _MODELS,
    'attempts': 1,
}


def _snapshot(cells):
    """A persisted snapshot dict shaped like public_probe_snapshot()."""
    return {
        'provider_id': 't-scoped',
        'status': 'done',
        'started_at': 1, 'finished_at': 2,
        'total': len(cells), 'done_count': len(cells),
        'attempts': 1,
        'cells': cells,
        'summary': {'ok': 0, 'disable': 0},
        'error': None,
    }


def _cell(key_idx, model_id, status='ok', recommend_disable=False):
    return {
        'key_idx': key_idx, 'model_id': model_id, 'root_model_id': model_id,
        'status': status, 'detail': 'prev',
        'recommend_disable': recommend_disable,
        'probe_surface': 'chat',
    }


@unittest.skipIf(cfg is None, 'routes.config unimportable: %s' % _CFG_IMPORT_ERROR)
class ScopedStartTest(unittest.TestCase):
    """Route-level: work filtering + seed/merge + cache short-circuit rules."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.captured = {}
        self._done = threading.Event()

        def stub(task, work, timeout):
            self.captured['task'] = task
            self.captured['work'] = list(work)
            self._done.set()

        self._patchers = [
            mock.patch.object(cfg, '_run_cell_probe_task', side_effect=stub),
            mock.patch.object(cfg, '_probe_cache_path',
                              lambda pid: os.path.join(self._tmp.name, pid + '.json')),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        with pp.CELL_PROBE_LOCK:
            pp.CELL_PROBE_TASKS.pop('t-scoped', None)
        self.addCleanup(self._drop_task)

    def _drop_task(self):
        with pp.CELL_PROBE_LOCK:
            pp.CELL_PROBE_TASKS.pop('t-scoped', None)

    def _write_cache(self, cells):
        path = os.path.join(self._tmp.name, 't-scoped.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(_snapshot(cells), f)

    def _start(self, **extra):
        body = dict(_BASE_BODY)
        body.update(extra)
        app = _make_app()

        async def _post():
            async with app.test_client() as client:
                return await client.post('/api/v1/providers/probe-cells/start',
                                         json=body)
        resp = asyncio.run(_post())
        return resp

    def _work_keys(self):
        return {(w[0], w[3]) for w in self.captured['work']}

    # ── FILTER ──────────────────────────────────────────────────────

    def test_column_scope_probes_one_key_all_ids(self):
        resp = self._start(only={'key_idxs': [1]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self._done.wait(5))
        self.assertEqual(self._work_keys(),
                         {(1, 'm1'), (1, 'm1-a'), (1, 'm2')})
        self.assertEqual(self.captured['task']['total'], 3)

    def test_row_scope_probes_one_id_all_keys(self):
        resp = self._start(only={'model_ids': ['m1-a']})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._work_keys(), {(0, 'm1-a'), (1, 'm1-a')})

    def test_cell_scope_probes_exactly_one_pair(self):
        resp = self._start(only={'key_idxs': [0], 'model_ids': ['m2']})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._work_keys(), {(0, 'm2')})
        self.assertEqual(self.captured['task']['total'], 1)

    def test_scope_matching_nothing_is_400(self):
        resp = self._start(only={'model_ids': ['deleted-model']})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('work', self.captured,
                         'no run may start when the scope matches nothing')

    def test_empty_scope_lists_behave_like_unscoped(self):
        """only={key_idxs:[], model_ids:[]} is not a scope at all."""
        resp = self._start(only={'key_idxs': [], 'model_ids': []}, force=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.captured['work']), 6,
                         '2 keys × (m1 + m1-a + m2) — full grid')

    # ── MERGE + PRUNE ───────────────────────────────────────────────

    def test_scoped_run_seeds_other_cells_and_prunes_ghosts(self):
        cache_cells = {
            '0::m1': _cell(0, 'm1'),
            '1::m2': _cell(1, 'm2', status='rate_limited', recommend_disable=True),
            '0::ghost': _cell(0, 'ghost'),          # model deleted since
            '3::m1': _cell(3, 'm1'),                # key removed since
        }
        self._write_cache(cache_cells)
        resp = self._start(only={'key_idxs': [0]})   # probe column 0
        self.assertEqual(resp.status_code, 200)
        task = self.captured['task']
        seeds = task['cells']
        # Probed cells are NOT seeded (they're being re-tested).
        self.assertNotIn('0::m1', seeds)
        # Other rows/columns keep their previous verdicts.
        self.assertIn('1::m2', seeds)
        # Ghost cells pruned.
        self.assertNotIn('0::ghost', seeds)
        self.assertNotIn('3::m1', seeds)
        self.assertEqual(len(seeds), 1)
        # The returned snapshot already carries the merged picture.
        # (response body = public_probe_snapshot of the seeded task)

    def test_scoped_start_does_not_return_cache_without_running(self):
        """Even with a complete disk snapshot, a scoped start must LAUNCH a
        fresh run — re-testing the requested cells is the whole point."""
        self._write_cache({'0::m1': _cell(0, 'm1')})
        resp = self._start(only={'model_ids': ['m1']})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self._done.wait(5),
                        'scoped start was short-circuited by the disk cache')

    def test_unscoped_start_still_resumes_from_disk(self):
        """Regression pin: the classic resume path is untouched."""
        self._write_cache({'0::m1': _cell(0, 'm1')})
        resp = self._start()  # no only, no force
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('work', self.captured,
                         'unscoped start must keep returning the disk snapshot')

    def test_force_full_grid_unaffected_by_scope_machinery(self):
        self._write_cache({'0::m1': _cell(0, 'm1')})
        resp = self._start(force=True)
        self.assertEqual(resp.status_code, 200)
        task = self.captured['task']
        self.assertEqual(task['cells'], {}, 'a full force retest starts empty')
        self.assertEqual(len(self.captured['work']), 6)


class SeededRunTest(unittest.TestCase):
    """Engine-level: progress + summary accounting with seeded cells."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig = pp.probe_cache_path
        pp.probe_cache_path = lambda pid: os.path.join(self._tmp.name, pid + '.json')
        self.addCleanup(self._restore)

    def _restore(self):
        pp.probe_cache_path = self._orig

    def test_done_count_excludes_seeds_summary_includes_them(self):
        task = {
            'provider_id': 't-seeded',
            'status': 'running',
            'started_at': 0, 'finished_at': None,
            'total': 2, 'done_count': 0,
            # 2 seeded cells (previous verdicts for other rows/columns), one
            # of them flagged — the merged summary must count it immediately.
            'cells': {
                '1::m1': _cell(1, 'm1'),
                '1::m2': _cell(1, 'm2', status='not_found', recommend_disable=True),
            },
            'summary': {'ok': 0, 'disable': 0},
            'error': None, 'attempts': 1, '_abort': False,
            '_base_url': 'https://gw.example.com/v1', '_extra_headers': {},
        }
        work = [(0, 'sk-a', 'm1', 'm1', ['text']), (0, 'sk-a', 'm2', 'm2', ['text'])]
        with mock.patch.object(pp, 'probe_cell_multi', return_value=('ok', 'HTTP 200')):
            pp.run_cell_probe_task(task, work, timeout=5)

        self.assertEqual(task['done_count'], 2,
                         'progress counts only this run — not the 2 seeds')
        self.assertEqual(len(task['cells']), 4, 'merged: 2 seeds + 2 probed')
        self.assertEqual(task['summary'],
                         {'ok': 3, 'disable': 1, 'skipped': 0,
                          'neutral': 0, 'failed': 1},
                         'summary reflects the merged grid incl. seeded flag')
        self.assertEqual(task['status'], 'done')

    def test_unseeded_run_accounting_unchanged(self):
        """Regression pin: classic full-grid runs still count len(cells)."""
        task = {
            'provider_id': 't-unseeded',
            'status': 'running',
            'started_at': 0, 'finished_at': None,
            'total': 2, 'done_count': 0, 'cells': {},
            'summary': {'ok': 0, 'disable': 0},
            'error': None, 'attempts': 1, '_abort': False,
            '_base_url': 'https://gw.example.com/v1', '_extra_headers': {},
        }
        work = [(0, 'sk-a', 'm1', 'm1', ['text']), (0, 'sk-a', 'm2', 'm2', ['text'])]
        with mock.patch.object(pp, 'probe_cell_multi',
                               return_value=('rate_limited', 'HTTP 429')):
            pp.run_cell_probe_task(task, work, timeout=5)
        self.assertEqual(task['done_count'], 2)
        self.assertEqual(task['summary'],
                         {'ok': 0, 'disable': 2, 'skipped': 0,
                          'neutral': 0, 'failed': 2})


if __name__ == '__main__':
    unittest.main()
