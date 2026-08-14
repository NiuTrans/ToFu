#!/usr/bin/env python3
"""Compressed frontend resource budgets used by CI.

Defaults are a ratchet just above the measured 2026-08-14 baseline.  Operators
may tighten them through environment variables; increasing a limit is an
explicit reviewable CI change rather than an accidental bundle regression.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _limit(name: str, default_kib: int) -> int:
    try:
        value = int(os.environ.get(name, '') or default_kib)
    except (TypeError, ValueError):
        value = default_kib
    return max(1, value) * 1024


def _gzip_size(path: Path) -> int:
    return len(gzip.compress(path.read_bytes(), compresslevel=9, mtime=0))


def _vite_javascript_paths(manifest: dict) -> set[str]:
    """Return every unique emitted JS asset, including shared static chunks."""
    paths: set[str] = set()
    for row in manifest.values():
        if not isinstance(row, dict):
            continue
        value = str(row.get('file') or '').replace('\\', '/')
        if not value.endswith(('.js', '.mjs')):
            continue
        if not value.startswith('assets/') or '..' in value.split('/'):
            raise ValueError(f'unsafe Vite asset path: {value!r}')
        paths.add(value)
    return paths


def _check(label: str, size: int, limit: int, failures: list[str]) -> None:
    print(f'{label}: {size / 1024:.1f} KiB gzip '
          f'(budget {limit / 1024:.0f} KiB)')
    if size > limit:
        failures.append(f'{label} is {size - limit} bytes over budget')


def main() -> int:
    from lib.vite_assets import ViteAssetError, validate_vite_artifact

    failures: list[str] = []

    manifest_path = ROOT / 'static' / 'vite' / 'manifest.json'
    try:
        manifest = validate_vite_artifact()
        vite_paths = _vite_javascript_paths(manifest)
        vite_sizes = {
            path: _gzip_size(ROOT / 'static' / 'vite' / path)
            for path in vite_paths
        }
    except (OSError, KeyError, TypeError, ValueError, ViteAssetError,
            json.JSONDecodeError) as exc:
        print(f'frontend-budget: invalid Vite manifest: {exc}', file=sys.stderr)
        return 1

    for entry_name, entry_key in (
            ('main', 'frontend/src/main.ts'), ('admin', 'frontend/src/admin.ts')):
        _check(f'Vite {entry_name} entry', vite_sizes[manifest[entry_key]['file']],
               _limit(f'TOFU_BUDGET_VITE_{entry_name.upper()}_KIB',
                      700 if entry_name == 'main' else 16), failures)

    for key, row in sorted(manifest.items()):
        if not isinstance(row, dict) or not row.get('isDynamicEntry'):
            continue
        chunk_size = vite_sizes[row['file']]
        _check(f'Vite async {key}', chunk_size,
               _limit('TOFU_BUDGET_VITE_CHUNK_KIB', 420), failures)

    # Total means every byte the build emits, not just named entry/dynamic
    # rows. Rollup creates shared static chunks (for example event-format) that
    # are referenced through ``imports`` and were previously invisible here.
    _check('total Vite JavaScript', sum(vite_sizes.values()),
           _limit('TOFU_BUDGET_FRONTEND_TOTAL_KIB', 1500), failures)
    if failures:
        print('frontend-budget: FAILED', file=sys.stderr)
        for failure in failures:
            print(f'  - {failure}', file=sys.stderr)
        return 1
    print('frontend-budget: OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
