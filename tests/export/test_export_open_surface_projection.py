"""Open-export test surface, checked from source before any export runs.

The export-time gate (``export_pkg._verify._verify_exported_test_surface``)
already refuses a public tree whose shipped tests import a dropped module,
but it only runs during an export — so keep-list drift surfaced as an
aborted release instead of a red unit test (2026-10: two packaged-search
generator scripts were missing and every open export failed at the gate).

This spec projects the tracked tree through the same exclusion predicate
and keep-list the export uses, then runs that gate against the projection.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


def _tracked_files(root: Path) -> list[str]:
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, capture_output=True,
                            timeout=120, check=True)
    return [path for path in result.stdout.decode('utf-8').split('\0') if path]


def test_open_export_projection_ships_every_module_its_tests_import(tmp_path):
    from export_pkg._excludes import _should_exclude
    from export_pkg._lists import ROOT, _OPENSOURCE_KEEP_FILES
    from export_pkg._verify import _test_surface_findings

    projection = tmp_path / 'open'
    for rel in _tracked_files(ROOT):
        parts = Path(rel).parts
        excluded = any(
            _should_exclude('/'.join(parts[:depth]), parts[depth - 1], 'opensource')
            for depth in range(1, len(parts) + 1))
        if excluded and rel not in _OPENSOURCE_KEEP_FILES:
            continue
        source = ROOT / rel
        if not source.is_file():
            continue  # deleted in the working tree; not part of this check
        target = projection / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Python files are read by the gate; everything else only has to exist.
        if rel.endswith('.py'):
            os.symlink(source, target)
        else:
            target.touch()

    findings = _test_surface_findings(projection, ROOT)
    assert not findings, (
        'shipped tests import modules the open export drops; add the module to '
        'export_pkg._lists._OPENSOURCE_KEEP_FILES (after a secret review) or stop '
        f'shipping the test: {findings}')
