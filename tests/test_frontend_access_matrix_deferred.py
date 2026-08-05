"""Guards for pt_3879f00e sub-part 5A — defer
settings/providers/access_matrix.js (55KB) from the CORE boot bundle
into _DEFERRED_FILES.

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


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
AM = ROOT / 'static' / 'js' / 'settings' / 'providers' / 'access_matrix.js'
CORE_PANEL = ROOT / 'static' / 'js' / 'settings' / 'core_panel.js'
PROV_RENDER = ROOT / 'static' / 'js' / 'settings' / 'provider_render.js'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'
ENTRY = 'settings/providers/access_matrix.js'


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move (failing-first drivers)
# ---------------------------------------------------------------------------
def test_access_matrix_in_deferred_files():
    _bf, deferred, _ep, _crit = _manifest()
    assert ENTRY in deferred, (
        f"'{ENTRY}' must be in _DEFERRED_FILES — 55KB of settings-panel "
        'grid out of the render-blocking core')


def test_access_matrix_not_in_core_bundle_files():
    bundle, _df, _ep, _crit = _manifest()
    assert ENTRY not in bundle, (
        f"'{ENTRY}' must NOT remain in _BUNDLE_FILES — listing it in both "
        'bundles would duplicate the matrix state (_stgMatrixOpen) and its '
        'probe registry')


# ---------------------------------------------------------------------------
# 2. the three external call sites stay typeof-guarded (controls)
# ---------------------------------------------------------------------------
def test_core_panel_fit_call_guarded():
    assert re.search(
        r"typeof\s+_fitMatrixPanelWidth\s*===\s*['\"]function['\"]",
        CORE_PANEL.read_text()), (
        'core_panel.js must keep its typeof guard on the _fitMatrixPanelWidth '
        'call — it fires on EVERY settings tab switch, module or not')


def test_provider_render_fit_call_guarded():
    src = PROV_RENDER.read_text()
    assert re.search(
        r"typeof\s+_fitMatrixPanelWidth\s*===\s*['\"]function['\"]", src), (
        'provider_render.js must keep its typeof guard on the post-render '
        '_fitMatrixPanelWidth call')


def test_provider_render_matrix_gate_guarded():
    src = PROV_RENDER.read_text()
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
    assert re.search(r'(?m)^var _stgMatrixOpen\b', AM.read_text()), (
        '_stgMatrixOpen must stay declared inside access_matrix.js so the '
        'state moves with the module (provider_render.js reads it guarded)')


def test_no_stub_entries():
    """No feature-loader stub: the matrix opens only via a button that does
    not render while the module is absent — a stub would have nothing to
    dispatch."""
    _bf, _df, entry_points, _crit = _manifest()
    for name in ('_renderAccessMatrix', '_toggleMatrixView',
                 '_fitMatrixPanelWidth'):
        assert name not in entry_points, (
            f'{name} must NOT be a deferred entry point — the toggle '
            'button only exists once the module is present')
    loader = FEATURE_LOADER.read_text()
    for name in ('_renderAccessMatrix', '_toggleMatrixView',
                 '_fitMatrixPanelWidth'):
        assert f"'{name}'" not in loader, (
            f'{name} must NOT be in feature-loader.js stub list either')


def test_dev_fallback_script_tag_kept():
    assert 'static/js/settings/providers/access_matrix.js' in INDEX_HTML.read_text(), (
        'index.html must carry the access_matrix.js dev-fallback <script> tag')
