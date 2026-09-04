#!/usr/bin/env python3
"""tests/test_export_pkg_publish_exclusion.py — the export toolchain never
reaches the public tree.

WHY
---
``export_pkg/_lists.py`` carries private markers (real API keys, Feishu
secrets, internal usernames/domains) as sanitization patterns — publishing
the package would leak exactly what the sanitizers exist to strip. The
package therefore sits in ``OPENSOURCE_EXTRA_EXCLUDE_DIRS`` (``export.py``
is in ``ALWAYS_EXCLUDE_FILES`` for the same reason): tracked in the private
repo as the release tool, physically absent from every opensource export.

WHAT IS PINNED
--------------
* ``_should_exclude`` drops ``export_pkg/…`` in opensource mode but keeps it
  in internal/personal (colleagues and self-use backups re-export).
* The gitignore drift-guard registers ``export_pkg/`` as a keeper PREFIX, so
  "tracked privately, never published" stays one sanctioned mechanism.
* The opensource mirror cleanup deletes a stale ``export_pkg/`` already
  sitting in the publish dest (excluded content must not ride ``git add -A``
  forever — the promo incident class); an internal live-install dest keeps it.
* No shipped runtime module references ``export_pkg`` — only the ``export.py``
  facade and ``tests/`` may, so an opensource tree never dangles an import.

Run:  python -B -m pytest tests/test_export_pkg_publish_exclusion.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# export.py is the maintainer's release tool; not shipped in opensource builds.
export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent


def test_export_pkg_excluded_only_in_opensource():
    reason = export._should_exclude(
        'export_pkg/_lists.py', '_lists.py', 'opensource')
    assert reason is not None, (
        'export_pkg/ must be stripped from opensource exports — '
        '_lists.py carries private markers as sanitization patterns')
    for mode in ('internal', 'personal'):
        assert export._should_exclude(
            'export_pkg/_lists.py', '_lists.py', mode) is None, (
            f'{mode}: export_pkg/ must SHIP — colleagues and self-use '
            'backups re-export from the installed tree')


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
        _ROOT / 'tests' / 'test_gitignore_covers_export_excludes.py')
    drift_guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drift_guard)

    assert 'export_pkg/' in drift_guard._KEEPER_PREFIXES, (
        'register export_pkg/ in _KEEPER_PREFIXES of the gitignore drift '
        'guard — it is tracked in the private repo (it IS the release tool) '
        'yet opensource-excluded, so without the keeper the guard reports '
        'every package module as a drift offender')


def test_stale_export_pkg_mirror_deleted_in_opensource_kept_in_internal():
    """A prior opensource export can leave a stale export_pkg/ in the publish
    dest; the mirror cleanup must delete it (same contract as promo/). An
    internal live-install dest preserves it (FUSE I/O optimisation — the tar
    copy refreshes it in place)."""
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
        assert 'export_pkg' not in internal_got, (
            'internal live-install dest keeps the toolchain dir (re-copied '
            'in place by the tar stream)')
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
