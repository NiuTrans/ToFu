"""Retained rich dispatch boundaries after typed Project and multimedia extraction.

Core/rich sections compose adjacently with no lazy stub or upgrade scan. Project
and multimedia bodies live in typed owners behind deletion-path bridges. The
checklist remains here; command/Watcher countdowns share one demand-scoped ticker.
Core-only materialization remains an explicit fault-tolerance seam.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section_names, runtime_section_path

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / 'index.html'
TR_CORE = pathlib.Path(runtime_section_path('ui/tool_rounds.js'))
TR_RICH = pathlib.Path(runtime_section_path('ui/tool_rounds_rich.js'))
FEATURE_LOADER = pathlib.Path(runtime_section_path('feature-bridge.js'))
RUNTIME_MANIFEST = ROOT / 'frontend/src/runtime/sections/manifest.json'

MOVED_SYMBOLS = (
    '_renderConvMetaBlock', '_renderTodoBlock',
    '_renderTimerWatcherBlock', '_tickTimerCountdowns',
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


def test_retained_dispatch_bridges_remain_in_rich_file():
    src = _rich_src()
    missing = [s for s in MOVED_SYMBOLS
               if not re.search(r'(?m)^(?:async )?(?:function|const) ' + s + r'\b', src)]
    assert not missing, (
        f'ui/tool_rounds_rich.js is missing moved symbols: {missing}')


def test_only_watcher_countdowns_use_the_demand_scoped_ticker():
    core, rich = _core_src(), _rich_src()
    assert '_cmdTimerTicker' not in core
    assert '_timerCountdownTicker' not in rich
    assert 'const ToolElapsedTicker = ' \
        'createDemandScopedPresentationTicker({' in core
    assert '_demandToolElapsedTicker();' not in core
    assert 'demandCountdown:' in rich
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
