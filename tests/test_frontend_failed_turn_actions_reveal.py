"""Failed Turn actions stay visible on the typed ConversationSurface."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
STYLE = ROOT / "frontend/src/styles/application/05-paper-report-media.css"
SURFACE = ROOT / "frontend/src/conversation/ui/conversation-surface.ts"


def _strip_comments(css: str) -> str:
    from tests._source_scan import strip_comments
    return strip_comments(css, lang="css", inline=True)


def test_css_reveals_failed_turn_action_bar() -> None:
    css = _strip_comments(STYLE.read_text(encoding="utf-8"))
    match = re.search(
        r"\.message\.turn-failed\s+\.message-actions\{([^}]*)\}", css,
    )
    assert match, "failed-Turn action reveal rule is missing"
    assert "opacity:1" in match.group(1).replace(" ", "")


def test_css_base_action_bar_remains_bottom_hover_reveal() -> None:
    css = _strip_comments(STYLE.read_text(encoding="utf-8"))
    match = re.search(r"(?<![.\w-])\.message-actions\{([^}]*)\}", css)
    assert match, "base message action rule is missing"
    body = match.group(1).replace(" ", "")
    assert "opacity:0" in body
    assert "position:absolute" not in body
    assert "margin-top:" in body


def test_typed_surface_owns_the_failed_turn_class() -> None:
    source = SURFACE.read_text(encoding="utf-8")
    assert "turn.status === 'failed' || turn.status === 'interrupted'" in source
    assert "' turn-failed'" in source
    assert "turn.actor !== 'human'" in source


def test_css_reveal_negative_control() -> None:
    css = _strip_comments(STYLE.read_text(encoding="utf-8"))
    neutered = re.sub(
        r"\.message\.turn-failed\s+\.message-actions\{[^}]*\}", "", css,
    )
    assert not re.search(
        r"\.message\.turn-failed\s+\.message-actions\{[^}]*opacity:1",
        neutered,
    )
