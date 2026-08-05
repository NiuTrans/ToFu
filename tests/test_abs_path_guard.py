# Incident anchor: born in commit e794681c — Snapshot chatui for MAPS in-container runtime: any-language→English a...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""tests/test_abs_path_guard.py — Absolute-path sandbox for remote API callers.

Covers lib.project_mod.abs_path_guard: default permissive (local/CLI),
restricted denies out-of-root abs read/write, allows within-root, follows
symlinks (escape denied), and task_is_remote classification.
"""

import os
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


if __name__ == '__main__':
    unittest.main()
