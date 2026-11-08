"""The resident execution host reaches the browser bridge owned by the API host.

Regression: with the managed API/execution split, the extension polls the API
process while tools and context composition run in the execution process, whose
process-local registry is always empty — the model was told "no usable
extension is connected" while the panel showed connected clients, and browser
commands were queued where no extension ever polls.
"""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

pytestmark = pytest.mark.unit

OWNER = '7'
CLIENT = 'device-relay-1'

_OWNER_SCRIPT = textwrap.dedent('''
    import sys, threading, time
    from lib.browser import bridge_relay
    from lib.browser.protocol import ALL_CAPABILITIES
    from lib.browser.queue import _dispatch, _registry
    path = sys.argv[1]
    server = bridge_relay.RelayServer(bridge_relay.record_path(path))
    def extension():
        while True:
            _registry.mark_poll(%(client)r, owner_user_id=%(owner)r,
                                protocol_version=2, capabilities=sorted(ALL_CAPABILITIES))
            for cmd in _dispatch.get_pending_commands(client_id=%(client)r,
                                                      owner_user_id=%(owner)r):
                _dispatch.resolve_command(cmd['id'], client_id=%(client)r,
                                          owner_user_id=%(owner)r,
                                          result={'echo': cmd['params'],
                                                  'pid': __import__('os').getpid()})
            time.sleep(0.05)
    threading.Thread(target=extension, daemon=True).start()
    time.sleep(0.2)
    server.start()
    print('ready', flush=True)
    sys.stdin.read()
    server.close(2.0)
''') % {'client': CLIENT, 'owner': OWNER}


@pytest.fixture
def isolated_registry():
    from lib.browser.queue import _state
    with _state._clients_lock:
        _state._clients.clear()
    with _state._commands_lock:
        _state._commands.clear()
    yield _state
    from lib.browser.bridge_relay import stop_bridge_owner
    stop_bridge_owner()
    with _state._clients_lock:
        _state._clients.clear()


@pytest.fixture
def owner_process(tmp_path):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.getcwd() + os.pathsep + env.get('PYTHONPATH', '')
    proc = subprocess.Popen([sys.executable, '-c', _OWNER_SCRIPT, str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert proc.stdout.readline().strip() == 'ready'
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    assert not (tmp_path / 'data' / 'browser-bridge-relay.json').exists()


def test_execution_role_sees_and_drives_extension_owned_by_another_process(
        tmp_path, owner_process, isolated_registry):
    from lib.browser import bridge_relay
    from lib.browser.availability import browser_extension_availability
    from lib.browser.protocol import client_protocol
    from lib.browser.queue import (
        get_connected_clients, is_extension_connected, send_browser_command)

    # This process never saw a poll: locally the extension is absent.
    assert browser_extension_availability(int(OWNER))['available'] is False
    record = json.loads((tmp_path / 'data' / 'browser-bridge-relay.json').read_text())
    assert record['host'] == '127.0.0.1' and record['pid'] == owner_process.pid
    assert oct(os.stat(tmp_path / 'data' / 'browser-bridge-relay.json').st_mode & 0o777) == '0o600'

    assert bridge_relay.configure_bridge_owner(
        process_role='execution', environ={'TOFU_PROJECT_PATH': str(tmp_path)}) == 'relay'
    assert browser_extension_availability(int(OWNER))['available'] is True
    assert browser_extension_availability(8)['available'] is False  # owner-scoped
    assert [c['client_id'] for c in get_connected_clients(owner_user_id=OWNER)] == [CLIENT]
    assert is_extension_connected(CLIENT, owner_user_id=OWNER) is True
    assert client_protocol(CLIENT)['client_id'] == CLIENT

    result, error = send_browser_command(
        'probe', {'x': 1}, 5, client_id=CLIENT, owner_user_id=OWNER)
    assert error is None
    assert result == {'echo': {'x': 1}, 'pid': owner_process.pid}


def test_relay_rejects_wrong_token_and_reports_missing_owner(tmp_path, isolated_registry):
    from lib.browser import bridge_relay
    from lib.browser.availability import browser_extension_availability
    from lib.browser.queue import send_browser_command

    bridge_relay.configure_bridge_owner(
        process_role='execution', environ={'TOFU_PROJECT_PATH': str(tmp_path)})
    evidence = browser_extension_availability(int(OWNER))
    assert evidence['reason_code'] == 'browser_extension_status_unknown'
    result, error = send_browser_command('probe', {}, 1, client_id=CLIENT, owner_user_id=OWNER)
    assert result is None and 'API process' in error

    server = bridge_relay.RelayServer(bridge_relay.record_path(tmp_path / 'other'))
    server.start()
    try:
        record = json.loads(bridge_relay.record_path(tmp_path / 'other').read_text())
        record['token'] = 'forged'
        forged = tmp_path / 'forged' / 'data' / bridge_relay.RECORD_NAME
        forged.parent.mkdir(parents=True)
        forged.write_text(json.dumps(record))
        with pytest.raises(bridge_relay.RelayUnavailable, match='relay_refused'):
            bridge_relay.RelayClient(forged).call('connected_clients', {'owner_user_id': OWNER})
    finally:
        assert server.close(2.0)


def test_caller_cancellation_withdraws_the_owner_side_command(tmp_path, isolated_registry):
    from lib.browser import bridge_relay
    from lib.browser.queue import _registry, _state

    from lib.browser.protocol import ALL_CAPABILITIES
    _registry.mark_poll(CLIENT, owner_user_id=OWNER, protocol_version=2,
                        capabilities=sorted(ALL_CAPABILITIES))
    server = bridge_relay.RelayServer(bridge_relay.record_path(tmp_path))
    server.start()
    client = bridge_relay.RelayClient(bridge_relay.record_path(tmp_path))
    cancel = threading.Event()
    outcome = {}

    def call():
        outcome['value'] = client.call('send_command', {
            'cmd_type': 'never-answered', 'params': {}, 'timeout': 30,
            'client_id': CLIENT, 'owner_user_id': OWNER}, wait_seconds=30, cancel_event=cancel)

    caller = threading.Thread(target=call)
    caller.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with _state._commands_lock:
            if _state._commands:
                break
        time.sleep(0.01)
    else:
        pytest.fail('relayed command never reached the owner queue')
    cancel.set()
    caller.join(5)
    assert bridge_relay.is_cancelled(outcome['value'])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with _state._commands_lock:
            if not _state._commands:
                break
        time.sleep(0.01)
    with _state._commands_lock:
        assert not _state._commands
    assert server.close(5.0)


def test_role_selection_keeps_all_in_one_local(tmp_path, isolated_registry):
    from lib.browser import bridge_relay
    assert bridge_relay.configure_bridge_owner(
        process_role='all', environ={'TOFU_PROJECT_PATH': str(tmp_path)}) == 'local'
    assert bridge_relay.selected_relay() is None
    assert bridge_relay.configure_bridge_owner(
        process_role='api', environ={'TOFU_PROJECT_PATH': str(tmp_path)}) == 'owner'
    assert bridge_relay.record_path(tmp_path).exists()
    assert bridge_relay.stop_bridge_owner()
    assert not bridge_relay.record_path(tmp_path).exists()
    with pytest.raises(RuntimeError):
        bridge_relay.configure_bridge_owner(process_role='execution', environ={})
