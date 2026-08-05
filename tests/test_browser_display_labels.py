# Incident anchor: 2026-08-05 owner screenshot — a browser_read_page tool card
# rendered "Read ?" because the v2 surface made tab_id OPTIONAL (omit = the
# working tab) while lib/browser/display.py kept rendering None as '?'.
"""tests/test_browser_display_labels.py — browser tool-card display contract.

Two layers, one contract: after ANY valid tool call the rendered card must be
absolutely correct — never '?', never a dangling colon, never an unbalanced
paren, never the wrong family icon/badge.

  * Backend labels (lib/browser/display.py): every shipped v2 tool rendered
    with {} args (the all-defaults call) and with typical v2 arg shapes.
  * Backend badges (lib/tasks_pkg/handlers/browser.py): ok/fail detection on
    the real result envelopes, browser_fill_form coverage.
  * Frontend parity guards (static/js/ui/tool_rounds.js): the
    _BROWSER_TOOL_FAMILY list must equal the backend family sets exactly, and
    every family name must have an icon-map entry whose glyph exists —
    otherwise the round silently degrades to the generic lightning icon and a
    spurious "✓ done" badge (the drift half of the same incident).
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TOOL_ROUNDS = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds.js'


@pytest.fixture(autouse=True)
def _clean_tab_state():
    """Reset the work-tab memory + title/URL caches around every test."""
    from lib.browser import _resolve
    import lib.browser.display as disp
    with _resolve._work_tab_lock:
        _resolve._work_tab['id'] = None
    with disp._tab_titles_lock:
        disp._tab_titles.clear()
        disp._tab_urls.clear()
    yield
    with _resolve._work_tab_lock:
        _resolve._work_tab['id'] = None
    with disp._tab_titles_lock:
        disp._tab_titles.clear()
        disp._tab_urls.clear()


def _v2_surface():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.tools.browser import BROWSER_TOOL_NAMES
    return set(BROWSER_TOOL_NAMES) | set(ADVANCED_BROWSER_TOOL_NAMES)


# ── 1. The all-defaults call ({} args) never renders '?' ───────────────

def test_every_shipped_tool_label_with_empty_args_is_clean():
    from lib.browser.display import browser_tool_display
    for name in sorted(_v2_surface()):
        label = browser_tool_display(name, {})
        assert label and label != name, f'{name}: empty-args label is {label!r}'
        assert '?' not in label, f'{name}: empty-args label contains ?: {label!r}'
        assert not label.endswith(':'), f'{name}: dangling colon: {label!r}'
        assert not label.endswith(': '), f'{name}: dangling colon: {label!r}'
        assert label.count('(') == label.count(')'), (
            f'{name}: unbalanced parens: {label!r}')


def test_read_page_default_is_current_tab_not_question_mark():
    """The exact screenshot incident: browser_read_page({}) → 'Read ?'."""
    from lib.browser.display import browser_tool_display
    assert browser_tool_display('browser_read_page', {}) == 'Read current tab'


def test_tab_label_names_remembered_work_tab_by_title():
    """Omitting tab_id lands on the working tab — when the memory + title
    cache know it, the card should NAME it, not anonymize it."""
    from lib.browser._resolve import remember_work_tab
    from lib.browser.display import browser_tool_display, update_tab_title
    update_tab_title(7, 'GitHub · tofu', url='https://github.com/x/y')
    remember_work_tab(7)
    assert browser_tool_display('browser_read_page', {}) == 'Read "GitHub · tofu"'


def test_tab_label_falls_back_to_hostname_then_current_tab():
    from lib.browser._resolve import remember_work_tab
    from lib.browser.display import browser_tool_display, update_tab_title
    update_tab_title(9, url='https://km.internal.example.com/page/123')
    remember_work_tab(9)
    assert browser_tool_display('browser_read_page', {}) == 'Read km.internal.example.com'
    # A remembered tab with NO cached metadata is still 'current tab', never '?'.
    remember_work_tab(555)
    assert browser_tool_display('browser_read_page', {}) == 'Read current tab'


# ── 2. v2 arg shapes render their salient target ───────────────────────

def test_click_shows_text_target_and_right_click():
    from lib.browser.display import browser_tool_display
    assert (browser_tool_display('browser_click', {'text': '登录'})
            == 'Click current tab: 登录')
    assert (browser_tool_display('browser_click', {'selector': '#go'})
            == 'Click current tab: #go')
    assert (browser_tool_display('browser_click', {'text': 'File', 'right_click': True})
            == 'Right-click current tab: File')
    # Neither text nor selector (execution rejects it, but the card must
    # still render cleanly — no dangling colon).
    assert browser_tool_display('browser_click', {}) == 'Click current tab'


def test_type_shows_field_not_value():
    from lib.browser.display import browser_tool_display
    assert (browser_tool_display('browser_type', {'text': '搜索', 'value': 'tofu'})
            == 'Type into current tab: 搜索')
    assert browser_tool_display('browser_type', {'value': 'x'}) == 'Type into current tab'


def test_navigate_new_tab_is_not_described_as_reuse():
    from lib.browser.display import browser_tool_display
    assert (browser_tool_display('browser_navigate', {'url': 'https://a.b', 'new_tab': True})
            == 'Open new tab → https://a.b')
    assert (browser_tool_display('browser_navigate', {'url': 'https://a.b'})
            == 'Navigate current tab → https://a.b')


def test_close_tab_variants():
    from lib.browser.display import browser_tool_display, update_tab_title
    assert browser_tool_display('browser_close_tab', {}) == 'Close current tab'
    assert browser_tool_display('browser_close_tab', {'tab_ids': [1, 2, 3]}) == 'Close 3 tabs'
    update_tab_title(4, 'Docs', url='https://d.c')
    assert browser_tool_display('browser_close_tab', {'tab_id': 4}) == 'Close "Docs"'


def test_fill_form_counts_fields_with_balanced_parens():
    from lib.browser.display import browser_tool_display
    label = browser_tool_display(
        'browser_fill_form', {'fields': [{'value': 'a'}, {'value': 'b'}]})
    assert label == 'Fill form current tab: 2 fields'


def test_explicit_tab_id_still_uses_cached_title():
    """Legacy/continuity: an explicit numeric id renders its cached title."""
    from lib.browser.display import browser_tool_display, update_tab_title
    update_tab_title(12, 'Issue #123', url='https://g.h/i/123')
    assert browser_tool_display('browser_read_tab', {'tabId': 12}) == 'Read "Issue #123"'
    # Unknown explicit id: a generic word, never the raw number.
    assert browser_tool_display('browser_read_tab', {'tabId': 99}) == 'Read tab'


# ── 3. Badges: ok/fail on the real result envelopes ────────────────────

def _badge(fn_name, text):
    from lib.tasks_pkg.handlers.browser import _BROWSER_BADGE_DISPATCH
    meta = {'badge': ''}
    fn = _BROWSER_BADGE_DISPATCH[fn_name]
    fn(meta, fn_name, text, len(text), False)
    return meta['badge']


def test_badge_dispatch_covers_every_shipped_tool():
    from lib.tasks_pkg.handlers.browser import _BROWSER_BADGE_DISPATCH
    missing = _v2_surface() - set(_BROWSER_BADGE_DISPATCH)
    assert not missing, f'shipped tools with no badge handler: {missing}'


def test_fill_form_badge_success_and_failure():
    assert _badge('browser_fill_form', 'browser_fill_form succeeded (8 steps)') == 'filled'
    assert _badge('browser_fill_form', 'browser_fill_form failed: no tab (completed 0 steps)') == 'failed'


def test_menu_click_failure_envelope_is_not_badged_clicked():
    assert _badge('browser_menu_click', 'browser_menu_click failed: Hover failed: x (completed 1 steps)') == 'failed'
    assert _badge('browser_menu_click', 'browser_menu_click succeeded (3 steps)') == 'clicked'


def test_click_failure_envelopes_are_not_badged_clicked():
    assert _badge('browser_click', 'Click failed: element not found') == 'failed'
    assert _badge('browser_click', 'No clear match for text="zzz" (no element matches "zzz").') == 'failed'
    assert _badge('browser_click', 'Error: no tab to act on. Pass tab_id.') == 'failed'
    assert _badge('browser_click', 'Clicked <button> "Save" (selector: #s)') == 'clicked'


def test_execute_js_arbitrary_json_never_false_fails():
    # A JS result that merely CONTAINS failure words must keep the ok badge.
    assert _badge('browser_execute_js', '{"failed": 0, "errorRate": "0%"}') == 'ok'
    assert _badge('browser_execute_js', 'Error executing JS: SyntaxError') == 'error'


# ── 4. Frontend parity guards (the drift half of the incident) ─────────

def _frontend_family():
    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    m = re.search(r'_BROWSER_TOOL_FAMILY\s*=\s*\[([^\]]+)\]', src)
    assert m, '_BROWSER_TOOL_FAMILY list not found in tool_rounds.js'
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _frontend_icon_map_block():
    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    m = re.search(r'if \(_isRoundBrowser\(round\)\) \{\s*const m = \{(.*?)\};', src, re.S)
    assert m, 'browser icon map not found in _getRoundIcon'
    return m.group(1)


def test_frontend_browser_family_matches_backend_sets():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.tools.browser import (
        BROWSER_TOOL_NAMES, LEGACY_BROWSER_TOOL_NAMES, PAGE_PREVIEW_TOOL_NAMES)
    backend = (set(BROWSER_TOOL_NAMES) | set(LEGACY_BROWSER_TOOL_NAMES)
               | set(ADVANCED_BROWSER_TOOL_NAMES) | set(PAGE_PREVIEW_TOOL_NAMES))
    frontend = _frontend_family()
    assert frontend == backend, (
        f'frontend/browser family drift — missing: {sorted(backend - frontend)}, '
        f'stale: {sorted(frontend - backend)}')


def test_frontend_icon_map_covers_the_whole_family():
    block = _frontend_icon_map_block()
    mapped = set(re.findall(r'(\w+):\s*"(\w+)"', block))
    mapped_names = {name for name, _icon in mapped}
    missing = _frontend_family() - mapped_names
    assert not missing, f'family tools with no icon-map entry: {sorted(missing)}'
    # …and every referenced glyph must exist in _browserToolSvg.
    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    svg_block = re.search(r'_browserToolSvg = \{(.*?)\n\};', src, re.S)
    assert svg_block, '_browserToolSvg block not found'
    glyphs = set(re.findall(r'^\s*(\w+):\s*\'<svg', svg_block.group(1), re.M))
    missing_glyphs = {icon for _name, icon in mapped} - glyphs
    assert not missing_glyphs, f'icon-map glyphs with no SVG: {sorted(missing_glyphs)}'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
