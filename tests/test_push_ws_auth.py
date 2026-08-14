"""Security regression tests for the /api/push WebSocket trust boundary."""

from __future__ import annotations

import hashlib
import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stable_auth(monkeypatch):
    monkeypatch.setattr('lib.api_keys.touch_key', lambda _key_id: None)
    monkeypatch.setattr('routes.api_v1.auth._OPEN_MODE_ALLOW_REMOTE', False)


def _resolve(monkeypatch, *, private=False, peer=('127.0.0.1', 1),
             headers=None, cookies=None, ctx=None):
    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: private)
    monkeypatch.setattr('lib.api_keys.validate_token', lambda _token: ctx)
    from routes.push import _resolve_push_ws_auth
    return _resolve_push_ws_auth(headers or {}, cookies or {}, peer)


def test_open_mode_keeps_zero_setup_loopback_socket(monkeypatch):
    ctx, reason = _resolve(monkeypatch)
    assert ctx is not None and ctx.via_open_mode is True
    assert reason == 'open_local'


@pytest.mark.parametrize('peer', [
    ('203.0.113.7', 5555), ('10.0.0.8', 5555), ('2001:db8::1', 5555),
])
def test_open_mode_remote_socket_is_rejected_without_opt_in(monkeypatch, peer):
    ctx, reason = _resolve(monkeypatch, peer=peer)
    assert ctx is None and reason == 'credential_required'


def test_open_mode_explicit_remote_opt_in_is_preserved(monkeypatch):
    monkeypatch.setattr('routes.api_v1.auth._OPEN_MODE_ALLOW_REMOTE', True)
    ctx, reason = _resolve(monkeypatch, peer=('203.0.113.7', 5555))
    assert ctx is not None and ctx.via_open_mode is True
    assert reason == 'open_local'


def test_private_mode_requires_a_socket_credential(monkeypatch):
    ctx, reason = _resolve(monkeypatch, private=True)
    assert ctx is None and reason == 'credential_required'


def test_private_mode_carries_valid_token_owner(monkeypatch):
    from lib.api_keys import AuthContext
    expected = AuthContext(key_id='k1', user_id='alice',
                           scopes=frozenset({'chat'}))
    ctx, reason = _resolve(
        monkeypatch, private=True, ctx=expected,
        headers={'Authorization': 'Bearer tofu_live_test'})
    assert ctx is expected and ctx.user_id == 'alice'
    assert reason == 'token'


def test_private_mode_rejects_a_bad_socket_token(monkeypatch):
    ctx, reason = _resolve(
        monkeypatch, private=True, ctx=None,
        headers={'Authorization': 'Bearer tofu_live_bad'})
    assert ctx is None and reason == 'invalid_token'


def test_legacy_tunnel_header_and_cookie_remain_compatible(monkeypatch):
    monkeypatch.setenv('TUNNEL_TOKEN', 'legacy-secret')
    ctx, reason = _resolve(
        monkeypatch, private=True,
        headers={'X-Tunnel-Token': 'legacy-secret'})
    assert ctx is not None and ctx.via_tunnel_token is True
    assert reason == 'tunnel'

    digest = hashlib.sha256(b'legacy-secret').hexdigest()[:32]
    ctx2, reason2 = _resolve(
        monkeypatch, private=True, cookies={'_tunnel_auth': digest})
    assert ctx2 is not None and ctx2.via_tunnel_token is True
    assert reason2 == 'tunnel'


def test_http_and_ws_share_loopback_address_parser():
    from routes.api_v1.auth import address_is_loopback
    assert address_is_loopback(('127.0.0.1', 5000))
    assert address_is_loopback(('::1', 5000))
    assert address_is_loopback(('::ffff:127.0.0.1', 5000))
    assert not address_is_loopback(('203.0.113.7', 5000))
    assert not address_is_loopback(None)


@pytest.fixture
def _task_registry():
    from lib.tasks_pkg import tasks, tasks_lock
    with tasks_lock:
        old = dict(tasks)
        tasks.clear()
    try:
        yield tasks, tasks_lock
    finally:
        with tasks_lock:
            tasks.clear()
            tasks.update(old)


def test_scoped_socket_cannot_abort_another_users_task(_task_registry):
    tasks, lock = _task_registry
    evt = threading.Event()
    task = {'id': 'foreign-task', '_userId': 'bob', 'aborted': False,
            'abort_event': evt}
    with lock:
        tasks[task['id']] = task

    from routes.push import _handle_abort
    _handle_abort(task['id'], req_id='alice-ws', user_id='alice')
    assert task['aborted'] is False
    assert not evt.is_set()


def test_owner_and_legacy_unscoped_socket_can_abort(_task_registry):
    tasks, lock = _task_registry
    from routes.push import _handle_abort

    owner_evt = threading.Event()
    owner_task = {'id': 'owner-task', '_userId': 'alice', 'aborted': False,
                  'abort_event': owner_evt}
    legacy_evt = threading.Event()
    legacy_task = {'id': 'legacy-task', '_userId': '', 'aborted': False,
                   'abort_event': legacy_evt}
    with lock:
        tasks[owner_task['id']] = owner_task
        tasks[legacy_task['id']] = legacy_task

    _handle_abort(owner_task['id'], req_id='alice-ws', user_id='alice')
    _handle_abort(legacy_task['id'], req_id='solo-ws', user_id='')
    assert owner_task['aborted'] is True and owner_evt.is_set()
    assert legacy_task['aborted'] is True and legacy_evt.is_set()


def test_push_handler_fails_closed_before_registering_socket():
    """Static order guard: rejection must precede PushClient/hub.register."""
    import ast
    import inspect
    from routes import push

    src = inspect.getsource(push.push_ws)
    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else '')
            if name in {'abort', 'PushClient', 'register'}:
                calls.append((node.lineno, name))
    first = {name: min(line for line, n in calls if n == name)
             for name in {'abort', 'PushClient', 'register'}}
    assert first['abort'] < first['PushClient'] < first['register']
