"""Both products share transport hooks without sharing server/UI dependencies."""
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(monkeypatch):
    import lib.desktop_agent as agent
    import lib.desktop_agent.config as config
    import lib.desktop_agent._pair as pair
    from desktop import agent_connection as connection

    calls = []
    ready = threading.Event()

    def run(url, perms, **kwargs):
        calls.append((url, perms, kwargs))
        ready.set()
        kwargs['stop_event'].wait(3)

    monkeypatch.setattr(agent, 'run_agent', run)
    monkeypatch.setattr(config, 'remote_server', lambda: ('https://example.test', 'token'))
    monkeypatch.setattr(pair, 'resume_attachment', lambda url, secret, **kw: (url, secret))
    monkeypatch.setattr(connection, 'ensure_relay',
                        lambda state, log: state.setdefault('browser_relay', Relay()))
    return connection, calls, ready


class Relay:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize('role', ['full', 'agent'])
def test_same_runtime_hooks_and_stop(runtime, monkeypatch, role):
    connection, calls, ready = runtime
    from desktop import launcher, agent_launcher
    import lib.desktop_agent._pair as pair
    monkeypatch.setattr(launcher, '_log', lambda msg: None)
    monkeypatch.setattr(agent_launcher, '_log', lambda msg: None)
    state = {'url': 'https://example.test', 'secret': 'token'}
    relay = state['browser_relay'] = Relay()
    perms = {'allow_exec': False}
    state['perms'] = perms
    try:
        if role == 'full':
            launcher._start_computer_control(15000, state)
            launcher._start_computer_control(15000, state)
        else:
            agent_launcher._start_agent(state, perms)
            agent_launcher._start_agent(state, perms)
        assert ready.wait(1)
        assert len(calls) == 1
        url, live_perms, hooks = calls[0]
        assert url == 'https://example.test'
        assert live_perms is perms
        assert hooks['browser_relay'] is relay
        hooks['on_status']({'state': 'ok', 'transport': 'browser'})
        assert state['last_status']['transport'] == 'browser'
        monkeypatch.setattr(pair, 'resume_attachment',
                            lambda *args, **kw: ('https://replacement.test', 'fresh'))
        assert hooks['route_repair']() == ('https://replacement.test', 'fresh')
        assert (state['url'], state['secret']) == ('https://replacement.test', 'fresh')
    finally:
        connection.stop(state, lambda msg: None)
    assert not state['thread'].is_alive()
    assert relay.closed
    assert not state['enabled']


def test_full_local_stays_local_and_deny_default(runtime, monkeypatch):
    connection, calls, ready = runtime
    from desktop import launcher
    from lib.desktop_agent import config
    from lib.desktop_agent._permissions import safe_default
    monkeypatch.setattr(config, 'remote_server', lambda: ('', ''))
    monkeypatch.setattr(launcher, '_log', lambda msg: None)
    monkeypatch.setattr(connection, 'ensure_relay', lambda *a: pytest.fail('local broker'))
    state = {}
    try:
        launcher._start_computer_control(15432, state)
        assert ready.wait(1)
        url, perms, hooks = calls[0]
        assert url == 'http://127.0.0.1:15432'
        assert perms == safe_default()
        assert hooks['route_repair'] is None
        assert hooks['browser_relay'] is None
    finally:
        launcher._stop_computer_control(state)


def test_restart_refuses_worker_that_has_not_stopped(monkeypatch):
    from desktop import agent_connection as connection

    class Stuck:
        def is_alive(self):
            return True

        def join(self, timeout):
            assert timeout == 5

    state = {'thread': Stuck(), 'stop': threading.Event(), 'enabled': True}
    assert connection.restart(state, {}, lambda msg: None) is False
    assert state['stop'].is_set()
    assert isinstance(state['thread'], Stuck)
    assert not state['enabled']


def test_shared_module_import_is_headless():
    code = '''import sys
import desktop.agent_connection
assert not any(name in sys.modules for name in
               ('server', 'quart', 'tkinter', 'pystray', 'PIL', 'desktop.launcher',
                'lib.desktop_agent'))
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_relay_singleton_dynamic_routes_and_cleanup(monkeypatch):
    from desktop import agent_connection as connection
    from lib.desktop_agent import _browser_relay, config
    created = []

    class Broker(Relay):
        def __init__(self, urls, **kwargs):
            super().__init__()
            self.urls = urls
            self.options = kwargs
            created.append(self)

        def start(self):
            return True

    monkeypatch.setattr(_browser_relay, 'BrowserRelay', Broker)
    monkeypatch.setattr(config, 'load_config', lambda: {'attach_candidates': ['https://candidate.test']})
    state = {'url': 'https://original.test'}
    connection.ensure_relay(state, lambda msg: None)
    connection.ensure_relay(state, lambda msg: None)
    assert len(created) == 1
    state['url'] = 'https://new.test'
    assert created[0].urls() == ['https://new.test', 'https://candidate.test']
    connection.stop(state, lambda msg: None)
    assert created[0].closed
    assert 'browser_relay' not in state
