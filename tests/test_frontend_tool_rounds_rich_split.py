"""Guards for pt_3879f00e sub-part 4 — split tool_rounds.js (261KB, the
largest non-i18n file in the core bundle): move the conv-meta rich-render
family (~40KB) + the timer-watcher block (~18KB) + its 1 Hz ticker into
ui/tool_rounds_rich.js (_DEFERRED_FILES).

Census (2026-08-01, all grep-verified):
  * the WHOLE public surface (renderToolRoundsHTML /
    renderSegmentTimelineHTML / renderMcpLoginHintHtml /
    renderTurnProvenanceHtml / renderPreferenceLearnedHtml) is called
    bare from first-paint paths (chat_render.js:1438-1499,
    streaming_ui.js:147-153, branch.js:249, branch_stream.js:293) —
    an overall manifest move is impossible (ledger ruling 2026-08-01),
  * `_renderConvMetaBlock` has exactly ONE caller
    (_renderUnifiedToolLine:2005) whose control flow ALREADY degrades
    gracefully (`if (convMetaHtml) return …` else generic ptool-line),
  * `_renderTimerWatcherBlock` has exactly ONE caller
    (_renderUnifiedToolLine:1913); adding a typeof guard makes absence
    fall through to the same generic line,
  * zero external users of any family helper outside tool_rounds.js,
  * `_localizeInspectOps` is the ONE cross-boundary helper (called by
    the boot-critical image-tiles renderer at L2798) — it STAYS,
  * `_isRoundConvMeta` + `_CONV_META_TOOLS` STAY (used by _getToolSvg),
  * `_cmdTimerTicker` STAYS (run_command chips are core cold-render);
    only `_timerCountdownTicker` moves with the timer-watcher block.

Degradation window: the idle prefetch lands the rich module ~2s after
boot; before that, conv-meta / timer rounds render as the generic
one-line summary. The module's load-time upgrade pass re-renders the
active conversation once IF it contains such rounds (skipped while a
stream is live — the stream re-renders itself).
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
TR_CORE = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds.js'
TR_RICH = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds_rich.js'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'

MOVED_SYMBOLS = (
    '_convMetaHeadLabel', '_convMetaPurpose', '_renderConvDigest',
    '_convMetaAbsTime', '_renderBoardSnapshot', '_unescapeEntities',
    '_renderBoardTransition', '_localizePeerStatusLabel', '_renderPeerStatus',
    '_renderCharterProposal', '_convMetaRelTime', '_renderFeedActivity',
    '_renderPeerDelivery', '_renderCommitResult', '_CONV_META_ROUTINE_READS',
    '_convMetaDefaultOpen', '_convMetaSummaryChip', '_structuredConvMetaBody',
    '_CONV_META_SOURCE_I18N', '_renderConvMetaBlock',
    '_timerNextPollText', '_timerPollReasonText', '_renderTimerWatcherBlock',
    '_tickTimerCountdowns',
)


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


def _core_src():
    return TR_CORE.read_text(encoding='utf-8')


def _rich_src():
    return TR_RICH.read_text(encoding='utf-8') if TR_RICH.exists() else ''


# ---------------------------------------------------------------------------
# 1. manifest (failing-first drivers)
# ---------------------------------------------------------------------------
def test_rich_module_in_deferred_files():
    _bf, deferred, _ep, _crit = _manifest()
    assert 'ui/tool_rounds_rich.js' in deferred, (
        "'ui/tool_rounds_rich.js' must be in _DEFERRED_FILES — the rich "
        'conv-meta + timer-watcher renderers (~58KB) out of the core boot '
        'bundle')


def test_rich_module_not_in_core_bundle_files():
    bundle, _df, _ep, _crit = _manifest()
    assert 'ui/tool_rounds_rich.js' not in bundle, (
        "'ui/tool_rounds_rich.js' must NOT be in _BUNDLE_FILES")


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


def test_ticker_moved_cmd_ticker_stays():
    core, rich = _core_src(), _rich_src()
    assert '_timerCountdownTicker' not in core, (
        'the timer-countdown ticker must move with the timer-watcher block')
    assert '_timerCountdownTicker' in rich, (
        'the timer-countdown ticker must live in the rich module')
    assert '_cmdTimerTicker' in core, (
        'the run_command cmd ticker is core cold-render — it must STAY')


# ---------------------------------------------------------------------------
# 3. cross-boundary dependencies that must STAY (controls)
# ---------------------------------------------------------------------------
def test_localize_inspect_ops_stays_in_core():
    assert re.search(r'(?m)^function _localizeInspectOps\b', _core_src()), (
        '_localizeInspectOps is called by the boot-critical image-tiles '
        'renderer (L2798) — it must STAY in the core file')
    assert not re.search(r'(?m)^function _localizeInspectOps\b', _rich_src()), (
        '_localizeInspectOps must not be duplicated into the rich module')


def test_conv_meta_predicate_stays_in_core():
    src = _core_src()
    assert re.search(r'(?m)^function _isRoundConvMeta\b', src), (
        '_isRoundConvMeta is used by _getToolSvg (core) — it must STAY')
    assert re.search(r'(?m)^const _CONV_META_TOOLS\b', src), (
        '_CONV_META_TOOLS backs the core predicate — it must STAY')


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


# ---------------------------------------------------------------------------
# 5. upgrade pass + no-stub + dev-fallback (controls)
# ---------------------------------------------------------------------------
def test_upgrade_hook_present():
    src = _rich_src()
    for needle in ('_upgradeDegradedToolRounds', 'getActiveConv',
                   'renderChat', 'activeStreams'):
        assert needle in src, (
            f'the rich module must carry the load-time upgrade pass '
            f'(missing {needle}) — otherwise rounds rendered during the '
            'prefetch window stay degraded until the next full render')


def test_no_stub_entries_for_moved_symbols():
    _bf, _df, entry_points, _crit = _manifest()
    for name in ('_renderConvMetaBlock', '_renderTimerWatcherBlock',
                 'renderToolRoundsHTML'):
        assert name not in entry_points, (
            f'{name} must NOT be a deferred entry point — core callers use '
            'typeof-guarded degradation, not the stub loader')
    loader = FEATURE_LOADER.read_text()
    for name in ('_renderConvMetaBlock', '_renderTimerWatcherBlock'):
        assert f"'{name}'" not in loader, (
            f'{name} must NOT be in feature-loader.js stub list either')


def test_dev_fallback_script_tag_kept():
    assert 'static/js/ui/tool_rounds_rich.js' in INDEX_HTML.read_text(), (
        'index.html must carry the tool_rounds_rich.js dev-fallback '
        '<script> tag (bundle-build failure path loads files individually)')
