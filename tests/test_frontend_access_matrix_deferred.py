"""Guards for pt_3879f00e sub-part 5A — defer
settings/providers/access_matrix.js (55KB) from the CORE boot bundle
into _CLASSIC_ASSET_FILES.

The access matrix (per-provider model×key health grid) renders only when
the user opens Settings → Providers and toggles the matrix view — never
on first paint.

Census (2026-08-01, all grep-verified):
  * exactly THREE external call sites, ALL already typeof-guarded:
    core_panel.js:108 (`typeof _fitMatrixPanelWidth === 'function'`),
    provider_render.js:261 (same guard after list render),
    provider_render.js:227/233/243 (`typeof _renderAccessMatrix` gates
    `canMatrix`, which gates both the toggle button AND the matrix
    render — the toggle button simply doesn't render while the module
    is absent, so the inline onclick="_toggleMatrixView(pi)" can never
    fire into a missing function),
  * `_stgMatrixOpen` is read by provider_render.js behind
    `typeof _stgMatrixOpen !== 'undefined'` — and is DECLARED inside
    access_matrix.js:40, so it moves with the module,
  * the module's only load-time side effect is a self-contained,
    window-only resize IIFE (node-guarded) — no boot wiring to stub,
  * the module's own generated onclick handlers reference only its own
    functions (self-contained once rendered).

NO feature-loader stub by design: the matrix opens only via a button
that doesn't exist until the module is present — a stub would have
nothing to dispatch. Degradation window: a user who opens Settings →
Providers within the ~2s prefetch window sees the card view instead of
the matrix toggle; the next render (any settings interaction re-renders
the provider list) shows it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
RUNTIME = ROOT / 'frontend' / 'src' / 'runtime' / 'app-runtime.js'
FEATURE_REGISTRY = ROOT / 'frontend' / 'src' / 'feature-registry.ts'
ENTRY = 'settings/providers/access_matrix.js'


def _source(name: str) -> str:
    return runtime_section(name)


# ---------------------------------------------------------------------------
# 1. manifest move (failing-first drivers)
# ---------------------------------------------------------------------------
def test_access_matrix_in_deferred_files():
    assert ENTRY in runtime_section_names(), (
        f"'{ENTRY}' must retain a named owner in the Vite runtime")


def test_access_matrix_not_in_core_bundle_files():
    source = RUNTIME.read_text(encoding='utf-8')
    marker = f'/* ===== migrated source: {ENTRY} ===== */'
    assert source.count(marker) == 1, (
        'the access-matrix owner must occur exactly once in app-runtime.js')
    assert not (ROOT / 'static' / 'js').exists(), (
        'the retired classic source tree must not be recreated')


# ---------------------------------------------------------------------------
# 2. the three external call sites stay typeof-guarded (controls)
# ---------------------------------------------------------------------------
def test_core_panel_fit_call_guarded():
    assert re.search(
        r"typeof\s+_fitMatrixPanelWidth\s*===\s*['\"]function['\"]",
        _source('settings/core_panel.js')), (
        'core_panel.js must keep its typeof guard on the _fitMatrixPanelWidth '
        'call — it fires on EVERY settings tab switch, module or not')


def test_provider_render_fit_call_guarded():
    src = _source('settings/provider_render.js')
    assert re.search(
        r"typeof\s+_fitMatrixPanelWidth\s*===\s*['\"]function['\"]", src), (
        'provider_render.js must keep its typeof guard on the post-render '
        '_fitMatrixPanelWidth call')


def test_provider_render_matrix_gate_guarded():
    src = _source('settings/provider_render.js')
    assert re.search(
        r"typeof\s+_renderAccessMatrix\s*===\s*['\"]function['\"]", src), (
        'provider_render.js must keep the typeof gate on canMatrix — it is '
        'what makes the toggle button + the matrix render absence-safe')
    assert re.search(
        r"typeof\s+_stgMatrixOpen\s*!==\s*['\"]undefined['\"]", src), (
        'provider_render.js must keep the typeof guard on _stgMatrixOpen — '
        'the state var moves with the module')


# ---------------------------------------------------------------------------
# 3. module self-containment (controls)
# ---------------------------------------------------------------------------
def test_matrix_state_declared_in_module():
    assert re.search(r'(?m)^var _stgMatrixOpen\b', _source(ENTRY)), (
        '_stgMatrixOpen must stay declared inside access_matrix.js so the '
        'state moves with the module (provider_render.js reads it guarded)')


def test_no_stub_entries():
    """No feature-loader stub: the matrix opens only via a button that does
    not render while the module is absent — a stub would have nothing to
    dispatch."""
    loader = FEATURE_REGISTRY.read_text(encoding='utf-8')
    for name in ('_renderAccessMatrix', '_toggleMatrixView',
                 '_fitMatrixPanelWidth'):
        assert name not in loader, (
            f'{name} must not be a Vite feature-registry stub; the matrix '
            'owner publishes it directly')


def test_index_has_no_raw_access_matrix_script():
    assert 'static/js/settings/providers/access_matrix.js' not in INDEX_HTML.read_text()
