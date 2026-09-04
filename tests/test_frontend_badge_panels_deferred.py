"""Residency and first-interaction contracts for Timer/Optimizer panels."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
MAIN = ROOT / 'frontend/src/main.ts'
MANIFEST = ROOT / 'frontend/src/runtime/sections/manifest.json'
UTILITY_FEATURE = ROOT / 'frontend/src/features/utility-panels.ts'


def _manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def test_badge_panels_have_one_lazy_utility_owner():
    manifest = _manifest()
    main = {row['source'] for row in manifest['sections']}
    utility = next(
        bundle for bundle in manifest['lazyBundles']
        if bundle['name'] == 'utility-panels'
    )
    owned = [row['source'] for row in utility['sections']]
    for name in ('optimizer.js', 'timer.js'):
        assert name in owned
        assert name not in main
        assert sum(
            name == row['source']
            for bundle in manifest['lazyBundles']
            for row in bundle['sections']
        ) == 1


def test_both_badges_use_preland_safe_declarative_actions():
    html = INDEX_HTML.read_text(encoding='utf-8')
    for action in ('toggleOptimizerPanel(event)', 'toggleTimerPanel(event)'):
        assert f'data-tofu-action="{action}"' in html
        assert f'onclick="{action}"' not in html
    optimizer = runtime_section('optimizer.js')
    assert '_bindOptimizerBadge' not in optimizer
    assert 'addEventListener("click", toggleOptimizerPanel)' not in optimizer


def test_badge_entries_route_to_the_utility_feature():
    main = MAIN.read_text(encoding='utf-8')
    match = re.search(
        r'const utilityPanelEntries = new Set\(\[(.*?)\]\);', main, re.S,
    )
    assert match
    for name in ('toggleOptimizerPanel', 'toggleTimerPanel'):
        assert f"'{name}'" in match.group(1)
    assert (
        "if (utilityPanelEntries.has(name)) return () => "
        "import('./features/utility-panels');"
    ) in main
    assert "import '../runtime/utility-panels-runtime.generated.js';" in (
        UTILITY_FEATURE.read_text(encoding='utf-8')
    )


def test_polling_and_mobile_rebinding_survive_late_evaluation():
    assert '_startOptimizerPolling();' in runtime_section('optimizer.js')
    assert '_startTimerPolling();' in runtime_section('timer.js')
    mobile = runtime_section('mobile_panels.js')
    for name in ('_setTimerPanelOpen', '_setOptimizerPanelOpen'):
        assert f'typeof runtimeScope.{name} === "function"' in mobile
    assert 'tofu:feature-domain-loaded' in mobile
