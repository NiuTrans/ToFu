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

import json
import logging
import os
import tempfile

import pytest

import tofu_search.config as _config

_conftest_logger = logging.getLogger('tests.conftest')


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
    # Shrink the bridge long-poll window so poll-route tests don't each block
    # the full production 8s (see lib/browser/queue.POLL_WAIT_TIMEOUT).
    _os.environ.setdefault('TOFU_BROWSER_POLL_WAIT', '0.2')
    _os.environ.setdefault('TOFU_DESKTOP_POLL_WAIT', '0.2')
    # Never start the real background scheduler / timer-resume threads in the
    # test process — they run live LLM polls + web searches against the
    # shared DB, stealing CPU/IO and making timing-sensitive tests flaky.
    _os.environ.setdefault('TOFU_DISABLE_SCHEDULER', '1')
    try:
        import server  # noqa: F401 — side-effect: installs Flask→Quart shim
    except Exception as _e:  # never block collection on the shim probe
        import sys as _sys
        _sys.stderr.write(f'[conftest] shim pre-install skipped: {_e}\n')


_install_shim_for_collection()


# ─── Module-load: make Quart's app_context() usable as a SYNC context ──
#
# Under the Flask→Quart shim ``app.app_context()`` returns a Quart
# ``AppContext`` that only implements ``__aenter__``/``__aexit__`` (async).
# A large family of sync-style tests (the ``tests/test_artifacts_*`` suite)
# wrap pure DB calls in ``with flask_app.app_context():`` — legacy Flask
# style. Under Quart that raises ``TypeError: 'AppContext' object does not
# support the context manager protocol`` and previously failed 50+ tests.
#
# The code those tests exercise (``lib/artifacts/*``) reads NO app/request
# globals, so a sync app context is semantically a no-op there. We wrap
# ``Quart.app_context`` in a dual-mode object:
#   * sync  ``with``  → null context (yields the app; pushes nothing)
#   * async ``async with`` → delegates to the genuine AppContext, so the
#     route/E2E tests that legitimately ``async with app.app_context()``
#     (test_branch_routes, test_sdk_parity_e2e) keep their real context.
def _install_sync_app_context_shim():
    # Add sync ``__enter__``/``__exit__`` DIRECTLY to Quart's AppContext class
    # rather than wrapping it — Quart's own request dispatch calls
    # ``app.app_context().push()`` on the real object, so a wrapper that hides
    # ``push``/``pop`` breaks live route handling. The async protocol
    # (``__aenter__``/``__aexit__``, used by route/E2E tests and Quart
    # internals) is left untouched; we only ADD the sync protocol, which is a
    # null context (the artifacts/DB code under test reads no app globals).
    try:
        from quart.ctx import AppContext
    except Exception as _e:  # pragma: no cover — quart always present in tests
        import sys as _sys
        _sys.stderr.write(f'[conftest] app_context shim skipped: {_e}\n')
        return
    if getattr(AppContext, '_tofu_sync_ctx', False):
        return  # idempotent

    def __enter__(self):
        return self.app

    def __exit__(self, *exc):
        return False

    AppContext.__enter__ = __enter__
    AppContext.__exit__ = __exit__
    AppContext._tofu_sync_ctx = True


_install_sync_app_context_shim()


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
    os.environ.setdefault("TOFU_BROWSER_POLL_WAIT", "0.2")
    os.environ.setdefault("TOFU_DESKTOP_POLL_WAIT", "0.2")
    os.environ.setdefault("TOFU_DISABLE_SCHEDULER", "1")
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
    """Register custom markers so ``--strict-markers`` is happy."""
    config.addinivalue_line(
        'markers',
        'auth_mode(mode): override TOFU_AUTH_MODE for this test '
        '(open / private / multi-user). Restored after the test.',
    )


# ─── Tier-marker safety net ───────────────────────────────────────────
#
# The suite selects tiers by marker: ``make test-unit`` runs ``-m unit``,
# ``make test-api`` runs ``-m api``, and ``make ci`` runs unit+api. A test
# with NO tier marker (unit / api / visual / slow / live_llm) is therefore
# collected by ``make test-all`` but SILENTLY SKIPPED by every standard CI
# target — so a broken unmarked test can rot undetected. Historically ~58%
# of the suite was unmarked.
#
# This hook closes that gap: any test missing a tier marker is auto-tagged
# ``unit`` so it lands in the default CI tiers, and the set of offending
# FILES is reported once as a warning (so the omission stays visible and
# authors are nudged to add the right marker — api/visual/slow where the
# default ``unit`` is wrong). New unmarked tests can never again vanish
# from CI.
_TIER_MARKERS = frozenset({'unit', 'api', 'visual', 'slow', 'live_llm'})


def pytest_collection_modifyitems(config, items):
    auto_marked_files = set()
    for item in items:
        own = {m.name for m in item.iter_markers()}
        if own & _TIER_MARKERS:
            continue
        item.add_marker(pytest.mark.unit)
        if item.nodeid:
            auto_marked_files.add(item.nodeid.split('::', 1)[0])
    if auto_marked_files:
        config.issue_config_time_warning(
            UserWarning(
                f'{len(auto_marked_files)} test file(s) had tests without a '
                f'tier marker (unit/api/visual/slow/live_llm); auto-tagged '
                f'them "unit" so they run in make test-unit / ci. Add an '
                f'explicit marker to silence this: '
                f'{", ".join(sorted(auto_marked_files))}'),
            stacklevel=1,
        )


# Session baseline for TOFU_AUTH_MODE, captured ONCE after _configure_test_env
# ran its setdefault('open'). Every test is forced back to THIS value on
# teardown — not to a live snapshot — so a unittest class whose setUpClass
# mutates the env to 'private' (those hooks run OUTSIDE the per-test fixture
# window, so a snapshot would capture the already-polluted value) can never
# leak 'private' into a later test that assumes the open default.
_AUTH_MODE_BASELINE = os.environ.get('TOFU_AUTH_MODE', 'open')


@pytest.fixture(autouse=True)
def _auth_mode_override(request):
    """Force every test to START from + END at the session baseline
    ``TOFU_AUTH_MODE``, and apply an optional ``@pytest.mark.auth_mode("...")``
    override for the test's duration.

    This makes auth-mode isolation leak-PROOF against the self-contained
    ``unittest.TestCase`` files that set the env in ``setUpClass`` (whose
    timing interleaves badly with per-test fixtures): regardless of what a
    prior class left in the env, this test is reset to the baseline on entry,
    the marker (if any) applies on top, and the baseline is re-asserted on
    exit. The auth_mode cache is cleared on every transition so the resolver
    re-reads the env.
    """
    def _reset():
        try:
            from lib.auth_mode import reset_for_tests
            reset_for_tests()
        except Exception:
            pass

    def _set_baseline():
        if _AUTH_MODE_BASELINE is None:
            os.environ.pop('TOFU_AUTH_MODE', None)
        else:
            os.environ['TOFU_AUTH_MODE'] = _AUTH_MODE_BASELINE

    # A test-method-level ``auth_mode`` marker takes effect for this test.
    # We do NOT force the baseline on ENTRY: a ``unittest`` class may have
    # set its own mode in ``setUpClass`` (which runs before this fixture),
    # and that intent must stand for the class's tests. We ONLY restore the
    # baseline on EXIT — that's what makes the suite leak-proof, because a
    # class that mutates the env without restoring can no longer poison the
    # next test.
    marker = request.node.get_closest_marker('auth_mode')
    if marker is not None:
        os.environ['TOFU_AUTH_MODE'] = marker.args[0] if marker.args else 'open'
        _reset()
    try:
        yield
    finally:
        _set_baseline()
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

    def get_json(self, silent=False):
        # Quart's Response.get_json takes no 'silent' kwarg (Flask's did);
        # accept + swallow it so legacy sync-style tests keep working.
        try:
            return _run_coro(self._resp.get_json())
        except Exception:
            if silent:
                return None
            raise


class _SyncClient:
    """Sync facade over QuartClient for legacy ``flask_client`` tests."""

    _METHODS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'open')

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
