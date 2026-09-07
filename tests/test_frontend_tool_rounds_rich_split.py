"""Guards for the retained tool-round core/rich presentation boundary.

The original split moved the conv-meta rich-render family (~40KB) and the
timer-watcher block (~18KB) into ``ui/tool_rounds_rich.js``. Command and Timer
Watcher clocks now share one typed, demand-scoped presentation ticker.

Census (2026-08-28, all grep-verified):
  * the retained tool surface (renderToolRoundsHTML /
    renderSegmentTimelineHTML) is called from the shared selector adapter and
    auxiliary branch views, so this increment moves only its optional rich
    sub-renderers,
  * Turn provenance and its safe inline-Markdown policy now live in the typed
    conversation presentation owner; the rich board title consumes only the
    narrow lexical inline-Markdown bridge,
  * `_renderConvMetaBlock` has exactly ONE caller
    (_renderUnifiedToolLine:2005) whose control flow ALREADY degrades
    gracefully (`if (convMetaHtml) return …` else generic ptool-line),
  * `_renderTimerWatcherBlock` has exactly ONE caller
    (_renderUnifiedToolLine:1913); adding a typeof guard makes absence
    fall through to the same generic line,
  * zero external users of any family helper outside tool_rounds.js,
  * conversation-metadata classification lives in the typed presentation
    owner shared by both retained render adapters,
  * command and Timer Watcher clocks share the typed, demand-scoped
    `ToolElapsedTicker`; neither section owns a boot interval.

The structured checklist renderer stays in the adjacent rich section; its
revision projection remains in core because grouped and timeline renderers both
consume it. Core-only materialization is an explicit fault-tolerance/test seam,
not a production loading phase, so no boot-time upgrade pass is permitted.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section_names, runtime_section_path

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
TR_CORE = pathlib.Path(runtime_section_path('ui/tool_rounds.js'))
TR_RICH = pathlib.Path(runtime_section_path('ui/tool_rounds_rich.js'))
FEATURE_LOADER = pathlib.Path(runtime_section_path('feature-bridge.js'))
RUNTIME_MANIFEST = ROOT / 'frontend/src/runtime/sections/manifest.json'

MOVED_SYMBOLS = (
    '_convMetaHeadLabel', '_convMetaPurpose', '_renderConvDigest',
    '_convMetaAbsTime', '_renderBoardSnapshot', '_unescapeEntities',
    '_renderBoardTransition', '_localizePeerStatusLabel', '_renderPeerStatus',
    '_renderCharterProposal', '_convMetaRelTime', '_renderFeedActivity',
    '_renderPeerDelivery', '_renderCommitResult', '_CONV_META_ROUTINE_READS',
    '_convMetaDefaultOpen', '_convMetaSummaryChip', '_structuredConvMetaBody',
    '_CONV_META_SOURCE_I18N', '_renderConvMetaBlock',
    '_renderTodoBlock', '_timerNextPollText', '_timerPollReasonText',
    '_renderTimerWatcherBlock',
    '_tickTimerCountdowns',
)


def _manifest():
    """Return the retained section order and compatibility entry points."""
    files = runtime_section_names()
    source = FEATURE_LOADER.read_text()
    match = re.search(r'_FEATURE_ENTRY_POINTS\s*=\s*\[([^\]]*)\]', source)
    entries = set(re.findall(r"'([^']+)'", match.group(1))) if match else set()
    return files, entries


def _core_src():
    return TR_CORE.read_text(encoding='utf-8')


def _rich_src():
    return TR_RICH.read_text(encoding='utf-8') if TR_RICH.exists() else ''


# ---------------------------------------------------------------------------
# 1. manifest (failing-first drivers)
# ---------------------------------------------------------------------------
def test_rich_module_is_retained_and_not_lazy():
    manifest = json.loads(RUNTIME_MANIFEST.read_text(encoding='utf-8'))
    retained = [row['source'] for row in manifest['sections']]
    lazy = [
        row['source']
        for bundle in manifest['lazyBundles']
        for row in bundle['sections']
    ]
    assert 'ui/tool_rounds_rich.js' in retained
    assert 'ui/tool_rounds_rich.js' not in lazy


def test_rich_module_not_in_core_bundle_files():
    bundle, _entries = _manifest()
    assert bundle.count('ui/tool_rounds_rich.js') == 1, (
        "'ui/tool_rounds_rich.js' must occur exactly once in the retained "
        'Vite runtime (a duplicate would duplicate every rich renderer)')


def test_rich_module_follows_core_owner_in_runtime_order():
    bundle, _entries = _manifest()
    core_index = bundle.index('ui/tool_rounds.js')
    assert bundle[core_index + 1] == 'ui/tool_rounds_rich.js', (
        'the structured renderers must immediately follow their core dispatch '
        'owner in retained-runtime order')


# ---------------------------------------------------------------------------
# 2. the move itself (failing-first drivers)
# ---------------------------------------------------------------------------
def test_moved_symbols_absent_from_core_file():
    src = _core_src()
    present = [s for s in MOVED_SYMBOLS
               if re.search(r'(?m)^(?:async )?(?:function|const) ' + s + r'\b', src)]
    assert not present, (
        'these symbols must live in ui/tool_rounds_rich.js, not the core '
        f'tool_rounds.js: {present}')


def test_moved_symbols_present_in_rich_file():
    src = _rich_src()
    missing = [s for s in MOVED_SYMBOLS
               if not re.search(r'(?m)^(?:async )?(?:function|const) ' + s + r'\b', src)]
    assert not missing, (
        f'ui/tool_rounds_rich.js is missing moved symbols: {missing}')


def test_timer_families_share_one_demand_scoped_ticker():
    core, rich = _core_src(), _rich_src()
    assert '_cmdTimerTicker' not in core
    assert '_timerCountdownTicker' not in rich
    assert 'const ToolElapsedTicker = ' \
        'createDemandScopedPresentationTicker({' in core
    assert '_demandToolElapsedTicker();' in core
    assert '_demandToolElapsedTicker();' in rich
    assert 'setInterval(' not in core
    assert 'setInterval(' not in rich


# ---------------------------------------------------------------------------
# 3. retired cross-boundary compatibility must not return
# ---------------------------------------------------------------------------
def test_retired_localize_inspect_ops_helper_is_not_reintroduced():
    combined = _core_src() + _rich_src()
    assert '_localizeInspectOps' not in combined, (
        'the retired classic cross-boundary helper must not return; image '
        'inspection localization no longer consumes it')


# ---------------------------------------------------------------------------
# 4. dispatch guards (the two edited call sites)
# ---------------------------------------------------------------------------
def test_timer_watcher_dispatch_guarded():
    assert re.search(
        r"typeof\s+_renderTimerWatcherBlock\s*===\s*['\"]function['\"]",
        _core_src()), (
        'the timer-watcher dispatch (_renderUnifiedToolLine) must be '
        'typeof-guarded so absence falls through to the generic line')


def test_conv_meta_dispatch_guarded():
    assert re.search(
        r"typeof\s+_renderConvMetaBlock\s*===\s*['\"]function['\"]",
        _core_src()), (
        'the conv-meta dispatch (_renderUnifiedToolLine) must be '
        'typeof-guarded so absence degrades to the generic line')


def test_todo_dispatch_guarded():
    assert re.search(
        r"typeof\s+_renderTodoBlock\s*===\s*['\"]function['\"]",
        _core_src()), (
        'the checklist-card dispatch must remain safe in a core-only '
        'materialization')


# ---------------------------------------------------------------------------
# 5. no boot upgrade pass + no compatibility stub
# ---------------------------------------------------------------------------
def test_retained_rich_section_has_no_boot_upgrade_scan():
    src = _rich_src()
    assert '_upgradeDegradedToolRounds' not in src
    assert 'requestAuthoritativeConversationRender(' not in src


def test_no_stub_entries_for_moved_symbols():
    _bundle, entry_points = _manifest()
    for name in ('_renderConvMetaBlock', '_renderTimerWatcherBlock',
                 'renderToolRoundsHTML'):
        assert name not in entry_points, (
            f'{name} must NOT be a feature entry point — the retained sections '
            'compose them directly')
    loader = FEATURE_LOADER.read_text()
    for name in ('_renderConvMetaBlock', '_renderTimerWatcherBlock'):
        assert f"'{name}'" not in loader, (
            f'{name} must NOT be in feature-bridge.js stub list either')


def test_index_has_no_raw_tool_rounds_rich_script():
    assert 'static/js/ui/tool_rounds_rich.js' not in INDEX_HTML.read_text()
