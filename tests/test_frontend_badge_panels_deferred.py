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

from tests._runtime_sections import runtime_section, runtime_section_names

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
MAIN = ROOT / 'frontend' / 'src' / 'main.ts'

BADGE_STUBS = ('toggleOptimizerPanel', 'toggleTimerPanel')


# ---------------------------------------------------------------------------
# 1. manifest move
# ---------------------------------------------------------------------------
def test_badge_panels_deferred_not_core():
    names = runtime_section_names()
    for name in ('optimizer.js', 'timer.js'):
        assert names.count(name) == 1, f'{name} must have exactly one Vite runtime owner'
    assert not (ROOT / 'static' / 'js').exists()


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
    src = runtime_section('optimizer.js')
    m = re.search(r'\(function _bindOptimizerBadge\(\).*?\}\)\(\);', src, re.S)
    assert m, 'optimizer.js lost the _bindOptimizerBadge IIFE'
    assert 'document.readyState' in m.group(0), (
        'the badge-bind IIFE must branch on document.readyState — a '
        'deferred module lands AFTER DOMContentLoaded, and the else-branch '
        'bind() must fire directly (myday precedent)')


def test_timer_badge_static_onclick_kept():
    html = INDEX_HTML.read_text()
    assert 'data-tofu-action="toggleTimerPanel(event)"' in html
    assert 'onclick="toggleTimerPanel(event)"' not in html


# ---------------------------------------------------------------------------
# 3. stubs (py + js dual tables)
# ---------------------------------------------------------------------------
def test_badge_stubs_in_py_table():
    main = MAIN.read_text(encoding='utf-8')
    missing = [s for s in BADGE_STUBS if f"'{s}'" not in main]
    assert not missing, (
        f'the Vite feature router is missing badge-panel actions: {missing}')


def test_badge_stubs_in_loader_table():
    loader = runtime_section('optimizer.js') + runtime_section('timer.js')
    missing = [s for s in BADGE_STUBS if f'function {s}(' not in loader]
    assert not missing, (
        f'the migrated runtime is missing badge-panel owners: {missing}')


# ---------------------------------------------------------------------------
# 4. mobile_panels stays gated (it wraps the deferred globals via window.*)
# ---------------------------------------------------------------------------
def test_mobile_panels_open_flag_sync_gated():
    src = runtime_section('mobile_panels.js')
    for name in ('_setTimerPanelOpen', '_setOptimizerPanelOpen'):
        assert f'typeof runtimeScope.{name} === "function"' in src, (
            f'mobile_panels.js must keep runtimeScope.{name} typeof-gated')
