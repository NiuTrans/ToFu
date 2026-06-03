"""Shared pytest fixtures for the Tofu test suite.

Provides the ``flask_client`` fixture consumed by ``tests/test_api_integration.py``
and ``tests/test_conversation_search.py``.

Design:
  * Each test session gets a fresh, isolated SQLite database via ``TOFU_DB_PATH``
    pointing at a temp file — no PostgreSQL required, no cross-test contamination.
  * The Flask ``app`` is imported lazily AFTER env-vars are set so
    ``lib.database._core`` picks the right backend at import time.
  * ``flask_client`` is function-scoped so each test gets a clean test client
    with its own cookie jar.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import sys
import tempfile

import pytest


# ─── Module-load: install the Flask→Quart shim ONCE, before any test
#     module is collected ─────────────────────────────────────────────
#
# ``server.py`` installs ``sys.modules['flask'] = quart`` at import time so
# all route/lib code that does ``from flask import …`` resolves to Quart.
# Historically each test module that needed it installed the shim itself at
# its own import top — which made pytest **collection order-dependent**: a
# module collected before any shim-installer (e.g. test_conversation_search
# importing ``routes.push`` with ``@push_bp.websocket``) crashed with
# ``'Blueprint' object has no attribute 'websocket'``, while a module doing
# ``from flask import Flask`` collected AFTER a shim-installer crashed with
# ``cannot import name 'Flask' from 'quart'``.
#
# Installing the shim here (conftest is imported before collection begins)
# makes every test see Quart consistently. Tests that need GENUINE Flask
# use the ``import_real_flask`` helper / ``real_flask`` fixture below, which
# temporarily lifts this shim.
def _install_flask_quart_shim():
    try:
        import quart
    except ImportError:
        return  # Quart not installed — legacy real-Flask environment.
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        try:
            sys.modules[f'flask.{attr}'] = __import__(f'quart.{attr}',
                                                      fromlist=[attr])
        except ImportError:
            pass


_install_flask_quart_shim()


# ─── Module-load: shim werkzeug.__version__ if missing ────────────────
#
# Werkzeug 3.x no longer exposes ``werkzeug.__version__`` as a module
# attribute, but older Flask checkouts (e.g. an editable install of
# Flask 2.3.0.dev0 pinned by a swebench workspace) still reference it
# from ``flask.testing`` and ``flask.helpers``. When that combination is
# present, ``app.test_client()`` raises ``AttributeError: module
# 'werkzeug' has no attribute '__version__'`` before any test even
# runs.
#
# Populate the attribute from package metadata so the legacy Flask path
# works without modifying the shared environment. No-op on installations
# where Werkzeug already exports it.
def _ensure_werkzeug_version():
    try:
        import werkzeug
    except ImportError:
        return
    if getattr(werkzeug, '__version__', None):
        return
    try:
        from importlib.metadata import version as _pkg_version
        werkzeug.__version__ = _pkg_version('werkzeug')
    except Exception:
        werkzeug.__version__ = '0+unknown'


_ensure_werkzeug_version()


# ─── Real-Flask import helper ─────────────────────────────────────────
#
# Several test modules install the production Flask→Quart shim at import
# time (``sys.modules['flask'] = quart``). Quart has no ``Flask`` class
# (it's ``Quart``), so any test that genuinely needs REAL Flask — e.g. to
# build a standalone WSGI app for Werkzeug ProxyFix / @rate_limit decorator
# smoke tests — must bypass the shim. This helper temporarily lifts the
# shim, imports the real distribution (plus the ``testing``/``cli``
# submodules that ``app.test_client()`` pulls in lazily), then restores
# the shim while keeping the real ``flask.*`` submodules pinned.
def import_real_flask():
    import importlib
    import sys

    cur = sys.modules.get('flask')
    if cur is not None and hasattr(cur, 'Flask'):
        return cur
    shim = sys.modules.pop('flask', None)
    shim_subs = {k: sys.modules.pop(k)
                 for k in list(sys.modules) if k.startswith('flask.')}
    try:
        real = importlib.import_module('flask')
        for sub in ('flask.testing', 'flask.cli'):
            try:
                importlib.import_module(sub)
            except ImportError:
                pass
        return real
    finally:
        if shim is not None:
            real_subs = {k: sys.modules[k]
                         for k in list(sys.modules) if k.startswith('flask.')}
            sys.modules['flask'] = shim
            sys.modules.update(shim_subs)
            sys.modules.update(real_subs)


@pytest.fixture()
def real_flask():
    """Fixture returning the genuine Flask package (bypassing the shim)."""
    return import_real_flask()


# ─── Session-level: one SQLite DB per pytest run ──────────────────────
@pytest.fixture(scope="session", autouse=True)
def _configure_test_env():
    """Set env vars BEFORE importing the Flask app so the DB layer picks
    SQLite and isolates data to a temp file. Trading features are disabled
    to keep the surface area small.
    """
    tmpdir = tempfile.mkdtemp(prefix="tofu-test-")
    db_path = os.path.join(tmpdir, "tofu-test.db")

    os.environ.setdefault("TOFU_DB_BACKEND", "sqlite")
    os.environ.setdefault("TOFU_DB_PATH", db_path)
    os.environ.setdefault("TRADING_ENABLED", "0")
    os.environ.setdefault("PPTX_TRANSLATE_ENABLED", "0")
    # Avoid accidental real LLM calls in CI.
    os.environ.setdefault("LLM_API_KEY", "test-key-placeholder")
    os.environ.setdefault("LLM_API_KEYS", "test-key-placeholder")
    # Lock auth mode to 'private' for the test suite by default. The
    # production default is 'open' (no credential required); tests that
    # exercise the gate need the stricter behavior. Individual tests
    # that need a different mode use the ``auth_mode`` marker:
    #
    #   @pytest.mark.auth_mode("open")
    #   def test_thing(): ...
    #
    # See the ``_auth_mode_override`` autouse fixture below.
    os.environ.setdefault("TOFU_AUTH_MODE", "private")

    yield

    # Best-effort cleanup — don't fail the run if files are still locked.
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


# ─── Session-level: build the Flask app once ──────────────────────────
@pytest.fixture(scope="session")
def flask_app(_configure_test_env):
    """Import and return the Flask app AFTER env-vars are set.

    This runs exactly once per test session so importing 800+ modules
    (server.py's full blueprint stack) doesn't dominate the wall-clock.
    """
    import server  # noqa: F401 — importing triggers app construction
    from server import app

    app.config.update(TESTING=True)

    # Importing server.py builds the app but does NOT create the database
    # schema — that only happens in server.py's async `_startup()` under
    # `if __name__ == '__main__'`. Tests that hit DB-backed endpoints
    # (conversation search) or the DB rate-limit store need the tables, so
    # initialise the schema once here against the session's temp SQLite DB.
    from lib.database import init_db
    init_db()
    return app


# ─── Function-level: per-test auth-mode override via marker ──────────
def pytest_configure(config):
    """Register the ``auth_mode`` marker so ``--strict-markers`` is happy."""
    config.addinivalue_line(
        'markers',
        'auth_mode(mode): override TOFU_AUTH_MODE for this test '
        '(open / private / multi-user). Restored after the test.',
    )


@pytest.fixture(autouse=True)
def _auth_mode_override(request):
    """Apply the ``@pytest.mark.auth_mode("...")`` marker if present.

    Without a marker this is a no-op and the session default
    (``TOFU_AUTH_MODE=private`` from ``_configure_test_env``) stands.
    With a marker we set the env var, clear ``lib.auth_mode``'s cache
    so the new value is observed, and restore both on teardown.

    Tests that hit the live ``server.app`` without a Bearer token (the
    paper-migration suite, the daily-report-migration suite, anything
    importing ``server`` and using ``app.test_client()``) should mark
    themselves ``auth_mode("open")``.
    """
    marker = request.node.get_closest_marker('auth_mode')
    if marker is None:
        yield
        return
    mode = marker.args[0] if marker.args else 'open'
    prev = os.environ.get('TOFU_AUTH_MODE')
    os.environ['TOFU_AUTH_MODE'] = mode
    try:
        from lib.auth_mode import reset_for_tests
        reset_for_tests()
    except Exception:
        pass
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop('TOFU_AUTH_MODE', None)
        else:
            os.environ['TOFU_AUTH_MODE'] = prev
        try:
            from lib.auth_mode import reset_for_tests
            reset_for_tests()
        except Exception:
            pass


# ─── Function-level: fresh test client per test ───────────────────────
#
# ``server.app`` is a **Quart** app, whose test client is async-only and
# does NOT support the synchronous ``with client:`` / ``resp.status_code``
# / ``resp.get_json()`` surface that the legacy ``@pytest.mark.api`` tests
# (test_api_integration.py, test_conversation_search.py,
# test_rate_limit_store.py) were written against for the old Flask app.
#
# Rather than rewrite ~50 sync assertions as ``async def`` + ``await``, we
# wrap the Quart client in a thin synchronous adapter that drives each
# request on a single persistent event loop (so the cookie jar persists
# across calls within one test) and eagerly reads the response body, then
# exposes a Werkzeug-like ``_SyncResponse`` with ``status_code``, ``data``,
# and ``get_json()``.


class _SyncResponse:
    """Werkzeug-test-response-shaped view over a Quart test response.

    The body is read eagerly (awaited) at construction time so the sync
    accessors below never need to await.
    """

    __slots__ = ('status_code', 'headers', 'content_type', 'data', '_text')

    def __init__(self, status_code, headers, body_bytes):
        self.status_code = status_code
        self.headers = headers
        self.content_type = headers.get('Content-Type', '')
        self.data = body_bytes
        self._text = body_bytes.decode('utf-8', errors='replace')

    def get_json(self, silent=False):
        try:
            return _json.loads(self._text)
        except (ValueError, TypeError):
            if silent:
                return None
            return None

    def get_data(self, as_text=False):
        return self._text if as_text else self.data


class _SyncQuartClient:
    """Synchronous facade over a Quart async test client.

    Each HTTP verb runs the underlying async call to completion on a
    shared loop and returns a fully-materialised :class:`_SyncResponse`.
    """

    def __init__(self, async_client, loop):
        self._client = async_client
        self._loop = loop

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    @staticmethod
    def _ascii_safe_path(path):
        """Percent-encode a non-ASCII query string embedded in ``path``.

        Quart's test client encodes the query string as strict ASCII and
        raises ``UnicodeEncodeError`` on raw UTF-8 in the URL (e.g.
        ``?q=搜索引擎``). Werkzeug's old client quoted it transparently, and
        the legacy tests rely on that, so replicate it here. Only the query
        component is touched; the path component is left as-is.
        """
        if '?' not in path:
            return path
        try:
            path.encode('ascii')
            return path  # already ASCII — nothing to do
        except UnicodeEncodeError:
            from urllib.parse import quote
            base, _, query = path.partition('?')
            return base + '?' + quote(query, safe='=&%+')

    async def _request(self, method, path, **kwargs):
        path = self._ascii_safe_path(path)
        resp = await self._client.open(path, method=method, **kwargs)
        body = await resp.get_data()
        if isinstance(body, str):
            body = body.encode('utf-8')
        return _SyncResponse(resp.status_code, resp.headers, body)

    def get(self, path, **kwargs):
        return self._run(self._request('GET', path, **kwargs))

    def post(self, path, **kwargs):
        return self._run(self._request('POST', path, **kwargs))

    def put(self, path, **kwargs):
        return self._run(self._request('PUT', path, **kwargs))

    def patch(self, path, **kwargs):
        return self._run(self._request('PATCH', path, **kwargs))

    def delete(self, path, **kwargs):
        return self._run(self._request('DELETE', path, **kwargs))

    def open(self, path, method='GET', **kwargs):
        return self._run(self._request(method, path, **kwargs))


@pytest.fixture()
def flask_client(flask_app, request):
    """Return a synchronous test client with its own cookie jar.

    Used by the ``@pytest.mark.api`` integration tests and conversation-
    search tests, which are written in a synchronous Flask-test style.
    The adapter drives the Quart async client under the hood.

    These legacy tests send no credential and were written for the
    open-by-default auth mode (which is also the production default). The
    session env locks ``TOFU_AUTH_MODE=private``, so unless the test opts
    into a specific mode via ``@pytest.mark.auth_mode(...)`` we force
    ``open`` for the duration of the test and restore it afterwards.
    """
    has_marker = request.node.get_closest_marker('auth_mode') is not None
    prev_mode = os.environ.get('TOFU_AUTH_MODE')
    if not has_marker:
        os.environ['TOFU_AUTH_MODE'] = 'open'
        try:
            from lib.auth_mode import reset_for_tests
            reset_for_tests()
        except Exception:
            pass

    loop = asyncio.new_event_loop()
    async_client = flask_app.test_client()
    try:
        yield _SyncQuartClient(async_client, loop)
    finally:
        loop.close()
        if not has_marker:
            if prev_mode is None:
                os.environ.pop('TOFU_AUTH_MODE', None)
            else:
                os.environ['TOFU_AUTH_MODE'] = prev_mode
            try:
                from lib.auth_mode import reset_for_tests
                reset_for_tests()
            except Exception:
                pass
