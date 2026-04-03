#!/usr/bin/env python3
"""Validate that all lib/ sub-packages and top-level modules import cleanly.

Usage:
    python -m lib.tests.validate_imports
    # or
    python lib/tests/validate_imports.py
"""
from __future__ import annotations

import importlib
import os
import sys

# Ensure project root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── Modules to validate ──────────────────────────────────────────────────────
# Sub-packages (each has __init__.py with re-exports)
_SUB_PACKAGES = [
    'lib',
    'lib.fetch',
    'lib.trading',
    'lib.trading_autopilot',
    'lib.trading_backtest_engine',
    'lib.trading_strategy_engine',
    'lib.llm_dispatch',
    'lib.project_mod',
    'lib.scheduler',
    'lib.swarm',
    'lib.tasks_pkg',
    'lib.tools',
]

# Top-level modules under lib/ (no sub-directory)
_TOP_LEVEL_MODULES = [
    'lib.browser',
    'lib.browser.advanced',
    'lib.conv_ref',
    'lib.database',
    # 'lib.desktop_agent' excluded — standalone client script requiring pyautogui/psutil/pyperclip
    'lib.desktop_tools',
    'lib.embeddings',
    'lib.image_gen',
    'lib.trading_risk',
    'lib.trading_signals',
    'lib.trading_tasks',
    'lib.llm_client',
    'lib.log',
    'lib.pdf_parser',
    'lib.pricing',
    'lib.protocols',
    'lib.rate_limiter',
    'lib.search',
    'lib.skills',
]

ALL_MODULES = _SUB_PACKAGES + _TOP_LEVEL_MODULES


def validate_imports() -> bool:
    """Try importing each module and report results. Returns True if all pass."""
    passed = 0
    failed = 0
    errors: list[tuple[str, str]] = []

    print('=' * 64)
    print('  lib/ Import Validation')
    print('=' * 64)
    print()

    for mod_name in ALL_MODULES:
        try:
            importlib.import_module(mod_name)
            print(f'  ✅  {mod_name}')
            passed += 1
        except Exception as exc:  # noqa: BLE001
            short = f'{type(exc).__name__}: {exc}'
            print(f'  ❌  {mod_name}  — {short}')
            errors.append((mod_name, short))
            failed += 1

    print()
    print('-' * 64)
    print(f'  Results: {passed} passed, {failed} failed, {passed + failed} total')
    print('-' * 64)

    if errors:
        print()
        print('  Failures:')
        for mod_name, err in errors:
            print(f'    • {mod_name}: {err}')
        print()

    return failed == 0


if __name__ == '__main__':
    ok = validate_imports()
    sys.exit(0 if ok else 1)
