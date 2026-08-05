"""Guards for Epic-E pt_3879f00e sub-9D — defer optimizer.js + timer.js
(27.5KB, the two badge panels).

Census (2026-08-01): both modules self-init their badge polling
(`_startOptimizerPolling` / `_startTimerPolling` + a 2.5s/3s initial
timeout) and their outside-click/Escape closers only matter with the
panel OPEN (post-load). Deferral delays the badge count ~2s — the
accepted sub-3B degradation class. mobile_panels.js keeps the open-flag
in sync through window._set*PanelOpen, all typeof-gated; its
openMobileTimer/openMobileOptimizer hit window.* names — served by the
feature-loader stubs.

The ONE structural edit: optimizer.js binds #optimizerBadge's click in a
_bindOptimizerBadge IIFE (no static onclick exists). Deferral leaves the
badge dead until bundle arrival — so the binding moves to a STATIC
onclick in index.html (mirroring timerBadge) and the IIFE is REMOVED
from optimizer.js. Keeping both would double-fire the toggle post-load
(static onclick + bound listener → open then instantly close).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'
OPTIMIZER = ROOT / 'static' / 'js' / 'optimizer.js'
MOBILE_PANELS = ROOT / 'static' / 'js' / 'mobile_panels.js'

BADGE_STUBS = ('toggleOptimizerPanel', 'toggleTimerPanel')


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move
# ---------------------------------------------------------------------------
def test_badge_panels_deferred_not_core():
    bundle, deferred, _ep, _crit = _manifest()
    for name in ('optimizer.js', 'timer.js'):
        assert name in deferred, f'{name} must be in _DEFERRED_FILES'
        assert name not in bundle, (
            f'{name} must NOT remain in _BUNDLE_FILES — double-load would '
            'duplicate the polling intervals')


# ---------------------------------------------------------------------------
# 2. the optimizerBadge re-wire: static onclick in, IIFE binder out
# ---------------------------------------------------------------------------
def test_optimizer_badge_no_static_onclick():
    """Shipped shape: the badge stays bound by optimizer.js's own IIFE
    (which branches on readyState and therefore self-arms when the
    deferred module lands). index.html must NOT gain a static onclick —
    two bindings would double-fire the toggle post-land (open then
    instantly close)."""
    html = INDEX_HTML.read_text()
    assert not re.search(
        r'id="optimizerBadge"[^>]*onclick="toggleOptimizerPanel', html), (
        'index.html #optimizerBadge must NOT carry a static '
        'toggleOptimizerPanel onclick — the module IIFE owns the binding')


def test_optimizer_iife_branches_ready_state():
    src = OPTIMIZER.read_text()
    m = re.search(r'\(function _bindOptimizerBadge\(\).*?\}\)\(\);', src, re.S)
    assert m, 'optimizer.js lost the _bindOptimizerBadge IIFE'
    assert 'document.readyState' in m.group(0), (
        'the badge-bind IIFE must branch on document.readyState — a '
        'deferred module lands AFTER DOMContentLoaded, and the else-branch '
        'bind() must fire directly (myday precedent)')


def test_timer_badge_static_onclick_kept():
    html = INDEX_HTML.read_text()
    assert 'onclick="toggleTimerPanel(event)"' in html, (
        'index.html #timerBadge must keep its static toggleTimerPanel onclick')


# ---------------------------------------------------------------------------
# 3. stubs (py + js dual tables)
# ---------------------------------------------------------------------------
def test_badge_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    missing = [s for s in BADGE_STUBS if s not in entry_points]
    assert not missing, (
        f'_DEFERRED_ENTRY_POINTS is missing badge-panel stubs: {missing}')


def test_badge_stubs_in_loader_table():
    loader = FEATURE_LOADER.read_text()
    missing = [s for s in BADGE_STUBS if f"'{s}'" not in loader]
    assert not missing, (
        f'feature-loader.js is missing badge-panel stubs: {missing}')


# ---------------------------------------------------------------------------
# 4. mobile_panels stays gated (it wraps the deferred globals via window.*)
# ---------------------------------------------------------------------------
def test_mobile_panels_open_flag_sync_gated():
    src = MOBILE_PANELS.read_text()
    for name in ('_setTimerPanelOpen', '_setOptimizerPanelOpen'):
        assert f'typeof window.{name} === "function"' in src, (
            f'mobile_panels.js must keep window.{name} typeof-gated')
