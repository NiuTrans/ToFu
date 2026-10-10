"""App dialog overlay must stack above the settings sub-overlay tiers.

ROOT CAUSE this guards: .app-dialog-overlay sat at z-index 10050, below the
settings manager/picker tiers (12000/12010). A confirm dialog opened from a
settings surface — e.g. OAuth 断开连接 in settings/oauth.js::_oauthLogout —
rendered underneath the settings window, so the click looked dead and every
re-click silently replaced the hidden dialog (replaceActiveDialog).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _z_index(css: str, selector: str) -> int:
    rule = re.search(re.escape(selector) + r"\{[^}]*\}", css)
    assert rule, f"rule not found: {selector}"
    value = re.search(r"z-index:\s*(\d+)", rule.group(0))
    assert value, f"z-index not found in rule: {selector}"
    return int(value.group(1))


def test_app_dialog_overlay_stacks_above_settings_sub_overlay_tiers() -> None:
    app_css = (ROOT / "frontend/src/styles/application"
               / "08-app-dialogs.css").read_text(encoding="utf-8")
    settings_css = (ROOT / "frontend/src/styles/settings"
                    / "06-model-routing-v2.css").read_text(encoding="utf-8")
    dialog = _z_index(app_css, ".app-dialog-overlay")
    for tier in (".stg-v2-manager-overlay", ".stg-v2-picker-overlay"):
        assert dialog > _z_index(settings_css, tier), (
            f"app dialog ({dialog}) must exceed settings tier {tier}"
        )
