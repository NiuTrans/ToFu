"""tests/test_browser_v2_surface.py — the intent-first browser tool surface.

Epic pt_869e5648403e4745: the browser family went from 19 shipped tools to
13 by moving work the MODEL used to do into code:

  * browser_read_page absorbs read_tab / summarize_page /
    get_interactive_elements / get_app_state (auto mode does the
    canvas/SPA diagnosis itself).
  * browser_click / browser_type accept text= (server-side fuzzy
    resolution), auto-wait, and return a page-state receipt.
  * browser_keyboard split into browser_type (clear-first) +
    browser_press_key; browser_create_tab folded into
    browser_navigate(new_tab=true); browser_wait retired from the schema.
  * tab_id optional everywhere (server-side working-tab memory).

Pins the merged surface, the schema diet, the resolver/worktab/receipt
behaviour with fake bridge sends, the registry declarations, and the
approval-enricher coverage for the three new write tools. Retired names
keep dispatch + display continuity (history rendering) — pinned too.
"""

import json

import pytest

pytestmark = pytest.mark.unit


# ── helpers ───────────────────────────────────────────────────────────

def _fake_send(script, calls=None):
    """Build a fake send_browser_command driven by a {cmd: (result, error)}
    script; records calls as (cmd, params) when `calls` list is given.
    A list entry POPS responses in order (last one repeats when exhausted)
    — for pre-action snapshot / post-action receipt list_tabs pairs."""
    def fake(cmd, params=None, timeout=None):
        if calls is not None:
            calls.append((cmd, params))
        entry = script.get(cmd, ({}, None))
        if callable(entry):
            return entry(params)
        if isinstance(entry, list):
            if len(entry) > 1:
                return entry.pop(0)
            return entry[0]
        return entry
    return fake


@pytest.fixture(autouse=True)
def _reset_work_tab():
    from lib.browser import _resolve
    with _resolve._work_tab_lock:
        _resolve._work_tab['id'] = None
    yield
    with _resolve._work_tab_lock:
        _resolve._work_tab['id'] = None


# ── 1. The merged surface ─────────────────────────────────────────────

EXPECTED_V2 = {
    'browser_list_tabs', 'browser_read_page', 'browser_execute_js',
    'browser_screenshot', 'browser_click', 'browser_type',
    'browser_press_key', 'browser_navigate', 'browser_close_tab',
    'browser_get_cookies', 'browser_get_history',
    'browser_fill_form', 'browser_menu_click',
}

RETIRED = {
    'browser_read_tab', 'browser_get_interactive_elements',
    'browser_summarize_page', 'browser_get_app_state',
    'browser_wait', 'browser_hover', 'browser_keyboard',
    'browser_create_tab', 'browser_hover_and_click',
    'browser_right_click_menu',
}


def test_surface_is_exactly_the_merged_13():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.tools.browser import BROWSER_TOOL_NAMES
    shipped = set(BROWSER_TOOL_NAMES) | set(ADVANCED_BROWSER_TOOL_NAMES)
    assert shipped == EXPECTED_V2


def test_retired_names_not_shipped():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.tools.browser import BROWSER_TOOL_NAMES, LEGACY_BROWSER_TOOL_NAMES
    shipped = set(BROWSER_TOOL_NAMES) | set(ADVANCED_BROWSER_TOOL_NAMES)
    assert not (RETIRED & shipped)
    # The retired set is recorded exactly — the display layer keys off it
    # for history rendering, so a silent drift here breaks old cards.
    assert set(LEGACY_BROWSER_TOOL_NAMES) == RETIRED


def test_schema_diet():
    """The 20-tool surface was 18,651 chars (~4.7k tokens per request).

    Pin the consolidated surface well under that so the diet cannot
    silently grow back.
    """
    from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
    from lib.tools.browser import BROWSER_TOOLS
    size = len(json.dumps(BROWSER_TOOLS + ADVANCED_BROWSER_TOOLS,
                          ensure_ascii=False))
    assert size < 15000, f'browser schema surface grew to {size} chars'


def test_tab_id_optional_on_action_tools():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
    from lib.tools.browser import BROWSER_TOOLS
    by_name = {t['function']['name']: t for t in BROWSER_TOOLS + ADVANCED_BROWSER_TOOLS}
    for name in ('browser_click', 'browser_type', 'browser_press_key',
                 'browser_read_page', 'browser_menu_click', 'browser_fill_form',
                 'browser_navigate', 'browser_execute_js'):
        required = by_name[name]['function']['parameters'].get('required', [])
        assert 'tab_id' not in required, f'{name} still requires tab_id'


def test_legacy_dispatch_and_display_continuity():
    """Retired names keep dispatch handlers (direct callers) + display
    formatters (history cards)."""
    from lib.browser.display import _DISPLAY_HANDLERS
    from lib.browser.dispatch import BROWSER_HANDLERS
    for name in RETIRED:
        assert name in BROWSER_HANDLERS, f'{name} lost its dispatch handler'
        assert name in _DISPLAY_HANDLERS, f'{name} lost its display formatter'


# ── 2. Element resolution (text=) ─────────────────────────────────────

_ELEMENTS = [
    {'tag': 'button', 'text': 'Login', 'selector': '#login-btn', 'role': 'button'},
    {'tag': 'a', 'text': 'Login with SSO', 'selector': '#sso', 'role': 'link'},
    {'tag': 'input', 'placeholder': 'Search flights', 'selector': '#q', 'type': 'text'},
    {'tag': 'button', 'text': 'Save draft', 'selector': '#save1', 'role': 'button'},
    {'tag': 'button', 'text': 'Save now', 'selector': '#save2', 'role': 'button'},
]


def _resolve_with(elements, query, kinds='clickable'):
    from lib.browser._resolve import resolve_element
    send = _fake_send({'get_interactive_elements': ({'elements': elements}, None)})
    return resolve_element(1, query, kinds, send=send)


def test_resolver_exact_match_beats_prefix():
    el, note, _ = _resolve_with(_ELEMENTS, 'login')
    assert el is not None and el['selector'] == '#login-btn'


def test_resolver_input_kind_matches_placeholder():
    el, note, _ = _resolve_with(_ELEMENTS, 'search flights', kinds='input')
    assert el is not None and el['selector'] == '#q'


def test_resolver_input_kind_skips_non_inputs():
    # 'login' matches buttons, but input-kind resolution must not return them.
    el, note, _ = _resolve_with(_ELEMENTS, 'login', kinds='input')
    assert el is None


def test_resolver_ambiguity_returns_candidates_no_winner():
    el, note, candidates = _resolve_with(_ELEMENTS, 'save')
    assert el is None and 'ambiguous' in note
    assert any('#save1' in c for c in candidates)


# ── 3. Working-tab memory ─────────────────────────────────────────────

def test_work_tab_explicit_wins_and_remembers():
    from lib.browser._resolve import resolve_work_tab
    send = _fake_send({})
    assert resolve_work_tab({'tabId': 42}, send) == 42
    # Next call without tabId reuses the remembered tab — no bridge call.
    assert resolve_work_tab({}, send) == 42


def test_work_tab_seeds_from_active_tab():
    from lib.browser._resolve import resolve_work_tab
    send = _fake_send({'list_tabs': (
        [{'id': 7, 'active': False}, {'id': 9, 'active': True}], None)})
    assert resolve_work_tab({}, send) == 9


def test_close_tab_forgets_work_tab():
    from lib.browser._resolve import forget_work_tab, resolve_work_tab
    send = _fake_send({'list_tabs': ([{'id': 5, 'active': True}], None)})
    assert resolve_work_tab({'tabId': 5}, send) == 5
    forget_work_tab(5)
    # Falls back to seeding again (active tab 5 from the fake).
    assert resolve_work_tab({}, send) == 5


# ── 4. Action receipt ─────────────────────────────────────────────────

def test_receipt_reports_navigation():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://b', 'title': 'New'}], None)})
    line = action_receipt(1, ('Old', 'http://a'), send)
    assert 'navigated' in line and 'http://b' in line


def test_receipt_reports_unchanged():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://a', 'title': 'Same'}], None)})
    line = action_receipt(1, ('Same', 'http://a'), send)
    assert 'unchanged' in line


def test_receipt_never_raises_on_bridge_failure():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (None, 'bridge down')})
    assert action_receipt(1, (None, None), send) == ''


# ── 5. Handlers end-to-end (fake bridge) ──────────────────────────────

def _patch_facade_send(monkeypatch, script, calls):
    fake = _fake_send(script, calls)
    import lib.browser.handlers as pkg
    monkeypatch.setattr(pkg, 'send_browser_command', fake)
    return fake


def test_click_by_text_resolves_and_reports_receipt(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'get_interactive_elements': ({'elements': _ELEMENTS}, None),
        'click_element': ({'clicked': True, 'tag': 'button', 'text': 'Login'}, None),
        # v3: tab_snapshot reads list_tabs LIVE pre-action (a stale cache
        # must not produce phantom navigations), the receipt post-action.
        'list_tabs': [
            ([{'id': 1, 'url': 'http://before', 'title': 'T'}], None),
            ([{'id': 1, 'url': 'http://after', 'title': 'T'}], None),
        ],
    }, calls)
    from lib.browser.display import update_tab_title
    update_tab_title(1, 'T', url='http://before')
    from lib.browser.handlers import _handle_click
    out = _handle_click({'tabId': 1, 'text': 'login'})
    assert 'Clicked <button>' in out
    assert 'matched "login"' in out
    assert 'page navigated' in out
    cmds = [c for c, _p in calls]
    assert cmds[0] == 'get_interactive_elements'
    assert 'click_element' in cmds
    # resolver-derived selector skips the advisory wait
    assert 'wait_for_element' not in cmds


def test_click_by_selector_gets_advisory_wait(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'wait_for_element': ({'found': True}, None),
        'click_element': ({'clicked': True, 'tag': 'button'}, None),
        'list_tabs': ([{'id': 1, 'url': 'http://x', 'title': 'T'}], None),
    }, calls)
    from lib.browser.handlers import _handle_click
    out = _handle_click({'tabId': 1, 'selector': '#go'})
    assert 'Clicked' in out
    assert [c for c, _ in calls][0] == 'wait_for_element'


def test_click_text_no_match_returns_candidates(monkeypatch):
    _patch_facade_send(monkeypatch, {
        'get_interactive_elements': ({'elements': _ELEMENTS}, None),
    }, [])
    from lib.browser.handlers import _handle_click
    out = _handle_click({'tabId': 1, 'text': 'nonexistent-zzz'})
    assert 'No clear match' in out


def test_type_uses_type_text_clear_first(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'type_text': ({'typed': True}, None),
        'list_tabs': ([{'id': 1, 'url': 'http://x', 'title': 'T'}], None),
    }, calls)
    from lib.browser.handlers import _handle_type
    out = _handle_type({'tabId': 1, 'selector': '#q', 'value': 'hello'})
    assert 'Typed 5 chars' in out
    # v3: a snapshot list_tabs runs before the action — find the type call.
    tcalls = [p for c, p in calls if c == 'type_text']
    assert tcalls and tcalls[0]['clearFirst'] is True
    assert tcalls[0]['text'] == 'hello'


def test_press_key_sends_keyboard_input(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'keyboard_input': ({'success': True, 'target': 'body'}, None),
        'list_tabs': ([{'id': 1, 'url': 'http://x', 'title': 'T'}], None),
    }, calls)
    from lib.browser.handlers import _handle_press_key
    out = _handle_press_key({'tabId': 1, 'keys': 'Enter'})
    assert 'Sent keys "Enter"' in out
    # v3: a snapshot list_tabs precedes the action.
    assert any(c == 'keyboard_input' for c, _ in calls)


def test_navigate_new_tab_uses_create_tab_and_remembers(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'create_tab': ({'id': 77, 'url': 'http://x', 'title': 'X'}, None),
    }, calls)
    from lib.browser.handlers import _handle_navigate
    out = _handle_navigate({'url': 'http://x', 'newTab': True})
    assert 'Opened new tab #77' in out
    from lib.browser._resolve import resolve_work_tab
    assert resolve_work_tab({}, _fake_send({})) == 77


def test_navigate_waits_for_load_by_default(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'navigate': ({'id': 1, 'url': 'http://x', 'status': 'complete'}, None),
    }, calls)
    from lib.browser.handlers import _handle_navigate
    _handle_navigate({'tabId': 1, 'url': 'http://x'})
    assert calls[0][1]['waitForLoad'] is True


# ── 6. read_page auto mode ────────────────────────────────────────────

def test_read_page_auto_substantive_skips_summary(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'read_tab': ({'title': 'T', 'url': 'http://x',
                      'text': 'lorem ipsum ' * 100, 'html': ''}, None),
    }, calls)
    from lib.browser.handlers import _handle_read_page
    out = _handle_read_page({'tabId': 1})
    assert 'lorem ipsum' in out
    assert 'summarize_page' not in [c for c, _ in calls]


def test_read_page_auto_sparse_attaches_summary(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'read_tab': ({'title': 'T', 'url': 'http://x', 'text': 'tiny', 'html': ''}, None),
        'summarize_page': ({'title': 'T', 'url': 'http://x', 'framework': 'Vue',
                            'canvasCount': 2, 'svgCount': 0, 'domElementCount': 50}, None),
    }, calls)
    from lib.browser.handlers import _handle_read_page
    out = _handle_read_page({'tabId': 1})
    assert 'sparse' in out
    assert 'Structural summary' in out
    assert 'summarize_page' in [c for c, _ in calls]


def test_read_page_modes_delegate(monkeypatch):
    calls = []
    _patch_facade_send(monkeypatch, {
        'read_tab': ({'title': 'T', 'url': 'http://x', 'text': 'body ' * 200, 'html': ''}, None),
        'get_interactive_elements': ({'elements': [], 'title': 'T', 'url': 'http://x'}, None),
        'get_app_state': ({'framework': 'Vue'}, None),
    }, calls)
    from lib.browser.handlers import _handle_read_page
    assert 'body' in _handle_read_page({'tabId': 1, 'mode': 'text'})
    assert 'Interactive elements' in _handle_read_page({'tabId': 1, 'mode': 'elements'})
    assert 'App State' in _handle_read_page({'tabId': 1, 'mode': 'app_state'})
    assert 'unknown mode' in _handle_read_page({'tabId': 1, 'mode': 'bogus'})


# ── 7. Registry + approval declarations ───────────────────────────────

def test_registry_declares_the_v2_surface():
    from lib.tools import all_specs
    spec = next(s for s in all_specs() if s.key == 'browser')
    assert set(spec.provides) == EXPECTED_V2
    for name in ('browser_type', 'browser_press_key', 'browser_menu_click'):
        assert name in spec.write_tools
    assert 'browser_read_page' not in spec.write_tools


def test_approval_enrichers_cover_new_write_tools():
    from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
    for name in ('browser_type', 'browser_press_key', 'browser_menu_click'):
        assert name in _APPROVAL_META_ENRICHERS, (
            f'{name} is a write tool with no approval enricher — the user '
            f'would approve blind')
        # Enrichers run on model-supplied args — must not raise on {}.
        _APPROVAL_META_ENRICHERS[name]({}, {})


def test_idempotent_partition_covers_read_page():
    from lib.tasks_pkg.tool_dispatch._flags import _IDEMPOTENT_TOOLS
    assert 'browser_read_page' in _IDEMPOTENT_TOOLS


# ── 8. menu_click (advanced) ──────────────────────────────────────────

def test_menu_click_hover_flow_text_matched(monkeypatch):
    import lib.browser.advanced as adv
    calls = []

    def fake(cmd, params=None, timeout=None):
        calls.append(cmd)
        if cmd == 'hover_element':
            return {'hovered': True}, None
        if cmd == 'get_interactive_elements':
            return {'elements': [
                {'text': 'Export CSV', 'selector': '#exp'},
                {'text': 'Import', 'selector': '#imp'},
            ]}, None
        if cmd == 'click_element':
            return {'clicked': True}, None
        return {}, None

    monkeypatch.setattr(adv, 'send_browser_command', fake)
    out = adv.menu_click(1, 'export', target_selector='#file-menu')
    assert out['success'] is True
    assert calls == ['hover_element', 'get_interactive_elements', 'click_element']


def test_menu_click_item_not_found_lists_available(monkeypatch):
    import lib.browser.advanced as adv

    def fake(cmd, params=None, timeout=None):
        if cmd == 'hover_element':
            return {'hovered': True}, None
        if cmd == 'get_interactive_elements':
            return {'elements': [{'text': 'Close', 'selector': '#c'}]}, None
        return {'clicked': True}, None

    monkeypatch.setattr(adv, 'send_browser_command', fake)
    out = adv.menu_click(1, 'Export', target_selector='#m')
    assert out['success'] is False
    assert 'Close' in out['available_items']


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
