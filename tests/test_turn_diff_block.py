"""tests/test_turn_diff_block.py — Per-turn net-diff block tests.

Covers the Codex-inspired (turn_diff_tracker.rs) summary enrichment:
``build_turn_diff_block`` derives a net unified diff per file from the
modifications journal's pre-images + current disk content, with hard size
caps, and both compaction paths append it to their summaries.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest

import pytest

pytestmark = pytest.mark.unit

# Boot the Flask→Quart shim BEFORE any lib.* imports (see test_hook_taxonomy).
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location(
    'server_for_shim_turndiff_test', 'server.py')
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
del _spec, _mod, _importlib_util

import lib.project_mod as _project_mod
from lib.tasks_pkg.commit_round._turn_diff import (
    _turn_mods,
    build_turn_diff_block,
)

_TASK = {'id': 'td-001', 'convId': 'cd-001', 'created_at': 1_000.0,
         '_userId': 1, 'config': {}}


def _mod(path, *, task_id='td-001', existed=True, original=None,
         mod_type='write_file', ts=2_000.0):
    m = {'type': mod_type, 'path': path, 'timestamp': ts,
         'taskId': task_id, 'convId': 'cd-001', 'existed': existed}
    if original is not None:
        if isinstance(original, bytes):
            m['originalContent'] = base64.b64encode(original).decode('ascii')
            m['originalContentB64'] = True
        else:
            m['originalContent'] = original
    return m


class _JournalSandbox(unittest.TestCase):
    """Fake the journal + a tmp workspace; restore both afterwards."""

    def setUp(self):
        self._orig_get_mods = _project_mod.get_modifications
        self._journal: dict[str, list] = {}
        _project_mod.get_modifications = (
            lambda root, conv_id=None: list(self._journal.get(root, [])))
        self._tmpdir = tempfile.mkdtemp(prefix='turndiff_')
        self.addCleanup(self._cleanup)

    def tearDown(self):
        _project_mod.get_modifications = self._orig_get_mods

    def _cleanup(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, rel, content):
        target = os.path.join(self._tmpdir, rel)
        os.makedirs(os.path.dirname(target) or self._tmpdir,
                    exist_ok=True)
        with open(target, 'w') as f:
            f.write(content)
        return target

    def _block(self, mods, **kw):
        self._journal[self._tmpdir] = mods
        return build_turn_diff_block(_TASK, self._tmpdir, None, **kw)


class TestTurnDiffBlock(_JournalSandbox):

    def test_modified_file_produces_unified_diff(self):
        self._write('a.py', 'line1\nline2 changed\nline3\n')
        block = self._block([
            _mod('a.py', original='line1\nline2\nline3\n')])
        self.assertIsNotNone(block)
        self.assertIn('### Files Modified This Turn', block)
        self.assertIn('1 file(s) changed this turn: +1/−1', block)
        self.assertIn('**a.py** (modified', block)
        self.assertIn('-line2', block)
        self.assertIn('+line2 changed', block)

    def test_created_file_all_adds(self):
        self._write('new.py', 'alpha\nbeta\n')
        block = self._block([_mod('new.py', existed=False)])
        self.assertIn('**new.py** (created, +2/−0)', block)
        self.assertIn('(new file)', block)

    def test_deleted_file(self):
        # No file on disk → deleted.
        block = self._block([
            _mod('gone.py', original='old content\n')])
        self.assertIn('**gone.py** (deleted, +0/−1)', block)

    def test_edited_then_reverted_is_skipped(self):
        self._write('same.py', 'identical\n')
        block = self._block([_mod('same.py', original='identical\n')])
        self.assertIsNone(block)

    def test_created_then_deleted_is_skipped(self):
        block = self._block([_mod('ghost.py', existed=False)])
        self.assertIsNone(block)

    def test_first_preimage_wins_for_repeat_mods(self):
        self._write('multi.py', 'v3\n')
        block = self._block([
            _mod('multi.py', original='v1\n', ts=2_000.0),
            _mod('multi.py', original='v2\n', ts=3_000.0),
        ])
        self.assertIn('-v1', block)
        self.assertIn('+v3', block)
        self.assertNotIn('v2', block)

    def test_task_isolation_and_ts_fallback(self):
        self._write('mine.py', 'now\n')
        other = _mod('mine.py', task_id='OTHER', original='zzz\n')
        block = self._block([other])
        # Legacy fallback: the foreign-task mod is newer than task start,
        # so the ts filter adopts it (mirrors _derive semantics).
        self.assertIn('-zzz', block)
        # An OLD foreign mod (before task start) is ignored entirely.
        block = self._block([_mod('mine.py', task_id='OTHER',
                                  original='zzz\n', ts=10.0)])
        self.assertIsNone(block)

    def test_max_files_cap_lists_remainder_without_diff(self):
        for k in range(4):
            self._write(f'f{k}.py', f'new {k}\n')
        block = self._block(
            [_mod(f'f{k}.py', original=f'old {k}\n') for k in range(4)],
            max_files=2)
        self.assertIn('4 file(s) changed', block)
        self.assertIn('2 file(s) listed without diff', block)
        self.assertEqual(block.count('**f'), 2)

    def test_oversized_file_gets_stats_only(self):
        big = 'x\n' * 5000
        self._write('big.py', big + 'tail\n')
        block = self._block(
            [_mod('big.py', original=big)],
            max_file_chars=1000)
        self.assertIn('1 file(s) changed', block)
        self.assertIn('listed without diff', block)
        self.assertNotIn('```diff', block)

    def test_binary_baseline_omitted(self):
        self._write('bin.dat', 'text\n')
        block = self._block([
            _mod('bin.dat', original=b'\x89PNG\r\n')])
        self.assertIn('1 file(s) changed', block)
        self.assertIn('listed without diff', block)

    def test_no_task_or_root_returns_none(self):
        self.assertIsNone(build_turn_diff_block(None, self._tmpdir))
        self.assertIsNone(build_turn_diff_block(_TASK, ''))
        self.assertIsNone(self._block([]))


class TestTurnModsTaskScoping(_JournalSandbox):

    def test_taskid_match_preferred_over_ts(self):
        own = _mod('a.py', task_id='td-001', ts=10.0)
        foreign_new = _mod('b.py', task_id='OTHER', ts=9_999.0)
        self._journal[self._tmpdir] = [own, foreign_new]
        mods = _turn_mods(_TASK, [self._tmpdir])
        # taskId matches exist → NO ts fallback (foreign mod excluded even
        # though its timestamp is after task start) — mirrors _derive.
        self.assertEqual([m['path'] for m in mods], ['a.py'])


class TestCompactionWiring(_JournalSandbox):
    """The automatic L2 summary carries the diff block when present."""

    def setUp(self):
        super().setUp()
        import lib.tasks_pkg.compaction._layer2._compact as _l2
        self._l2 = _l2
        self._orig_summary = _l2._generate_query_aware_summary
        self._orig_archive = _l2._archive_transcript
        _l2._generate_query_aware_summary = (
            lambda msgs, query, pfx, **kw: 'SUMMARY')
        _l2._archive_transcript = lambda *a, **kw: 1

    def tearDown(self):
        self._l2._generate_query_aware_summary = self._orig_summary
        self._l2._archive_transcript = self._orig_archive
        super().tearDown()

    def test_execute_compact_tool_appends_diff_block(self):
        from lib.tasks_pkg.compaction.api import execute_compact_tool
        self._write('code.py', 'after\n')
        self._journal[self._tmpdir] = [
            _mod('code.py', original='before\n')]
        task = dict(_TASK)
        task['config'] = {'projectPath': self._tmpdir,
                          'projectPaths': [self._tmpdir]}
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'the goal'},
            {'role': 'assistant', 'content': 'old work'},
            {'role': 'user', 'content': 'current turn'},
            {'role': 'assistant', 'content': 'working'},
        ]
        result = execute_compact_tool(
            messages, task=task, preserve_budget_tokens=1)
        self.assertIn('### Files Modified This Turn', result)
        self.assertIn('**code.py** (modified', result)
        self.assertIn('-before', result)
        self.assertIn('+after', result)


if __name__ == '__main__':
    unittest.main()
