"""tests/test_desktop_smoke_gate.py — the release must prove the binary STARTS,
not merely that it exists and is of a plausible size.

THE GAP THIS CLOSES
-------------------
Two gates already guard the release artifacts:

  * every required per-platform file is present (name match);
  * each one clears a size floor (the 49 MB hollow Windows installer that
    shipped with its dependencies missing is rejected by the 81 MB floor).

Both are blind to a MISSING HIDDEN IMPORT. ``tofu.spec`` declares 48 of them,
and dropping one:

  * changes no byte count worth noticing — size is sensitive to a whole absent
    dependency TREE (measured: 48,960,018 hollow vs 115,822,886 healthy) and
    insensitive to one absent module;
  * changes no PyInstaller exit code — the build succeeds;
  * fails at the instant a user double-clicks, with a ``ModuleNotFoundError``
    nobody is present to read.

So the smoke step runs the freshly built binary with ``TOFU_SMOKE=1``. The
launcher then imports ``server``, which at MODULE level constructs the Quart
app and calls ``routes.register_all(app)`` — one import therefore exercises the
whole blueprint tree and the transitive graph the hiddenimports exist to
preserve. Measured from source: exit 0, ``TOFU_SMOKE_OK … blueprints=67``.

WHY THE VERDICT IS EXIT CODE + NO TRACEBACK
--------------------------------------------
Two tempting criteria are both wrong here, and this module pins against them:

**"the process stayed alive for N seconds"** — green by construction. The build
is ``console=False``; a windowed binary detaches and lingers whether or not a
single import resolved. A liveness check would measure the wrong thing and
pass forever.

**"stderr is empty"** — permanently red, MEASURED not assumed. A healthy boot
writes to fd 2 on purpose: the mlockall notice, the ``[boot +N.Ns]`` progress
lines, and the ``[boot] libstdc++ soname`` forensics line another epic added
precisely so a crash stays diagnosable. Demanding empty stderr would fail every
healthy build, and a permanently-red gate is how a suite gets muted. The real
signal is the absence of a TRACEBACK.

WHAT THIS TEST DOES NOT CLAIM
------------------------------
It verifies the smoke branch exists, is reachable, has the right verdict, and
is wired into the legs that can run it. It does NOT execute a frozen
PyInstaller bundle — that needs a real build, which is what CI is for. The
end-to-end evidence recorded here is a from-source run of the same branch.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'
_LAUNCHER = _ROOT / 'desktop' / 'launcher.py'

# Legs whose runner can execute the artifact it just built. macOS is excluded
# deliberately: its build produces a .app bundle whose inner binary path
# differs, and no defect has been measured there — adding an unverified step
# would be speculation, not coverage.
_SMOKE_JOBS = ('build-windows', 'build-linux')


def _jobs():
    return yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))['jobs']


def _smoke_step(job_name):
    for step in _jobs()[job_name].get('steps', []):
        if 'smoke' in (step.get('name') or '').lower():
            return step
    return None


@pytest.mark.parametrize('job', _SMOKE_JOBS)
def test_the_leg_smoke_tests_what_it_built(job):
    """Each runnable leg must execute its own artifact before shipping it."""
    step = _smoke_step(job)
    assert step is not None, (
        f'{job} has no smoke step: it builds an artifact and ships it without '
        'ever starting it. A missing hidden import would reach users.'
    )
    assert (step.get('env') or {}).get('TOFU_SMOKE') == '1', (
        f'{job} smoke step does not set TOFU_SMOKE=1, so the launcher would '
        'boot the full GUI/tray instead of the import-and-exit branch.'
    )


@pytest.mark.parametrize('job', _SMOKE_JOBS)
def test_the_verdict_is_the_exit_code_not_liveness(job):
    """Pin the criterion the ticket warned about.

    `console=False` makes a windowed binary linger regardless of whether it
    imported anything, so any sleep-then-check-alive form is a green light by
    construction.
    """
    body = _smoke_step(job)['run']
    assert 'rc=$?' in body and 'rc" -ne 0' in body, (
        f'{job} smoke step does not branch on the process EXIT CODE. That is '
        'the only signal that cannot be faked by a detached windowed build.'
    )
    assert not re.search(r'sleep\s+\d+.*\n.*(kill -0|ps -p|tasklist)', body), (
        f'{job} smoke step appears to assert process LIVENESS. For a '
        'console=False build that is true whether or not any import '
        'succeeded — it would be a permanently green gate.'
    )


@pytest.mark.parametrize('job', _SMOKE_JOBS)
def test_the_stderr_check_tolerates_a_healthy_boot(job):
    """It must look for a traceback, never for empty stderr.

    Measured: a healthy boot writes the mlockall notice, `[boot +N.Ns]`
    progress lines and the `[boot] libstdc++ soname` forensics line to fd 2.
    `[ -s stderr ]` would therefore fail every good build.
    """
    body = _smoke_step(job)['run']
    assert 'Traceback' in body, (
        f'{job} smoke step does not scan stderr for a traceback, so an '
        'exception that is caught and logged would pass as healthy.'
    )
    assert not re.search(r'\[\s+-s\s+/tmp/smoke\.err\s+\]', body), (
        f'{job} smoke step fails when stderr is NON-EMPTY. A healthy Tofu boot '
        'writes diagnostics to fd 2 by design, so this gate would be red on '
        'every good build — and a permanently-red gate gets muted.'
    )


@pytest.mark.parametrize('job', _SMOKE_JOBS)
def test_the_step_requires_positive_evidence_the_branch_ran(job):
    """Exit 0 alone is not proof: the sentinel must be on stdout.

    If TOFU_SMOKE were ever ignored (renamed var, reordered branch), the
    launcher would fall through to the GUI path. On a headless runner that
    could still exit 0 while importing none of the server.
    """
    body = _smoke_step(job)['run']
    assert 'TOFU_SMOKE_OK' in body, (
        f'{job} smoke step accepts exit 0 without requiring the '
        'TOFU_SMOKE_OK sentinel, so a build where the smoke branch never ran '
        'would pass having proved nothing.'
    )


def test_the_smoke_branch_exists_and_precedes_the_gui_path():
    """The launcher must handle TOFU_SMOKE before it opens windows or ports."""
    src = _LAUNCHER.read_text(encoding='utf-8')
    assert "os.environ.get('TOFU_SMOKE')" in src, (
        'desktop/launcher.py has no TOFU_SMOKE branch — the workflow step '
        'would boot the real GUI app and hang the runner.'
    )
    smoke_at = src.index("os.environ.get('TOFU_SMOKE')")
    for marker in ('_enable_dpi_awareness()', '_find_free_port()', '_spawn_server('):
        pos = src.index(marker, src.index('def main('))
        assert smoke_at < pos, (
            f'the TOFU_SMOKE branch runs AFTER {marker}; smoke mode must not '
            'create windows, bind ports or spawn children.'
        )


def test_the_smoke_branch_asserts_blueprints_were_registered():
    """A bare import is not enough — an app with no routes booted hollow.

    The assertion deliberately looks for the RAISE, not for the word
    "blueprints". A first version of this test matched the substring anywhere
    in the branch and passed after the check was replaced with ``n = 0``,
    because the word survives in an explanatory comment and in the
    ``TOFU_SMOKE_OK … blueprints=%d`` format string. Comments are the first
    source of false-positives for any "this symbol is present" assertion, so
    they are stripped before matching.
    """
    src = _LAUNCHER.read_text(encoding='utf-8')
    branch = src[src.index("os.environ.get('TOFU_SMOKE')"):]
    branch = branch[:branch.index('_enable_dpi_awareness()')]
    code = '\n'.join(ln for ln in branch.splitlines()
                     if not ln.strip().startswith('#'))

    assert "getattr(app, 'blueprints'" in code, (
        'the smoke branch no longer READS app.blueprints, so an app that '
        'imported but registered no routes would report success.'
    )
    assert re.search(r'if\s+n\s*==\s*0\s*:\s*\n\s*raise', code), (
        'the smoke branch reads app.blueprints but does not RAISE on an empty '
        'map — a hollow app would still print TOFU_SMOKE_OK and exit 0.'
    )
    assert 'sys.exit(1)' in code, (
        'the smoke branch does not exit non-zero on failure, so CI could not '
        'tell a broken bundle from a working one.'
    )


@pytest.mark.slow
def test_the_smoke_branch_really_runs_from_source():
    """End-to-end: the branch must exit 0 and print the sentinel.

    Marked slow because it imports the whole server (DB bootstrap, blueprint
    registration). This is the check that caught the empty-stderr mistake:
    the run succeeds while writing several diagnostic lines to fd 2.
    """
    env = dict(os.environ)
    env['TOFU_SMOKE'] = '1'
    proc = subprocess.run([sys.executable, str(_LAUNCHER)], cwd=_ROOT,
                          capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, (
        f'smoke branch exited {proc.returncode}\n'
        f'stdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-2000:]}'
    )
    assert 'TOFU_SMOKE_OK' in proc.stdout, (
        f'sentinel missing from stdout: {proc.stdout[-800:]!r}'
    )
    m = re.search(r'blueprints=(\d+)', proc.stdout)
    assert m and int(m.group(1)) > 0, 'no blueprints reported'
