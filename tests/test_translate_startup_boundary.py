"""Executable contracts for request-loaded translation engines."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    return subprocess.run(
        [sys.executable, '-c', source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.mark.unit
def test_package_import_keeps_translation_engines_dormant():
    proc = _run_isolated(
        'import sys; import lib.translate as translate; '
        'print("TRANSLATE-PACKAGE", len(translate.__all__), '
        'set(translate.__all__) == set(translate._EXPORT_MODULES), '
        '"lib.translate.engine" in sys.modules, '
        '"lib.translate.runtime._worker" in sys.modules, '
        '"lib.translate.incremental" in sys.modules, '
        '"lib.translate.pptx" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'TRANSLATE-PACKAGE 32 True False False False False' in proc.stdout


@pytest.mark.unit
def test_runtime_export_resolves_only_shared_task_authority():
    proc = _run_isolated(
        'import sys; import lib.translate as translate; '
        'runtime = translate._translate_runtime; '
        'print("TRANSLATE-RUNTIME", runtime.kind, '
        '"lib.translate.runtime._state" in sys.modules, '
        '"lib.translate.runtime._worker" in sys.modules, '
        '"lib.translate.engine" in sys.modules, '
        '"lib.translate.incremental" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'TRANSLATE-RUNTIME translate True False False False' in proc.stdout


@pytest.mark.unit
def test_server_boot_keeps_translation_execution_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-TRANSLATE", '
        '"lib.translate.runtime._state" in sys.modules, '
        '"lib.translate.engine" in sys.modules, '
        '"lib.translate.engine._engine" in sys.modules, '
        '"lib.translate.runtime._worker" in sys.modules, '
        '"lib.translate.incremental" in sys.modules, '
        '"lib.pptx_translator" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-TRANSLATE True False False False False False' in proc.stdout
