"""Lazy public facades must not initialize dispatch or HTTP on import."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEAVY_MODULES = (
    'lib.llm_dispatch.api',
    'lib.llm_dispatch.dispatcher',
    'lib.llm_dispatch._api_chat',
    'lib.llm_dispatch._api_stream',
    'lib.llm_dispatch._api_multi',
    'lib.http_transport.client',
    'lib.llm.stream',
    'lib.llm.astream',
    'lib.llm._transport',
)


def _run_isolated(script: str) -> None:
    env = os.environ.copy()
    existing = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = (
        str(_REPO_ROOT) + os.pathsep + existing if existing else str(_REPO_ROOT)
    )
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_REPO_ROOT),
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            result.stderr or result.stdout or f'isolated import failed ({result.returncode})'
        )


def test_llm_dispatch_package_import_does_not_load_execution_modules():
    heavy = ', '.join(repr(name) for name in _HEAVY_MODULES)
    _run_isolated(
        'import sys\n'
        'import lib.llm_dispatch\n'
        f'loaded = [name for name in ({heavy}) if name in sys.modules]\n'
        'assert not loaded, loaded\n'
    )


def test_llm_package_import_does_not_load_transports():
    _run_isolated(
        'import sys\n'
        'import lib.llm\n'
        'loaded = [name for name in ('
        "'requests', 'lib.http_transport.client', 'lib.llm.stream', "
        "'lib.llm.astream', 'lib.llm._transport'"
        ') if name in sys.modules]\n'
        'assert not loaded, loaded\n'
    )


def test_llm_service_import_does_not_load_execution_modules():
    heavy = ', '.join(repr(name) for name in _HEAVY_MODULES)
    _run_isolated(
        'import sys\n'
        'from lib.llm_dispatch.service import resolve_llm_service\n'
        'from lib.protocols import resolve_llm_service as protocol_resolve\n'
        'assert resolve_llm_service is not None\n'
        'assert protocol_resolve is not None\n'
        f'loaded = [name for name in ({heavy}) if name in sys.modules]\n'
        'assert not loaded, loaded\n'
    )


def test_smart_chat_attribute_access_loads_dispatch_api():
    _run_isolated(
        'import sys\n'
        'import lib.llm_dispatch\n'
        'assert "lib.llm_dispatch.api" not in sys.modules\n'
        'lib.llm_dispatch.smart_chat  # noqa: B018 — attribute resolution is the test\n'
        'assert "lib.llm_dispatch.api" in sys.modules\n'
    )
