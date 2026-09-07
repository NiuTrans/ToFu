"""tests/test_project_upload_drop.py — save_uploaded_file (folder-browser drop).

Covers the binary-safe drag-and-drop-into-project-folder capability
(``lib.project_mod.write_tools.save_uploaded_file``, backing
``POST /api/v1/project/upload``):

  * raw BYTES land on disk intact (binary-safe, unlike text-only write)
  * a name collision AUTO-RENAMES (never clobbers) → ``name (1).ext``
  * a drop into a READ-ONLY root is refused
  * a drop into a dir that is NOT inside any attached root is refused
    (a UI drop must never silently auto-register a new workspace root)
  * the write is RECORDED so it appears in the undo journal / file-changes bar
"""

from __future__ import annotations

import os
import unittest

from lib.project_mod import config as cfg
from lib.project_mod import get_modifications
from lib.project_mod.scanner import clear_project, set_project_paths
from lib.project_mod.write_tools import _dedupe_target, save_uploaded_file

PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01'  # non-UTF-8 bytes


class _Workspace(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='upload-drop-')
        self.rw = os.path.join(self._tmp, 'writable')
        self.ro = os.path.join(self._tmp, 'readonly')
        os.makedirs(self.rw)
        os.makedirs(self.ro)

    def tearDown(self):
        clear_project()
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class SaveBytesTest(_Workspace):
    def setUp(self):
        super().setUp()
        set_project_paths([self.rw, self.ro], readonly_paths=[self.ro])

    def test_binary_bytes_land_intact(self):
        res = save_uploaded_file(self.rw, os.path.join(self.rw, 'logo.png'), PNG)
        self.assertTrue(res['ok'], res)
        self.assertTrue(res['created'])
        self.assertFalse(res['renamed'])
        with open(os.path.join(self.rw, 'logo.png'), 'rb') as f:
            self.assertEqual(f.read(), PNG)

    def test_relative_path_targets_active_root(self):
        res = save_uploaded_file(self.rw, 'notes.txt', b'hi')
        self.assertTrue(res['ok'], res)
        self.assertTrue(os.path.isfile(os.path.join(self.rw, 'notes.txt')))

    def test_collision_auto_renames(self):
        with open(os.path.join(self.rw, 'a.bin'), 'wb') as f:
            f.write(b'first')
        res = save_uploaded_file(self.rw, os.path.join(self.rw, 'a.bin'), b'second')
        self.assertTrue(res['ok'], res)
        self.assertTrue(res['renamed'])
        self.assertEqual(os.path.basename(res['path']), 'a (1).bin')
        # Original untouched, new sibling written.
        with open(os.path.join(self.rw, 'a.bin'), 'rb') as f:
            self.assertEqual(f.read(), b'first')
        with open(os.path.join(self.rw, 'a (1).bin'), 'rb') as f:
            self.assertEqual(f.read(), b'second')

    def test_readonly_root_refused(self):
        res = save_uploaded_file(self.rw, os.path.join(self.ro, 'x.png'), PNG)
        self.assertFalse(res['ok'])
        self.assertIn('READ-ONLY', res['error'])
        self.assertFalse(os.path.exists(os.path.join(self.ro, 'x.png')))

    def test_unattached_dir_refused_no_autoregister(self):
        import tempfile
        stray = tempfile.mkdtemp(prefix='stray-')
        try:
            target = os.path.join(stray, 'y.png')
            res = save_uploaded_file(self.rw, target, PNG)
            self.assertFalse(res['ok'])
            self.assertIn('attached workspace', res['error'])
            self.assertFalse(os.path.exists(target))
            # Crucially, the stray dir was NOT registered as a root.
            with cfg._lock:
                roots = [os.path.abspath(rs['path']) for rs in cfg._roots.values()]
            self.assertNotIn(os.path.abspath(stray), roots)
        finally:
            import shutil
            shutil.rmtree(stray, ignore_errors=True)

    def test_write_is_recorded_for_undo(self):
        save_uploaded_file(self.rw, os.path.join(self.rw, 'tracked.png'), PNG,
                           conv_id='conv-x', task_id='task-x')
        mods = get_modifications(self.rw, conv_id='conv-x')
        paths = [m.get('path') for m in mods]
        self.assertIn('tracked.png', paths)


class DedupeTargetUnitTest(_Workspace):
    def test_returns_same_when_absent(self):
        t = os.path.join(self.rw, 'nope.txt')
        self.assertEqual(_dedupe_target(t), t)

    def test_suffixes_until_free(self):
        for nm in ('c.txt', 'c (1).txt'):
            with open(os.path.join(self.rw, nm), 'w') as f:
                f.write('x')
        got = _dedupe_target(os.path.join(self.rw, 'c.txt'))
        self.assertEqual(os.path.basename(got), 'c (2).txt')


if __name__ == '__main__':
    unittest.main()
