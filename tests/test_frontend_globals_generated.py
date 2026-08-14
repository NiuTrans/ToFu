"""Guards for the Vite runtime's explicit TypeScript declaration boundary.

The classic concatenated frontend needed a generated ambient ``declare var``
file because unrelated scripts shared one implicit global scope.  The Vite
migration removed that bundle and its generator: retained code is now an ESM
module, and typed owners import its small bridge through
``runtime/app-runtime.d.ts``.  These tests pin that current contract so a new
runtime export cannot silently become ``any`` or creep back into ambient
globals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_JS = ROOT / 'frontend' / 'src' / 'runtime' / 'app-runtime.js'
RUNTIME_DTS = ROOT / 'frontend' / 'src' / 'runtime' / 'app-runtime.d.ts'
VITE_DTS = ROOT / 'frontend' / 'src' / 'vite-env.d.ts'
TSCONFIG = ROOT / 'tsconfig.json'
PACKAGE = ROOT / 'package.json'


def _named_runtime_exports(path: Path) -> set[str]:
    src = path.read_text(encoding='utf-8')
    return set(re.findall(
        r'^export\s+(?:async\s+)?(?:function|const)\s+([A-Za-z_$][\w$]*)',
        src,
        re.MULTILINE,
    ))


def test_runtime_declaration_boundary_exists():
    assert RUNTIME_JS.is_file(), 'retained ESM runtime is missing'
    assert RUNTIME_DTS.is_file(), 'typed runtime bridge declaration is missing'
    assert VITE_DTS.is_file(), 'Vite client declaration entrypoint is missing'


def test_runtime_declarations_cover_every_named_export():
    """Every value exported by the JS runtime has an explicit TS declaration."""
    js_exports = _named_runtime_exports(RUNTIME_JS)
    dts_exports = _named_runtime_exports(RUNTIME_DTS)
    assert js_exports, 'runtime export scan lost its anchor'
    assert dts_exports == js_exports, (
        'app-runtime.d.ts must exactly cover the JS runtime value exports; '
        f'missing={sorted(js_exports - dts_exports)}, '
        f'stale={sorted(dts_exports - js_exports)}'
    )


def test_runtime_declaration_is_in_the_tsc_program():
    cfg = json.loads(TSCONFIG.read_text(encoding='utf-8'))
    includes = cfg.get('include', [])
    assert 'frontend/src/**/*.ts' in includes, (
        'tsconfig.json must include the frontend TypeScript tree (including '
        'runtime/app-runtime.d.ts)'
    )


def test_vite_client_types_are_explicitly_loaded():
    src = VITE_DTS.read_text(encoding='utf-8')
    assert '/// <reference types="vite/client" />' in src


def test_runtime_bridge_does_not_degrade_to_any_or_blanket_globals():
    src = RUNTIME_DTS.read_text(encoding='utf-8')
    assert not re.search(r'\bany\b', src), (
        'runtime bridge declarations must preserve unknown/callable boundaries, '
        'not erase them with any'
    )
    assert not re.search(r'\bdeclare\s+(?:var|let|const)\b', src), (
        'application runtime values are ESM exports, not ambient globals'
    )
    assert not re.search(r'\[\s*\w+\s*:\s*string\s*\]\s*:', src), (
        'blanket string index signatures would hide misspelled bridge members'
    )


def test_frontend_gate_runs_module_typecheck():
    package = json.loads(PACKAGE.read_text(encoding='utf-8'))
    scripts = package.get('scripts', {})
    assert 'tsc -p tsconfig.vite.json --noEmit' in scripts.get('typecheck:modules', '')
    assert 'typecheck:modules' in scripts.get('check:frontend', ''), (
        'the frontend release gate must exercise the typed Vite module graph'
    )
