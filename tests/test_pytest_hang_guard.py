"""Guard: a hanging test must abort itself, not stall the run to the ceiling.

WHY
---
2026-07-27, while NEUTER-testing the agent-loop no-progress breaker, deleting
the fingerprint comparison did NOT turn the suite red — it made it HANG. The
breaker is the only exit from an infinite dispatcher, so removing it leaves
the test spinning forever. That is a legitimate NEUTER bite, but it is only
*observable* if pytest aborts the wedged test on its own.

MEASURED at the time (two separate defects, one of which was my own error):

  1. My first probe reported ``ImportError: No module named pytest_timeout``.
     That was a MEASUREMENT ERROR — it ran ``/usr/bin/python``, not the
     project interpreter. pytest-timeout 2.4.0 *is* installed in the conda
     env and ``pyproject.toml`` has declared it since before this incident.
  2. The REAL defect: no default timeout is configured anywhere, so the
     installed plugin never fires. Probe: a ``time.sleep(600)`` test ran
     until an EXTERNAL ``timeout 25`` killed the shell (pytest emitted no
     verdict at all); with ``--timeout=5`` the same test self-aborted in
     5.04s with a clean FAILED line.

A hang that only an external kill can stop is indistinguishable from an
infrastructure stall, and it silently un-verifies every hang-shaped NEUTER in
the suite. So the default belongs in config, where every invocation inherits
it — not in the muscle memory of whoever happens to run pytest.

NOTE on ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` (this project's standard test
env, per the user's convention): it suppresses entry-point autoloading, so
pytest-timeout must be named explicitly via ``-p pytest_timeout`` in
``addopts`` for the setting to take effect under that env too. Both halves
are asserted below — the timeout VALUE and the explicit plugin LOAD — because
either alone leaves the guard inert.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_pyproject() -> str:
    with open(os.path.join(_ROOT, 'pyproject.toml'), encoding='utf-8') as f:
        return f.read()


class TestHangGuardConfigured(unittest.TestCase):

    def test_default_timeout_is_configured(self):
        """A per-test timeout MUST be set in config, not left to the caller."""
        text = _read_pyproject()
        self.assertRegex(
            text, r'(?m)^\s*timeout\s*=\s*\d+',
            'pyproject.toml [tool.pytest.ini_options] has no `timeout = N`; '
            'an installed pytest-timeout with no configured default NEVER '
            'fires, so a wedged test stalls the whole run (measured: a '
            'sleep(600) test produced no pytest verdict at all)')

    def test_timeout_value_is_sane(self):
        """Long enough for real suites, short enough to catch a wedge."""
        import re
        m = re.search(r'(?m)^\s*timeout\s*=\s*(\d+)', _read_pyproject())
        self.assertIsNotNone(m, 'no timeout configured')
        secs = int(m.group(1))
        self.assertGreaterEqual(secs, 60, 'too tight — slow-but-healthy '
                                'suites (jsdom harnesses, PDF parsing) would '
                                'flake')
        self.assertLessEqual(secs, 900, 'too loose to bound a wedged loop')

    def test_plugin_explicitly_loaded_for_no_autoload_env(self):
        """``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` is this project's standard test
        env; under it the plugin only loads when named explicitly."""
        text = _read_pyproject()
        self.assertIn(
            '-p pytest_timeout', text,
            'addopts must name `-p pytest_timeout` explicitly: under '
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 (the standard env for this '
            'suite) entry-point autoload is off, so `timeout = N` alone is '
            'silently inert')


class TestHangGuardActuallyBites:
    """End-to-end: a wedged test really does get aborted by the config."""

    @pytest.mark.slow
    def test_wedged_test_is_aborted_by_config(self, tmp_path):
        # The probe lives under pytest's ``tmp_path`` — never in the live repo
        # ``tests/`` tree, where a crash before the finally could leave a 600s
        # sleeper for a concurrent pytest to collect. Point pytest at the
        # repo's pyproject.toml explicitly (``-c``) so addopts still name
        # ``-p pytest_timeout`` under PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, and
        # use ``--confcutdir`` so collection stays hermetic: the probe only
        # needs the timeout plugin, not the heavy repo tests/conftest.py.
        probe = tmp_path / 'test_zz_hang_probe_generated.py'
        probe.write_text(textwrap.dedent('''
            import time
            def test_wedged():
                time.sleep(600)
        '''), encoding='utf-8')
        env = dict(os.environ)
        env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
        # Shorten the window so this test is quick; the plugin is loaded by
        # `-p pytest_timeout` in the repo addopts. If that were missing,
        # --timeout would be an "unrecognized argument" and this fails.
        r = subprocess.run(
            [sys.executable, '-m', 'pytest', str(probe), '-q',
             '-p', 'no:cacheprovider', '--timeout=6',
             '-c', os.path.join(_ROOT, 'pyproject.toml'),
             '--confcutdir', str(tmp_path)],
            cwd=_ROOT, env=env, capture_output=True, text=True,
            timeout=180)
        assert '1 failed' in r.stdout, (
            f'wedged test was not aborted.\n{r.stdout[-2000:]}')
        assert 'Timeout' in r.stdout, (
            'aborted, but not by the timeout mechanism')


if __name__ == '__main__':
    unittest.main(verbosity=2)
