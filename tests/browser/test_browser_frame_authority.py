"""An iframe action belongs to its actual document, owner and device."""

from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit

TOP = 'https://app.test/main'
CHILD = 'https://child.test/article'


@pytest.fixture
def browser(monkeypatch):
    from lib.browser import page as pages
    from lib.browser.sessions import BrowserSessionLease

    checks, calls = [], []
    state = {'tabId': 7, 'url': TOP, 'frame': {
        'frameId': 3, 'url': CHILD, 'documentId': 'document-child',
    }}
    denied = set()
    def require(owner, url, *, access, client_id, profile):
        from lib.browser.access import BrowserAccessDenied
        checks.append((owner, url, access, client_id, profile))
        if url in denied:
            raise BrowserAccessDenied('Denied frame')
    monkeypatch.setattr(pages, 'require_access', require)
    monkeypatch.setattr(pages, 'require_capabilities', lambda *a, **k: None)
    def sender(command, params, **route):
        assert route['client_id'] == 'frame-device'
        assert route['owner_user_id'] == '41'
        calls.append((command, deepcopy(params)))
        if command == 'page_state':
            return deepcopy(state), None
        return {'text': 'child content', 'url': CHILD}, None
    page = pages.BrowserPage(BrowserSessionLease(
        'frames', '41', 'frame-device', profile='Work', tab_id=7), sender=sender)
    return page, state, denied, checks, calls


def test_denied_child_never_inherits_parent_read_permission(browser):
    from lib.browser.access import BrowserAccessDenied

    page, state, denied, checks, calls = browser
    denied.add(CHILD)
    with pytest.raises(BrowserAccessDenied):
        page.read(frame_id=3)
    assert all(command == 'page_state' for command, _ in calls)


def test_frame_write_authorizes_the_actual_target_and_only_reads_parent(browser):
    page, state, denied, checks, calls = browser
    page.click(selector='#submit', frame_id=3)
    assert ('41', CHILD, 'write', 'frame-device', 'Work') in checks
    assert ('41', TOP, 'read', 'frame-device', 'Work') in checks
    assert not any(url == TOP and access == 'write' for _, url, access, _, _ in checks)
    sent = next(params for command, params in calls if command == 'page_click')
    assert sent['expectedDocumentId'] == 'document-child'
    assert sent['frameId'] == 3


@pytest.mark.parametrize('method,kwargs', [
    ('read', {}), ('snapshot', {}), ('click', {'selector': '#submit'}),
    ('fill', {'value': 'text', 'selector': '#field'}),
    ('press', {'keys': 'Enter'}), ('select', {'value': 'one', 'selector': '#choice'}),
    ('scroll', {}), ('wait', {'selector': '#ready'}), ('execute', {'expression': '1 + 1'}),
])
def test_every_frame_primitive_pins_the_authorized_document(browser, method, kwargs):
    page, state, denied, checks, calls = browser
    getattr(page, method)(frame_id=3, **kwargs)
    assert calls[0] == ('page_state', {'tabId': 7, 'frameId': 3})
    action = next(params for command, params in calls if command != 'page_state')
    assert action['expectedDocumentId'] == 'document-child'


@pytest.mark.parametrize('frame', [None, {}, {'frameId': 3, 'url': CHILD},
    {'frameId': 3, 'url': '', 'documentId': 'child'},
    {'frameId': 4, 'url': CHILD, 'documentId': 'other'},
])
def test_unverifiable_frame_receipt_fails_before_action(browser, frame):
    from lib.browser.page import BrowserCommandError

    page, state, denied, checks, calls = browser
    state['frame'] = frame
    with pytest.raises(BrowserCommandError):
        page.read(frame_id=3)
    assert all(command == 'page_state' for command, _ in calls)


def test_declared_site_scope_applies_to_child_document(browser):
    from lib.browser.adapter_errors import AdapterExecutionError
    from lib.browser.site_scope import SiteScope

    page, state, denied, checks, calls = browser
    with page.site_scope(SiteScope(('app.test',), site='Fixture')):
        with pytest.raises(AdapterExecutionError):
            page.read(frame_id=3)
    assert all(command == 'page_state' for command, _ in calls)


@pytest.mark.parametrize('site_scope', [False, True])
def test_child_redirect_is_authorized_before_returning_content(browser, site_scope):
    from lib.browser.access import BrowserAccessDenied
    from lib.browser.adapter_errors import AdapterExecutionError
    from lib.browser.site_scope import SiteScope

    page, state, denied, checks, calls = browser
    original = page._raw_send
    def sender(command, params, **route):
        result = original(command, params, **route)
        if command == 'read_tab':
            state['frame']['url'] = 'https://denied.test/private'
            state['frame']['documentId'] = 'new-child'
        return result
    page._raw_send = sender
    denied.add('https://denied.test/private')
    if site_scope:
        with page.site_scope(SiteScope(('app.test', 'child.test'), site='Fixture', login_url=TOP)):
            with pytest.raises(AdapterExecutionError) as failure:
                page.read(frame_id=3)
        assert failure.value.code == 'login_required'
    else:
        with pytest.raises(BrowserAccessDenied):
            page.read(frame_id=3)


def test_old_extension_is_refused_before_frame_dispatch(monkeypatch):
    from lib.browser import protocol
    from lib.browser.page import BrowserPage
    from lib.browser.queue import _state, mark_poll
    from lib.browser.sessions import BrowserSessionLease

    monkeypatch.setitem(_state._clients, 'old-frame-device', {})
    mark_poll('old-frame-device', owner_user_id='41', protocol_version=2,
              capabilities=sorted(protocol.ALL_CAPABILITIES - {'frame_authority'}))
    def sender(*a, **k):
        pytest.fail('An unguarded extension must not receive a frame command')
    page = BrowserPage(BrowserSessionLease('old', '41', 'old-frame-device', tab_id=7), sender=sender)
    with pytest.raises(protocol.BrowserUpgradeRequired) as failure:
        page.read(frame_id=3)
    assert failure.value.missing == ('frame_authority',)
