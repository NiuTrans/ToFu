"""Device-routing stability + dead-tab self-heal for the browser bridge.

Two Chrome instances of one owner poll every 1-2s. Before this module's
coverage, an un-pinned call picked the most-recent poller EVERY time, so
consecutive tool calls hopped between machines and tab ids learned on one
instance were meaningless on the other ("element discovery failed: Tab N
not found"). These tests pin:

* dispatch sticky routing — an un-pinned owner keeps ONE device while it
  stays connected, and re-picks only when that device drops;
* click/type self-heal — a remembered working tab that died between calls
  is forgotten, re-seeded from the live browser, and the action retried
  once; an explicit tabId is never silently rerouted.
"""

import pytest

pytestmark = pytest.mark.unit

OWNER = '101'
CLIENT_A = 'machine-a'
CLIENT_B = 'machine-b'


@pytest.fixture(autouse=True)
def _clean_state():
    from lib.browser import dispatch
    from lib.browser.queue import _state
    from lib.browser._resolve import clear_work_tab_cache

    def _clear():
        dispatch._clear_sticky_clients()
        clear_work_tab_cache()
        with _state._commands_lock:
            _state._commands.clear()
        with _state._clients_lock:
            _state._clients.clear()

    _clear()
    yield
    _clear()


def _register(client_id, owner_user_id=OWNER):
    from lib.browser.protocol import ALL_CAPABILITIES
    from lib.browser.queue import mark_poll
    mark_poll(
        client_id,
        owner_user_id=owner_user_id,
        protocol_version=2,
        capabilities=sorted(ALL_CAPABILITIES),
    )


# ── 1. Sticky device pick ─────────────────────────────────────────────


def test_sticky_pick_keeps_device_while_connected():
    from lib.browser import dispatch

    owned_poll1 = [
        {'client_id': CLIENT_A, 'last_poll': 100.0},
        {'client_id': CLIENT_B, 'last_poll': 200.0},
    ]
    assert dispatch._pick_connected_client(OWNER, owned_poll1) == CLIENT_B
    # A polls again and becomes the freshest — the route must NOT hop.
    owned_poll2 = [
        {'client_id': CLIENT_A, 'last_poll': 300.0},
        {'client_id': CLIENT_B, 'last_poll': 200.0},
    ]
    assert dispatch._pick_connected_client(OWNER, owned_poll2) == CLIENT_B


def test_sticky_pick_repicks_only_after_drop():
    from lib.browser import dispatch

    owned = [
        {'client_id': CLIENT_A, 'last_poll': 100.0},
        {'client_id': CLIENT_B, 'last_poll': 200.0},
    ]
    assert dispatch._pick_connected_client(OWNER, owned) == CLIENT_B
    # B disconnected; only A remains.
    assert dispatch._pick_connected_client(
        OWNER, [{'client_id': CLIENT_A, 'last_poll': 300.0}]) == CLIENT_A
    # ...and the route sticks to A afterwards.
    owned_again = [
        {'client_id': CLIENT_A, 'last_poll': 300.0},
        {'client_id': CLIENT_B, 'last_poll': 400.0},
    ]
    assert dispatch._pick_connected_client(OWNER, owned_again) == CLIENT_A


def test_dispatch_without_pin_routes_consecutive_calls_to_one_client(
        monkeypatch):
    import lib.browser.access as access
    import lib.browser.dispatch as dispatch
    from lib.browser.tool_runtime import BrowserToolRuntime

    _register(CLIENT_A)
    _register(CLIENT_B)
    used = []

    def runtime_factory(*, owner_user_id, client_id):
        used.append(client_id)
        return BrowserToolRuntime(
            owner_user_id=owner_user_id, client_id=client_id,
            sender=lambda *a, **k: ({'tabs': []}, None))

    monkeypatch.setattr(dispatch, 'BrowserToolRuntime', runtime_factory)
    monkeypatch.setattr(
        access, 'browser_tool_access', lambda *a, **k: None)
    monkeypatch.setitem(
        dispatch.BROWSER_HANDLERS, 'browser_list_tabs',
        lambda fn_args, runtime: 'ok')

    assert dispatch.execute_browser_tool(
        'browser_list_tabs', {}, owner_user_id=OWNER) == 'ok'
    # The OTHER machine polls again, becoming the freshest — the old rule
    # would route the next call there.
    _register(CLIENT_A)
    _register(CLIENT_B)
    _register(CLIENT_A)
    assert dispatch.execute_browser_tool(
        'browser_list_tabs', {}, owner_user_id=OWNER) == 'ok'
    assert used[0] == used[1], (
        f'un-pinned calls drifted across devices: {used}')


def test_dispatch_explicit_pin_unaffected_by_sticky(monkeypatch):
    import lib.browser.access as access
    import lib.browser.dispatch as dispatch
    from lib.browser.tool_runtime import BrowserToolRuntime

    _register(CLIENT_A)
    _register(CLIENT_B)
    used = []

    def runtime_factory(*, owner_user_id, client_id):
        used.append(client_id)
        return BrowserToolRuntime(
            owner_user_id=owner_user_id, client_id=client_id,
            sender=lambda *a, **k: ({'tabs': []}, None))

    monkeypatch.setattr(dispatch, 'BrowserToolRuntime', runtime_factory)
    monkeypatch.setattr(
        access, 'browser_tool_access', lambda *a, **k: None)
    monkeypatch.setitem(
        dispatch.BROWSER_HANDLERS, 'browser_list_tabs',
        lambda fn_args, runtime: 'ok')

    dispatch.execute_browser_tool('browser_list_tabs', {}, owner_user_id=OWNER)
    assert dispatch.execute_browser_tool(
        'browser_list_tabs', {}, client_id=CLIENT_A,
        owner_user_id=OWNER) == 'ok'
    assert used[-1] == CLIENT_A


# ── 2. Dead-tab self-heal in click/type ───────────────────────────────


_TAB_GONE = 'Tab 99 not found: No tab with id: 99.'
_ELEMENTS = [
    {'tag': 'button', 'text': 'Save', 'selector': '#save', 'role': 'button'},
    {'tag': 'input', 'placeholder': 'Search', 'selector': '#q',
     'type': 'text'},
]


def _fake_send(script, calls):
    def fake(cmd, params=None, timeout=None, **_route):
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


def _runtime(send):
    from lib.browser.tool_runtime import BrowserToolRuntime
    return BrowserToolRuntime(
        owner_user_id=OWNER, client_id=CLIENT_A, sender=send)


def _seed_dead_work_tab():
    from lib.browser._resolve import remember_work_tab
    remember_work_tab((OWNER, CLIENT_A), 99)


def test_click_retargets_when_remembered_tab_died():
    calls = []
    runtime = _runtime(_fake_send({
        'get_interactive_elements': [
            (None, _TAB_GONE),
            ({'elements': _ELEMENTS}, None),
        ],
        'list_tabs': [
            ([{'id': 1, 'url': 'http://a', 'title': 'A', 'active': True}],
             None),
        ],
        'click_element': (
            {'clicked': True, 'tag': 'button', 'text': 'Save'}, None),
    }, calls))
    _seed_dead_work_tab()

    from lib.browser.handlers import _handle_click
    out = _handle_click({'text': 'save'}, runtime)

    assert 'Clicked <button>' in out, out
    clicks = [c for c, p in calls if c == 'click_element']
    assert clicks and clicks[0] is not None
    click_params = [p for c, p in calls if c == 'click_element'][0]
    assert click_params['tabId'] == 1, (
        f'click must land on the re-seeded live tab, got {click_params}')


def test_click_explicit_tab_id_is_never_rerouted():
    calls = []
    runtime = _runtime(_fake_send({
        'get_interactive_elements': (None, _TAB_GONE),
    }, calls))

    from lib.browser.handlers import _handle_click
    out = _handle_click({'tabId': 99, 'text': 'save'}, runtime)

    assert 'No clear match' in out
    assert [c for c, _ in calls] == ['get_interactive_elements'], (
        f'explicit tabId must not trigger re-seeding, calls={calls}')


def test_click_non_tab_gone_note_does_not_retarget():
    calls = []
    runtime = _runtime(_fake_send({
        'get_interactive_elements': ({'elements': []}, None),
    }, calls))
    _seed_dead_work_tab()

    from lib.browser.handlers import _handle_click
    out = _handle_click({'text': 'save'}, runtime)

    assert 'No clear match' in out
    assert 'list_tabs' not in [c for c, _ in calls], (
        'an honest empty enumeration must not be mistaken for a dead tab')


def test_type_retargets_when_remembered_tab_died():
    calls = []
    runtime = _runtime(_fake_send({
        'get_interactive_elements': [
            (None, _TAB_GONE),
            ({'elements': _ELEMENTS}, None),
        ],
        'list_tabs': [
            ([{'id': 1, 'url': 'http://a', 'title': 'A', 'active': True}],
             None),
        ],
        'type_text': ({'typed': True}, None),
    }, calls))
    _seed_dead_work_tab()

    from lib.browser.handlers import _handle_type
    out = _handle_type({'text': 'search', 'value': 'quota'}, runtime)

    assert 'Typed' in out, out
    type_params = [p for c, p in calls if c == 'type_text'][0]
    assert type_params['tabId'] == 1
