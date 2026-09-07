"""tests/test_write_outside_workspace_gate.py — the out-of-workspace write gate.

2026-08-31 decision: an absolute-path write OUTSIDE every registered
workspace root must never silently auto-register a new root. The model-facing
write tools refuse with :class:`OutsideWorkspaceError` (a ValueError) naming
the ``allow_outside_workspace`` schema flag; only after the user explicitly
confirms the exact destination may the call be re-issued with that flag set,
which performs the (conv-scoped) root registration and the write.

Pins:
  1. Refusal: no flag → ok:False, error names the flag, no file, no root
     (global or conv), no root-added signal.
  2. Confirmation: flag set → write + conv-scoped registration proceed; a
     REPEAT unflagged write to the same tree then resolves directly (rule §1
     scans the conv registry) — confirmation is per-expansion, not per-write.
  3. Interactive (no conv_id): confirmed write registers globally; repeat
     unflagged write resolves.
  4. Forbidden system paths stay refused even WITH the flag.
  5. Temp-dir scratch writes are exempt (they register nothing — there is no
     silent expansion to confirm).
  6. OutsideWorkspaceError IS a ValueError and carries abs_path/anchor.
  7. Batch tools (apply_diffs / edit_file) refuse per-edit with the same
     flag-named error.
  8. All six wire schemas expose ``allow_outside_workspace`` as a boolean
     inside ``parameters.properties`` (guards the additionalProperties:false
     schemas from silently rejecting the flag).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from lib.project_mod import config as cfg
from lib.project_mod import write_tools as wt
from lib.project_mod.scanner import clear_project, set_project
from lib.project_mod.write_tools import (
    OutsideWorkspaceError,
    _resolve_write_path,
    drain_root_added_signals,
    tool_apply_diffs,
    tool_edit_file,
    tool_write_file,
)


class _Base(unittest.TestCase):
    """Hermetic temp-root detection (same pattern as
    test_temp_write_and_root_signal): one carved-out base dir with a
    designated ``tmp/`` temp root and a non-temp ``work/`` workspace area."""

    def setUp(self):
        self._fs = tempfile.mkdtemp(prefix='owg-fs-')
        self._tmp_root = os.path.join(self._fs, 'tmp')
        self._work = os.path.join(self._fs, 'work')
        os.makedirs(self._tmp_root)
        os.makedirs(self._work)
        self._saved_cache = getattr(wt._temp_roots, '_cache', None)
        wt._temp_roots._cache = {os.path.realpath(self._tmp_root)}

        self._proj = os.path.join(self._work, 'proj')
        self._sibling = os.path.join(self._work, 'sibling')
        os.makedirs(self._proj)
        os.makedirs(self._sibling)
        set_project(self._proj)
        drain_root_added_signals()

    def tearDown(self):
        clear_project()
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()
        drain_root_added_signals()
        wt._temp_roots._cache = self._saved_cache
        shutil.rmtree(self._fs, ignore_errors=True)

    def _roots_paths(self):
        with cfg._lock:
            return {os.path.abspath(rs['path']) for rs in cfg._roots.values()}

    def _conv_root_paths(self, conv_id):
        return {os.path.abspath(rs['path'])
                for rs in cfg.get_conv_roots(conv_id).values()}


class RefusalTest(_Base):
    def test_refusal_names_flag_and_mutates_nothing(self):
        target = os.path.join(self._sibling, 'pkg', 'mod.py')
        roots_before = self._roots_paths()

        res = tool_write_file(self._proj, target, 'x = 1\n',
                              conv_id='c1', task_id='t1')

        self.assertFalse(res['ok'], res)
        self.assertIn('allow_outside_workspace=true', res['error'])
        self.assertIn(self._sibling, res['error'],
                      'refusal must name the workspace expansion being gated')
        self.assertFalse(os.path.exists(target),
                         'refused write must not create the file')
        self.assertEqual(self._roots_paths(), roots_before,
                         'refusal must not touch the global registry')
        self.assertNotIn('c1', cfg._conv_roots,
                         'refusal must not seed/extend a conv registry')
        self.assertEqual(drain_root_added_signals(), [],
                         'refusal must not queue a root-added signal')

    def test_error_is_value_error_with_context_attrs(self):
        target = os.path.join(self._sibling, 'x.py')
        with self.assertRaises(OutsideWorkspaceError) as cm:
            _resolve_write_path(self._proj, target, conv_id='c1')
        err = cm.exception
        self.assertIsInstance(err, ValueError,
                              'existing except-ValueError rejection paths must keep working')
        self.assertEqual(os.path.abspath(err.abs_path), os.path.abspath(target))
        self.assertEqual(os.path.abspath(err.anchor), os.path.abspath(self._sibling))


class ConfirmedWriteTest(_Base):
    def test_confirmed_write_registers_conv_scoped_then_repeat_passes(self):
        target = os.path.join(self._sibling, 'pkg', 'mod.py')

        res = tool_write_file(self._proj, target, 'x = 1\n',
                              conv_id='c1', task_id='t1', allow_outside=True)
        self.assertTrue(res['ok'], res)
        self.assertTrue(os.path.isfile(target))
        self.assertIn(os.path.abspath(self._sibling), self._conv_root_paths('c1'))
        self.assertNotIn(os.path.abspath(self._sibling), self._roots_paths(),
                         'background (conv_id) write must not pollute global _roots')

        # The confirmation covered the EXPANSION: the sibling is now part of
        # this conversation's workspace, so further writes need no flag.
        res2 = tool_write_file(self._proj, os.path.join(self._sibling, 'b.py'),
                               'y = 2\n', conv_id='c1', task_id='t1')
        self.assertTrue(res2['ok'], res2)
        self.assertTrue(os.path.isfile(os.path.join(self._sibling, 'b.py')))

    def test_confirmed_interactive_write_registers_globally_then_repeat_passes(self):
        target = os.path.join(self._sibling, 'x.py')
        res = tool_write_file(self._proj, target, '1\n', allow_outside=True)
        self.assertTrue(res['ok'], res)
        self.assertIn(os.path.abspath(self._sibling), self._roots_paths())

        res2 = tool_write_file(self._proj, os.path.join(self._sibling, 'y.py'), '2\n')
        self.assertTrue(res2['ok'], res2)


class ForbiddenStillRefusedTest(_Base):
    def test_system_path_refused_even_with_flag(self):
        res = tool_write_file(self._proj, '/etc/tofu-owg-should-not-exist.conf',
                              'x\n', conv_id='c1', task_id='t1', allow_outside=True)
        self.assertFalse(res['ok'], res)
        self.assertIn('system path', res['error'])
        self.assertFalse(os.path.exists('/etc/tofu-owg-should-not-exist.conf'))


class TempExemptTest(_Base):
    def test_temp_scratch_needs_no_flag_and_registers_nothing(self):
        scratch = os.path.join(self._tmp_root, 'scratch.py')
        roots_before = self._roots_paths()
        res = tool_write_file(self._proj, scratch, '1\n',
                              conv_id='c1', task_id='t1')
        self.assertTrue(res['ok'], res)
        self.assertEqual(self._roots_paths(), roots_before)
        self.assertNotIn('c1', cfg._conv_roots)
        self.assertEqual(drain_root_added_signals(), [])


class BatchGateTest(_Base):
    def test_apply_diffs_refusal_names_flag(self):
        target = os.path.join(self._sibling, 'mod.py')
        res = tool_apply_diffs(self._proj, [
            {'description': 'd', 'path': target, 'search': 'a', 'replace': 'b'},
        ], conv_id='c1', task_id='t1')
        self.assertIn('allow_outside_workspace=true', str(res))
        self.assertNotIn('c1', cfg._conv_roots)

    def test_edit_file_refusal_names_flag(self):
        target = os.path.join(self._sibling, 'mod.py')
        res = tool_edit_file(self._proj, [
            {'path': target, 'operation': 'replace', 'anchor': 'a', 'content': 'b'},
        ], conv_id='c1', task_id='t1')
        self.assertIn('allow_outside_workspace=true', str(res))
        self.assertNotIn('c1', cfg._conv_roots)


class WireSchemaTest(unittest.TestCase):
    def test_all_six_write_schemas_expose_the_flag(self):
        from lib.tools import project as P
        for name in ('PROJECT_TOOL_WRITE_FILE', 'PROJECT_TOOL_APPLY_DIFF',
                     'PROJECT_TOOL_APPLY_DIFFS', 'PROJECT_TOOL_INSERT_CONTENT',
                     'PROJECT_TOOL_INSERT_CONTENTS', 'PROJECT_TOOL_EDIT_FILE'):
            props = getattr(P, name)['function']['parameters']['properties']
            self.assertIn('allow_outside_workspace', props, name)
            self.assertEqual(props['allow_outside_workspace']['type'], 'boolean', name)


if __name__ == '__main__':
    unittest.main()
