"""tests/test_temp_write_and_root_signal.py — two related write-tool fixes.

Fix 1 — temp-dir scratch writes are UNTRACKED:
  * write_file to an absolute path under the OS temp dir succeeds but
    registers NO workspace root (no bogus ``tmp:`` project) and records NO
    modification (nothing in the undo journal / file-changes bar).
  * This mirrors run_command, whose snapshot/diff only walks ``base_path``
    and therefore already ignores /tmp.

Fix 2 — silent absolute-path auto-registration is now OBSERVABLE:
  * a write to a NON-temp absolute path outside all roots auto-registers the
    nearest existing ancestor AND queues a per-thread ``workspace_root_added``
    signal that the handler drains + emits as an SSE event.
  * a write under an EXISTING registered root resolves directly: no new root,
    no signal (attribution falls to the parent root, as today).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from lib.project_mod import config as cfg
from lib.project_mod import write_tools as wt
from lib.project_mod.modifications import get_modifications
from lib.project_mod.scanner import clear_project, set_project, set_project_paths
from lib.project_mod.write_tools import (
    _is_temp_path,
    _resolve_write_path,
    drain_root_added_signals,
    tool_write_file,
)


class _Base(unittest.TestCase):
    """Hermetic temp-root detection.

    ``tempfile.mkdtemp()`` always returns paths UNDER the OS temp dir, so we
    can't rely on it to produce a "non-temp" fixture. Instead we carve out a
    single base dir and DECLARE one subdir (``tmp/``) to be the temp root by
    overriding ``_temp_roots._cache``. The workspace fixtures live under a
    sibling ``work/`` subdir which is therefore NOT temp.
    """

    def setUp(self):
        self._fs = tempfile.mkdtemp(prefix='twr-fs-')
        self._tmp_root = os.path.join(self._fs, 'tmp')      # designated temp dir
        self._work = os.path.join(self._fs, 'work')         # non-temp workspace area
        os.makedirs(self._tmp_root)
        os.makedirs(self._work)
        # Override temp-root detection deterministically (independent of $TMPDIR).
        self._saved_cache = getattr(wt._temp_roots, '_cache', None)
        wt._temp_roots._cache = {os.path.realpath(self._tmp_root)}

        # Primary project lives in the NON-temp work area.
        self._proj = os.path.join(self._work, 'proj')
        os.makedirs(self._proj)
        # Scratch area lives UNDER the designated temp dir (true temp).
        self._tmp_scratch = os.path.join(self._tmp_root, 'scratch')
        os.makedirs(self._tmp_scratch)

        set_project(self._proj)
        drain_root_added_signals()

    def tearDown(self):
        clear_project()
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()
        drain_root_added_signals()
        # Restore the real temp-root cache so other suites are unaffected.
        wt._temp_roots._cache = self._saved_cache
        shutil.rmtree(self._fs, ignore_errors=True)

    def _roots_paths(self):
        with cfg._lock:
            return {os.path.abspath(rs['path']) for rs in cfg._roots.values()}


class TempPathDetectionTest(_Base):
    def test_designated_temp_root_is_temp(self):
        self.assertTrue(_is_temp_path(os.path.join(self._tmp_root, 'x.py')))

    def test_scratch_dir_is_temp(self):
        self.assertTrue(_is_temp_path(os.path.join(self._tmp_scratch, 'a', 'b.py')))

    def test_project_root_is_not_temp(self):
        self.assertFalse(_is_temp_path(os.path.join(self._proj, 'src', 'main.py')))


class TempWriteUntrackedTest(_Base):
    """Required scenario #1: temp-dir write → no root, no modification."""

    def test_temp_write_succeeds_but_registers_no_root(self):
        roots_before = self._roots_paths()
        target = os.path.join(self._tmp_scratch, 'scratch.py')

        res = tool_write_file(self._proj, target, "print('hi')\n",
                              conv_id='c1', task_id='t1')

        self.assertTrue(res['ok'], res)
        self.assertTrue(os.path.isfile(target))
        # No workspace root was added for the temp dir.
        self.assertEqual(self._roots_paths(), roots_before,
                         'temp-dir write must NOT auto-register a workspace root')
        # And no per-thread root-added signal was queued.
        self.assertEqual(drain_root_added_signals(), [])

    def test_temp_write_records_no_modification(self):
        target = os.path.join(self._tmp_scratch, 'scratch2.py')
        tool_write_file(self._proj, target, 'data\n', conv_id='c1', task_id='t1')

        # The primary project's journal must have no entry for the temp write.
        mods = get_modifications(self._proj, conv_id='c1')
        self.assertEqual([m for m in mods if 'scratch2' in m.get('path', '')], [],
                         'temp-dir write must not be recorded in the undo journal')

    def test_resolve_write_path_temp_returns_abspath_no_root(self):
        roots_before = self._roots_paths()
        target = os.path.join(self._tmp_scratch, 'deep', 'x.py')
        os.makedirs(os.path.dirname(target), exist_ok=True)
        resolved = _resolve_write_path(self._proj, target)
        self.assertEqual(resolved, os.path.abspath(target))
        self.assertEqual(self._roots_paths(), roots_before)
        self.assertEqual(drain_root_added_signals(), [])


class NonTempAutoRegisterSignalTest(_Base):
    """Required scenario #2: non-temp abs write outside roots → root + signal."""

    def setUp(self):
        super().setUp()
        # A sibling project dir OUTSIDE both the primary and the temp dir,
        # under the non-temp work area so it isn't itself classified temp.
        self._sibling = os.path.join(self._work, 'sibling')
        os.makedirs(self._sibling)
        self.assertFalse(_is_temp_path(self._sibling),
                         'sibling fixture must not live under the temp dir')

    def test_abs_write_outside_roots_registers_and_signals(self):
        roots_before = self._roots_paths()
        target = os.path.join(self._sibling, 'pkg', 'mod.py')

        res = tool_write_file(self._proj, target, 'x = 1\n',
                              conv_id='c1', task_id='t1')

        self.assertTrue(res['ok'], res)
        self.assertTrue(os.path.isfile(target))
        # A NEW root was registered (the nearest existing ancestor).
        roots_after = self._roots_paths()
        self.assertGreater(len(roots_after), len(roots_before),
                           'non-temp abs write should auto-register a new root')
        self.assertIn(os.path.abspath(self._sibling), roots_after)
        # And a workspace_root_added signal was queued for the handler.
        signals = drain_root_added_signals()
        self.assertEqual(len(signals), 1, signals)
        self.assertEqual(os.path.abspath(signals[0]['path']),
                         os.path.abspath(self._sibling))
        self.assertTrue(signals[0]['rootName'])

    def test_signal_drains_once(self):
        target = os.path.join(self._sibling, 'a.py')
        tool_write_file(self._proj, target, '1\n', conv_id='c1', task_id='t1')
        first = drain_root_added_signals()
        self.assertEqual(len(first), 1)
        # Second drain is empty — signals are consume-once.
        self.assertEqual(drain_root_added_signals(), [])


class SubdirOfExistingRootTest(_Base):
    """Required scenario #3: write under an EXISTING root → no new root, no signal.

    This is the llm_platform/llm-mcp case: a subdir of a registered root.
    Attribution falls to the parent root (as today); nothing new registers.
    """

    def test_subdir_write_registers_no_new_root_no_signal(self):
        # The subdir lives INSIDE the already-registered primary.
        roots_before = self._roots_paths()
        target = os.path.join(self._proj, 'llm-mcp', 'client.py')

        res = tool_write_file(self._proj, target, 'y = 2\n',
                              conv_id='c1', task_id='t1')

        self.assertTrue(res['ok'], res)
        self.assertTrue(os.path.isfile(target))
        # No new root — the path already resolves under the primary.
        self.assertEqual(self._roots_paths(), roots_before)
        self.assertEqual(drain_root_added_signals(), [])
        # The modification IS recorded (it's a real project file, not temp).
        mods = get_modifications(self._proj, conv_id='c1')
        self.assertTrue(any('llm-mcp' in m.get('path', '') for m in mods),
                        'a real subdir write must still be tracked')


if __name__ == '__main__':
    unittest.main()
