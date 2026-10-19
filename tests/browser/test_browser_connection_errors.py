"""Browser connection failures settle as errors and recover on a fresh poll."""
from functools import partial

import pytest

from lib.browser import dispatch
from lib.browser.protocol import ALL_CAPABILITIES
from lib.browser.queue import mark_poll
from lib.browser.tool_runtime import BrowserToolRuntime
from lib.tools.result_envelope import tool_result_error

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_browser(monkeypatch):
    from lib.browser.queue import _state
    monkeypatch.setattr(_state, '_clients', {})
    monkeypatch.setattr('lib.browser.queue._registry._clients', _state._clients)
    monkeypatch.setattr(dispatch, '_STICKY_CLIENTS', dispatch.OrderedDict())


def register(client, owner='771'):
    mark_poll(client, owner_user_id=owner, protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES))


@pytest.mark.parametrize(('owner', 'client', 'code'), [
    ('', None, 'browser_owner_required'),
    ('771', None, 'browser_extension_disconnected'),
    ('771', 'missing-device', 'browser_client_disconnected'),
    ('771', 'foreign-device', 'browser_client_disconnected'),
])
def test_connection_refusals_are_typed_and_never_borrow_another_owner(
        monkeypatch, owner, client, code):
    register('foreign-device', owner='772')
    def unexpected_runtime(**kwargs):
        pytest.fail('A refused browser request must not construct a runtime')
    monkeypatch.setattr(dispatch, 'BrowserToolRuntime', unexpected_runtime)
    result = dispatch.execute_browser_tool(
        'browser_list_tabs', {}, owner_user_id=owner, client_id=client)
    error = tool_result_error(result)
    assert error is not None
    assert error.code == code
    assert error.next_action
    assert error.retryable is bool(owner)


def test_pinned_connection_recovers_only_after_its_own_poll(monkeypatch):
    sent = []
    def send(command, params, **authority):
        sent.append((command, authority['owner_user_id'], authority['client_id']))
        return [], None
    monkeypatch.setattr(dispatch, 'BrowserToolRuntime',
                        partial(BrowserToolRuntime, sender=send))
    register('other-device')
    result = dispatch.execute_browser_tool(
        'browser_list_tabs', {}, owner_user_id='771', client_id='selected-device')
    assert tool_result_error(result).code == 'browser_client_disconnected'
    assert sent == []
    register('selected-device')
    result = dispatch.execute_browser_tool(
        'browser_list_tabs', {}, owner_user_id='771', client_id='selected-device')
    assert tool_result_error(result) is None
    assert 'Open tabs (0 total)' in result
    assert sent == [('list_tabs', '771', 'selected-device')]


def test_list_tabs_transport_failure_is_typed_without_automatic_replay(monkeypatch):
    sent = []
    def send(command, params, **authority):
        sent.append(command)
        return None, 'Browser command timed out'
    monkeypatch.setattr(dispatch, 'BrowserToolRuntime',
                        partial(BrowserToolRuntime, sender=send))
    register('selected-device')
    result = dispatch.execute_browser_tool(
        'browser_list_tabs', {}, owner_user_id='771', client_id='selected-device')
    error = tool_result_error(result)
    assert error is not None
    assert error.code == 'browser_list_tabs_failed'
    assert error.retryable is True
    assert sent == ['list_tabs']
