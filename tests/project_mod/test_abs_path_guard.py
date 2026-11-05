"""tests/project_mod/test_abs_path_guard.py — Absolute-path sandbox for remote API callers.

Covers lib.project_mod.abs_path_guard: default permissive (local/CLI),
restricted denies out-of-root abs read/write, allows within-root, follows
symlinks (escape denied), and task_is_remote classification.
"""

import os
import shutil
import tempfile
import unittest

import lib.project_mod.abs_path_guard as g
from lib.project_mod.scanner import set_project


class AbsPathGuardTest(unittest.TestCase):

    def setUp(self):
        self._root = os.path.realpath(tempfile.mkdtemp())
        set_project(self._root)
        self._inside = os.path.join(self._root, 'sub', 'f.txt')
        os.makedirs(os.path.dirname(self._inside), exist_ok=True)
        with open(self._inside, 'w') as f:
            f.write('hello')

    def tearDown(self):
        # Best-effort: clear any restriction left set by a failed test.
        g.set_restricted(False)

    def test_default_is_permissive(self):
        self.assertFalse(g.is_restricted())
        # No raise even for system paths when unrestricted.
        g.enforce_abs_read('/etc/passwd')
        g.enforce_abs_write('/root/.bashrc')

    def test_restricted_denies_out_of_root_read(self):
        tok = g.set_restricted(True)
        try:
            with self.assertRaises(g.AbsPathDenied):
                g.enforce_abs_read('/etc/passwd')
        finally:
            g.reset_restricted(tok)

    def test_restricted_denies_out_of_root_write(self):
        tok = g.set_restricted(True)
        try:
            with self.assertRaises(g.AbsPathDenied):
                g.enforce_abs_write('/root/.bashrc')
        finally:
            g.reset_restricted(tok)

    def test_restricted_allows_within_root(self):
        tok = g.set_restricted(True)
        try:
            # The allow half of the allow/deny contract: inside-root calls are
            # silent no-ops (return None, raise nothing).
            assert g.enforce_abs_read(self._inside) is None
            assert g.enforce_abs_write(self._inside) is None
        finally:
            g.reset_restricted(tok)

    def test_symlink_escape_denied(self):
        link = os.path.join(self._root, 'evil')
        try:
            os.symlink('/etc/passwd', link)
        except OSError:
            self.skipTest('symlinks not supported here')
        tok = g.set_restricted(True)
        try:
            with self.assertRaises(g.AbsPathDenied):
                g.enforce_abs_read(link)
        finally:
            g.reset_restricted(tok)

    def test_task_is_remote(self):
        self.assertTrue(g.task_is_remote({'_via_agent_run': True}))
        self.assertTrue(g.task_is_remote({'_compat_openai': True}))
        self.assertTrue(g.task_is_remote({'_compat_anthropic': True}))
        self.assertTrue(g.task_is_remote({'_api_key_id': 'k_x'}))
        self.assertFalse(g.task_is_remote({}))
        self.assertFalse(g.task_is_remote({'_api_key_id': ''}))
        self.assertFalse(g.task_is_remote('not-a-dict'))

    def _png(self, directory):
        from PIL import Image
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, 'pixel.png')
        Image.new('RGB', (4, 4), 'red').save(path)
        return path

    def _outside_png(self):
        directory = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        return self._png(directory)

    def test_restricted_inspect_image_denies_out_of_root_absolute_path(self):
        from lib.project_mod import execute_tool
        from lib.project_mod.read_tools import tool_inspect_image
        outside = self._outside_png()
        inside = self._png(os.path.join(self._root, 'img'))
        tok = g.set_restricted(True)
        try:
            for denied in (tool_inspect_image(self._root, outside),
                           execute_tool('inspect_image', {'path': outside}, self._root)):
                self.assertIsInstance(denied, str)
                self.assertIn('Access denied', denied)
            allowed = tool_inspect_image(self._root, inside)
            self.assertTrue(isinstance(allowed, dict) and allowed.get('__screenshot__'))
        finally:
            g.reset_restricted(tok)

    def test_unrestricted_inspect_image_reads_absolute_paths(self):
        from lib.project_mod.read_tools import tool_inspect_image
        shot = tool_inspect_image(self._root, self._outside_png())
        self.assertTrue(isinstance(shot, dict) and shot.get('__screenshot__'))


if __name__ == '__main__':
    unittest.main()
