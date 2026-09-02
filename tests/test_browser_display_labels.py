"""Browser timeline labels, badges, and frontend catalogue parity."""

from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TOOL_ROUNDS = ROOT / 'frontend' / 'src' / 'runtime' / 'sections' / 'ui' / 'tool_rounds.js'


def _browser_surface():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.tools.browser import BROWSER_TOOL_NAMES, PAGE_PREVIEW_TOOL_NAMES

    return (
        set(BROWSER_TOOL_NAMES)
        | set(ADVANCED_BROWSER_TOOL_NAMES)
        | set(PAGE_PREVIEW_TOOL_NAMES)
    )


def test_every_catalogued_tool_has_a_clean_stateless_label():
    from lib.browser.display import _DISPLAY_HANDLERS, browser_tool_display

    assert set(_DISPLAY_HANDLERS) == _browser_surface()
    for name in sorted(_browser_surface()):
        label = browser_tool_display(name, {})
        assert label and label != name
        assert '?' not in label
        assert not label.endswith((':', ': '))
        assert label.count('(') == label.count(')')


def test_labels_never_depend_on_live_tab_metadata():
    from lib.browser.display import browser_tool_display

    assert browser_tool_display('browser_read_page', {}) == 'Read current tab'
    assert browser_tool_display(
        'browser_read_page', {'tab_id': 77}) == 'Read tab'
    assert browser_tool_display(
        'browser_close_tab', {'tab_ids': [1, 2, 3]}) == 'Close 3 tabs'


def test_click_type_navigation_and_form_labels_capture_intent():
    from lib.browser.display import browser_tool_display

    assert browser_tool_display(
        'browser_click', {'text': '登录'}) == 'Click current tab: 登录'
    assert browser_tool_display(
        'browser_click', {'text': 'File', 'right_click': True}
    ) == 'Right-click current tab: File'
    assert browser_tool_display(
        'browser_type', {'text': '搜索', 'value': 'tofu'}
    ) == 'Type into current tab: 搜索'
    assert browser_tool_display(
        'browser_navigate', {'url': 'https://a.b', 'new_tab': True}
    ) == 'Open new tab → https://a.b'
    assert browser_tool_display(
        'browser_fill_form', {'fields': [{'value': 'a'}, {'value': 'b'}]}
    ) == 'Fill form current tab: 2 fields'
    assert browser_tool_display(
        'browser_devtools', {'action': 'step_over'}
    ) == 'DevTools step over (current tab)'


def test_display_marks_non_object_and_wire_case_arguments_invalid():
    from lib.browser.display import browser_tool_display

    assert browser_tool_display(
        'browser_click', {'tabId': 1}) == 'browser_click (invalid arguments)'
    assert browser_tool_display(
        'browser_click', None) == 'browser_click (invalid arguments)'


def _badge(fn_name, text):
    from lib.tasks_pkg.handlers.browser import _BROWSER_BADGE_DISPATCH

    meta = {'badge': ''}
    _BROWSER_BADGE_DISPATCH[fn_name](
        meta, fn_name, text, len(text), False)
    return meta['badge']


def test_badges_cover_the_catalogue_and_classify_failures():
    from lib.tasks_pkg.handlers.browser import _BROWSER_BADGE_DISPATCH

    assert set(_BROWSER_BADGE_DISPATCH) == _browser_surface()
    assert _badge(
        'browser_fill_form',
        'browser_fill_form succeeded (8 steps)',
    ) == 'filled'
    assert _badge(
        'browser_fill_form',
        'browser_fill_form failed: no tab (completed 0 steps)',
    ) == 'failed'
    assert _badge(
        'browser_menu_click',
        'browser_menu_click failed: Hover failed: x (completed 1 steps)',
    ) == 'failed'
    assert _badge(
        'browser_click', 'No clear match for text="zzz"') == 'failed'
    assert _badge(
        'browser_execute_js', '{"failed": 0, "errorRate": "0%"}') == 'ok'
    assert _badge(
        'browser_devtools', 'DevTools script_source · https://example.test'
    ) == 'script source'


def _frontend_family():
    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    match = re.search(r'_BROWSER_TOOL_FAMILY\s*=\s*\[([^\]]+)\]', src)
    assert match, '_BROWSER_TOOL_FAMILY list not found'
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _frontend_icon_map():
    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    match = re.search(
        r'if \(_isRoundBrowser\(round\)\) \{\s*const m = \{(.*?)\};',
        src,
        re.S,
    )
    assert match, 'browser icon map not found'
    return dict(re.findall(r'(\w+):\s*"(\w+)"', match.group(1)))


def test_frontend_browser_family_and_icons_equal_the_backend_catalogue():
    assert _frontend_family() == _browser_surface()
    icon_map = _frontend_icon_map()
    assert set(icon_map) == _browser_surface()

    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    svg_block = re.search(r'_browserToolSvg = \{(.*?)\n\};', src, re.S)
    assert svg_block, '_browserToolSvg block not found'
    glyphs = set(re.findall(
        r'^\s*(\w+):\s*\'<svg', svg_block.group(1), re.M))
    assert set(icon_map.values()) <= glyphs
