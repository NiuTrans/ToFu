"""Current folder badges use the shared quiet presentation, not a retired chip.

Chromium paints both badges through their real owner and computes the shipped
application stylesheet under the tofu theme: body sans font, one uniform border
colour (no bevel) and no hard ``0 2px 0`` drop. The NEUTER restores the retired
bevel with a later rule and proves both checks bite.
"""
from pathlib import Path

import pytest

from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_OWNER = ROOT / 'frontend/src/conversation/ui/new-chat-folder-badges.ts'
_ENTRY = """
import { paintNewChatFolderBadges } from './new-chat-folder-badges';
(window as any).paintNewChatFolderBadges = paintNewChatFolderBadges;
"""
_RETIRED_BEVEL = (
    '[data-theme="tofu"] .topbar-folder-badge,[data-theme="tofu"] .welcome-folder-badge'
    '{border:1px solid #888;border-top-color:#fff;box-shadow:0 2px 0 #000}')
_PROBE = """() => {
  paintNewChatFolderBadges(document, {name: 'Research', color: '#3366cc'});
  const token = document.createElement('span');
  token.style.color = 'var(--text-secondary)';
  token.style.fontFamily = 'var(--sans-body)';
  document.body.append(token);
  const read = (selector) => {
    const style = getComputedStyle(document.querySelector(selector));
    return {color: style.color, font: style.fontFamily, shadow: style.boxShadow,
            borders: [style.borderTopColor, style.borderRightColor,
                      style.borderBottomColor, style.borderLeftColor]};
  };
  const tokens = getComputedStyle(token);
  return {topbar: read('.topbar-folder-badge'), welcome: read('.welcome-folder-badge'),
          secondary: tokens.color, sans: tokens.fontFamily};
}"""


def _paint(browser, tmp_path, extra_css=''):
    page_path = tmp_path / 'badges.html'
    page_path.write_text(
        '<!doctype html><html data-theme="tofu"><head><meta charset="utf-8">'
        f'<link rel="stylesheet" href="{(ROOT / "static/styles.css").as_uri()}">'
        f'<style>{extra_css}</style></head><body>'
        '<div id="topbarTitle">Chat</div>'
        '<div id="welcome"><div class="tofu-brand">Tofu</div></div></body></html>',
        encoding='utf-8')
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(page_path.as_uri())
        with compiled_typescript(_OWNER, contents=_ENTRY) as bundle:
            page.add_script_tag(path=bundle)
        return page.evaluate(_PROBE)
    finally:
        context.close()


def test_current_badges_have_one_quiet_presentation(browser, tmp_path):
    painted = _paint(browser, tmp_path)
    for name in ('topbar', 'welcome'):
        badge = painted[name]
        assert badge['font'] == painted['sans'], name
        assert len(set(badge['borders'])) == 1, name
        assert '0px 2px 0px' not in badge['shadow'], name
    assert painted['welcome']['color'] == painted['secondary']
    # The topbar badge carries the folder's own validated colour inline.
    assert painted['topbar']['color'] == 'rgb(51, 102, 204)'


def test_neuter_restoring_bevel_is_detected(browser, tmp_path):
    painted = _paint(browser, tmp_path, extra_css=_RETIRED_BEVEL)
    for name in ('topbar', 'welcome'):
        badge = painted[name]
        assert len(set(badge['borders'])) > 1, name
        assert '0px 2px 0px' in badge['shadow'], name
