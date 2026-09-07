"""tests/test_browser_v2_surface.py — the intent-first browser tool surface.

Epic pt_869e5648403e4745: the browser family first went from 19 shipped tools
to 13 by moving work the MODEL used to do into code. The generic background
research intent adds one compound read tool without restoring raw primitives:

  * browser_read_page absorbs read_tab / summarize_page /
    get_interactive_elements / get_app_state (auto mode does the
    canvas/SPA diagnosis itself).
  * browser_click / browser_type accept text= (server-side fuzzy
    resolution), auto-wait, and return a page-state receipt.
  * browser_keyboard split into browser_type (clear-first) +
    browser_press_key; browser_create_tab folded into
    browser_navigate(new_tab=true); browser_wait retired from the schema.
  * tab_id optional everywhere (server-side working-tab memory).

Pins the canonical surface, schema budget, resolver/worktab/receipt
behaviour, registry declarations, and approval-enricher coverage. Retired
names are rejected at every runtime and rendering boundary.
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
    def fake(cmd, params=None, timeout=None, **_route):
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


_ROUTE_KEY = ('101', 'test-browser')


def _runtime(send):
    from lib.browser.tool_runtime import BrowserToolRuntime

    return BrowserToolRuntime(
        owner_user_id=_ROUTE_KEY[0],
        client_id=_ROUTE_KEY[1],
        sender=send,
    )


@pytest.fixture(autouse=True)
def _reset_work_tab():
    import lib.browser._resolve as _resolve
    with _resolve._work_tab_lock:
        _resolve._work_tabs.clear()
    yield
    with _resolve._work_tab_lock:
        _resolve._work_tabs.clear()


# ── 1. The merged surface ─────────────────────────────────────────────

EXPECTED_V2 = {
    'browser_list_tabs', 'browser_read_page', 'browser_research_page',
    'browser_devtools', 'browser_execute_js',
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


def test_surface_is_exactly_the_merged_15():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.tools.browser import BROWSER_TOOL_NAMES
    shipped = set(BROWSER_TOOL_NAMES) | set(ADVANCED_BROWSER_TOOL_NAMES)
    assert shipped == EXPECTED_V2


def test_retired_names_not_shipped():
    from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
    from lib.browser.display import _DISPLAY_HANDLERS
    from lib.browser.dispatch import BROWSER_HANDLERS
    from lib.tools.browser import BROWSER_TOOL_NAMES
    shipped = set(BROWSER_TOOL_NAMES) | set(ADVANCED_BROWSER_TOOL_NAMES)
    assert not (RETIRED & shipped)
    assert not (RETIRED & set(BROWSER_HANDLERS))
    assert not (RETIRED & set(_DISPLAY_HANDLERS))


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
                 'browser_navigate', 'browser_devtools', 'browser_execute_js'):
        required = by_name[name]['function']['parameters'].get('required', [])
        assert 'tab_id' not in required, f'{name} still requires tab_id'


def test_retired_names_are_rejected_without_a_compatibility_surface():
    from lib.browser.dispatch import execute_browser_tool
    for name in RETIRED:
        assert execute_browser_tool(
            name, {}, owner_user_id='101', client_id='test-browser'
        ) == f'Error: Unknown browser tool: {name}'


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
    assert resolve_work_tab(
        {'tabId': 42}, route_key=_ROUTE_KEY, send=send) == 42
    # Next call without tabId reuses the remembered tab — no bridge call.
    assert resolve_work_tab({}, route_key=_ROUTE_KEY, send=send) == 42


def test_work_tab_seeds_from_active_tab():
    from lib.browser._resolve import resolve_work_tab
    send = _fake_send({'list_tabs': (
        [{'id': 7, 'active': False}, {'id': 9, 'active': True}], None)})
    assert resolve_work_tab({}, route_key=_ROUTE_KEY, send=send) == 9


def test_work_tab_seed_skips_client_tab():
    """The Tofu client tab (flagged isClient by the extension) is never an
    implicit action target — seeding falls through to the next tab even
    when the client tab is the active one."""
    from lib.browser._resolve import resolve_work_tab
    send = _fake_send({'list_tabs': (
        [{'id': 7, 'active': True, 'isClient': True},
         {'id': 9, 'active': False}], None)})
    assert resolve_work_tab({}, route_key=_ROUTE_KEY, send=send) == 9


def test_work_tab_seed_returns_none_when_only_client_tabs():
    from lib.browser._resolve import resolve_work_tab
    send = _fake_send({'list_tabs': (
        [{'id': 7, 'active': True, 'isClient': True}], None)})
    assert resolve_work_tab({}, route_key=_ROUTE_KEY, send=send) is None


def test_close_tab_forgets_work_tab():
    from lib.browser._resolve import forget_work_tab, resolve_work_tab
    send = _fake_send({'list_tabs': ([{'id': 5, 'active': True}], None)})
    assert resolve_work_tab(
        {'tabId': 5}, route_key=_ROUTE_KEY, send=send) == 5
    forget_work_tab(_ROUTE_KEY, 5)
    # Falls back to seeding again (active tab 5 from the fake).
    assert resolve_work_tab({}, route_key=_ROUTE_KEY, send=send) == 5


def test_work_tab_memory_is_isolated_by_owner_and_device():
    from lib.browser._resolve import current_work_tab, remember_work_tab

    alice = ('101', 'alice-browser')
    bob = ('202', 'bob-browser')
    remember_work_tab(alice, 7)
    remember_work_tab(bob, 9)
    assert current_work_tab(alice) == 7
    assert current_work_tab(bob) == 9


def test_work_tab_memory_is_lru_bounded(monkeypatch):
    import lib.browser._resolve as _resolve

    monkeypatch.setattr(_resolve, '_work_tab_capacity', lambda: 2)
    now = [1.0]
    monkeypatch.setattr(_resolve.time, 'monotonic', lambda: now[0])
    first = ('101', 'first-browser')
    second = ('101', 'second-browser')
    third = ('202', 'third-browser')

    _resolve.remember_work_tab(first, 1)
    now[0] = 2.0
    _resolve.remember_work_tab(second, 2)
    now[0] = 3.0
    assert _resolve.resolve_work_tab(
        {}, route_key=first, send=_fake_send({})) == 1
    now[0] = 4.0
    _resolve.remember_work_tab(third, 3)

    assert _resolve.current_work_tab(first) == 1
    assert _resolve.current_work_tab(second) is None
    assert _resolve.current_work_tab(third) == 3


def test_work_tab_memory_expires_and_reseeds(monkeypatch):
    import lib.browser._resolve as _resolve

    now = [100.0]
    monkeypatch.setattr(_resolve.time, 'monotonic', lambda: now[0])
    _resolve.remember_work_tab(_ROUTE_KEY, 7)
    now[0] += _resolve._WORK_TAB_TTL_S + 1
    calls = []

    def send(command, **_kwargs):
        calls.append(command)
        return [{'id': 9, 'active': True}], None

    assert _resolve.resolve_work_tab(
        {}, route_key=_ROUTE_KEY, send=send) == 9
    assert calls == ['list_tabs']


def test_actual_resolution_renews_work_tab_but_display_read_does_not(
        monkeypatch):
    import lib.browser._resolve as _resolve

    now = [100.0]
    monkeypatch.setattr(_resolve.time, 'monotonic', lambda: now[0])
    _resolve.remember_work_tab(_ROUTE_KEY, 7)
    now[0] += _resolve._WORK_TAB_TTL_S - 1
    assert _resolve.resolve_work_tab(
        {}, route_key=_ROUTE_KEY, send=_fake_send({})) == 7
    now[0] += _resolve._WORK_TAB_TTL_S - 1
    assert _resolve.current_work_tab(_ROUTE_KEY) == 7
    now[0] += 2
    assert _resolve.current_work_tab(_ROUTE_KEY) is None


def test_runtime_binds_owner_and_device_on_every_bridge_send():
    from lib.browser.tool_runtime import BrowserToolRuntime

    seen = []

    def send(command, params=None, timeout=30, **route):
        seen.append((command, params, timeout, route))
        return {'ok': True}, None

    runtime = BrowserToolRuntime(
        owner_user_id='101', client_id='alice-browser', sender=send)
    assert runtime.send('read_tab', {'tabId': 7}, timeout=11) == (
        {'ok': True}, None)
    assert seen == [(
        'read_tab', {'tabId': 7}, 11,
        {'client_id': 'alice-browser', 'owner_user_id': '101'},
    )]


def test_list_tabs_seeds_work_tab_only_from_owner_allowed_rows(monkeypatch):
    import lib.browser.access as access
    from lib.browser._resolve import current_work_tab
    from lib.browser.handlers import _handle_list_tabs

    monkeypatch.setattr(
        access,
        'is_read_allowed',
        lambda owner, url: owner == '101' and 'denied.example' not in url,
    )
    calls = []
    runtime = _handler_runtime({
        'list_tabs': ([
            {'id': 8, 'url': 'https://denied.example/', 'title': 'Secret',
             'active': True},
            {'id': 9, 'url': 'https://allowed.example/', 'title': 'Allowed',
             'active': False},
        ], None),
    }, calls)

    rendered = _handle_list_tabs({}, runtime)

    assert 'Secret' not in rendered
    assert 'Allowed' in rendered
    assert current_work_tab(_ROUTE_KEY) == 9
    assert [command for command, _params in calls] == ['list_tabs']


def test_list_tabs_marks_client_tab_and_never_seeds_it():
    from lib.browser._resolve import current_work_tab
    from lib.browser.handlers import _handle_list_tabs

    calls = []
    runtime = _handler_runtime({
        'list_tabs': ([
            {'id': 3, 'url': 'http://127.0.0.1:8080/', 'title': 'Tofu',
             'active': True, 'isClient': True},
            {'id': 9, 'url': 'https://allowed.example/', 'title': 'Allowed',
             'active': False},
        ], None),
    }, calls)

    rendered = _handle_list_tabs({}, runtime)

    assert 'never navigated' in rendered
    assert current_work_tab(_ROUTE_KEY) == 9


# ── 4. Action receipt ─────────────────────────────────────────────────

def test_receipt_reports_navigation():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://b', 'title': 'New'}], None)})
    line = action_receipt(
        1, ('Old', 'http://a', {1}), route_key=_ROUTE_KEY, send=send)
    assert 'navigated' in line and 'http://b' in line


def test_receipt_reports_unchanged():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://a', 'title': 'Same'}], None)})
    line = action_receipt(
        1, ('Same', 'http://a', {1}), route_key=_ROUTE_KEY, send=send)
    assert 'same page (URL unchanged)' in line


def test_receipt_never_raises_on_bridge_failure():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (None, 'bridge down')})
    assert action_receipt(
        1, (None, None, None), route_key=_ROUTE_KEY, send=send) == ''


# ── 5. Handlers end-to-end (fake bridge) ──────────────────────────────

def _handler_runtime(script, calls):
    return _runtime(_fake_send(script, calls))


def test_click_by_text_resolves_and_reports_receipt(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'get_interactive_elements': ({'elements': _ELEMENTS}, None),
        'click_element': ({'clicked': True, 'tag': 'button', 'text': 'Login'}, None),
        # v3: tab_snapshot reads list_tabs LIVE pre-action (a stale cache
        # must not produce phantom navigations), the receipt post-action.
        'list_tabs': [
            ([{'id': 1, 'url': 'http://before', 'title': 'T'}], None),
            ([{'id': 1, 'url': 'http://after', 'title': 'T'}], None),
        ],
    }, calls)
    from lib.browser.handlers import _handle_click
    out = _handle_click({'tabId': 1, 'text': 'login'}, runtime)
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
    runtime = _handler_runtime({
        'wait_for_element': ({'found': True}, None),
        'click_element': ({'clicked': True, 'tag': 'button'}, None),
        'list_tabs': ([{'id': 1, 'url': 'http://x', 'title': 'T'}], None),
    }, calls)
    from lib.browser.handlers import _handle_click
    out = _handle_click({'tabId': 1, 'selector': '#go'}, runtime)
    assert 'Clicked' in out
    assert [c for c, _ in calls][0] == 'wait_for_element'


def test_click_text_no_match_returns_candidates(monkeypatch):
    runtime = _handler_runtime({
        'get_interactive_elements': ({'elements': _ELEMENTS}, None),
    }, [])
    from lib.browser.handlers import _handle_click
    out = _handle_click(
        {'tabId': 1, 'text': 'nonexistent-zzz'}, runtime)
    assert 'No clear match' in out


def test_type_uses_type_text_clear_first(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'type_text': ({'typed': True}, None),
        'list_tabs': ([{'id': 1, 'url': 'http://x', 'title': 'T'}], None),
    }, calls)
    from lib.browser.handlers import _handle_type
    out = _handle_type(
        {'tabId': 1, 'selector': '#q', 'value': 'hello'}, runtime)
    assert 'Typed 5 chars' in out
    # v3: a snapshot list_tabs runs before the action — find the type call.
    tcalls = [p for c, p in calls if c == 'type_text']
    assert tcalls and tcalls[0]['clearFirst'] is True
    assert tcalls[0]['text'] == 'hello'


def test_press_key_sends_keyboard_input(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'keyboard_input': ({'success': True, 'target': 'body'}, None),
        'list_tabs': ([{'id': 1, 'url': 'http://x', 'title': 'T'}], None),
    }, calls)
    from lib.browser.handlers import _handle_press_key
    out = _handle_press_key({'tabId': 1, 'keys': 'Enter'}, runtime)
    assert 'Sent keys "Enter"' in out
    # v3: a snapshot list_tabs precedes the action.
    assert any(c == 'keyboard_input' for c, _ in calls)


def test_navigate_new_tab_uses_create_tab_and_remembers(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'create_tab': ({'id': 77, 'url': 'http://x', 'title': 'X'}, None),
    }, calls)
    from lib.browser.handlers import _handle_navigate
    out = _handle_navigate({'url': 'http://x', 'newTab': True}, runtime)
    assert 'Opened new tab #77' in out
    assert calls[0][1]['waitForLoad'] is True
    assert calls[0][1]['timeoutMs'] == 20_000
    from lib.browser._resolve import resolve_work_tab
    assert resolve_work_tab(
        {}, route_key=_ROUTE_KEY, send=_fake_send({})) == 77


def test_navigate_client_tab_redirect_remembers_the_new_tab():
    """The extension refuses to navigate the Tofu client tab and opens a new
    tab instead — the handler must re-bind the working tab to the new id so
    the next tab_id-less call lands on the new page, not back on the chat."""
    calls = []
    runtime = _handler_runtime({
        'navigate': ({'id': 88, 'url': 'https://dev.example/',
                      'status': 'complete', 'redirectedToNewTab': True,
                      'protectedTabId': 3}, None),
    }, calls)
    from lib.browser.handlers import _handle_navigate
    out = _handle_navigate({'tabId': 3, 'url': 'https://dev.example/'}, runtime)
    assert 'never navigated' in out
    assert '#88' in out
    from lib.browser._resolve import current_work_tab
    assert current_work_tab(_ROUTE_KEY) == 88


def test_navigate_waits_for_load_by_default(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'navigate': ({'id': 1, 'url': 'http://x', 'status': 'complete'}, None),
    }, calls)
    from lib.browser.handlers import _handle_navigate
    _handle_navigate({'tabId': 1, 'url': 'http://x'}, runtime)
    assert calls[0][1]['waitForLoad'] is True


# ── 6. read_page auto mode ────────────────────────────────────────────

def test_read_page_auto_substantive_skips_summary(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'read_tab': ({'title': 'T', 'url': 'http://x',
                      'text': 'lorem ipsum ' * 100, 'html': ''}, None),
    }, calls)
    from lib.browser.handlers import _handle_read_page
    out = _handle_read_page({'tabId': 1}, runtime)
    assert 'lorem ipsum' in out
    assert 'summarize_page' not in [c for c, _ in calls]


def test_read_page_auto_sparse_attaches_summary(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'read_tab': ({'title': 'T', 'url': 'http://x', 'text': 'tiny', 'html': ''}, None),
        'summarize_page': ({'title': 'T', 'url': 'http://x', 'framework': 'Vue',
                            'canvasCount': 2, 'svgCount': 0, 'domElementCount': 50}, None),
    }, calls)
    from lib.browser.handlers import _handle_read_page
    out = _handle_read_page({'tabId': 1}, runtime)
    assert 'sparse' in out
    assert 'Structural summary' in out
    assert 'summarize_page' in [c for c, _ in calls]


def test_read_page_modes_delegate(monkeypatch):
    calls = []
    runtime = _handler_runtime({
        'read_tab': ({'title': 'T', 'url': 'http://x', 'text': 'body ' * 200, 'html': ''}, None),
        'get_interactive_elements': ({'elements': [], 'title': 'T', 'url': 'http://x'}, None),
        'get_app_state': ({'framework': 'Vue'}, None),
    }, calls)
    from lib.browser.handlers import _handle_read_page
    assert 'body' in _handle_read_page(
        {'tabId': 1, 'mode': 'text'}, runtime)
    assert 'Interactive elements' in _handle_read_page(
        {'tabId': 1, 'mode': 'elements'}, runtime)
    assert 'App State' in _handle_read_page(
        {'tabId': 1, 'mode': 'app_state'}, runtime)
    assert 'unknown mode' in _handle_read_page(
        {'tabId': 1, 'mode': 'bogus'}, runtime)


# ── 7. Registry + approval declarations ───────────────────────────────

def test_registry_declares_the_v2_surface():
    from lib.tools.registry import all_specs
    spec = next(s for s in all_specs() if s.key == 'browser')
    assert set(spec.provides) == EXPECTED_V2
    for name in ('browser_type', 'browser_press_key', 'browser_menu_click'):
        assert name in spec.write_tools
    assert 'browser_read_page' not in spec.write_tools
    assert 'browser_research_page' in spec.write_tools
    assert 'browser_devtools' in spec.write_tools


def test_approval_enrichers_cover_new_write_tools():
    from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
    for name in ('browser_type', 'browser_press_key', 'browser_menu_click',
                 'browser_devtools'):
        assert name in _APPROVAL_META_ENRICHERS, (
            f'{name} is a write tool with no approval enricher — the user '
            f'would approve blind')
        # Enrichers run on model-supplied args — must not raise on {}.
        _APPROVAL_META_ENRICHERS[name]({}, {})


def test_live_browser_observers_are_not_result_cached():
    from lib.tasks_pkg.tool_dispatch._flags import _CACHEABLE_TOOLS
    assert 'browser_read_page' not in _CACHEABLE_TOOLS
    assert 'browser_list_tabs' not in _CACHEABLE_TOOLS
    assert 'browser_get_history' not in _CACHEABLE_TOOLS
    assert 'browser_get_cookies' not in _CACHEABLE_TOOLS


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

    out = adv.menu_click(
        1, 'export', target_selector='#file-menu', send=fake)
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

    out = adv.menu_click(1, 'Export', target_selector='#m', send=fake)
    assert out['success'] is False
    assert 'Close' in out['available_items']


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
