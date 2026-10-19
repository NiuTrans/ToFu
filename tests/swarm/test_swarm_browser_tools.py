"""Real child invocation preserves browser identity and gateway error evidence."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Event
import json

import pytest

from lib.browser.tool_runtime import BrowserToolRuntime
from lib.swarm.agent import SubAgent
from lib.swarm.protocol import SubTaskSpec
from lib.tools.browser import BROWSER_TOOLS
from tests.goals import test_goal_invocation_integration as goal_fixture
from tests.swarm.test_swarm_invocation import reply, scripted_provider, tool_call

pytestmark = pytest.mark.unit
runtime = goal_fixture.runtime


@pytest.mark.parametrize('connected', [True, False, 'reconnect'])
@pytest.mark.parametrize('gateway', [True, False])
def test_child_browser_owner_device_and_failure_settlement(
        runtime, monkeypatch, connected, gateway):
    from lib.browser import dispatch
    from lib.browser.queue import _registry, _state, mark_poll
    from lib.browser.protocol import ALL_CAPABILITIES
    monkeypatch.setattr('lib.tasks_pkg.handlers.browser.BROWSER_CONNECTION_WAIT_SECONDS',
                        2 if connected == 'reconnect' else 0)
    monkeypatch.setattr(_state, '_clients', {})
    monkeypatch.setattr(_registry, '_clients', _state._clients)
    monkeypatch.setattr(dispatch, '_STICKY_CLIENTS', dispatch.OrderedDict())
    runtime.parent['config']['browserClientId'] = 'parent-device'
    # A different owner's live device never repairs this child's connection.
    mark_poll('foreign-device', owner_user_id='772', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES))
    if connected is True:
        mark_poll('parent-device', owner_user_id='771', protocol_version=2,
                  capabilities=sorted(ALL_CAPABILITIES))
    sent = []
    def send(command, params, **authority):
        sent.append((command, authority['owner_user_id'], authority['client_id']))
        return [], None
    monkeypatch.setattr(dispatch, 'BrowserToolRuntime',
                        partial(BrowserToolRuntime, sender=send))
    browser = next(tool for tool in BROWSER_TOOLS
                   if tool['function']['name'] == 'browser_list_tabs')
    calls = [tool_call('browser-call', 'browser_list_tabs')]
    if gateway:
        calls = [tool_call('batch-call', 'execute_tools', json.dumps({
            'calls': [{'name': 'browser_list_tabs', 'arguments': {}}]}))]
    final = ('Browser inspection finished; the connected owner has zero tabs.' if connected
             else 'Browser inspection is blocked by a disconnected device; reconnect before retrying.')
    scripted_provider(monkeypatch, runtime, [reply('', calls), reply(final), reply(final)])
    agent = SubAgent(SubTaskSpec(role='general', objective='Inspect browser tabs'),
                     parent_task=runtime.parent, all_tools=[browser], model='fixture-model')
    if connected == 'reconnect':
        import lib.browser.queue as queue
        waiting = Event()
        original = queue.wait_for_connected_clients
        def observe(**kwargs):
            waiting.set()
            return original(**kwargs)
        monkeypatch.setattr(queue, 'wait_for_connected_clients', observe)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(agent.run)
            assert waiting.wait(2)
            mark_poll('parent-device', owner_user_id='771', protocol_version=2,
                      capabilities=sorted(ALL_CAPABILITIES))
            result = future.result(timeout=3)
    else:
        result = agent.run()
    assert result.status == 'completed', result.error_message
    child = runtime.children[0]
    assert child['_userId'] == 771
    assert child['config']['browserClientId'] == 'parent-device'
    rows = [row for row in child['toolRounds'] if row.get('toolName') == 'browser_list_tabs']
    assert rows
    assert rows[0]['status'] == ('done' if connected else 'error')
    assert sent == ([('list_tabs', '771', 'parent-device')] if connected else [])
    outputs = '\n'.join(str(message.get('content', ''))
                        for message in runtime.calls[1]['messages']
                        if message.get('role') == 'tool')
    assert ('Open tabs (0 total)' if connected else 'browser_client_disconnected') in outputs
    if gateway and not connected:
        assert 'partial_failure' in outputs


def test_child_stop_interrupts_browser_reconnect_wait(runtime, monkeypatch):
    from lib.browser import dispatch
    from lib.browser.queue import _registry, _state, mark_poll
    from lib.browser.protocol import ALL_CAPABILITIES
    import lib.browser.queue as queue
    monkeypatch.setattr('lib.tasks_pkg.handlers.browser.BROWSER_CONNECTION_WAIT_SECONDS', 5)
    monkeypatch.setattr(_state, '_clients', {})
    monkeypatch.setattr(_registry, '_clients', _state._clients)
    waiting = Event()
    original = queue.wait_for_connected_clients
    def observe(**kwargs):
        waiting.set()
        return original(**kwargs)
    monkeypatch.setattr(queue, 'wait_for_connected_clients', observe)
    sent = []
    def send(*args, **kwargs):
        sent.append(args)
        return [], None
    monkeypatch.setattr(dispatch, 'BrowserToolRuntime', partial(BrowserToolRuntime, sender=send))
    browser = next(tool for tool in BROWSER_TOOLS
                   if tool['function']['name'] == 'browser_list_tabs')
    scripted_provider(monkeypatch, runtime, [reply('', [tool_call('tabs', 'browser_list_tabs')])])
    agent = SubAgent(SubTaskSpec(role='general', objective='Inspect browser'),
        parent_task=runtime.parent, all_tools=[browser], model='fixture-model',
        abort_check=runtime.stop.is_set)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(agent.run)
        assert waiting.wait(2)
        runtime.stop.set()
        result = future.result(timeout=2)
    assert result.status == 'cancelled'
    assert len(runtime.calls) == 1
    mark_poll('late-device', owner_user_id='771', protocol_version=2,
              capabilities=sorted(ALL_CAPABILITIES))
    assert sent == []
