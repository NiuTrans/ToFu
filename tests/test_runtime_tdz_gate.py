"""Executable contract for the retained-runtime import-time TDZ gate."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / 'scripts/check_frontend_tdz.mjs'


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['node', str(CHECK), str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_gate_rejects_direct_import_time_use_before_lexical_declaration(tmp_path: Path):
    source = tmp_path / 'broken.js'
    source.write_text('registry.value = 1;\nconst registry = {};\n', encoding='utf-8')
    result = _run(source)
    assert result.returncode != 0
    assert 'registry executes before its lexical declaration' in result.stderr


def test_gate_allows_deferred_function_reference(tmp_path: Path):
    source = tmp_path / 'safe.js'
    source.write_text(
        'const read = () => registry.value;\nconst registry = {};\nread();\n',
        encoding='utf-8',
    )
    result = _run(source)
    assert result.returncode == 0, result.stderr


def test_shipped_runtime_graph_passes_import_time_gate():
    result = subprocess.run(
        ['node', str(CHECK)], cwd=ROOT, text=True, capture_output=True,
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
