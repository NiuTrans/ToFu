#!/usr/bin/env python3
"""Fail packaging early unless every Vite entry and referenced file exists."""

from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from lib.vite_assets import VITE_MANIFEST, ViteAssetError, validate_vite_artifact  # noqa: E402


def main() -> int:
    try:
        manifest = validate_vite_artifact()
    except ViteAssetError as exc:
        print(f'frontend artifact validation failed: {exc}', file=sys.stderr)
        return 1
    entries = sorted(key for key, row in manifest.items()
                     if isinstance(row, dict) and row.get('isEntry'))
    print(f'validated {VITE_MANIFEST}: {", ".join(entries)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
