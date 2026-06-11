"""tests/test_readonly_roots.py — read-only workspace-root enforcement.

Covers the multi-root "read-only" feature:
  * config.is_readonly_path resolves against per-conv + global registries
  * write_file / apply_diff / insert_content refuse RO targets (relative,
    absolute, and rootname: forms all funnel through _resolve_write_path)
  * create_project refuses an RO destination
  * a writable sibling root in the same workspace is unaffected
  * single-root / all-writable behaviour is unchanged (no regression)
"""

from __future__ import annotations

import os
import unittest

from lib.project_mod import config as cfg
from lib.project_mod.scanner import clear_project, set_project_paths
from lib.project_mod.write_tools import (
    tool_apply_diff, tool_create_project, tool_insert_content, tool_write_file,
)


class _TmpWorkspace(unittest.TestCase):
    """Two sibling dirs: a writable primary and a read-only extra."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='ro-roots-')
        self.rw = os.path.join(self._tmp, 'writable')
        self.ro = os.path.join(self._tmp, 'readonly')
        os.makedirs(self.rw)
        os.makedirs(self.ro)
        # Seed an existing file in each for apply_diff / insert_content.
        with open(os.path.join(self.rw, 'a.txt'), 'w') as f:
            f.write('hello rw\n')
        with open(os.path.join(self.ro, 'a.txt'), 'w') as f:
            f.write('hello ro\n')

    def tearDown(self):
        clear_project()
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class IsReadonlyPathTest(_TmpWorkspace):

    def test_global_registry_flags_ro_root(self):
        set_project_paths([self.rw, self.ro], readonly_paths=[self.ro])
        self.assertFalse(cfg.is_readonly_path(os.path.join(self.rw, 'x.py')))
        self.assertTrue(cfg.is_readonly_path(os.path.join(self.ro, 'x.py')))

    def test_conv_registry_isolated(self):
        cfg.set_conv_roots('conv-1', self.rw, extras=[self.ro],
                           readonly_paths=[self.ro])
        self.assertTrue(
            cfg.is_readonly_path(os.path.join(self.ro, 'x.py'), conv_id='conv-1'))
        self.assertFalse(
            cfg.is_readonly_path(os.path.join(self.rw, 'x.py'), conv_id='conv-1'))

    def test_primary_can_be_readonly(self):
        set_project_paths([self.ro, self.rw], readonly_paths=[self.ro])
        # Primary (self.ro) is RO; extra (self.rw) is writable.
        self.assertTrue(cfg.is_readonly_path(os.path.join(self.ro, 'x.py')))
        self.assertFalse(cfg.is_readonly_path(os.path.join(self.rw, 'x.py')))

    def test_unregistered_path_not_readonly(self):
        set_project_paths([self.rw, self.ro], readonly_paths=[self.ro])
        self.assertFalse(cfg.is_readonly_path('/tmp/some/other/place.py'))


class WriteEnforcementTest(_TmpWorkspace):

    def setUp(self):
        super().setUp()
        set_project_paths([self.rw, self.ro], readonly_paths=[self.ro])

    def test_write_file_blocked_in_ro_via_abspath(self):
        res = tool_write_file(self.rw, os.path.join(self.ro, 'new.txt'),
                              'nope')
        self.assertFalse(res['ok'])
        self.assertIn('READ-ONLY', res['error'])
        self.assertFalse(os.path.exists(os.path.join(self.ro, 'new.txt')))

    def test_write_file_allowed_in_rw(self):
        res = tool_write_file(self.rw, 'fresh.txt', 'yes')
        self.assertTrue(res['ok'])
        self.assertTrue(os.path.isfile(os.path.join(self.rw, 'fresh.txt')))

    def test_apply_diff_blocked_in_ro(self):
        res = tool_apply_diff(self.rw, os.path.join(self.ro, 'a.txt'),
                              'hello ro', 'HACKED')
        self.assertFalse(res['ok'])
        self.assertIn('READ-ONLY', res['error'])
        with open(os.path.join(self.ro, 'a.txt')) as f:
            self.assertEqual(f.read(), 'hello ro\n')  # untouched

    def test_insert_content_blocked_in_ro(self):
        res = tool_insert_content(self.rw, os.path.join(self.ro, 'a.txt'),
                                  'hello ro', '\nINJECTED')
        self.assertFalse(res['ok'])
        self.assertIn('READ-ONLY', res['error'])

    def test_create_project_blocked_in_ro(self):
        res = tool_create_project(os.path.join(self.ro, 'subproj'))
        self.assertFalse(res['ok'])
        self.assertIn('READ-ONLY', res['error'])


class AllWritableRegressionTest(_TmpWorkspace):

    def test_no_readonly_means_all_writable(self):
        set_project_paths([self.rw, self.ro])  # no readonly_paths
        self.assertFalse(cfg.is_readonly_path(os.path.join(self.ro, 'x.py')))
        res = tool_write_file(self.rw, os.path.join(self.ro, 'new.txt'), 'ok')
        self.assertTrue(res['ok'])


if __name__ == '__main__':
    unittest.main()
