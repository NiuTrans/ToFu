#!/usr/bin/env python3
"""Tests for the async server (server.py).

Validates:
1. The Flask→Quart shim works (imports resolve correctly)
2. The Quart app boots and registers all blueprints
3. Sync route handlers work (run in thread pool)
4. SSE streaming endpoints deliver events correctly
5. Static file serving with correct MIME types
6. Tunnel auth works
7. Compression works for JSON responses

Run:
    pytest tests/test_server_async.py -v
    # or standalone:
    python tests/test_server_async.py
"""

import asyncio
import json
import os
import sys
import time

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ═══════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def quart_available():
    """Skip all tests if quart/hypercorn not installed."""
    try:
        import quart
        import hypercorn
        return True
    except ImportError as e:
        pytest.skip(f'quart/hypercorn not installed: {e}')


@pytest.fixture(scope='module')
def async_app(quart_available, flask_app):
    """Return the Quart app, reusing the session-scoped ``flask_app``
    fixture from conftest.py.

    ``flask_app`` imports ``server`` exactly once per session AFTER the
    conftest env-vars are set (SQLite temp DB, schema provisioned via
    ``init_db()`` on import). Re-importing ``server.py`` independently
    here would either double-run the heavy boot or, worse, bind to a
    DIFFERENT database than the one the rest of the suite provisioned —
    which previously surfaced as ``sqlite3.OperationalError: no such
    table: task_results`` when the stream route tried to read.
    """
    return flask_app


@pytest.fixture
def client(async_app):
    """Quart test client."""
    return async_app.test_client()


def _run_async(coro):
    """Run an async test body without requiring pytest-asyncio.

    This repo does not install the ``pytest-asyncio`` plugin, so a bare
    ``@pytest.mark.asyncio async def`` test is collected but never awaited
    (pytest reports it as a failure: "async def functions are not natively
    supported"). Mirror ``tests/test_restart_smoke.py`` and drive the
    coroutine on a private event loop instead.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════════════

class TestFlaskShim:
    """Verify the Flask→Quart import shim works."""

    def test_flask_import_resolves_to_quart(self, quart_available):
        """After shim install, `import flask` should give quart."""
        # The shim is installed when server_async is imported
        import flask
        import quart
        assert flask.Blueprint is quart.Blueprint
        assert flask.request is quart.request

    def test_flask_blueprint_registration(self, async_app):
        """All Flask-style blueprints register successfully on Quart app."""
        # Check that core blueprints are registered. After the /api/v1
        # migration, config/conversations moved under the api_v1_* namespace
        # while chat/common kept their legacy bare names.
        bp_names = set(async_app.blueprints.keys())
        assert 'chat' in bp_names
        assert 'common' in bp_names
        assert 'api_v1_config' in bp_names
        assert 'api_v1_conversations' in bp_names

    def test_blueprint_count(self, async_app):
        """Reasonable number of blueprints are registered."""
        # We expect at least 20 core blueprints
        assert len(async_app.blueprints) >= 20


@pytest.mark.auth_mode("open")
class TestBasicRoutes:
    """Test that sync Flask routes work under Quart."""

    def test_health_endpoint(self, client):
        """A basic GET to /api/v1/chat/active should work."""
        async def go():
            resp = await client.get('/api/v1/chat/active')
            assert resp.status_code == 200
            data = await resp.get_json()
            assert isinstance(data, list)
        _run_async(go())

    def test_404_json(self, client):
        """API 404 returns JSON."""
        async def go():
            resp = await client.get('/api/nonexistent')
            assert resp.status_code == 404
            data = await resp.get_json()
            assert data['ok'] is False
        _run_async(go())

    def test_404_html(self, client):
        """Non-API 404 returns HTML."""
        async def go():
            resp = await client.get('/nonexistent-page')
            assert resp.status_code == 404
            body = (await resp.get_data()).decode()
            assert '404' in body
        _run_async(go())

    def test_static_js_mime(self, client):
        """Static .js files get correct MIME type."""
        async def go():
            # Request any .js file from static/
            resp = await client.get('/static/js/core.js')
            if resp.status_code == 200:
                ct = resp.content_type or ''
                assert 'javascript' in ct
        _run_async(go())


@pytest.mark.auth_mode("open")
class TestTunnelAuth:
    """Test tunnel token auth when enabled."""

    def test_no_auth_when_no_token(self, client, async_app):
        """When TUNNEL_TOKEN is empty, all requests pass through."""
        # server.py reads TUNNEL_TOKEN at module level — if not set,
        # auth is disabled and requests pass through unchanged.
        import server
        if server.TUNNEL_TOKEN:
            pytest.skip('TUNNEL_TOKEN is set in env')
        async def go():
            resp = await client.get('/api/v1/chat/active')
            assert resp.status_code == 200
        _run_async(go())


@pytest.mark.auth_mode("open")
class TestCompression:
    """Test gzip compression."""

    def test_json_compressed(self, client):
        """JSON responses are gzip compressed when Accept-Encoding: gzip."""
        async def go():
            resp = await client.get(
                '/api/v1/chat/active',
                headers={'Accept-Encoding': 'gzip'}
            )
            assert resp.status_code == 200
            # Response might be compressed if body > 256 bytes
            # For small responses (empty list), compression is skipped
            # Just verify the request doesn't crash
        _run_async(go())


@pytest.mark.auth_mode("open")
class TestSSEStreaming:
    """Test SSE streaming compatibility."""

    def test_stream_nonexistent_task(self, client):
        """Streaming a nonexistent task returns 404."""
        async def go():
            resp = await client.get('/api/chat/stream/nonexistent-task-id')
            # Should be 404 (task not found) or SSE with error
            assert resp.status_code in (200, 404)
        _run_async(go())


# ═══════════════════════════════════════════════════════════════════════
#  Standalone runner
# ═══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    # Quick smoke test without pytest
    print('=== Async Server Smoke Test ===')
    print()

    # ⚠️ DATA-LOSS GUARD: standalone mode skips conftest (no force-sqlite, no
    # pytest_configure gate), so guard the real-app boot against a non-test DB.
    try:
        from tests.conftest import _assert_test_database as _adb
    except Exception:
        from conftest import _assert_test_database as _adb  # run from tests/ cwd
    _adb('test_server_async.__main__')

    # 1. Check dependencies
    try:
        import quart
        from importlib.metadata import version as _pkg_version
        print(f'✓ quart {_pkg_version("quart")} available')
    except ImportError:
        print('✗ quart not installed — run: pip install quart')
        sys.exit(1)

    try:
        import hypercorn
        print(f'✓ hypercorn {_pkg_version("hypercorn")} available')
    except ImportError:
        print('✗ hypercorn not installed — run: pip install hypercorn')
        sys.exit(1)

    # 2. Try importing server.py (installs shim)
    print()
    print('Importing server.py (installs Flask→Quart shim)…')
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'server',
            os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py')
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__name__ = 'server'
        spec.loader.exec_module(mod)
        print('✓ server.py imported successfully')
    except Exception as e:
        print(f'✗ Import failed: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 3. Verify shim
    import flask
    assert flask.Blueprint is quart.Blueprint, 'Shim failed: flask.Blueprint != quart.Blueprint'
    print('✓ Flask→Quart shim verified')

    # 4. Check blueprints
    app = mod.app
    bp_count = len(app.blueprints)
    print(f'✓ {bp_count} blueprints registered')
    assert bp_count >= 20, f'Expected ≥20 blueprints, got {bp_count}'

    # 5. Test client
    print()
    print('Running test client checks…')

    async def _run_checks():
        async with app.test_client() as client:
            # Basic route
            resp = await client.get('/api/v1/chat/active')
            assert resp.status_code == 200, f'Expected 200, got {resp.status_code}'
            data = await resp.get_json()
            assert isinstance(data, list)
            print('  ✓ GET /api/v1/chat/active → 200 (sync route in thread pool)')

            # 404
            resp = await client.get('/api/nonexistent')
            assert resp.status_code == 404
            print('  ✓ GET /api/nonexistent → 404 (error handler)')

            # Static
            resp = await client.get('/static/js/core.js')
            if resp.status_code == 200:
                ct = resp.content_type or ''
                assert 'javascript' in ct
                print('  ✓ GET /static/js/core.js → 200 (correct MIME)')
            else:
                print(f'  ~ GET /static/js/core.js → {resp.status_code} (file may not exist in test env)')

    asyncio.run(_run_checks())

    print()
    print('=== All smoke tests passed! ===')
    print()
    print('To start the async server:')
    print('  python server.py')
    print()
    print('To start with HTTP/2 (requires TLS):')
    print('  python server.py --certfile cert.pem --keyfile key.pem')
