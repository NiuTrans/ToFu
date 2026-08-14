"""tests/test_browser_tab_outcome.py — v3 action-outcome perception.

Epic pt_4ef0583e3ad44278 (2026-08-05, incident conv msft42tqheea8x):
in the 钱管家 reimbursement flow the model clicked the 餐车补贴报销 card,
the click DID open the detail page — in a NEW TAB — and the bridge
reported nothing, so the model concluded "no navigation happened" and
burned ~10 more rounds on screenshots, JS DOM archaeology and a lucky
browser-history search. Two root causes, pinned here:

  1. The action receipt compared only the ACTED tab's title/URL. A
     target=_blank click never touches the old tab — the new page was
     invisible. Now tab_snapshot also captures the tab-ID set and
     action_receipt diffs it: a new tab is reported (with a settle retry
     so the receipt names the real URL, not about:blank) and becomes the
     working tab automatically — where a human's attention goes, the
     agent's next call goes.
  2. The extension's interactive-element enumeration only knew semantic
     interactives (a/button/input/role/...). React/Vue attach listeners
     at the root, so a clickable card is a plain <div> whose only tell
     is the computed cursor — the 钱管家 homepage enumerated ZERO
     elements and text= clicks could never resolve. Extension 4.8.0
     adds a bounded cursor:pointer sweep (outermost pointer roots only);
     the server ranks those just below real buttons.

The receipt is entirely server-side: every extension version gains
new-tab perception without an update.
"""

import json

import pytest

pytestmark = pytest.mark.unit


# ── helpers (same fake-bridge pattern as test_browser_v2_surface) ─────

def _fake_send(script, calls=None):
    """Fake send_browser_command driven by a {cmd: (result, error)} script.

    A callable entry is invoked with params; a list entry POPS responses in
    order (last one repeats when exhausted) — for pre/post list_tabs pairs.
    """
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


# ── 1. Snapshot captures the tab-id set ───────────────────────────────

def test_snapshot_captures_live_ids_and_url():
    from lib.browser._resolve import tab_snapshot
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://live', 'title': 'Live'},
         {'id': 2, 'url': 'http://other', 'title': 'O'}], None)})
    title, url, ids = tab_snapshot(1, send)
    assert ids == {1, 2}
    # Live data beats the (possibly stale) title cache.
    assert (title, url) == ('Live', 'http://live')


def test_snapshot_degrades_to_none_ids_on_bridge_failure():
    from lib.browser._resolve import tab_snapshot
    send = _fake_send({'list_tabs': (None, 'bridge down')})
    title, url, ids = tab_snapshot(99, send)
    assert ids is None


def test_snapshot_never_raises_when_send_raises():
    from lib.browser._resolve import tab_snapshot

    def boom(cmd, params=None, timeout=None):
        raise RuntimeError('queue exploded')
    title, url, ids = tab_snapshot(1, boom)
    assert ids is None


# ── 2. Receipt: new-tab detection ─────────────────────────────────────

def test_receipt_detects_new_tab_and_auto_follows():
    from lib.browser import _resolve
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://home', 'title': 'Home', 'active': False},
         {'id': 2, 'url': 'http://detail', 'title': 'Detail', 'active': True}],
        None)})
    before = ('Home', 'http://home', {1})
    line = _resolve.action_receipt(1, before, send)
    assert 'NEW TAB #2' in line
    assert 'http://detail' in line
    assert 'now the working tab' in line
    assert _resolve.current_work_tab() == 2


def test_receipt_new_tab_prefers_the_active_one():
    from lib.browser import _resolve
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://home', 'title': 'Home'},
         {'id': 5, 'url': 'http://bg', 'title': 'Bg', 'active': False},
         {'id': 3, 'url': 'http://fg', 'title': 'Fg', 'active': True}],
        None)})
    line = _resolve.action_receipt(1, ('Home', 'http://home', {1}), send)
    assert 'NEW TAB #3' in line
    assert '(+1 more new tabs)' in line
    assert _resolve.current_work_tab() == 3


def test_receipt_settles_blank_new_tab_url(monkeypatch):
    """A target=_blank tab is born as about:blank; one bounded settle retry
    must make the receipt name the REAL destination."""
    from lib.browser import _resolve
    slept = []
    monkeypatch.setattr(_resolve, '_settle', lambda s: slept.append(s))
    send = _fake_send({'list_tabs': [
        # first post-action list: the new tab has not loaded yet
        ([{'id': 1, 'url': 'http://home', 'title': 'Home'},
          {'id': 2, 'url': 'about:blank', 'title': '', 'active': True}], None),
        # after the settle: the real URL landed
        ([{'id': 1, 'url': 'http://home', 'title': 'Home'},
          {'id': 2, 'url': 'http://detail?expenseCategory=1',
           'title': '餐车补贴报销详情', 'active': True}], None),
    ]})
    line = _resolve.action_receipt(1, ('Home', 'http://home', {1}), send)
    assert slept == [1.2]
    assert 'http://detail?expenseCategory=1' in line
    assert 'about:blank' not in line


def test_receipt_new_tab_and_same_tab_navigation_both_reported():
    from lib.browser import _resolve
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://moved', 'title': 'Moved'},
         {'id': 2, 'url': 'http://detail', 'title': 'D', 'active': True}],
        None)})
    line = _resolve.action_receipt(1, ('Home', 'http://home', {1}), send)
    assert 'NEW TAB #2' in line
    assert 'page navigated' in line and 'http://moved' in line


def test_receipt_no_new_tab_keeps_same_page_line():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://a', 'title': 'Same'}], None)})
    line = action_receipt(1, ('Same', 'http://a', {1}), send)
    assert 'same page (URL unchanged)' in line
    assert 'NEW TAB' not in line


def test_receipt_acted_tab_gone_still_reports_new_tab():
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (
        [{'id': 2, 'url': 'http://x', 'title': 'X', 'active': True}], None)})
    line = action_receipt(1, ('Old', 'http://a', {1}), send)
    assert 'no longer exists' in line
    assert 'NEW TAB #2' in line


def test_receipt_legacy_2tuple_skips_id_diff():
    """Old callers passing (title, url) keep the pre-v3 behaviour exactly."""
    from lib.browser._resolve import action_receipt
    send = _fake_send({'list_tabs': (
        [{'id': 1, 'url': 'http://a', 'title': 'Same'},
         {'id': 2, 'url': 'http://sneaky', 'title': 'S', 'active': True}],
        None)})
    line = action_receipt(1, ('Same', 'http://a'), send)
    assert line == '\n→ same page (URL unchanged)'


# ── 3. Click handler end-to-end (fake bridge) ─────────────────────────

def _patch_facade_send(monkeypatch, script, calls):
    fake = _fake_send(script, calls)
    import lib.browser.handlers as pkg
    monkeypatch.setattr(pkg, 'send_browser_command', fake)
    return fake


def test_click_text_on_pointer_card_resolves_and_reports_new_tab(monkeypatch):
    """The 钱管家 incident, replayed the v3 way: ONE browser_click call.

    The extension-4.8 enumeration carries the cursor-pointer card; the
    resolver matches the text against it; the receipt spots the new tab
    and follows it."""
    calls = []
    card = {'tag': 'div', 'text': '餐车补贴报销 餐费 620.23 CNY 交通费 13 次',
            'selector': 'div.grid > div:nth-of-type(3)', 'pointer': True}
    _patch_facade_send(monkeypatch, {
        'get_interactive_elements': ({'elements': [
            {'tag': 'div', 'text': '出差申请 点击进行出差申请',
             'selector': 'div.grid > div:nth-of-type(1)', 'pointer': True},
            card,
        ]}, None),
        'list_tabs': [
            # snapshot (pre-click)
            ([{'id': 1, 'url': 'http://home', 'title': '钱管家'}], None),
            # receipt (post-click): the detail tab appeared and is focused
            ([{'id': 1, 'url': 'http://home', 'title': '钱管家'},
              {'id': 9, 'url': 'http://detail?expenseCategory=1',
               'title': '餐车补贴报销详情', 'active': True}], None),
        ],
        'click_element': ({'clicked': True, 'tag': 'div',
                           'text': '餐车补贴报销', 'trusted': True}, None),
    }, calls)
    from lib.browser.handlers import _handle_click
    out = _handle_click({'tabId': 1, 'text': '餐车补贴报销'})
    assert 'Clicked <div>' in out
    assert 'matched "餐车补贴报销"' in out
    assert 'NEW TAB #9' in out
    assert 'http://detail?expenseCategory=1' in out
    cmds = [c for c, _p in calls]
    assert cmds == ['get_interactive_elements', 'list_tabs',
                    'click_element', 'list_tabs']
    from lib.browser import _resolve
    assert _resolve.current_work_tab() == 9


# ── 4. Pointer boost in the resolver ──────────────────────────────────

def test_pointer_card_outranks_plain_div():
    from lib.browser._resolve import _score_element
    plain = _score_element({'tag': 'div', 'text': '餐车补贴报销'}, '餐车补贴报销', 'clickable')
    card = _score_element({'tag': 'div', 'text': '餐车补贴报销', 'pointer': True},
                          '餐车补贴报销', 'clickable')
    button = _score_element({'tag': 'button', 'text': '餐车补贴报销'},
                            '餐车补贴报销', 'clickable')
    assert card > plain > 0
    assert button > card  # semantic interactives still win


def test_resolver_unique_pointer_card_wins():
    from lib.browser._resolve import resolve_element
    elements = [
        {'tag': 'div', 'text': '出差申请 点击进行出差申请',
         'selector': 'div.a', 'pointer': True},
        {'tag': 'div', 'text': '餐车补贴报销 餐费 620.23 CNY 交通费 13 次',
         'selector': 'div.b', 'pointer': True},
    ]
    send = _fake_send({'get_interactive_elements': ({'elements': elements}, None)})
    el, note, _c = resolve_element(1, '餐车补贴报销', 'clickable', send=send)
    assert note is None
    assert el['selector'] == 'div.b'


# ── 5. Advanced flows ride the SAME receipt seam (dispatch layer) ─────
#
# menu_click / fill_form / hover_and_click / right_click_menu call
# click_element DIRECTLY — and a menu item or a form SUBMIT are the two
# highest-frequency new-tab openers. The v3 receipt therefore lives in
# dispatch._handle_advanced_tool: one snapshot/diff wrap covering all four
# flows (owner review follow-up, 2026-08-05).

def _patch_dispatch_and_flow(monkeypatch, flow_script, tabs_script):
    """Two patch points, two call journals.

    The FLOW commands (hover/click/type/elements) go through
    lib.browser.advanced's module-level send_browser_command; the receipt
    seam's list_tabs goes through lib.browser.dispatch's. Both are the
    documented monkeypatch contracts — patch each where it lives.
    """
    import lib.browser.advanced as adv
    import lib.browser.dispatch as disp
    calls = {'flow': [], 'tabs': []}
    monkeypatch.setattr(adv, 'send_browser_command',
                        _fake_send(flow_script, calls['flow']))
    monkeypatch.setattr(disp, 'send_browser_command',
                        _fake_send(tabs_script, calls['tabs']))
    return calls


def test_menu_click_reports_new_tab_and_follows(monkeypatch):
    calls = _patch_dispatch_and_flow(monkeypatch, {
        'hover_element': ({'hovered': True}, None),
        'get_interactive_elements': (
            {'elements': [{'text': '查看详情', 'selector': '#detail'}]}, None),
        'click_element': ({'clicked': True}, None),
    }, {
        'list_tabs': [
            ([{'id': 1, 'url': 'http://list', 'title': 'List'}], None),
            ([{'id': 1, 'url': 'http://list', 'title': 'List'},
              {'id': 7, 'url': 'http://detail', 'title': 'Detail',
               'active': True}], None),
        ],
    })
    from lib.browser.dispatch import _handle_advanced_tool
    out = _handle_advanced_tool('browser_menu_click', {
        'tabId': 1, 'target_selector': '#m', 'item_text': '查看详情',
        'menu_wait': 0})
    assert 'succeeded' in out
    assert 'NEW TAB #7' in out
    assert 'http://detail' in out
    from lib.browser import _resolve
    assert _resolve.current_work_tab() == 7
    # The seam's snapshot/diff both went through the dispatch send.
    assert [c for c, _ in calls['tabs']] == ['list_tabs', 'list_tabs']


def test_fill_form_submit_reports_new_tab(monkeypatch):
    """The form SUBMIT is the classic new-tab opener — it must not be a
    blind spot when the model fills via browser_fill_form."""
    _patch_dispatch_and_flow(monkeypatch, {
        'type_text': ({'typed': True}, None),
        'click_element': ({'clicked': True}, None),
    }, {
        'list_tabs': [
            ([{'id': 1, 'url': 'http://form', 'title': 'Form'}], None),
            ([{'id': 1, 'url': 'http://form', 'title': 'Form'},
              {'id': 9, 'url': 'http://done', 'title': 'Done',
               'active': True}], None),
        ],
    })
    from lib.browser.dispatch import _handle_advanced_tool
    out = _handle_advanced_tool('browser_fill_form', {
        'tabId': 1,
        'fields': [{'selector': '#q', 'value': 'x', 'type': 'type'}],
        'submit_selector': '#go', 'field_delay': 0})
    assert 'succeeded' in out
    assert 'NEW TAB #9' in out
    from lib.browser import _resolve
    assert _resolve.current_work_tab() == 9


def test_menu_click_same_page_receipt_has_no_new_tab(monkeypatch):
    _patch_dispatch_and_flow(monkeypatch, {
        'hover_element': ({'hovered': True}, None),
        'get_interactive_elements': (
            {'elements': [{'text': 'Export', 'selector': '#exp'}]}, None),
        'click_element': ({'clicked': True}, None),
    }, {
        'list_tabs': (
            [{'id': 1, 'url': 'http://a', 'title': 'A'}], None),
    })
    from lib.browser.dispatch import _handle_advanced_tool
    out = _handle_advanced_tool('browser_menu_click', {
        'tabId': 1, 'target_selector': '#m', 'item_text': 'Export',
        'menu_wait': 0})
    assert 'same page (URL unchanged)' in out
    assert 'NEW TAB' not in out


def test_menu_click_failure_still_appends_receipt(monkeypatch):
    """A flow can open a tab and STILL fail (submenu item missing) — the
    receipt must fire on the failure path too."""
    _patch_dispatch_and_flow(monkeypatch, {
        'hover_element': ({'hovered': True}, None),
        'get_interactive_elements': [
            ({'elements': [{'text': '导出', 'selector': '#exp'}]}, None),
            ({'elements': []}, None),
        ],
        'click_element': ({'clicked': True}, None),
    }, {
        'list_tabs': [
            ([{'id': 1, 'url': 'http://a', 'title': 'A'}], None),
            ([{'id': 1, 'url': 'http://a', 'title': 'A'},
              {'id': 2, 'url': 'http://opened', 'title': 'Opened',
               'active': True}], None),
        ],
    })
    from lib.browser.dispatch import _handle_advanced_tool
    out = _handle_advanced_tool('browser_menu_click', {
        'tabId': 1, 'target_selector': '#m', 'item_text': '导出',
        'submenu_text': 'CSV', 'menu_wait': 0})
    assert 'failed' in out          # submenu item never matched
    assert 'NEW TAB #2' in out      # …but the first click DID open a tab
    from lib.browser import _resolve
    assert _resolve.current_work_tab() == 2


def test_legacy_flow_without_tab_id_skips_receipt(monkeypatch):
    """Direct legacy callers that omit tabId: no snapshot, no receipt, no
    bridge call wasted — and no crash."""
    calls = _patch_dispatch_and_flow(monkeypatch, {
        'hover_element': ({'hovered': True}, None),
        'click_element': ({'clicked': True}, None),
    }, {})
    from lib.browser.dispatch import _handle_advanced_tool
    out = _handle_advanced_tool('browser_hover_and_click', {
        'hover_selector': '#h', 'click_selector': '#c', 'hover_wait': 0})
    assert 'succeeded' in out
    assert calls['tabs'] == []


# ── 5. Extension source pins (no JS harness in this repo — the
#       established convention for background.js) ──────────────────────

def _ext_src(rel='browser_extension/background.js'):
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, rel), encoding='utf-8') as f:
        return f.read()


def test_extension_pointer_sweep_shipped():
    src = _ext_src()
    assert 'cursor:pointer sweep' in src
    assert 'POINTER_SCAN_BUDGET' in src
    assert "{ pointer: true }" in src
    # Outermost-only rule — without it every card floods the list with its
    # own inherited-cursor children.
    assert 'cursor INHERITS' in src


def test_extension_version_bumped_for_protocol_v2():
    import json as _json
    manifest = _json.loads(_ext_src('browser_extension/manifest.json'))
    assert tuple(int(x) for x in manifest['version'].split('.')) >= (5, 0, 0)


def test_click_schema_documents_new_tab_follow():
    from lib.tools.browser import BROWSER_TOOL_CLICK
    desc = BROWSER_TOOL_CLICK['function']['description']
    assert 'NEW TAB' in desc
    # Schema diet guard: the surface must stay consolidated (13 tools).
    from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
    from lib.tools.browser import BROWSER_TOOLS
    size = len(json.dumps(BROWSER_TOOLS + ADVANCED_BROWSER_TOOLS,
                          ensure_ascii=False))
    assert size < 15000, f'browser schema surface grew to {size} chars'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
