"""Guards for pt_3879f00e sub-part 8 — split finish_info.js (90KB): the
cost-popover family (~24KB) defers to ui/finish_info_rich.js and builds
LAZILY on first open.

Census (2026-08-01, grep-verified):
  * renderFinishInfo used to embed the FULLY-BUILT popover HTML into
    every painted assistant message (<span class="cost-popover-data"
    hidden>${popHtml}</span>) — the builder was cold even though the
    popover opens only on click,
  * the cache-break phrase family (_CACHE_CAUSE_PHRASES /
    _translateCacheCause / _cacheBreakReason / _cacheBreakState /
    _cacheBreakCulprits / _CP_KEY_SVG / _CP_WARN_SVG) is COLD: the
    collapsed bar's warn tooltip calls _cacheBreakReason at paint —
    it STAYS in core,
  * new contract: renderFinishInfo stashes the build ctx in the
    _costCtxByMsg WeakMap (keyed by the msg object) and embeds an EMPTY
    placeholder; the deferred _toggleCostPopover (feature-loader entry
    point) builds the popover on FIRST open — legacy embedded content
    (mixed-shape bundles) still wins when present,
  * _msgElIndex (chat_render.js, core) resolves the msg from the clicked
    tag; getActiveConv is core.

Behaviour harness (tests/test_frontend_finish_info_rich_modes.py)
proves both modes against the REAL files: degraded (core alone → bar
renders with empty placeholder, no throw) and rich (core + rich →
toggle builds the popover from the stash into the placeholder).
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section_names, runtime_section_path

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
FI = pathlib.Path(runtime_section_path('ui/finish_info.js'))
RICH = pathlib.Path(runtime_section_path('ui/finish_info_rich.js'))
FEATURE_LOADER = pathlib.Path(runtime_section_path('feature-bridge.js'))

MOVED = ('_buildCostPopover', '_hideCostPopover', '_costPopoverOutside',
         '_costPopoverScroll', '_toggleCostPopover', '_costPopoverEl')
COLD_STAYS = ('_CACHE_CAUSE_PHRASES', '_translateCacheCause',
              '_cacheBreakReason', '_cacheBreakState', '_cacheBreakCulprits',
              '_CP_WARN_SVG')


def _manifest():
    files = runtime_section_names()
    source = FEATURE_LOADER.read_text()
    match = re.search(r'_FEATURE_ENTRY_POINTS\s*=\s*\[([^\]]*)\]', source)
    entries = set(re.findall(r"'([^']+)'", match.group(1))) if match else set()
    return files, files, entries, set()


def _fi():
    return FI.read_text(encoding='utf-8')


def _rich():
    return RICH.read_text(encoding='utf-8') if RICH.exists() else ''


# ---------------------------------------------------------------------------
# 1. manifest (failing-first drivers)
# ---------------------------------------------------------------------------
def test_rich_in_deferred_files():
    _bf, deferred, _ep, _crit = _manifest()
    assert 'ui/finish_info_rich.js' in deferred, (
        "'ui/finish_info_rich.js' must be in _CLASSIC_ASSET_FILES — the ~24KB "
        'cost-popover family out of the render-blocking core')


def test_rich_not_in_core_bundle_files():
    bundle, _df, _ep, _crit = _manifest()
    assert bundle.count('ui/finish_info_rich.js') == 1, (
        "'ui/finish_info_rich.js' must occur exactly once in the Vite runtime")


# ---------------------------------------------------------------------------
# 2. the move itself
# ---------------------------------------------------------------------------
def test_popover_family_absent_from_core():
    src = _fi()
    present = [s for s in MOVED
               if re.search(r'(?m)^(?:async )?(?:function|let|var) '
                            + re.escape(s) + r'\b', src)]
    assert not present, (
        f'finish_info.js must not keep the popover family: {present}')


def test_popover_family_present_in_rich():
    src = _rich()
    missing = [s for s in MOVED
               if not re.search(r'(?m)^(?:async )?(?:function|let|var) '
                                + re.escape(s) + r'\b', src)]
    assert not missing, (
        f'finish_info_rich.js is missing popover family members: {missing}')


def test_phrase_family_stays_cold():
    src = _fi()
    missing = [s for s in COLD_STAYS
               if not re.search(r'(?m)^(?:async )?(?:function|const|let|var) '
                                + re.escape(s) + r'\b', src)]
    assert not missing, (
        f'the cache-break phrase family must STAY in core (the collapsed '
        f'bar warn tooltip renders at paint): {missing}')


# ---------------------------------------------------------------------------
# 3. the lazy-build contract
# ---------------------------------------------------------------------------
def test_core_stashes_ctx_no_inline_build():
    src = _fi()
    assert '_costCtxByMsg.set(msg,' in src, (
        'renderFinishInfo must stash the popover ctx in _costCtxByMsg')
    assert 'var _costCtxByMsg = new WeakMap()' in src, (
        'the _costCtxByMsg WeakMap must be declared in core — as a VAR, '
        'not const: the deferred rich module reads it across the bundle '
        'boundary, and only a top-level var lands on the global object '
        '(reachable from any script/eval scope); a const lives only in '
        'the shared lexical env and is invisible to the deferred '
        "bundle's eval in the modes harness (2026-08-01 sub-8 fix)")
    assert '${popHtml}' not in src, (
        'renderFinishInfo must no longer embed pre-built popover HTML')
    assert '_buildCostPopover({' not in src, (
        'renderFinishInfo must no longer call the deferred builder')


def test_rich_toggle_lazy_builds():
    src = _rich()
    assert '_costCtxByMsg.get(' in src, (
        'the deferred toggle must read the ctx stash for the lazy build')
    assert '_msgElIndex' in src, (
        'the deferred toggle must resolve the msg via _msgElIndex')
    assert 'data.innerHTML = _buildCostPopover(' in src, (
        'the deferred toggle must build the popover into the placeholder')


def test_toggle_stubbed_py_and_js():
    _bf, _df, entry_points, _crit = _manifest()
    assert '_toggleCostPopover' in entry_points, (
        '_toggleCostPopover must be a _FEATURE_ENTRY_POINTS member — the '
        'cost tag is chat-rendered on every assistant message')
    assert "'_toggleCostPopover'" in FEATURE_LOADER.read_text(), (
        'feature-bridge.js must install the _toggleCostPopover stub')


def test_builder_not_stubbed():
    """_buildCostPopover is internal to the rich module — only the toggle
    is an entry point."""
    _bf, _df, entry_points, _crit = _manifest()
    assert '_buildCostPopover' not in entry_points


def test_index_has_no_raw_rich_finish_script():
    assert 'static/js/ui/finish_info_rich.js' not in INDEX_HTML.read_text()
