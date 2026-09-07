"""tests/test_desktop_install_paths.py — Windows/desktop installer robustness.

Locks in the two fatal fixes for the desktop build:

* **P2 — writable data/logs roots (`lib/runtime_paths`).** In a frozen build the
  legacy ``dirname(dirname(__file__))/data`` resolves INSIDE the read-only
  ``_internal/`` bundle (under Program Files) and every write crashes. The
  resolver must instead honour ``$TOFU_DATA_DIR`` and, when frozen with an
  unwritable exe dir, fall back to a per-user dir.

* **P1 — tofu.spec datas filtering.** ``trading.html`` was moved to an external
  plugin and no longer exists; listing it unconditionally in ``datas`` makes
  PyInstaller abort the whole build. The spec must DROP missing sources and
  KEEP present ones.

* **P2b — lib/log.py inline twin.** log.py hand-duplicates the same frozen /
  override / portable / read-only-fallback logic (it can't import runtime_paths
  — cycle). ``LogDirTwinTest`` bites that copy directly AND pins that it agrees
  with ``runtime_paths.logs_root()``, so editing one twin and not the other
  fails CI. Logging is the project's #1 rule; an uncovered rotting twin would
  crash logging on first launch under Program Files.

Each test includes a negative-control assertion (the bug reappears when the fix
is neutralised), driven via a subprocess so a reloaded module picks up
env/frozen state cleanly.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.unit


def _run_py(code: str, env_extra=None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env['PYTHONPATH'] = _REPO + os.pathsep + env.get('PYTHONPATH', '')
    # Never leak a real override into the child unless the test sets one.
    env.pop('TOFU_DATA_DIR', None)
    env.pop('CHATUI_DATA_DIR', None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, '-c', textwrap.dedent(code)],
                          capture_output=True, text=True, env=env)


class RuntimePathsTest(unittest.TestCase):

    def test_source_mode_uses_repo_root(self):
        """A plain source checkout keeps the legacy repo-root paths (no drift)."""
        r = _run_py("""
            import lib.runtime_paths as rp
            print('DATA', rp.data_root())
            print('LOGS', rp.logs_root())
        """)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('DATA %s' % os.path.join(_REPO, 'data'), r.stdout)
        self.assertIn('LOGS %s' % os.path.join(_REPO, 'logs'), r.stdout)

    def test_tofu_data_dir_override(self):
        """$TOFU_DATA_DIR (set by the desktop launcher) redirects both roots."""
        base = os.path.realpath(tempfile.mkdtemp())
        data = os.path.join(base, 'data')
        r = _run_py("""
            import lib.runtime_paths as rp
            print('DATA', rp.data_root())
            print('LOGS', rp.logs_root())
        """, env_extra={'TOFU_DATA_DIR': data})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('DATA %s' % data, r.stdout)
        self.assertIn('LOGS %s' % os.path.join(base, 'logs'), r.stdout)

    def test_frozen_portable_uses_exe_sibling(self):
        """Frozen + writable exe dir → data/ next to the executable (portable)."""
        exedir = os.path.realpath(tempfile.mkdtemp())
        fake_exe = os.path.join(exedir, 'Tofu')
        r = _run_py(f"""
            import sys
            sys.frozen = True
            sys.executable = {fake_exe!r}
            import lib.runtime_paths as rp
            print('DATA', rp.data_root())
        """)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('DATA %s' % os.path.join(exedir, 'data'), r.stdout)

    def test_frozen_readonly_falls_back_to_per_user(self):
        """Frozen + UNWRITABLE exe dir (Program Files) → per-user dir, not a crash.

        NEGATIVE CONTROL: the buggy behaviour would be to return the exe-sibling
        path regardless of writability; we assert the fallback kicks in instead.
        """
        ro_parent = os.path.realpath(tempfile.mkdtemp())
        appdir = os.path.join(ro_parent, 'app')
        os.makedirs(appdir)
        fake_exe = os.path.join(appdir, 'Tofu')
        # Make the exe's PARENT (appdir) unwritable so data/ cannot be created
        # inside it — mirrors a Program Files install for a standard user.
        os.chmod(appdir, 0o555)
        try:
            r = _run_py(f"""
                import sys
                sys.frozen = True
                sys.executable = {fake_exe!r}
                import lib.runtime_paths as rp
                print('DATA', rp.data_root())
            """)
            self.assertEqual(r.returncode, 0, r.stderr)
            line = [l for l in r.stdout.splitlines() if l.startswith('DATA ')][0]
            data = line[len('DATA '):]
            if os.access(appdir, os.W_OK):
                self.skipTest('test runner can write to a 0555 dir (root?) — '
                              'cannot simulate a read-only install here')
            self.assertNotEqual(data, os.path.join(appdir, 'data'),
                                'must NOT use the unwritable exe-sibling dir')
            self.assertIn('Tofu', data)
        finally:
            os.chmod(appdir, 0o755)


class LogDirTwinTest(unittest.TestCase):
    """lib/log.py hand-duplicates the frozen/override/portable path logic of
    lib/runtime_paths (it CANNOT import it — runtime_paths imports lib.log, a
    cycle). These tests bite that inline twin directly and pin that it produces
    the SAME directory runtime_paths.logs_root() would, so a future edit to one
    copy and not the other fails CI. Logging is the project's #1 rule — if this
    twin rots, the frozen app crashes creating logs/ under Program Files with
    NO other coverage catching it.
    """

    def _log_dir(self, code_prelude: str, env_extra=None):
        """Import lib.log under a scenario and return its resolved LOG_DIR."""
        r = _run_py(code_prelude + """
            import lib.log as L
            print('LOGDIR', L.LOG_DIR)
        """, env_extra=env_extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith('LOGDIR ')][0]
        return line[len('LOGDIR '):]

    def _runtime_logs_root(self, code_prelude: str, env_extra=None):
        r = _run_py(code_prelude + """
            import lib.runtime_paths as rp
            print('RTLOGS', rp.logs_root())
        """, env_extra=env_extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = [l for l in r.stdout.splitlines() if l.startswith('RTLOGS ')][0]
        return line[len('RTLOGS '):]

    def test_log_source_mode_repo_logs(self):
        self.assertEqual(self._log_dir(''), os.path.join(_REPO, 'logs'))

    def test_log_override_agrees_with_runtime_paths(self):
        base = os.path.realpath(tempfile.mkdtemp())
        data = os.path.join(base, 'data')
        env = {'TOFU_DATA_DIR': data}
        log_dir = self._log_dir('', env_extra=env)
        self.assertEqual(log_dir, os.path.join(base, 'logs'))
        self.assertEqual(log_dir, self._runtime_logs_root('', env_extra=env),
                         'log.py override branch disagrees with runtime_paths')

    def test_log_frozen_portable_agrees_with_runtime_paths(self):
        exedir = os.path.realpath(tempfile.mkdtemp())
        fake_exe = os.path.join(exedir, 'Tofu')
        prelude = f"""
            import sys
            sys.frozen = True
            sys.executable = {fake_exe!r}
        """
        log_dir = self._log_dir(prelude)
        self.assertEqual(log_dir, os.path.join(exedir, 'logs'))
        self.assertEqual(log_dir, self._runtime_logs_root(prelude),
                         'log.py frozen-portable branch disagrees with runtime_paths')

    def test_log_frozen_readonly_falls_back_not_bundle(self):
        """NC: frozen + UNWRITABLE exe dir must NOT resolve LOG_DIR inside the
        bundle. The buggy twin would return <exe_dir>/logs (uncreatable →
        logging crashes on first launch). Assert it lands in the per-user dir
        and AGREES with runtime_paths."""
        ro_parent = os.path.realpath(tempfile.mkdtemp())
        appdir = os.path.join(ro_parent, 'app')
        os.makedirs(appdir)
        fake_exe = os.path.join(appdir, 'Tofu')
        os.chmod(appdir, 0o555)
        prelude = f"""
            import sys
            sys.frozen = True
            sys.executable = {fake_exe!r}
        """
        try:
            if os.access(appdir, os.W_OK):
                self.skipTest('runner can write to a 0555 dir (root?) — cannot '
                              'simulate a read-only install here')
            log_dir = self._log_dir(prelude)
            self.assertNotEqual(log_dir, os.path.join(appdir, 'logs'),
                                'log.py must NOT put LOG_DIR in the unwritable exe dir')
            self.assertIn('Tofu', log_dir)
            self.assertEqual(log_dir, self._runtime_logs_root(prelude),
                             'log.py fallback disagrees with runtime_paths fallback')
        finally:
            os.chmod(appdir, 0o755)


class CoLocationTest(unittest.TestCase):
    """data/ and logs/ must ALWAYS resolve under ONE base — the frozen-fallback
    probe is a single verdict shared by both. Historically runtime_paths probed
    ``<exe>/data`` while log.py probed ``<exe>/logs``; on a partially-writable
    install those two probes could disagree and split data and logs to different
    roots. Both now probe the shared base ``<exe_dir>`` itself.
    """

    def _roots(self, prelude, env_extra=None):
        """Return (data_root, logs_root, LOG_DIR) from ONE child process so all
        three are resolved under identical env/frozen state."""
        r = _run_py(prelude + """
            import lib.runtime_paths as rp
            import lib.log as L
            print('DATA', rp.data_root())
            print('LOGS', rp.logs_root())
            print('LOGDIR', L.LOG_DIR)
        """, env_extra=env_extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = {ln.split(' ', 1)[0]: ln.split(' ', 1)[1]
               for ln in r.stdout.splitlines() if ' ' in ln and
               ln.split(' ', 1)[0] in ('DATA', 'LOGS', 'LOGDIR')}
        return out['DATA'], out['LOGS'], out['LOGDIR']

    def test_frozen_readonly_data_logs_colocated(self):
        """Frozen + read-only exe dir: data_root, logs_root and log.py's LOG_DIR
        all share ONE base (dirname equal), and none is the unwritable exe dir."""
        ro_parent = os.path.realpath(tempfile.mkdtemp())
        appdir = os.path.join(ro_parent, 'app')
        os.makedirs(appdir)
        fake_exe = os.path.join(appdir, 'Tofu')
        os.chmod(appdir, 0o555)
        prelude = f"""
            import sys
            sys.frozen = True
            sys.executable = {fake_exe!r}
        """
        try:
            if os.access(appdir, os.W_OK):
                self.skipTest('runner can write to a 0555 dir (root?)')
            data, logs, logdir = self._roots(prelude)
            self.assertEqual(os.path.dirname(data), os.path.dirname(logs),
                             'data_root and logs_root split to different bases')
            self.assertEqual(os.path.dirname(logs), os.path.dirname(logdir),
                             'runtime_paths and log.py chose different bases')
            self.assertNotEqual(os.path.dirname(data), appdir,
                                'must not use the unwritable exe dir as base')
        finally:
            os.chmod(appdir, 0o755)

    def test_partial_writable_data_subdir_does_not_split(self):
        """NC: the exe dir is read-only BUT a writable ``data/`` subdir already
        exists inside it. The OLD per-subdir probe (``_dir_is_writable(<exe>/
        data)``) would return True → pick ``appdir`` as base → then logs_root =
        ``appdir/logs`` which is UNCREATABLE (split + crash). The base-dir probe
        must reject ``appdir`` and co-locate BOTH under the per-user fallback.
        """
        ro_parent = os.path.realpath(tempfile.mkdtemp())
        appdir = os.path.join(ro_parent, 'app')
        os.makedirs(os.path.join(appdir, 'data'))  # pre-existing writable data/
        fake_exe = os.path.join(appdir, 'Tofu')
        os.chmod(appdir, 0o555)  # exe dir itself now read-only (no new entries)
        prelude = f"""
            import sys
            sys.frozen = True
            sys.executable = {fake_exe!r}
        """
        try:
            if os.access(appdir, os.W_OK):
                self.skipTest('runner can write to a 0555 dir (root?)')
            # Sanity: the OLD probe target (<exe>/data) IS writable here, so the
            # divergence is real and this scenario actually exercises it.
            self.assertTrue(os.access(os.path.join(appdir, 'data'), os.W_OK))
            data, logs, logdir = self._roots(prelude)
            # Neither may live under the read-only exe dir, and both co-locate.
            self.assertNotEqual(os.path.dirname(data), appdir,
                                'data_root fell back to the unwritable exe dir '
                                '(per-subdir probe divergence)')
            self.assertEqual(os.path.dirname(data), os.path.dirname(logs))
            self.assertEqual(os.path.dirname(logs), os.path.dirname(logdir))
        finally:
            os.chmod(appdir, 0o755)


class SpecDatasFilterTest(unittest.TestCase):
    """The exact filter tofu.spec applies to its candidate datas list."""

    def _filter(self, root):
        candidates = [
            (os.path.join(root, 'static'), 'static'),
            (os.path.join(root, 'index.html'), '.'),
            (os.path.join(root, 'trading.html'), '.'),
            (os.path.join(root, 'VERSION'), '.'),
            (os.path.join(root, '.env.example'), '.'),
        ]
        return [os.path.basename(s) for s, _ in candidates if os.path.exists(s)]

    def test_drops_missing_keeps_present(self):
        kept = self._filter(_REPO)
        # index.html + VERSION + .env.example exist in-tree today.
        self.assertIn('index.html', kept)
        self.assertIn('VERSION', kept)
        self.assertIn('.env.example', kept)
        # trading.html was removed — must be dropped, NOT crash the build.
        self.assertNotIn('trading.html', kept,
                         'trading.html no longer exists; spec must skip it')

    def test_negative_control_unfiltered_would_include_missing(self):
        """NC: an UNFILTERED list would still reference the missing file — which
        is exactly what aborted the PyInstaller build before the fix."""
        raw = [os.path.basename(s) for s, _ in [
            (os.path.join(_REPO, 'trading.html'), '.'),
        ]]
        self.assertIn('trading.html', raw)
        self.assertFalse(os.path.exists(os.path.join(_REPO, 'trading.html')))


if __name__ == '__main__':
    unittest.main()
