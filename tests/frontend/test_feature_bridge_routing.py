"""Every feature-bridge stub must resolve to a registered domain owner.

The classic bridge in ``frontend/src/runtime/sections/feature-bridge.js``
installs a routing stub for each ``_FEATURE_ENTRY_POINTS`` name. At click time
the stub asks ``window.TofuModules.canInvokeFeature(name)`` — which answers
from ``routedFeatureEntries`` in ``frontend/src/main.ts`` — and fails closed
with the ``feature.loadFailed`` toast (「功能模块加载失败」) when the name was
never added to an entry set. ``selectBatchChoice``/``submitBatchAnswers``
shipped in the bridge without a ``miscEntries`` route and every click toasted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
BRIDGE_JS = ROOT / 'frontend/src/runtime/sections/feature-bridge.js'
MAIN_TS = ROOT / 'frontend/src/main.ts'


def _bridge_entry_points() -> list[str]:
    source = BRIDGE_JS.read_text(encoding='utf-8')
    match = re.search(
        r'const _FEATURE_ENTRY_POINTS = \[([\s\S]*?)\];', source,
    )
    assert match, '_FEATURE_ENTRY_POINTS block not found in feature-bridge.js'
    return re.findall(r"'([^']+)'", match.group(1))


def _routed_entries() -> set[str]:
    source = MAIN_TS.read_text(encoding='utf-8')
    routed: set[str] = set()
    # Every `const xxxEntries = new Set([...])` block feeds either the
    # routedFeatureEntries spread or a domainLoader branch by name.
    for block in re.findall(r'new Set\(\[([\s\S]*?)\]\)', source):
        routed.update(re.findall(r"'([^']+)'", block))
    return routed


def test_every_bridge_entry_point_has_a_registered_owner():
    bridge = _bridge_entry_points()
    assert len(bridge) >= 20, 'feature-bridge entry list unexpectedly small'
    routed = _routed_entries()
    missing = [name for name in bridge if name not in routed]
    assert not missing, (
        'feature-bridge stubs without a main.ts route fail closed with the '
        f'feature.loadFailed toast at click time: {missing}'
    )


def test_batch_guidance_actions_route_with_human_guidance_owner():
    source = MAIN_TS.read_text(encoding='utf-8')
    misc = re.search(r'const miscEntries = new Set\(\[([\s\S]*?)\]\)', source)
    assert misc, 'miscEntries block not found in main.ts'
    members = set(re.findall(r"'([^']+)'", misc.group(1)))
    assert {'selectBatchChoice', 'submitBatchAnswers'} <= members
