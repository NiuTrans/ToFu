"""Shared pytest fixtures for the Tofu test suite.

Two independent fixture families live here:

  * ``flask_client`` / ``flask_app`` — a Quart (Flask-shim) test client over
    the REAL ``server.app``, consumed by ``tests/test_api_integration.py``,
    ``tests/test_conversation_search.py`` and any other API integration test.
    Importing ``server`` (in ``flask_app``) installs the Flask→Quart shim
    BEFORE any ``routes.*`` import, which is also what keeps
    ``routes/push.py``'s ``@push_bp.websocket`` from crashing collection with
    ``AttributeError: 'Blueprint' object has no attribute 'websocket'``.
  * ``_reset_global_config`` — snapshots/restores the tofu_search global
    ``SearchConfig`` singleton around every test so ``configure()`` mutations
    don't leak between tests.

Design notes for the API client family:
  * Each session gets a fresh, isolated SQLite DB via ``TOFU_DB_PATH`` → no
    PostgreSQL required, no cross-test contamination.
  * The app is imported lazily AFTER env-vars are set so
    ``lib.database._core`` picks SQLite at import time.
  * Default auth mode is ``open`` (the production default) so client tests
    act as an authenticated local principal without plumbing a token. A test
    that needs the credential gate marks itself ``@pytest.mark.auth_mode(
    "private")``; the ``_auth_mode_override`` fixture applies + restores it.
"""

from __future__ import annotations

import os
import tempfile

import pytest

import tofu_search.config as _config


# ─── Module-load: shim werkzeug.__version__ if missing ────────────────
#
# Werkzeug 3.x dropped the module-level ``werkzeug.__version__`` that older
# Flask checkouts (e.g. an editable Flask 2.3.0.dev0 pinned by a swebench
# workspace) still read from ``flask.testing`` / ``flask.helpers``. Without
# it, ``app.test_client()`` raises ``AttributeError`` before any test runs.
# Populate it from package metadata; no-op when already present.
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


# ─── Module-load: install the Flask→Quart shim BEFORE collection ──────
#
# pytest imports this conftest before it collects any test module. Several
# test files do top-level ``from routes... import X`` / ``import lib...``
# which transitively hits ``routes/push.py``'s ``@push_bp.websocket`` — an
# attribute that only exists once ``server._install_flask_shim()`` has
# pointed ``sys.modules['flask']`` at Quart. Installing the shim here (at
# conftest import) makes every test file's module-level imports safe,
# regardless of collection order, without forcing the full app build (that
# stays lazy in the ``flask_app`` fixture). The shim is idempotent, so the
# later ``import server`` re-runs it harmlessly.
def _install_shim_for_collection():
    # A plain ``import server`` runs ``_install_flask_shim()`` at server.py's
    # module top and caches the module in ``sys.modules``, so the ``import
    # server`` inside the ``flask_app`` fixture is a no-op (no double app
    # build). We must set the SQLite env BEFORE this import so the DB layer
    # picks the right backend (mirrors _configure_test_env's setdefaults,
    # which haven't run yet at conftest-import time).
    import os as _os
    _os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
    _os.environ.setdefault('TRADING_ENABLED', '0')
    _os.environ.setdefault('PPTX_TRANSLATE_ENABLED', '0')
    try:
        import server  # noqa: F401 — side-effect: installs Flask→Quart shim
    except Exception as _e:  # never block collection on the shim probe
        import sys as _sys
        _sys.stderr.write(f'[conftest] shim pre-install skipped: {_e}\n')


_install_shim_for_collection()


# ─── tofu_search global-config isolation (pre-existing) ───────────────
@pytest.fixture(autouse=True)
def _reset_global_config():
    """Snapshot and restore the global SearchConfig around every test.

    configure() mutates a process-global singleton; without this an early
    test could leak settings into a later one.
    """
    saved = _config._global_config
    _config._global_config = _config.SearchConfig()
    try:
        yield
    finally:
        _config._global_config = saved


# ─── Session-level: one SQLite DB per pytest run ──────────────────────
@pytest.fixture(scope="session", autouse=True)
def _configure_test_env():
    """Set env vars BEFORE importing the Flask app so the DB layer picks
    SQLite and isolates data to a temp file.
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
    # Default to the production 'open' mode so client tests act as an
    # authenticated local principal. Gate tests opt into stricter behavior
    # with @pytest.mark.auth_mode("private") (see _auth_mode_override).
    os.environ.setdefault("TOFU_AUTH_MODE", "open")

    yield

    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


# ─── Session-level: build the Flask (Quart-shim) app once ─────────────
@pytest.fixture(scope="session")
def flask_app(_configure_test_env):
    """Import and return ``server.app`` AFTER env-vars are set.

    Importing ``server`` installs the Flask→Quart shim and constructs the
    full blueprint stack exactly once per session.
    """
    import server  # noqa: F401 — import side-effect installs shim + builds app
    from server import app

    app.config.update(TESTING=True)
    return app


# ─── Per-test auth-mode override via marker ───────────────────────────
def pytest_configure(config):
    """Register the ``auth_mode`` marker so ``--strict-markers`` is happy."""
    config.addinivalue_line(
        'markers',
        'auth_mode(mode): override TOFU_AUTH_MODE for this test '
        '(open / private / multi-user). Restored after the test.',
    )


@pytest.fixture(autouse=True)
def _auth_mode_override(request):
    """Snapshot/restore ``TOFU_AUTH_MODE`` around EVERY test, and apply an
    optional ``@pytest.mark.auth_mode("...")`` override.

    Two jobs:
      1. If the test is marked, set the requested mode for its duration.
      2. Regardless of marking, snapshot the env var on entry and restore it
         on exit. This makes the suite leak-PROOF: a test whose own teardown
         forgets to reset the mode (or hardcodes the wrong one) can no longer
         poison every downstream test in the session. The auth_mode cache is
         cleared on both ends so the resolver re-reads the restored value.
    """
    snapshot = os.environ.get('TOFU_AUTH_MODE')

    def _reset():
        try:
            from lib.auth_mode import reset_for_tests
            reset_for_tests()
        except Exception:
            pass

    marker = request.node.get_closest_marker('auth_mode')
    if marker is not None:
        os.environ['TOFU_AUTH_MODE'] = marker.args[0] if marker.args else 'open'
        _reset()
    try:
        yield
    finally:
        if snapshot is None:
            os.environ.pop('TOFU_AUTH_MODE', None)
        else:
            os.environ['TOFU_AUTH_MODE'] = snapshot
        _reset()


# ─── Sync adapter over the async Quart test client ────────────────────
#
# The app is Quart (via the Flask→Quart shim), so ``app.test_client()`` is a
# ``QuartClient`` whose ``.get()/.post()/...`` are COROUTINES and whose
# response ``.get_json()/.get_data()`` are async too. The API integration
# tests, however, are written in the legacy SYNC Flask style
# (``resp = flask_client.get(...); resp.status_code; resp.get_json()`` with no
# ``await``). Rather than rewrite ~40 tests, we wrap the async client so each
# call drives the coroutine to completion on a private event loop and returns
# a response object exposing sync ``.status_code / .headers / .data /
# .get_json()``. This IS the "sync-adapted test client" the suite was always
# documented to use.
def _run_coro(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _SyncResponse:
    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = resp.headers

    @property
    def data(self):
        return _run_coro(self._resp.get_data())

    def get_data(self, as_text=False):
        raw = _run_coro(self._resp.get_data())
        if as_text and isinstance(raw, (bytes, bytearray)):
            return raw.decode('utf-8', 'replace')
        return raw

    def get_json(self):
        return _run_coro(self._resp.get_json())


class _SyncClient:
    """Sync facade over QuartClient for legacy ``flask_client`` tests."""

    _METHODS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options')

    def __init__(self, qclient):
        self._c = qclient

    @staticmethod
    def _encode_path(args):
        # Quart's test client encodes the raw query string as ASCII
        # (quart/testing/utils.py), so a non-ASCII inline query like
        # ``/x?q=搜索引擎`` raises UnicodeEncodeError — whereas the legacy Flask
        # client percent-encoded it. Replicate that: percent-encode the query
        # portion of a positional path so existing tests that inline unicode
        # in the URL keep working. (Tests passing ``query_string=`` are
        # unaffected.)
        if not args or not isinstance(args[0], str) or '?' not in args[0]:
            return args
        from urllib.parse import quote
        path, _, query = args[0].partition('?')
        enc = '&'.join(
            (quote(k, safe='') + '=' + quote(v, safe=''))
            if '=' in pair else quote(pair, safe='')
            for pair in query.split('&')
            for k, _, v in [pair.partition('=')]
        )
        return (path + '?' + enc,) + tuple(args[1:])

    def __getattr__(self, name):
        if name in self._METHODS:
            def _call(*args, **kwargs):
                args = self._encode_path(args)
                return _SyncResponse(
                    _run_coro(getattr(self._c, name)(*args, **kwargs)))
            return _call
        return getattr(self._c, name)


# ─── Function-level: fresh test client per test ───────────────────────
@pytest.fixture()
def flask_client(flask_app):
    """Return a sync-adapted test client with its own cookie jar (per test)."""
    return _SyncClient(flask_app.test_client())
