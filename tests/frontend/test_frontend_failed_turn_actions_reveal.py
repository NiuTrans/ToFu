"""Failed Turn actions stay visible on the typed ConversationSurface."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.frontend.test_frontend_conversation_surface_vite import (
    conversation_bundle as conversation_bundle,
    _run as run_conversation,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
STYLE = ROOT / "frontend/src/styles/application/05-paper-report-media.css"


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


def test_typed_surface_owns_the_failed_turn_class(conversation_bundle) -> None:
    result = run_conversation(conversation_bundle, r"""
const dom = new (require('jsdom').JSDOM)('<main></main>');
global.Element = dom.window.Element;
const surface = feature.createConversationSurface(dom.window.document.querySelector('main'));
const cases = [['assistant','failed'],['assistant','interrupted'],['assistant','running'],
  ['assistant','completed'],['human','failed'],['human','interrupted']];
const failed = cases.map(([actor, status]) => {
  const state = feature.createTurnState('conversation');
  state.turnsById.turn = {turnId:'turn', presentationId:'turn', conversationId:'conversation',
    laneId:'main', ordinal:1, actor, kind:'reply', status, projectionRevision:1,
    projection:{content:'Visible content'}, settlement:{}, createdAt:1, updatedAt:1};
  state.laneOrder.main = ['turn'];
  surface.render(feature.selectConversationViewModel(state));
  const node = surface.root.querySelector('[data-turn-id="turn"]');
  return node.classList.contains('turn-failed');
});
surface.dispose();
console.log(JSON.stringify({failed}));
""")
    assert result['failed'] == [True, True, False, False, False, False]


def test_css_reveal_negative_control() -> None:
    css = _strip_comments(STYLE.read_text(encoding="utf-8"))
    neutered = re.sub(
        r"\.message\.turn-failed\s+\.message-actions\{[^}]*\}", "", css,
    )
    assert not re.search(
        r"\.message\.turn-failed\s+\.message-actions\{[^}]*opacity:1",
        neutered,
    )
