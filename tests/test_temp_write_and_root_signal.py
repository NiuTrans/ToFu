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
        # A NEW root was registered — but CONV-SCOPED, never in the global
        # registry (a background write must never pollute the UI-facing
        # global _roots). c1 had no scoped registry, so the fallback seeds
        # one from the primary and registers the sibling there.
        self.assertEqual(self._roots_paths(), roots_before,
                         'background write must not pollute global _roots')
        conv_roots = cfg.get_conv_roots('c1')
        self.assertTrue(
            any(os.path.abspath(rs['path']) == os.path.abspath(self._sibling)
                for rs in conv_roots.values()),
            f'sibling root must land in the conv registry: {conv_roots}')
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


class ConvRegistryAutoRegisterTest(_Base):
    """(a)+(b) scope: the abs-write auto-register must ALSO extend the
    conversation's OWN scoped registry (_conv_roots), not only the global
    _roots — so a subsequent ``newroot:rel/path`` namespaced write IN THE
    SAME TASK resolves instead of raising UnknownWorkspaceRootError (the
    conv-scoped resolver does NOT fall through to the global registry, per
    the 2026-05-05 isolation fix).
    """

    def setUp(self):
        super().setUp()
        self._sibling = os.path.join(self._work, 'sibling')
        os.makedirs(self._sibling)
        # This conversation OWNS a scoped registry (as a real running task
        # does — ensure_project_state(conv_id=...) writes it up front). Only
        # then may the auto-register extend it.
        cfg.set_conv_roots('cX', self._proj)

    def test_abs_write_extends_conv_registry_and_namespaced_resolves(self):
        target = os.path.join(self._sibling, 'pkg', 'mod.py')

        res = tool_write_file(self._proj, target, 'x = 1\n',
                              conv_id='cX', task_id='t1')
        self.assertTrue(res['ok'], res)

        # The new root landed in THIS conv's scoped registry, under the same
        # name the global registry assigned (basename 'sibling').
        conv_roots = cfg.get_conv_roots('cX')
        sib_names = [rn for rn, rs in conv_roots.items()
                     if os.path.abspath(rs['path']) == os.path.abspath(self._sibling)]
        self.assertEqual(len(sib_names), 1,
                         f'conv registry must gain the sibling root: {conv_roots}')
        root_name = sib_names[0]

        # THE PAYOFF: a same-task ``newroot:rel/path`` namespaced write now
        # resolves against the conv registry (no UnknownWorkspaceRootError).
        base, rel = cfg.resolve_namespaced_path(f'{root_name}:sub/f.py', conv_id='cX')
        self.assertEqual(os.path.abspath(base), os.path.abspath(self._sibling))
        self.assertEqual(rel, 'sub/f.py')

    def test_no_conv_registry_is_left_untouched_for_other_convs(self):
        # A DIFFERENT conv with its own registry must not gain the root — the
        # auto-register only extends the writing conv ('cX'), never a sibling.
        cfg.set_conv_roots('cOther', self._proj)
        target = os.path.join(self._sibling, 'a.py')
        tool_write_file(self._proj, target, '1\n', conv_id='cX', task_id='t1')

        other = cfg.get_conv_roots('cOther')
        self.assertFalse(
            any(os.path.abspath(rs['path']) == os.path.abspath(self._sibling)
                for rs in other.values()),
            f'sibling conv registry must be untouched: {other}')

    def test_bg_write_no_registry_seeds_conv_scope_not_global(self):
        # A background write for a conv that never had a scoped registry must
        # NOT pollute the process-global _roots (the UI-facing "active project"
        # every conv's bar reflects — the "extra path appears on another conv"
        # leak). Instead it SEEDS a conv-scoped registry from the task's own
        # primary (``base``) and registers the sibling root THERE.
        self.assertNotIn('cGhost', cfg._conv_roots)
        roots_before = self._roots_paths()
        target = os.path.join(self._sibling, 'ghost.py')
        res = tool_write_file(self._proj, target, '1\n',
                              conv_id='cGhost', task_id='t1')
        self.assertTrue(res['ok'], res)
        self.assertTrue(os.path.isfile(target))
        # The global registry did NOT gain the sibling root.
        self.assertEqual(self._roots_paths(), roots_before,
                         'background write must never pollute global _roots')
        self.assertNotIn(os.path.abspath(self._sibling), self._roots_paths())
        # A conv-scoped registry was seeded (primary) and gained the sibling.
        self.assertIn('cGhost', cfg._conv_roots,
                      'fallback must seed a conv-scoped registry from the primary')
        conv_roots = cfg.get_conv_roots('cGhost')
        self.assertTrue(
            any(os.path.abspath(rs['path']) == os.path.abspath(self._sibling)
                for rs in conv_roots.values()),
            f'sibling root must land in the conv registry: {conv_roots}')
        # And the primary itself is present as the seeded scope root.
        self.assertTrue(
            any(os.path.abspath(rs['path']) == os.path.abspath(self._proj)
                for rs in conv_roots.values()),
            f'seeded registry must include the primary: {conv_roots}')


class RecentProjectPersistenceTest(_Base):
    """Model-added workspace roots are persisted to the recent-projects list.

    A root the ASSISTANT registers (create_project OR the absolute-path-write
    auto-register) must land in recent_projects server-side — so it shows up
    under "recent" even when the emitting conversation is NOT the active one
    (the frontend workspace_root_added handler only refreshes for the active
    conv). Temp-dir scratch paths must NOT be saved.
    """

    def setUp(self):
        super().setUp()
        self._sibling = os.path.join(self._work, 'sibling')
        os.makedirs(self._sibling)
        # Capture save_recent_project calls without touching the DB. The
        # helper imports it via ``from lib.project_mod.config import
        # save_recent_project`` at call time, so patching the attribute on
        # the config module is what the helper resolves.
        self._saved_paths = []
        self._orig_srp = cfg.save_recent_project
        cfg.save_recent_project = lambda p: self._saved_paths.append(p)

    def tearDown(self):
        cfg.save_recent_project = self._orig_srp
        super().tearDown()

    def test_conv_scoped_bg_auto_register_does_not_save_recent(self):
        # A CONV-SCOPED background auto-register (running task with conv_id)
        # must NOT write to the process-global recent-projects list — recent is
        # global UI state, and the "background write must never pollute global
        # state" invariant (see NonTempAutoRegisterSignalTest / ConvRegistry*)
        # WINS. The root lands only in the conv-scoped registry; recent-save
        # stays reserved for the human-driven interactive + create_project
        # paths. (Corrects the earlier expectation that contradicted the
        # no-global-pollution guarantee its sibling tests enforce.)
        target = os.path.join(self._sibling, 'pkg', 'mod.py')
        tool_write_file(self._proj, target, 'x = 1\n', conv_id='c1', task_id='t1')
        self.assertEqual(
            [], [os.path.abspath(p) for p in self._saved_paths
                 if os.path.abspath(p) == os.path.abspath(self._sibling)],
            'conv-scoped background auto-register must NOT pollute global recent projects')

    def test_temp_write_does_not_save_recent(self):
        target = os.path.join(self._tmp_scratch, 'scratch.py')
        tool_write_file(self._proj, target, '1\n', conv_id='c1', task_id='t1')
        self.assertEqual(self._saved_paths, [],
                         'temp-dir scratch write must NOT pollute recent projects')

    def test_subdir_of_existing_root_does_not_save_recent(self):
        # A write under the already-registered primary registers no new root
        # → nothing new should be saved to recent.
        target = os.path.join(self._proj, 'sub', 'f.py')
        tool_write_file(self._proj, target, '1\n', conv_id='c1', task_id='t1')
        self.assertEqual(self._saved_paths, [],
                         'a subdir write under an existing root saves nothing new')

    def test_create_project_saves_recent(self):
        from lib.project_mod.write_tools import tool_create_project
        new_dir = os.path.join(self._work, 'brand_new')
        res = tool_create_project(new_dir, conv_id='c1', task_id='t1')
        self.assertTrue(res.get('ok'), res)
        self.assertIn(os.path.abspath(new_dir),
                      [os.path.abspath(p) for p in self._saved_paths],
                      'create_project root must be saved to recent projects')


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
