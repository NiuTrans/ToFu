"""Optional work may fail; execution authority and task lifetime may not."""

import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('group', [False, True])
@pytest.mark.parametrize('stop', ['cancel', 'deadline', 'released', 'expired',
                                 'denied', 'upgrade', 'optional_absent',
                                 'cancel_wait', 'deadline_wait'])
def test_optional_pipeline_keeps_terminal_failures(monkeypatch, tmp_path, group, stop):
    from lib.browser import access, page as page_module
    from lib.browser.page import BrowserCommandError, BrowserOperationStopped, BrowserPage
    from lib.browser.protocol import BrowserUpgradeRequired
    from lib.browser.sessions import BrowserSessionLease
    from lib.site_packs._interpreter import operation_handler
    from lib.site_packs._manifest import parse_manifest

    monkeypatch.setattr(access, '_STORE_PATH', str(tmp_path / 'access.json'))
    monkeypatch.setattr(access, 'audit_log', lambda *a, **kw: None)
    if stop == 'denied':
        access.replace_read_denials('41', ['example.com'])
    def capabilities(*args):
        if stop == 'upgrade':
            raise BrowserUpgradeRequired(['wait'])
    monkeypatch.setattr(page_module, 'require_capabilities', capabilities)
    clock = [0.0]
    monkeypatch.setattr(page_module, 'time', SimpleNamespace(
        time=page_module.time.time, monotonic=lambda: clock[0]))
    step = {'do': 'wait', 'selector': '#optional', 'optional': not group}
    if group:
        step = {'do': 'try', 'steps': [step]}
    pack = parse_manifest({
        'pack': 'tofu.site-pack/v1', 'id': 'optional', 'name': 'Optional',
        'domains': ['example.com'], 'operations': [{
            'name': 'inspect', 'description': 'Inspect optional content', 'steps': [step],
        }],
    }, source='optional.json')
    lease = BrowserSessionLease('optional', '41', 'device', tab_id=7)
    cancelled = threading.Event()
    if stop == 'cancel':
        cancelled.set()
    if stop == 'released':
        lease.released_at = 1
    if stop == 'expired':
        lease.expires_at = 1
    calls = []
    def sender(command, params, **kwargs):
        calls.append(command)
        if command == 'page_state':
            return {'url': 'https://example.com', 'tabId': 7}, None
        if command == 'close_tab':
            return {'closed': [7]}, None
        if stop == 'cancel_wait':
            cancelled.set()
            return None, 'Browser command cancelled'
        if stop == 'deadline_wait':
            clock[0] = 2.0
            return None, 'Browser command timed out'
        return None, 'optional element absent'
    page = BrowserPage(lease, sender=sender, cancel_event=cancelled,
                       deadline={'deadline': 0, 'deadline_wait': 1}.get(stop))
    expected = {'denied': access.BrowserAccessDenied, 'upgrade': BrowserUpgradeRequired}
    try:
        if stop == 'optional_absent':
            assert operation_handler(pack, pack.operations[0])(page, {}) is None
            assert 'wait_for_element' in calls
        else:
            with pytest.raises(expected.get(stop, BrowserOperationStopped)) as failure:
                operation_handler(pack, pack.operations[0])(page, {})
            if stop in ('cancel', 'deadline', 'released', 'expired'):
                assert isinstance(failure.value, BrowserCommandError)
            assert ('wait_for_element' in calls) == stop.endswith('_wait')
    finally:
        page.close()
