"""A reconnect before enqueue recovers one call, with owner and Stop fences."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import threading
import time

import pytest

from lib.browser import dispatch
from lib.browser.queue import _registry, _state, mark_poll
from lib.browser.protocol import ALL_CAPABILITIES
from lib.browser.tool_runtime import BrowserToolRuntime
from lib.llm.llm_errors import AbortedError
from lib.tools.result_envelope import tool_result_error

pytestmark = pytest.mark.unit


@pytest.fixture
def connection(monkeypatch):
    monkeypatch.setattr(_state, '_clients', {})
    monkeypatch.setattr(_registry, '_clients', _state._clients)
    monkeypatch.setattr(dispatch, '_STICKY_CLIENTS', dispatch.OrderedDict())
    waiting = threading.Event()
    original = _registry.get_connected_clients
    def observe(**kwargs):
        result = original(**kwargs)
        if not result:
            waiting.set()
        return result
    monkeypatch.setattr(_registry, 'get_connected_clients', observe)
    sent = []
    def send(command, params, **authority):
        sent.append((command, authority['owner_user_id'], authority['client_id']))
        return [], None
    monkeypatch.setattr(dispatch, 'BrowserToolRuntime', partial(BrowserToolRuntime, sender=send))
    return waiting, sent


def register(client='selected', owner='771'):
    mark_poll(client, owner_user_id=owner, protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES))


def call(**kwargs):
    return dispatch.execute_browser_tool('browser_list_tabs', {},
        owner_user_id='771', client_id='selected', connection_wait=1, **kwargs)


def test_matching_poll_recovers_original_call_once(connection):
    waiting, sent = connection
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call)
        assert waiting.wait(1)
        register('selected', '772')
        register('other', '771')
        assert sent == []
        register()
        result = future.result(timeout=1)
    assert 'Open tabs (0 total)' in result
    assert sent == [('list_tabs', '771', 'selected')]


def test_stop_wakes_wait_without_issuing_command(connection):
    waiting, sent = connection
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call, cancellation=cancel)
        assert waiting.wait(1)
        cancel.set()
        with pytest.raises(AbortedError):
            future.result(timeout=1)
    register()
    assert sent == []


def test_connection_timeout_does_not_leave_work_for_late_poll(connection):
    _, sent = connection
    start = time.monotonic()
    result = dispatch.execute_browser_tool('browser_list_tabs', {},
        owner_user_id='771', client_id='selected', connection_wait=0.02)
    assert tool_result_error(result).code == 'browser_client_disconnected'
    assert time.monotonic() - start < 1
    register()
    assert sent == []


def test_root_handler_uses_same_reconnect_boundary(connection, monkeypatch):
    from lib.tasks_pkg.handlers import browser
    monkeypatch.setattr(browser, '_finalize_tool_round', lambda *a, **kw: None)
    monkeypatch.setattr(browser, 'BROWSER_CONNECTION_WAIT_SECONDS', 1)
    waiting, sent = connection
    task = {'id': 'root', '_userId': 771, 'abort_event': threading.Event()}
    def invoke():
        return browser._handle_browser_tool(task, {}, 'browser_list_tabs', 'tabs', {},
            1, {}, {'browserClientId': 'selected'}, '', False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke)
        assert waiting.wait(1)
        register()
        _, output, _ = future.result(timeout=1)
    assert 'Open tabs (0 total)' in output
    assert sent == [('list_tabs', '771', 'selected')]
