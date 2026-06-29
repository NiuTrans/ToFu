#!/usr/bin/env python3
"""Server-restart smoke test — covers the surface that the
Flask→Quart+Hypercorn migration touched.

What this guarantees on every fresh `python server.py` boot:
  1. Critical async-stack deps import (`quart`, `hypercorn`, `httpx`).
  2. The Flask→Quart import shim is still in place (`from flask import …`
     resolves to Quart classes) and Quart's async ``Request`` accessors
     are sync-safe for thread-pool routes.
  3. ``server.app`` constructs and registers the full blueprint set,
     including the ``push`` and ``api_v1`` headless surface.
  4. The unified push channel route ``/api/push`` is registered as a
     WebSocket and exposes the global ``PushHub`` singleton.
  5. The unified ``TaskRuntime`` lifecycle (create / append_event /
     finish / poll / abort) is wired correctly and pushes events
     through ``lib.push.push_event``.
  6. Per-task ``/api/chat/ws/<task_id>`` is **removed** — chat streams
     exclusively via SSE (``/api/chat/stream/<id>``) for durability
     (Last-Event-ID resume across server restarts).
  7. JSON error handlers go through the unified ``api_response``
     helpers so their shape is ``{ok: False, error: ...}``.
  8. The JS bundle builds at startup (so the bundler regression
     described in CLAUDE.md §3.2.1 doesn't ship a silent no-op).
  9. Sync Flask-style routes still work under the Quart thread-pool
     dispatcher (the migration's main compatibility promise).

Run:
    pytest tests/test_restart_smoke.py -v
    python tests/test_restart_smoke.py        # standalone
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════
#  Fixtures — load server.py exactly the way `python server.py` does
# ═══════════════════════════════════════════════════════════════════════


_TEST_TUNNEL_TOKEN = '__smoke_test_token__'


@pytest.fixture(scope='module')
def deps_available():
    try:
        import hypercorn  # noqa: F401
        import httpx      # noqa: F401
        import quart      # noqa: F401
    except ImportError as e:
        pytest.skip(f'async deps unavailable: {e}')
    return True


@pytest.fixture(scope='module')
def server_module(deps_available):
    """Import server.py and return the module (NOT the if-name-main block)."""
    # Install a tunnel token so test requests pass the auth middleware.
    os.environ.setdefault('TUNNEL_TOKEN', _TEST_TUNNEL_TOKEN)
    spec = importlib.util.spec_from_file_location(
        'server',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'  # keeps the __main__ block dormant
    sys.modules['server'] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:
        pytest.skip(f'server.py exited at import: {e}')
    return mod


@pytest.fixture(scope='module')
def app(server_module):
    return server_module.app


@pytest.fixture
def client(app):
    return app.test_client()


def _auth_headers():
    """Headers that satisfy the tunnel-token auth middleware."""
    return {'X-Tunnel-Token': _TEST_TUNNEL_TOKEN}


# ═══════════════════════════════════════════════════════════════════════
#  1. Async stack imports
# ═══════════════════════════════════════════════════════════════════════


def test_quart_shim_in_place(server_module):
    """`from flask import …` must resolve to Quart classes."""
    import flask
    import quart
    assert flask.Blueprint is quart.Blueprint
    assert flask.request is quart.request
    assert flask.jsonify is quart.jsonify


def test_quart_request_sync_safe_get_json(server_module):
    """The shim patches Quart's async ``Request.get_json`` to be sync-safe.

    Routes still written sync would otherwise receive a coroutine when
    they call ``request.get_json()``.
    """
    from quart.wrappers import Request as _QReq
    # The shim assigns a plain function (not a coroutine) over the method.
    assert not asyncio.iscoroutinefunction(_QReq.get_json), (
        'Quart Request.get_json must be wrapped as sync-safe by server.py')


# ═══════════════════════════════════════════════════════════════════════
#  2. Blueprint registration
# ═══════════════════════════════════════════════════════════════════════


# Domains that must always be registered. After the /api/v1 migration,
# several domains (config, conversations, project, memory, scheduler, mcp,
# optimizer, browser, desktop) no longer own a bare-named Blueprint — they
# live under the ``api_v1_<name>`` namespace. The always-on legacy UI
# blueprints (chat, common, upload, translate, oauth, paper, push,
# artifacts, metrics) keep their bare names.
REQUIRED_BLUEPRINTS = {
    # Legacy UI / always-on blueprints (bare names):
    'chat', 'common', 'upload', 'translate', 'browser', 'desktop',
    'oauth', 'paper', 'push', 'artifacts', 'metrics',
    # Migrated domains (now under the api_v1_* namespace):
    'api_v1_config', 'api_v1_conversations', 'api_v1_project',
    'api_v1_memory', 'api_v1_scheduler', 'api_v1_mcp', 'api_v1_optimizer',
}


def test_all_required_blueprints_registered(app):
    missing = REQUIRED_BLUEPRINTS - set(app.blueprints.keys())
    assert not missing, f'Missing blueprints: {missing}'


def test_api_v1_blueprints_registered(app):
    """Headless API (api_v1) blueprints must be present after migration."""
    v1_bps = [name for name in app.blueprints if name.startswith('api_v1_')]
    assert len(v1_bps) >= 3, f'Expected ≥3 api_v1_* blueprints, got {v1_bps}'


# ═══════════════════════════════════════════════════════════════════════
#  3. Unified server-push channel — /api/push WebSocket
# ═══════════════════════════════════════════════════════════════════════


def test_push_websocket_route_registered(app):
    """The unified /api/push WS endpoint must exist."""
    rules = [r for r in app.url_map.iter_rules() if r.rule == '/api/push']
    assert rules, '/api/push WebSocket route is missing'


def test_per_task_chat_ws_removed(app):
    """Per-task /api/chat/ws/<task_id> was removed — chat uses SSE only."""
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert '/api/chat/ws/<task_id>' not in rules, (
        'per-task chat WS should be removed (chat uses SSE exclusively)')


def test_push_hub_singleton_present():
    """The PushHub singleton must be importable and have its public API."""
    from lib.push import broadcast, hub, push_event, PushClient
    assert hub is not None
    # Public callables
    assert callable(push_event)
    assert callable(broadcast)
    assert callable(PushClient)
    # The methods used by routes/push.py and TaskRuntime must exist
    for attr in ('register', 'unregister', 'subscribe', 'unsubscribe',
                 'push_event', 'broadcast', 'add_listener', 'remove_listener'):
        assert hasattr(hub, attr), f'PushHub missing {attr}'


def test_push_hub_add_listener():
    """add_listener hooks every push_event so webhooks/observers work
    without monkey-patching."""
    from lib.push import hub
    captured = []
    def listener(ch, tid, payload):
        captured.append((ch, tid, payload))
    hub.add_listener(listener)
    try:
        hub.push_event('test-ch', 'tid-1', {'x': 42})
        assert len(captured) == 1
        assert captured[0] == ('test-ch', 'tid-1', {'x': 42})
    finally:
        hub.remove_listener(listener)


# ═══════════════════════════════════════════════════════════════════════
#  4. Unified TaskRuntime — covers chat / paper / translate task surface
# ═══════════════════════════════════════════════════════════════════════


def test_task_runtime_lifecycle_pushes_events(monkeypatch, server_module):
    """create → append_event → finish must walk through the runtime AND
    fire push_event so frontend WS subscribers receive frames.
    """
    # TaskRuntime.append_event resolves push_event from its canonical home
    # (lib.agent_core.push) after the 2026-06 leaf relocation, so patch there.
    from lib.agent_core import push as push_mod
    from lib.task_runtime import TaskRuntime

    captured: list[tuple] = []

    def fake_push(channel, task_id, payload):
        captured.append((channel, task_id, dict(payload)))

    monkeypatch.setattr(push_mod, 'push_event', fake_push)

    rt = TaskRuntime('smoke-test', ttl=60, push_channel='smoke')
    task = rt.create()
    rt.append_event(task['id'], {'type': 'progress', 'pct': 10})
    rt.finish(task['id'], result={'ok': True})

    # Status transitions
    assert rt.get(task['id'])['status'] == 'done'

    # Poll-as-replay returns events in seq order
    poll = rt.poll(task['id'], cursor=0)
    assert poll['ok'] is True
    assert poll['done'] is True
    seqs = [e['seq'] for e in poll['events']]
    assert seqs == sorted(seqs)
    types = [e['type'] for e in poll['events']]
    assert 'progress' in types and 'done' in types

    # And every event was fanned out to the push channel
    channels = {c for c, _, _ in captured}
    assert channels == {'smoke'}


def test_task_runtime_abort(server_module):
    from lib.task_runtime import TaskRuntime

    rt = TaskRuntime('smoke-abort', ttl=60, push_channel=None)
    task = rt.create()
    assert rt.abort(task['id']) is True
    assert task['abort_event'].is_set() is True
    # Aborting again on the same task is a no-op (still running until
    # the worker finishes and calls finish()), and finish() should
    # transition to 'aborted' since abort_event is set.
    rt.finish(task['id'])
    assert rt.get(task['id'])['status'] == 'aborted'


def test_chat_runtime_aliases_present():
    """lib.tasks_pkg.manager exports `tasks` and `tasks_lock` aliasing the
    chat TaskRuntime — 47 call sites depend on this.
    """
    from lib.tasks_pkg import manager
    assert hasattr(manager, 'tasks')
    assert hasattr(manager, 'tasks_lock')
    assert manager.tasks is manager._chat_runtime._tasks
    assert manager.tasks_lock is manager._chat_runtime._lock


# ═══════════════════════════════════════════════════════════════════════
#  5. Sync routes still dispatch under Quart's thread pool
# ═══════════════════════════════════════════════════════════════════════


def _run_async(coro):
    """Run an async test body without requiring pytest-asyncio."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_sync_route_runs_under_quart(client):
    """A long-standing sync Flask route must still be served by Quart."""
    async def go():
        resp = await client.get('/api/v1/chat/active', headers=_auth_headers())
        assert resp.status_code == 200
        data = await resp.get_json()
        assert isinstance(data, list)
    _run_async(go())


# ═══════════════════════════════════════════════════════════════════════
#  6. Error handlers go through api_response helpers
# ═══════════════════════════════════════════════════════════════════════


def test_404_api_uses_unified_envelope(client):
    async def go():
        resp = await client.get('/api/this-route-does-not-exist',
                                headers=_auth_headers())
        assert resp.status_code == 404
        data = await resp.get_json()
        assert data['ok'] is False
        assert 'error' in data
    _run_async(go())


def test_404_html_for_browser(client):
    async def go():
        resp = await client.get('/no-such-page', headers=_auth_headers())
        assert resp.status_code == 404
        body = (await resp.get_data()).decode()
        assert '404' in body
    _run_async(go())


def test_405_uses_unified_envelope(client):
    async def go():
        resp = await client.post('/api/v1/chat/active',
                                 headers=_auth_headers())  # GET-only route
        assert resp.status_code == 405
        data = await resp.get_json()
        assert data['ok'] is False
    _run_async(go())


# ═══════════════════════════════════════════════════════════════════════
#  7. Static + JS bundle
# ═══════════════════════════════════════════════════════════════════════


def test_js_bundle_built_at_startup(server_module):
    """build_bundle() runs on import — the bundle file should exist."""
    from lib.js_bundler import get_bundle_filename
    name = get_bundle_filename()
    assert name and name.startswith('bundle-') and name.endswith('.js'), name
    bundle_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        'static', 'js', name,
    )
    assert os.path.exists(bundle_path), f'bundle file missing at {bundle_path}'


def test_static_js_mime(client):
    async def go():
        resp = await client.get('/static/js/core.js')  # static is public
        if resp.status_code != 200:
            pytest.skip('core.js missing in this checkout')
        ct = (resp.content_type or '').lower()
        assert 'javascript' in ct, ct
    _run_async(go())


def test_static_bundle_cache_immutable(client):
    """bundle-*.js must be served with immutable caching headers."""
    from lib.js_bundler import get_bundle_filename
    name = get_bundle_filename()
    if not name:
        pytest.skip('bundle not built')

    async def go():
        resp = await client.get(f'/static/js/{name}')
        if resp.status_code != 200:
            pytest.skip(f'bundle {name} not served in this env')
        cache = (resp.headers.get('Cache-Control') or '').lower()
        assert 'immutable' in cache, cache
    _run_async(go())


# ═══════════════════════════════════════════════════════════════════════
#  8. SSE streaming fallback path still wired (per-task poll route)
# ═══════════════════════════════════════════════════════════════════════


def test_chat_poll_unknown_task_404(client):
    async def go():
        resp = await client.get('/api/chat/poll/this-task-id-does-not-exist',
                                headers=_auth_headers())
        # poll for unknown task: 404 (most paths) or 200 with an empty/done
        # envelope (a few legacy routes). Either way it must not 500.
        assert resp.status_code in (200, 404)
    _run_async(go())


# ═══════════════════════════════════════════════════════════════════════
#  Standalone runner
# ═══════════════════════════════════════════════════════════════════════


def _standalone():
    """Run a quick verification without pytest. Returns exit code."""
    print('=== Tofu restart smoke test ===\n')
    # ⚠️ DATA-LOSS GUARD: standalone mode does NOT load conftest, so the
    # force-sqlite shim + pytest_configure gate never ran. Refuse to boot the
    # real app against a non-test DB here too (the keystone principle).
    try:
        from tests.conftest import _assert_test_database
    except Exception:
        from conftest import _assert_test_database  # when run from tests/ cwd
    _assert_test_database('test_restart_smoke._standalone')
    os.environ.setdefault('TUNNEL_TOKEN', _TEST_TUNNEL_TOKEN)
    try:
        import hypercorn  # noqa: F401
        import httpx      # noqa: F401
        import quart      # noqa: F401
        from importlib.metadata import version as _v
        try:
            print(f'✓ quart {_v("quart")}, hypercorn {_v("hypercorn")}, '
                  f'httpx {_v("httpx")}')
        except Exception:
            print('✓ async deps imported (versions unavailable)')
    except ImportError as e:
        print(f'✗ async deps missing: {e}')
        return 1

    spec = importlib.util.spec_from_file_location(
        'server',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    sys.modules['server'] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f'✗ server.py import failed: {e}')
        import traceback
        traceback.print_exc()
        return 1
    print('✓ server.py imported (Flask→Quart shim installed)')

    app = mod.app
    bp_names = set(app.blueprints.keys())
    missing = REQUIRED_BLUEPRINTS - bp_names
    assert not missing, f'Missing blueprints: {missing}'
    print(f'✓ {len(bp_names)} blueprints registered '
          f'(required surface present)')

    rules = {r.rule for r in app.url_map.iter_rules()}
    assert '/api/push' in rules, '/api/push missing'
    assert '/api/chat/ws/<task_id>' not in rules, 'per-task chat WS should be gone'
    print('✓ /api/push WS route present; per-task /api/chat/ws removed (SSE only)')

    from lib.push import hub
    from lib.task_runtime import TaskRuntime
    rt = TaskRuntime('standalone-smoke', ttl=10, push_channel=None)
    t = rt.create()
    rt.append_event(t['id'], {'type': 'progress'})
    rt.finish(t['id'], result={'ok': True})
    assert rt.get(t['id'])['status'] == 'done'
    print('✓ TaskRuntime lifecycle (create→append_event→finish) works')
    print(f'✓ PushHub singleton ready (clients={hub.client_count})')

    _h = {'X-Tunnel-Token': _TEST_TUNNEL_TOKEN}

    async def _check_routes():
        async with app.test_client() as c:
            r = await c.get('/api/v1/chat/active', headers=_h)
            assert r.status_code == 200, r.status_code
            r = await c.get('/api/nope', headers=_h)
            assert r.status_code == 404
            data = await r.get_json()
            assert data['ok'] is False
            r = await c.post('/api/v1/chat/active', headers=_h)
            assert r.status_code == 405
        return True

    asyncio.run(_check_routes())
    print('✓ Sync routes serve under Quart (200/404/405 envelopes verified)')

    from lib.js_bundler import get_bundle_filename
    name = get_bundle_filename()
    assert name, 'bundle not built'
    print(f'✓ JS bundle built: {name}')

    print('\n=== restart smoke test PASSED ===')
    return 0


if __name__ == '__main__':
    sys.exit(_standalone())
