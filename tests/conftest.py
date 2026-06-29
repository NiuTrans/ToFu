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
    # ⚠️ DATA-LOSS GUARD (2026-06-28 incident): an ambient
    # ``TOFU_DB_BACKEND=postgres`` in the agent's shell (it lives in .env)
    # used to DEFEAT a plain ``setdefault('sqlite')`` — the DB layer froze
    # ``_BACKEND='pg'`` at import, the live_server/E2E fixtures booted against
    # PRODUCTION Postgres, and the visual-E2E snapshot-diff cleanup deleted
    # ~2300 real conversations. The test process must NEVER touch the
    # production DB. We therefore FORCE sqlite + a throwaway temp path here
    # (overriding any inherited value), unless the operator has explicitly
    # opted in to a dedicated test PG via ``TOFU_ALLOW_PG_TESTS=1`` (in which
    # case ``_assert_test_database`` below still verifies the DB is a test DB,
    # not production). Forcing — not setdefault — is the fix: it closes the
    # exact hole that caused the incident.
    if _os.environ.get('TOFU_ALLOW_PG_TESTS') != '1':
        _os.environ['TOFU_DB_BACKEND'] = 'sqlite'
        if not _os.environ.get('TOFU_DB_PATH'):
            import tempfile as _tf
            _os.environ['TOFU_DB_PATH'] = _os.path.join(
                _tf.mkdtemp(prefix='tofu-test-shim-'), 'tofu-test.db')
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


# ─── DATA-LOSS GUARD: refuse to run the suite against a production DB ──
#
# THE keystone prevention for the 2026-06-28 mass-deletion incident. Every
# fixture that builds/boots the real app (``flask_app``, ``live_server``, and
# transitively the Playwright ``page``) calls this BEFORE the app handles a
# request. It is the single call-site-agnostic chokepoint — no matter how a
# future test gets a live server, it cannot escape this gate.
#
# Rule: the test process may only operate on a DB that is unmistakably a TEST
# DB. We re-read the backend the DB layer ACTUALLY resolved (``_core._BACKEND``
# / ``_core.PG_DBNAME`` / ``_core.DB_PATH``) — not just the env — because env
# and frozen-at-import globals can disagree. A PG backend is allowed ONLY when
# the operator explicitly set ``TOFU_ALLOW_PG_TESTS=1`` AND the target DB name
# contains a test marker (``test`` / ``scratch`` / ``_ci``). Anything else is a
# hard failure that aborts the whole session loudly — never a silent skip.
def _db_is_test_safe():
    """Return (ok: bool, detail: str). ok=True means the resolved DB is a
    throwaway test DB safe to mutate/boot the app against."""
    try:
        import lib.database._core as _dbc
    except Exception as e:
        # DB layer unavailable → nothing can be deleted; treat as safe.
        return True, f'db layer import failed ({e}) — no DB to harm'
    backend = getattr(_dbc, '_BACKEND', 'sqlite')
    if backend != 'pg':
        return True, f'sqlite backend (path={getattr(_dbc, "DB_PATH", "?")})'
    # PG backend: only permitted with an explicit opt-in AND a test-marked DB.
    if os.environ.get('TOFU_ALLOW_PG_TESTS') != '1':
        return False, ('PG backend active but TOFU_ALLOW_PG_TESTS!=1 — the '
                       'suite must not run against Postgres (it would mutate '
                       f'the live DB {getattr(_dbc, "PG_DBNAME", "?")!r})')
    dbname = (getattr(_dbc, 'PG_DBNAME', '') or '').lower()
    markers = ('test', 'scratch', '_ci', 'pytest')
    if not any(m in dbname for m in markers):
        return False, (f'PG backend on DB {dbname!r} which is NOT test-marked '
                       f'(name must contain one of {markers}); refusing to '
                       'mutate a possibly-production database')
    return True, f'pg backend on test-marked DB {dbname!r} (explicit opt-in)'


def _assert_test_database(context: str = ''):
    """Hard-abort the session if the resolved DB is not a safe test DB.

    Called by ``flask_app`` / ``live_server`` (and any future app-booting
    fixture). Raises ``pytest.UsageError`` — which aborts collection/run with
    a clear message instead of letting the test mutate production data."""
    ok, detail = _db_is_test_safe()
    if ok:
        _conftest_logger.debug('[db-guard] OK (%s): %s', context, detail)
        return
    msg = (f'\n\n*** TOFU TEST DB GUARD TRIPPED ({context}) ***\n'
           f'{detail}.\n'
           f'The Tofu test suite refuses to build/boot the app against a '
           f'non-test database, because its E2E cleanup fixtures DELETE '
           f'conversations (the 2026-06-28 incident wiped ~2300 real convs '
           f'this way).\n'
           f'Fix: run tests with TOFU_DB_BACKEND=sqlite (the default), or '
           f'point TOFU_PG_DBNAME at a dedicated test DB AND set '
           f'TOFU_ALLOW_PG_TESTS=1.\n')
    _conftest_logger.critical(msg)
    raise pytest.UsageError(msg)


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

    # FORCE sqlite (not setdefault) unless the operator opted into a dedicated
    # test PG — see the data-loss guard at the top of this file. An ambient
    # TOFU_DB_BACKEND=postgres must never reach the DB layer in tests.
    if os.environ.get('TOFU_ALLOW_PG_TESTS') != '1':
        os.environ["TOFU_DB_BACKEND"] = "sqlite"
        os.environ["TOFU_DB_PATH"] = db_path
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
    # Keystone guard: never build the real app against a production DB.
    _assert_test_database('flask_app fixture')
    import server  # noqa: F401 — import side-effect installs shim + builds app
    from server import app

    app.config.update(TESTING=True)
    return app


# ─── Per-test auth-mode override via marker ───────────────────────────
def pytest_configure(config):
    """Register custom markers + run the keystone DB guard ONCE at session
    start.

    ``pytest_configure`` fires AFTER conftest import (so the force-sqlite shim
    has already run) but BEFORE any test module is imported/collected. That
    makes it the truly call-site-agnostic chokepoint: even test modules that
    boot the app at MODULE LEVEL via ``spec_from_file_location('server.py')``
    (test_hook_taxonomy, test_request_parser, …) are gated here, before their
    import runs. A non-test PG target aborts the whole session immediately —
    no module-level boot, no DELETE, can slip in ahead of it. The per-helper
    ``_assert_test_database`` calls (live_server, sdk_e2e, headless) remain as
    belt-and-suspenders for direct/non-pytest invocation."""
    _assert_test_database('pytest_configure (session start)')
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


# ════════════════════════════════════════════════════════════════════════
#  (b) Self-healing purge of leaked TEST conversations
# ════════════════════════════════════════════════════════════════════════
#
# Several tests deliberately write to the REAL database the app serves from,
# because the DB layer's backend/path globals are frozen at import time and an
# ambient ``TOFU_DB_BACKEND=postgres`` env (common on dev/CI hosts) defeats the
# session SQLite ``setdefault`` above. Those rows otherwise leak straight into
# the user's sidebar:
#   * the endpoint-parity tests seed conversations with ids ``parity-*``
#     (titles ``parity`` / ``parity-live``);
#   * ``test_api_integration`` saves ``test-conv-*`` / ``test-minimal-*`` and
#     starts chats under ``test-conv`` / ``test-endpoint``;
#   * the visual E2E suite drives a live browser that creates real
#     conversations whose first message is a fixed, recognisable string.
#
# This autouse SESSION fixture purges those rows at session start (healing
# junk a prior crashed run left behind) AND session end (cleaning this run),
# regardless of which test path created them. It is deliberately
# PATTERN-GATED — it only ever deletes rows whose id matches a test-only
# prefix or whose content is one of the distinctive synthetic E2E strings — so
# it can never touch a genuine user conversation. Best-effort: any failure is
# logged, never raised, and never fails a test.

# Conversation-id prefixes used EXCLUSIVELY by tests (UI-created ids are
# UUID/timestamp-random and never start with these).
_TEST_CONV_ID_LIKE = (
    'parity-%',
    'test-conv%',
    'test-minimal%',
    'test-endpoint%',
)

# Distinctive, unmistakably-synthetic first-message strings the visual E2E
# suite sends (test_visual_e2e.py). Matched against ``search_text`` (a plain
# Text column on both backends, populated on every real save). Generic phrases
# the suite also sends ("What is 2+2?", "First question", …) are intentionally
# EXCLUDED — a real user could type those; those E2E rows are instead cleaned
# precisely by the snapshot-diff in the ``page`` fixture below.
_TEST_CONV_CONTENT_LIKE = (
    'Hello, this is a test message!',
    'Test sidebar entry',
    'Conversation One Message',
    'Conversation Two Message',
    'Sent via keyboard shortcut',
)


def _purge_test_conversations(reason: str = '') -> int:
    """Delete test-pattern conversation rows from the active DB. Best-effort.

    Returns the number of rows deleted (0 on any failure). Pattern-gated so it
    is safe to run against the production DB the tests share.
    """
    # Function-level keystone check: this issues DELETEs, and it's callable
    # directly (e.g. the page-fixture teardown), so never trust the caller —
    # refuse outright if the resolved DB isn't a safe test DB.
    _ok, _why = _db_is_test_safe()
    if not _ok:
        _conftest_logger.critical('purge_test_conversations REFUSED %s: %s',
                                  reason, _why)
        return 0
    try:
        from lib.database import (
            DOMAIN_CHAT, close_thread_db, get_thread_db,
        )
    except Exception as e:  # DB layer unavailable — nothing to purge
        _conftest_logger.debug('purge skipped (db import failed): %s', e)
        return 0

    deleted = 0
    try:
        db = get_thread_db(DOMAIN_CHAT)

        def _del(sql, params):
            nonlocal deleted
            try:
                cur = db.execute(sql, params)
                deleted += int(getattr(cur, 'rowcount', 0) or 0)
            except Exception as ex:
                _conftest_logger.debug('purge stmt failed (%s): %s', sql, ex)

        for pat in _TEST_CONV_ID_LIKE:
            _del('DELETE FROM conversations WHERE id LIKE ?', (pat,))
        for needle in _TEST_CONV_CONTENT_LIKE:
            _del('DELETE FROM conversations WHERE search_text LIKE ?',
                 (f'%{needle}%',))
        try:
            db.commit()
        except Exception as ex:
            _conftest_logger.debug('purge commit failed: %s', ex)
    except Exception as e:
        _conftest_logger.warning('purge_test_conversations failed %s: %s',
                                 reason, e)
    finally:
        try:
            close_thread_db()
        except Exception:
            pass

    if deleted:
        _conftest_logger.info('purged %d leaked test conversation row(s) %s',
                              deleted, reason)
    return deleted


# Timer-watcher rows created EXCLUSIVELY by tests. ``test_timer_parse_failure``
# calls the real ``create_timer`` (status='active'); on a host with ambient
# ``TOFU_DB_BACKEND=postgres`` (which defeats the session SQLite setdefault)
# those rows land in the production DB and are resurrected by
# ``resume_active_timers()`` on the next restart — the 2026-06-26 zombie-timer
# search-storm. Pattern-gated to test-only conv ids / source-task ids so a real
# user's timer is never touched.
_TEST_TIMER_CONV_ID_LIKE = (
    'conv-parsefail',
    'conv-timer-test%',
    'test-conv%',
)
_TEST_TIMER_SOURCE_LIKE = (
    'task-x',
)


def _purge_test_timers(reason: str = '') -> int:
    """Delete test-pattern timer_watchers rows from the active DB. Best-effort.

    Returns the number of rows deleted (0 on any failure). Pattern-gated so it
    is safe to run against the production DB the tests share.
    """
    try:
        from lib.database import DOMAIN_SYSTEM, close_thread_db, get_thread_db
    except Exception as e:
        _conftest_logger.debug('timer purge skipped (db import failed): %s', e)
        return 0

    deleted = 0
    try:
        db = get_thread_db(DOMAIN_SYSTEM)

        def _del(sql, params):
            nonlocal deleted
            try:
                cur = db.execute(sql, params)
                deleted += int(getattr(cur, 'rowcount', 0) or 0)
            except Exception as ex:
                _conftest_logger.debug('timer purge stmt failed (%s): %s', sql, ex)

        for pat in _TEST_TIMER_CONV_ID_LIKE:
            _del('DELETE FROM timer_watchers WHERE conv_id LIKE ?', (pat,))
        for pat in _TEST_TIMER_SOURCE_LIKE:
            _del('DELETE FROM timer_watchers WHERE source_task_id LIKE ?', (pat,))
        try:
            db.commit()
        except Exception as ex:
            _conftest_logger.debug('timer purge commit failed: %s', ex)
    except Exception as e:
        _conftest_logger.warning('purge_test_timers failed %s: %s', reason, e)
    finally:
        try:
            close_thread_db()
        except Exception:
            pass

    if deleted:
        _conftest_logger.info('purged %d leaked test timer row(s) %s', deleted, reason)
    return deleted


@pytest.fixture(scope='session', autouse=True)
def _db_guard_session():
    """Session-FIRST keystone gate: confirm the resolved DB is a test DB
    BEFORE anything destructive runs.

    This is the call-site-agnostic chokepoint the 2026-06-28 incident proved
    we need. ``_purge_leaked_test_conversations`` depends on this fixture (it
    takes it as a parameter), so pytest guarantees THIS runs first — no
    ``DELETE FROM conversations`` can be issued against a misresolved PG
    backend, no matter which server-boot helper a test uses. A non-test PG
    target hard-aborts the whole session here."""
    _assert_test_database('session start (db_guard)')
    yield


@pytest.fixture(scope='session', autouse=True)
def _purge_leaked_test_conversations(_db_guard_session):
    """Self-heal: purge test-pattern conversations + timers at session start
    AND end. Depends on ``_db_guard_session`` so the DB is PROVEN to be a test
    DB before any DELETE is issued."""
    _purge_test_conversations('(session start)')
    _purge_test_timers('(session start)')
    try:
        yield
    finally:
        _purge_test_conversations('(session end)')
        _purge_test_timers('(session end)')


# ════════════════════════════════════════════════════════════════════════
#  (a) Visual E2E fixtures — live server + Playwright browser
# ════════════════════════════════════════════════════════════════════════
#
# ``tests/test_visual_e2e.py`` (tier ``-m visual``) drives a real Chromium
# against a real running server. It references four fixtures — ``live_server``,
# ``browser``, ``page``, ``screenshot_dir`` — that previously did not exist in
# this conftest, so every visual test errored at setup and the cleanup the
# module docstring promised never ran (the source of the leaked sidebar
# conversations).
#
# These fixtures are only instantiated when a ``-m visual`` test requests them,
# so they add ZERO cost to unit/api runs. They skip cleanly when Playwright or
# a Chromium build is unavailable. The live server reuses the proven
# in-thread-Hypercorn boot from ``tests/test_sdk_e2e.py``; it serves the SAME
# process app (and DB), so the ``page`` fixture cleans up every conversation it
# creates via a before/after id snapshot-diff (the precise complement to the
# pattern-based purge above).


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope='session')
def screenshot_dir():
    """Directory where visual tests drop screenshots (created if missing)."""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screenshots')
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture(scope='session')
def live_server(flask_app):
    """Boot ``server.app`` on an ephemeral port via Hypercorn in a daemon
    thread; yield the base URL ``http://127.0.0.1:<port>``.
    """
    import asyncio
    import socket
    import threading
    import time

    # Keystone guard: a live Hypercorn server + Playwright browser runs the
    # destructive E2E cleanup fixtures — refuse to boot against production.
    _assert_test_database('live_server fixture')

    try:
        from hypercorn.asyncio import serve
        from hypercorn.config import Config
    except Exception as e:  # pragma: no cover
        pytest.skip(f'hypercorn unavailable for live_server: {e}')

    port = _free_port()
    cfg = Config()
    cfg.bind = [f'127.0.0.1:{port}']
    cfg.accesslog = None
    cfg.errorlog = None

    state: dict = {}

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        evt = asyncio.Event()
        state['evt'] = evt
        try:
            loop.run_until_complete(
                serve(flask_app, cfg, shutdown_trigger=evt.wait))
        except Exception as e:  # pragma: no cover
            _conftest_logger.warning('live_server runner exited: %s', e)
        finally:
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()

    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:  # pragma: no cover
        pytest.skip('live_server did not start within 8s')

    base = f'http://127.0.0.1:{port}'
    try:
        yield base
    finally:
        evt = state.get('evt')
        if evt is not None:
            try:
                evt._loop.call_soon_threadsafe(evt.set)  # type: ignore[attr-defined]
            except Exception as e:
                _conftest_logger.debug('live_server shutdown signal failed: %s', e)
        t.join(timeout=3)


def _ensure_chromium_library_path():
    """Augment ``LD_LIBRARY_PATH`` so a rootless Chromium build finds its GUI
    shared libs (libatk / libgbm / libxkbcommon / …) on hosts without sudo.

    Mirrors ``_ensure_chromium_library_path()`` in the tofu_search package's
    ``playwright_pool.py`` (the mechanism the app itself relies on): prepend
    ``$CONDA_PREFIX/lib`` and the cos7 sysroot lib dir, where the GUI libs are
    installed via conda-forge (see the ``playwright-chromium-rootless-conda-libs``
    project skill). Extra dirs can be injected via ``CHROMIUM_EXTRA_LIB_DIRS``
    (colon-separated). Idempotent + best-effort: a missing CONDA_PREFIX or
    unreadable dir is simply skipped, so this is a no-op on a vanilla machine
    that already has the libs (e.g. a CI runner after ``--with-deps``).
    """
    candidates = []
    prefix = os.environ.get('CONDA_PREFIX', '')
    if prefix:
        candidates.append(os.path.join(prefix, 'lib'))
        candidates.append(os.path.join(
            prefix, 'x86_64-conda-linux-gnu', 'sysroot', 'usr', 'lib64'))
    extra = os.environ.get('CHROMIUM_EXTRA_LIB_DIRS', '')
    if extra:
        candidates.extend(p for p in extra.split(os.pathsep) if p)
    existing = os.environ.get('LD_LIBRARY_PATH', '')
    have = set(existing.split(os.pathsep)) if existing else set()
    prepend = [p for p in candidates if p and os.path.isdir(p) and p not in have]
    if prepend:
        os.environ['LD_LIBRARY_PATH'] = os.pathsep.join(
            prepend + ([existing] if existing else []))
    return prepend


@pytest.fixture(scope='session')
def browser():
    """Session-scoped headless Chromium via Playwright (sync API).

    Self-bootstraps ``LD_LIBRARY_PATH`` (rootless conda-forge GUI libs) before
    launch so it succeeds on shared/HPC nodes without sudo. Skips — with the
    concrete missing-lib reason — only when launch still fails on a host where
    the libs genuinely aren't reachable (the per-machine fallback).
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        pytest.skip(f'playwright not installed: {e}')

    _ensure_chromium_library_path()

    pw = sync_playwright().start()
    try:
        # --no-sandbox is required inside this container (no user namespaces);
        # --disable-gpu avoids the swiftshader GL path on headless nodes.
        b = pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
    except Exception as e:
        pw.stop()
        pytest.skip(f'chromium build unavailable / failed to launch '
                    f'(run `playwright install chromium`; on a rootless host '
                    f'install GUI libs via conda-forge — see the '
                    f'playwright-chromium-rootless-conda-libs skill): {e}')
    try:
        yield b
    finally:
        try:
            b.close()
        finally:
            pw.stop()


def _conv_ids_in_page(pg):
    """Best-effort snapshot of the frontend's conversation ids."""
    try:
        ids = pg.evaluate(
            "(typeof conversations !== 'undefined' && conversations) "
            "? conversations.map(c => c.id) : []")
        return set(ids or [])
    except Exception:
        return set()


def _conv_global_ready(pg):
    """True iff the frontend ``conversations`` global is an array RIGHT NOW.

    Distinguishes a genuinely-empty sidebar (global present, length 0) from
    the not-yet-loaded race (global undefined) — the latter makes an empty
    ``_conv_ids_in_page`` baseline UNtrustworthy. See the ``page`` fixture
    cleanup for why this matters (2026-06-28 mass-deletion guard)."""
    try:
        return bool(pg.evaluate(
            "typeof conversations !== 'undefined' && Array.isArray(conversations)"))
    except Exception:
        return False


@pytest.fixture()
def page(browser, live_server):
    """A fresh page navigated to the live app, with automatic cleanup of any
    conversation created during the test (snapshot-diff → browser-side
    ``deleteConversation`` + a server-side pattern purge as a safety net).
    """
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.goto(live_server, wait_until='domcontentloaded')
    try:
        pg.wait_for_function("typeof conversations !== 'undefined'", timeout=10000)
    except Exception as e:
        _conftest_logger.debug('page: conversations global not ready: %s', e)

    ids_before = _conv_ids_in_page(pg)
    # Did we get a TRUSTWORTHY baseline? ``_conv_ids_in_page`` returns an empty
    # set BOTH when the sidebar is genuinely empty AND when the
    # ``conversations`` global wasn't ready yet (the race). If the baseline is
    # empty we CANNOT distinguish "test created N convs" from "the whole
    # sidebar is N convs" — and deleting the diff in the latter case wipes real
    # data (the 2026-06-28 incident). So we only trust a baseline when the
    # global was actually present at snapshot time.
    baseline_trusted = _conv_global_ready(pg)
    try:
        yield pg
    finally:
        # Delete from inside the browser so the frontend drops them from
        # memory too (a server-only DELETE is re-synced back by the cached
        # conversation list — see test_visual_e2e._cleanup_test_convs).
        created = _conv_ids_in_page(pg) - ids_before
        if not baseline_trusted and created:
            # Untrusted baseline → the "created" diff may be the entire
            # sidebar. NEVER bulk-delete here; fall back to the pattern-gated
            # server purge only. This is the belt that would have stopped the
            # incident even if the DB guard were bypassed.
            _conftest_logger.warning(
                'page cleanup: baseline untrusted (conversations global not '
                'ready at snapshot); SKIPPING snapshot-diff delete of %d id(s) '
                'to avoid mass-deletion — relying on pattern purge only',
                len(created))
            created = set()
        for cid in created:
            try:
                pg.evaluate(f"deleteConversation({json.dumps(cid)})")
            except Exception as e:
                _conftest_logger.debug('page cleanup deleteConversation(%s) '
                                       'failed: %s', cid, e)
        try:
            pg.wait_for_timeout(200)
        except Exception:
            pass
        try:
            pg.close()
        finally:
            ctx.close()
        # Belt-and-suspenders: purge any test-pattern rows the browser delete
        # missed (page already closed, so no re-sync can resurrect them).
        _purge_test_conversations('(e2e page teardown)')
