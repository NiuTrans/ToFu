#!/usr/bin/env python3
"""Shareable exports omit private maintainer tooling, including stale copies.

The export package contains private sanitization markers. Internal and public
exports exclude it; personal backups retain it. The keeper registry tracks the
private source, and runtime imports must not depend on an excluded package.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.parametrize('mode', ['internal', 'opensource'])
def test_export_pkg_excluded_from_shareable_exports(mode):
    assert export._should_exclude(
        'export_pkg/_lists.py', '_lists.py', mode) is not None
    assert export._should_exclude('export_pkg', 'export_pkg', mode) is not None


def test_personal_backup_keeps_export_toolchain():
    assert export._should_exclude(
        'export_pkg/_lists.py', '_lists.py', 'personal') is None


def test_export_py_facade_excluded_everywhere_but_personal():
    """Companion boundary: the facade carries the same marker surface via its
    re-exports, so it is ALWAYS-excluded; personal backups keep it."""
    assert export._should_exclude(
        'export.py', 'export.py', 'opensource') is not None
    assert export._should_exclude(
        'export.py', 'export.py', 'internal') is not None
    assert export._should_exclude('export.py', 'export.py', 'personal') is None


def test_keeper_prefix_registered_in_drift_guard():
    """The drift guard must sanction export_pkg/ as tracked-but-never-published
    via a PREFIX (adding a module to the package must not require editing the
    keeper list). Executable check, not a source-text grep: this imports the
    guard module and inspects its live tuple."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        'export_drift_guard',
        _ROOT / 'tests' / 'meta' / 'test_gitignore_covers_export_excludes.py')
    drift_guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drift_guard)

    assert 'export_pkg/' in drift_guard._KEEPER_PREFIXES, (
        'register export_pkg/ in _KEEPER_PREFIXES of the gitignore drift '
        'guard — it is tracked in the private repo (it IS the release tool) '
        'yet opensource-excluded, so without the keeper the guard reports '
        'every package module as a drift offender')


def test_stale_export_pkg_removed_from_both_shareable_destinations():
    """Neither shareable tier may preserve a previously exported toolchain."""
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d)
        (dest / 'export_pkg').mkdir()
        (dest / 'lib').mkdir()
        src_names = {'export_pkg', 'lib'}

        opensource_got = sorted(
            x.name for x in export._dest_cleanup_targets(
                dest, 'opensource', src_names))
        assert opensource_got == ['export_pkg', 'lib'], (
            'opensource publish mirror must delete the excluded toolchain dir')

        internal_got = [x.name for x in export._dest_cleanup_targets(
            dest, 'internal', src_names)]
        assert 'export_pkg' in internal_got, (
            'internal publication must remove a previously exported private toolchain')
        assert 'lib' in internal_got


def test_no_shipped_runtime_module_references_export_pkg():
    """Only export.py (facade), tests/, and export_pkg/ itself may mention
    export_pkg. Any other reference means a shipped runtime module would
    dangle an import in the opensource tree (where the package does not
    exist)."""
    out = subprocess.run(
        ['git', 'ls-files', '*.py'], cwd=_ROOT,
        capture_output=True, text=True, check=True)
    offenders = []
    for rel in out.stdout.split():
        if rel == 'export.py' or rel.startswith(('tests/', 'export_pkg/')):
            continue
        if export._should_exclude(rel, Path(rel).name, 'opensource'):
            continue
        path = _ROOT / rel
        if not path.is_file():
            continue
        if 'export_pkg' in path.read_text(encoding='utf-8', errors='replace'):
            offenders.append(rel)
    assert not offenders, (
        'runtime modules reference export_pkg — the import dangles in the '
        'opensource tree where the package is never shipped:\n  '
        + '\n  '.join(offenders))


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
