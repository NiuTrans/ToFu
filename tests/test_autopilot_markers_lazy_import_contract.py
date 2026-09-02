"""Import-order contract for the cycle-free autopilot marker owner.

The fresh subprocess is the behavior boundary: importing
``lib.tasks_pkg.autopilot_markers`` must load the leaf lifecycle module,
must not pull in the facade, and must expose the leaf callable by identity.
This covers the former source-AST pins without coupling the test to import-line
placement or private source layout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ══════════════════════════════════════════════════════════
#  Guard 1 — fresh-subprocess import-order test
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_autopilot_markers_imports_alone_in_fresh_subprocess():
    """``lib.tasks_pkg.autopilot_markers`` must be import-safe as the FIRST
    thing loaded — NO top-level dependency on ``lib.tasks_pkg.autopilot``.

    Runs in a FRESH ``python -c`` subprocess so the current test process's
    already-imported ``autopilot`` module cannot mask a regression: the
    subprocess starts with a clean sys.modules and imports ONLY
    autopilot_markers plus its true (non-cyclic) dependencies. The leaf
    module ``autopilot_run_lifecycle`` is expected to be pulled in as a
    top-level dep; ``autopilot`` itself must NOT be.
    """
    script = textwrap.dedent("""
        import sys
        assert 'lib.tasks_pkg.autopilot' not in sys.modules, (
            'unexpected pre-import of lib.tasks_pkg.autopilot in subprocess')
        assert 'lib.tasks_pkg.autopilot_markers' not in sys.modules, (
            'unexpected pre-import of lib.tasks_pkg.autopilot_markers')

        import lib.tasks_pkg.autopilot_markers as m

        # Sanity: the three extracted symbols must be defined at module top
        # after the fresh import (proves the module's __init__ ran to
        # completion, not aborted midway on a cycle).
        for name in ('arm_autopilot', 'disarm_autopilot', '_marker_exists'):
            assert callable(getattr(m, name, None)), (
                'lib.tasks_pkg.autopilot_markers.' + name +
                ' missing after fresh import — the cycle-free contract has '
                'regressed and the module failed to initialise.')

        # The leaf module IS an expected top-level dep (that's how the
        # cycle was eliminated).
        assert 'lib.tasks_pkg.autopilot_run_lifecycle' in sys.modules, (
            'autopilot_markers must import conclude_run from '
            'autopilot_run_lifecycle at module top; the leaf module is '
            'missing from sys.modules after the import.')

        # Prove `lib.tasks_pkg.autopilot` was NOT pulled in as a side
        # effect. If a future refactor reintroduced a dependency on
        # autopilot (e.g. someone reverted to importing conclude_run
        # through the facade), this assertion would flip.
        assert 'lib.tasks_pkg.autopilot' not in sys.modules, (
            'importing autopilot_markers pulled in lib.tasks_pkg.autopilot '
            '— the cycle-free contract has regressed. autopilot_markers '
            'must depend on autopilot_run_lifecycle (the leaf module), '
            'NOT on autopilot itself.')

        # Bound-callable check: the conclude_run visible on autopilot_markers
        # must BE the leaf module's conclude_run (identity, not a copy).
        import lib.tasks_pkg.autopilot_run_lifecycle as leaf
        assert m.conclude_run is leaf.conclude_run, (
            'autopilot_markers.conclude_run identity does not match the '
            'leaf module; the top-level import wiring is wrong.')

        print('OK')
    """).strip()

    proc = subprocess.run(
        [sys.executable, '-c', script],
        cwd=_ROOT,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        'Fresh-subprocess import of lib.tasks_pkg.autopilot_markers failed — '
        'the cycle-free contract is broken.\n'
        f'stdout:\n{proc.stdout}\n'
        f'stderr:\n{proc.stderr}'
    )
    assert 'OK' in proc.stdout, (
        f'Subprocess exited 0 but did not confirm OK. stdout: {proc.stdout!r}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
