"""Guards for pt_3879f00e sub-part 5B — defer
ui/streaming_swarm_panel.js (55KB) from the CORE boot bundle into
_CLASSIC_ASSET_FILES.

The swarm "Parallel Execution" panel renders only for conversations with
swarm (multi-agent) activity. Census (2026-08-01, grep-verified):

  * exactly THREE exported symbols, SEVEN call sites, ALL previously
    bare: _buildSwarmInboxChipsHTML (streaming_ui.js:126,
    chat_render.js:1493), _buildSwarmPanelHTML (streaming_ui.js:927/956/
    966, tool_rounds.js:2730), _morphSwarmSlot (streaming_ui.js:956/966),
  * degradation contract (same as sub-4): typeof-guarded absence falls
    back to _renderUnifiedToolLine's generic line for swarm rounds, and
    drops the inbox chips; the panel self-heals on the next SSE event /
    re-render once the idle prefetch lands (~2s),
  * the module's load-time side effects are two tickers
    (_swReconcileTicker / _swTimerTicker) that only touch DOM the module
    itself rendered — they move with it,
  * swarm_push.js drives updates indirectly (via updateStreamingUI
    re-renders), never calls the builders directly.
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
STREAMING_UI = ROOT / 'static' / 'js' / 'ui' / 'streaming_ui.js'
CHAT_RENDER = ROOT / 'static' / 'js' / 'ui' / 'chat_render.js'
TOOL_ROUNDS = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds.js'
PANEL = ROOT / 'static' / 'js' / 'ui' / 'streaming_swarm_panel.js'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-bridge.js'
ENTRY = 'ui/streaming_swarm_panel.js'


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move (failing-first drivers)
# ---------------------------------------------------------------------------
def test_swarm_panel_in_deferred_files():
    _bf, deferred, _ep, _crit = _manifest()
    assert ENTRY in deferred, (
        f"'{ENTRY}' must be in _CLASSIC_ASSET_FILES — 55KB of swarm-only panel "
        'out of the render-blocking core')


def test_swarm_panel_not_in_core_bundle_files():
    bundle, _df, _ep, _crit = _manifest()
    assert ENTRY not in bundle, (
        f"'{ENTRY}' must NOT remain in _BUNDLE_FILES — listing it in both "
        'bundles would double its two tickers and the reconciler')


# ---------------------------------------------------------------------------
# 2. the seven call sites are typeof-guarded (drivers until the guards land)
# ---------------------------------------------------------------------------
def test_streaming_ui_inbox_chips_guarded():
    assert re.search(
        r"typeof\s+_buildSwarmInboxChipsHTML\s*===\s*['\"]function['\"]",
        STREAMING_UI.read_text()), (
        'streaming_ui.js:126 must typeof-guard the inbox-chips build')


def test_streaming_ui_render_row_guarded():
    assert re.search(
        r"_isRoundSwarm\(r\)\s*&&\s*typeof\s+_buildSwarmPanelHTML\s*===\s*['\"]function['\"]",
        STREAMING_UI.read_text()), (
        'streaming_ui.js _renderRow must fall back to the generic line '
        'when the panel module is absent')


def test_streaming_ui_morph_sites_guarded():
    guards = re.findall(
        r"typeof\s+_morphSwarmSlot\s*===\s*['\"]function['\"]",
        STREAMING_UI.read_text())
    assert len(guards) >= 2, (
        f'streaming_ui.js must typeof-guard BOTH _morphSwarmSlot sites '
        f'(relocate + in-place morph); found {len(guards)}')


def test_chat_render_inbox_chips_guarded():
    assert re.search(
        r"typeof\s+_buildSwarmInboxChipsHTML\s*===\s*['\"]function['\"]",
        CHAT_RENDER.read_text()), (
        'chat_render.js:1493 (first-paint restore path) must typeof-guard '
        'the inbox-chips build')


def test_tool_rounds_slot_guarded():
    assert re.search(
        r"isSwarm\s*&&\s*typeof\s+_buildSwarmPanelHTML\s*===\s*['\"]function['\"]",
        TOOL_ROUNDS.read_text()), (
        'tool_rounds.js _renderToolSlot must fall back to the generic line '
        'when the panel module is absent')


# ---------------------------------------------------------------------------
# 3. module self-containment + no-stub + dev-fallback (controls)
# ---------------------------------------------------------------------------
def test_tickers_live_in_module():
    src = PANEL.read_text()
    assert '_swReconcileTicker' in src and '_swTimerTicker' in src, (
        'both tickers must stay inside the module they serve (they only '
        'touch DOM the module itself rendered)')


def test_no_stub_entries():
    _bf, _df, entry_points, _crit = _manifest()
    for name in ('_buildSwarmPanelHTML', '_buildSwarmInboxChipsHTML',
                 '_morphSwarmSlot'):
        assert name not in entry_points, (
            f'{name} must NOT be a deferred entry point — callers degrade '
            'via typeof guards, not the stub loader')
    loader = FEATURE_LOADER.read_text()
    for name in ('_buildSwarmPanelHTML', '_buildSwarmInboxChipsHTML',
                 '_morphSwarmSlot'):
        assert f"'{name}'" not in loader


def test_index_has_no_raw_swarm_panel_script():
    assert 'static/js/ui/streaming_swarm_panel.js' not in INDEX_HTML.read_text()
