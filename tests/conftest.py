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
import re
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
        # ⚠️ XDIST ISOLATION: each ``-n`` worker is its OWN process that
        # re-imports this conftest, but inherits the controller's environment —
        # including whatever ``TOFU_DB_PATH`` the controller's shim already set.
        # ``lib.database._core`` freezes ``DB_PATH`` from this env var at import
        # (which happens right below via ``import server``), and the naive
        # ``if not TOFU_DB_PATH`` guard let every worker REUSE the inherited
        # controller path → all workers froze ``_core.DB_PATH`` to ONE shared
        # SQLite file and hammered it concurrently → "database is locked" /
        # "no such table". Key the path on ``PYTEST_XDIST_WORKER`` so each
        # worker gets its own file (and the controller/serial run gets one too).
        _worker = _os.environ.get('PYTEST_XDIST_WORKER', '')
        _existing = _os.environ.get('TOFU_DB_PATH', '')
        if (not _existing) or (_worker and _worker not in _existing):
            import tempfile as _tf
            _suffix = f'-{_worker}' if _worker else ''
            _os.environ['TOFU_DB_PATH'] = _os.path.join(
                _tf.mkdtemp(prefix=f'tofu-test-shim{_suffix}-'), 'tofu-test.db')
    # Never mlockall() in the test process. server.py pins its whole C-extension
    # working set (~340 MB) as UNRECLAIMABLE memory when the checkout sits on a
    # FUSE mount with a generous cgroup limit (the common dev/CI layout here).
    # Harmless for one long-lived server, but under `pytest -n auto` every xdist
    # worker re-imports server and pins its own copy → on a many-core box that's
    # a burst of tens of GB of pinned pages the kernel cannot reclaim, which
    # OOM-reaps the pod (taking any co-resident live server with it). Test
    # workers never serve FUSE-mmap'd requests under load, so pinning buys them
    # nothing. Force it off (overridable) BEFORE `import server` reads it.
    _os.environ.setdefault('TOFU_MLOCK', '0')
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
    # Never start the netpath prober thread in test processes either:
    # server.py calls start_prober() at import, and the daemon would fire
    # real network probes (through the env proxy) 10s later and write into
    # the production logs/app.log. netpath's own tests opt back in via
    # monkeypatch (tests/test_netpath.py).
    _os.environ.setdefault('TOFU_NETPATH', 'off')
    # ⚠️ LOG ISOLATION (2026-07-27, after app.log hit 9.1 GB in one day).
    # Everything else in this block is already isolated — DB, scheduler,
    # netpath, mlock — but logs were NOT, so the test process appended to the
    # REAL <repo>/logs/app.log. Measured: one test emitting a single
    # logger.error() + audit_log() grew the production app.log by 83 bytes and
    # landed its marker in app.log AND audit.log (four production file
    # handlers are attached to the root logger once `import server` runs).
    # The netpath comment right above even names this hazard out loud —
    # evidence the risk was known but only ever patched case-by-case.
    #
    # ORDERING IS LOAD-BEARING: lib/log.py computes LOG_DIR at IMPORT time
    # from _writable_base_dir(), which honours TOFU_DATA_DIR first. So this
    # must be set BEFORE the `import server` below — the same constraint that
    # governs TOFU_DB_PATH above. Keyed on PYTEST_XDIST_WORKER for the same
    # reason: each worker is its own process inheriting the controller's env.
    if not _os.environ.get('TOFU_DATA_DIR'):
        import tempfile as _tf_log
        _log_worker = _os.environ.get('PYTEST_XDIST_WORKER', '')
        _log_suffix = f'-{_log_worker}' if _log_worker else ''
        _os.environ['TOFU_DATA_DIR'] = _tf_log.mkdtemp(
            prefix=f'tofu-test-logs{_log_suffix}-')
    try:
        import server  # noqa: F401 — side-effect: installs Flask→Quart shim
    except Exception as _e:  # never block collection on the shim probe
        import sys as _sys
        _sys.stderr.write(f'[conftest] shim pre-install skipped: {_e}\n')

    # ─── HARD PATH LOCK (2026-07-26) ──────────────────────────────────
    # Force lib.database._core.DB_PATH to the session-specific temp path
    # immediately after the server import. This is a belt-and-suspenders
    # guard against any test or background thread that might try to
    # resolve the default data/tofu.db path, ensuring all test-side
    # DB writes are contained within the temporary test area.
    if _os.environ.get('TOFU_ALLOW_PG_TESTS') != '1':
        try:
            import lib.database._core as _dbc
            _dbc.DB_PATH = _os.environ['TOFU_DB_PATH']
            _dbc._BACKEND = 'sqlite'
        except Exception as _e:
            _sys.stderr.write(f'[conftest] hard path lock failed: {_e}\n')


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



# ─── Safety net: restore NC-patched source files a crashed test left dirty ──
#
# A family of "negative-control" tests physically PATCH a shipped source file
# on disk (``_patch_restore``: write a neutered variant → run → restore
# byte-identical in a ``finally``). If that test is KILLED mid-patch — a
# per-test timeout, an xdist worker crash, a KeyboardInterrupt — its ``finally``
# never runs and the shipped source is left in its NEUTERED state, which then
# fails EVERY later test that imports it (the corruption cascade that stuck
# ``_effective_status`` / ``pending_proposals`` in their NC forms). This
# autouse fixture is the belt: it snapshots each known NC-target source once,
# and after every test RESTORES any that differ from the snapshot — so a
# crashed patch can poison at most the one test that crashed, never the rest of
# the session (and never the working tree after the run). Cheap: a handful of
# small files, str-compared, only rewritten on a mismatch.
# Every shipped source that ANY negative-control test still byte-patches on
# disk (via a legacy ``_patch_restore`` or an inline ``open(..,'w')``). Keep
# this in sync with an audit of on-disk NC writers — a target NOT listed here
# is UNPROTECTED: a crashed patch leaves it poisoned for the rest of the
# session (this is exactly how ``_persist.py``'s vertical-relocation line was
# left neutered, cascading into every later importer). The durable fix is to
# migrate the NC to ``tests/_nc_harness.py`` (in-memory, never writes disk);
# this belt is the backstop for any not-yet-migrated on-disk NC.
_NC_GUARDED_SOURCES = (
    'lib/conversations/project_board.py',
    'lib/conversations/project_dispatch.py',
    'lib/conversations/project_charter.py',
    'lib/conversations/project_feed.py',
    'lib/conversations/project_brain_summary.py',
    'lib/conversations/project_brain_influence.py',
    'lib/conversations/project_peer.py',
    'lib/message_queue.py',
    'lib/tasks_pkg/compaction/_persist/_splitters.py',
    'lib/tools/conversation.py',
    'lib/scheduler/manager.py',
    'lib/project_mod/config.py',
    'routes/conversations.py',
    # Frontend: test_frontend_tofu_scene_pixeldiff.py byte-patches this shipped
    # source IN PLACE (record-freeze + pale-neuter) and restores in a finally.
    # If the node subprocess is SIGKILLed / times out, the finally is skipped
    # and the shipped file is left neutered — poisoning every later tofu-scene
    # test. It is the only UNPROTECTED in-place shipped-source writer, so it
    # belongs on the belt like the on-disk NC .py targets above.
    'static/js/tofu-scene.js',
    # Discovered by tests/test_nc_guard_registry.py (the self-enforcing
    # meta-guard) — each is byte-patched IN PLACE by an NC with a finally
    # restore, so they need the same crash-heal backstop:
    #   static/styles.css — test_memory_modal_specificity.py (CSS-cascade NC)
    #                        + test_mobile_tofu_touch_polish.py (touch-padding NC)
    #   lib/conversations/reconcile.py — test_orphan_resumable_classifier.py
    'static/styles.css',
    'lib/conversations/reconcile.py',
)
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_nc_source_snapshots: dict = {}
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_nc_source_snapshots: dict = {}

# NC poison signature: every on-disk NC patch embeds an ``NC-WORD`` marker in
# its replacement text (project convention — ``# NC-STORM``,
# ``pass  # NC-OBSERVE``, ``'nc-deny-forced'``, ``# NC-DISPATCH-HUMAN`` …).
# The belt heals ONLY when this matches (see restore_drifted_nc_sources).
_NC_POISON_RE = re.compile(r'(?i)\bNC-[A-Z0-9][A-Z0-9_-]{2,}')


def _snapshot_nc_sources():
    for rel in _NC_GUARDED_SOURCES:
        p = os.path.join(_ROOT_DIR, rel)
        try:
            with open(p, encoding='utf-8') as f:
                _nc_source_snapshots[p] = f.read()
        except OSError as e:
            _conftest_logger.debug('[nc-guard] snapshot skip %s: %s', rel, e)


def restore_drifted_nc_sources() -> list:
    """Rewrite any guarded source that drifted from the session snapshot back to
    byte-identical. Returns the list of relpaths it healed (empty when clean).

    Plain callable (not the fixture) so it can be driven directly by the belt's
    own regression test — the fixture body just delegates here in its finally.

    ★ MARKER GATE (2026-07-25, the "phantom reverter" incident): heal ONLY
    when the drifted bytes carry the NC poison signature (``NC-WORD`` — every
    on-disk NC patch embeds it in the replacement text by convention:
    ``# NC-STORM`` / ``pass  # NC-OBSERVE`` / ``'nc-deny-forced'`` …). A file
    that merely differs from the session-start baseline WITHOUT a marker is
    LEGITIMATE work — a commit landed mid-run, or sibling WIP — and must be
    left alone. Before this gate, a long suite on the shared tree silently
    un-wrote a real mid-run commit (lib/message_queue.py, 83c7f1ed) every
    per-test cadence for over an hour (strace: O_WRONLY|O_TRUNC from the
    pytest pid). A leftover neuter from a crashed patch ALWAYS carries the
    marker, so the crash-heal behaviour is unchanged.
    """
    healed = []
    for p, original in _nc_source_snapshots.items():
        try:
            with open(p, encoding='utf-8') as f:
                current = f.read()
            if current == original:
                continue
            if not _NC_POISON_RE.search(current):
                # Legit mid-run work (a commit / sibling WIP), not NC poison —
                # the belt has no mandate to touch it.
                _conftest_logger.debug(
                    '[nc-guard] drift without NC marker, leaving as-is '
                    '(legit mid-run work): %s', p)
                continue
            with open(p, 'w', encoding='utf-8') as f:
                f.write(original)
            rel = os.path.relpath(p, _ROOT_DIR)
            healed.append(rel)
            _conftest_logger.warning(
                '[nc-guard] restored NC-patched source left dirty by a '
                'test: %s', rel)
        except OSError as e:
            _conftest_logger.debug('[nc-guard] restore skip %s: %s', p, e)
    return healed


def warn_on_nc_source_poison_at_session_start() -> list:
    """Surface a guarded source that a PRIOR run may have left poisoned.

    The autouse fixture below heals WITHIN a session — but its ``finally`` only
    runs on normal teardown / exception / KeyboardInterrupt. A HARD crash
    (SIGKILL, OOM-killer, ``os._exit``, power loss) mid-patch skips it, leaving
    the shipped file NEUTERED on disk. Worse: the fixture snapshots LAZILY from
    the WORKING TREE on the first test, so the NEXT session would adopt that
    leftover neuter AS ITS BASELINE and heal nothing — the poison becomes
    permanent until a human notices (this is the recurring-"new bug" engine).

    We can't silently auto-restore, because in this shared-HEAD repo a guarded
    file may carry LEGITIMATE uncommitted sibling WIP (e.g. message_queue.py
    right now) — a blind ``git checkout`` would destroy it. So the safe move is
    DETECT + WARN LOUDLY, using git HEAD as the known-good oracle: at session
    start, list every guarded file that differs from HEAD so a human/CI sees
    "these may be poisoned from a prior aborted run — verify before trusting a
    green/red result". Returns the drifted relpaths (also emitted as a warning).
    Best-effort: silent no-op when git is unavailable (nothing to compare to)."""
    import subprocess
    drifted = []
    for rel in _NC_GUARDED_SOURCES:
        try:
            r = subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', rel],
                               cwd=_ROOT_DIR, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            _conftest_logger.debug('[nc-guard] HEAD-drift probe skip %s: %s', rel, e)
            continue
        if r.returncode == 1:  # 1 = differs; 0 = clean; other = git error
            drifted.append(rel)
    if drifted:
        msg = ('[nc-guard] SESSION START: %d guarded NC source(s) differ from '
               'git HEAD: %s. This is EITHER legitimate uncommitted WIP OR a '
               'leftover neuter from a HARD-CRASHED prior run (SIGKILL/OOM skips '
               'the restore finally). The belt will snapshot the CURRENT '
               '(possibly poisoned) bytes as its baseline, so verify these are '
               'intended before trusting this run. To clear a suspected poison: '
               'git checkout HEAD -- <file>.' % (len(drifted), ', '.join(drifted)))
        _conftest_logger.warning(msg)
    return drifted


@pytest.fixture(autouse=True)
def _restore_nc_patched_sources():
    """Restore any NC-target source file a test (or a crashed ``_patch_restore``)
    left byte-different from the session snapshot. Runs after every test."""
    if not _nc_source_snapshots:
        _snapshot_nc_sources()
    try:
        yield
    finally:
        restore_drifted_nc_sources()


# ─── Session-level: one SQLite DB per pytest run ──────────────────────
@pytest.fixture(scope="session", autouse=True)
def _configure_test_env():
    """Set env vars BEFORE importing the Flask app so the DB layer picks
    SQLite and isolates data to a temp file.
    """
    tmpdir = None

    # FORCE sqlite (not setdefault) unless the operator opted into a dedicated
    # test PG — see the data-loss guard at the top of this file. An ambient
    # TOFU_DB_BACKEND=postgres must never reach the DB layer in tests.
    if os.environ.get('TOFU_ALLOW_PG_TESTS') != '1':
        os.environ["TOFU_DB_BACKEND"] = "sqlite"
        # ``lib.database._core`` froze ``DB_PATH`` at conftest-import time from
        # the (worker-unique) ``TOFU_DB_PATH`` the shim block set — setting a
        # DIFFERENT path here would be a dead no-op (the frozen global wins),
        # so REUSE the resolved path and keep the env var consistent with it.
        try:
            import lib.database._core as _dbc
            db_path = getattr(_dbc, 'DB_PATH', '') or ''
        except Exception:
            db_path = ''
        if not db_path:
            tmpdir = tempfile.mkdtemp(prefix="tofu-test-")
            db_path = os.path.join(tmpdir, "tofu-test.db")
        os.environ["TOFU_DB_PATH"] = db_path
        # Create the schema in THIS worker's isolated SQLite file up front, so
        # direct-DB tests (which never build the app via ``flask_app``) don't
        # hit "no such table". Under ``-n`` each xdist worker is its own
        # process with its own DB file; without this, a worker that happens to
        # run only direct-DB tests never initialises its schema. ``init_db`` is
        # idempotent (schema-version cache) so the later ``flask_app`` call is
        # a no-op. Best-effort: never block the session on it.
        try:
            from lib.database import init_db as _init_db
            _init_db()
        except Exception as _e:
            _conftest_logger.warning('[conftest] session init_db failed: %s', _e)
    os.environ.setdefault("TOFU_MLOCK", "0")  # see _install_shim_for_collection
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

    # Create the DB schema. On the real serving path this runs inside
    # ``server._startup()`` (a Hypercorn before-serving hook); ``test_client()``
    # NEVER fires that hook, so without this the session's fresh SQLite file has
    # no tables and every route that touches the DB 500s with
    # "no such table: conversations". ``init_db`` is idempotent (schema-version
    # cache) and needs no app context, so calling it once here is safe.
    from lib.database import init_db as _init_db
    _init_db()

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
    # NC belt: BEFORE any test can byte-patch a guarded source, (1) warn if one
    # already differs from git HEAD — a possible leftover neuter from a
    # hard-crashed prior run (SIGKILL skips the restore finally) — and (2)
    # snapshot eagerly here at session start rather than lazily on the first
    # test, so the baseline is captured as early as possible in the run.
    warn_on_nc_source_poison_at_session_start()
    if not _nc_source_snapshots:
        _snapshot_nc_sources()
    config.addinivalue_line(
        'markers',
        'auth_mode(mode): override TOFU_AUTH_MODE for this test '
        '(open / private / multi-user). Restored after the test.',
    )
    # xdist_group is provided by pytest-xdist, but register it so a run WITHOUT
    # xdist (or with --strict-markers) doesn't warn/error on the marks the
    # collection hook stamps for worker-affinity grouping.
    config.addinivalue_line(
        'markers',
        'xdist_group(name): pytest-xdist worker-affinity group — tests sharing '
        'a name run on the same worker under --dist loadgroup.',
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
        # ── xdist per-file affinity (honoured under ``--dist loadgroup``) ──
        # Stamp every test with an xdist_group == its file basename so ALL of a
        # file's tests run on ONE worker, sequentially — never split per-test
        # across workers. This is load-bearing for the on-disk source-mutating
        # NC tests (``_patch_restore`` byte-patches) + the frontend fixed-name
        # ``.nc_copy.js`` tests: splitting a file's tests across workers lets a
        # neutered source / temp copy be live while a sibling reads it. Under
        # ``--dist load``/``worksteal`` (per-test) the marker is inert and this
        # is a no-op; it only bites under ``--dist loadgroup``. The nc-guard
        # fixture above is the belt that heals a crashed patch regardless.
        _fname = os.path.basename(item.nodeid.split('::', 1)[0]) if item.nodeid else ''
        if _fname:
            item.add_marker(pytest.mark.xdist_group(_fname))
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


# ─── Frontend skip sentinel (P0-1: skip 必须响亮) ─────────────────────────
#
# docs/TESTING_STRATEGY.md §4: lanes that promise to run the frontend suites
# (CI frontend job, ``make test-frontend``) set TOFU_REQUIRE_FRONTEND=1, which
# turns the per-suite dep guards in tests/_jsdom.py from skip into FAIL. This
# sentinel is the NET for hand-written skip sites that bypass _jsdom.py — if
# any test_frontend_* item STILL skips with a missing-dep reason
# (node/jsdom/npm/tsc) under the flag, the whole session goes red. Skips for
# data conditions (e.g. 'no unsent run records') are not counted — the
# classifier lives in tests._jsdom.is_frontend_dep_skip (unit-tested).
_FRONTEND_DEP_SKIPS = []


def pytest_runtest_logreport(report):
    if report.outcome != 'skipped' or report.when not in ('setup', 'call'):
        return
    try:
        from tests._jsdom import is_frontend_dep_skip
    except Exception:
        try:
            from _jsdom import is_frontend_dep_skip
        except Exception:
            # The sentinel must never break the session it guards.
            return
    if is_frontend_dep_skip(report.nodeid or '', str(report.longrepr or '')):
        _FRONTEND_DEP_SKIPS.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if not _FRONTEND_DEP_SKIPS:
        return
    try:
        from tests._jsdom import frontend_required
    except Exception:
        try:
            from _jsdom import frontend_required
        except Exception:
            return
    if not frontend_required():
        return
    session.exitstatus = 1
    shown = '\n'.join(f'  - {nid}' for nid in _FRONTEND_DEP_SKIPS[:50])
    print(
        f'\n[frontend-skip-sentinel] TOFU_REQUIRE_FRONTEND=1 but '
        f'{len(_FRONTEND_DEP_SKIPS)} frontend test(s) silently skipped on '
        f'missing deps (node/jsdom/npm/tsc):\n{shown}\n'
        f'Fix the lane (install node + npm deps) — a skipped frontend suite '
        f'protects NOTHING.\n')


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

# Task-id patterns used EXCLUSIVELY by tests (production task ids are
# timestamp-random and never start with these).
_TEST_TASK_ID_LIKE = (
    'usagetas%',
    'task-cause%',
    'seamtask%',
    'task-freeze%',
    'task-parallel%',
    'task-artifact%',
    'tfeedpb%',
    'aaaaaaaa%',
)


def _purge_test_task_events(reason: str = '') -> int:
    """Delete test-pattern task_events rows from the active DB. Best-effort.

    Returns the number of rows deleted (0 on any failure). Pattern-gated so it
    is safe to run against the production DB the tests share.
    """
    try:
        from lib.database import DOMAIN_SYSTEM, close_thread_db, get_thread_db
    except Exception as e:
        _conftest_logger.debug('task event purge skipped (db import failed): %s', e)
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
                _conftest_logger.debug('task event purge stmt failed (%s): %s', sql, ex)

        for pat in _TEST_TASK_ID_LIKE:
            _del('DELETE FROM task_events WHERE task_id LIKE ?', (pat,))
        try:
            db.commit()
        except Exception as ex:
            _conftest_logger.debug('task event purge commit failed: %s', ex)
    except Exception as e:
        _conftest_logger.warning('purge_test_task_events failed %s: %s', reason, e)
    finally:
        try:
            close_thread_db()
        except Exception:
            pass

    if deleted:
        _conftest_logger.info('purged %d leaked test task_event row(s) %s', deleted, reason)
    return deleted


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
    _purge_test_task_events('(session start)')
    try:
        yield
    finally:
        _purge_test_conversations('(session end)')
        _purge_test_timers('(session end)')
        _purge_test_task_events('(session end)')


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
    """Make headless Chromium launchable, via the shared single source of truth.

    Delegates to ``chromium_env.ensure_chromium_env()`` (repo root), which the
    app itself uses. This used to be a local copy keyed on ``$CONDA_PREFIX``,
    which is unset in any shell that never ran ``conda activate`` and on the
    uv/venv install path — so the fixture skipped with "chromium unavailable"
    on hosts where the libs were present all along. chromium_env resolves from
    ``sys.prefix`` instead, and also handles the fontconfig half (a fontless
    Chromium screenshots blank-but-styled rather than erroring).

    Returns the list of directories added (empty when nothing was needed).
    """
    import sys as _sys
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in _sys.path:
        _sys.path.insert(0, _repo_root)
    from chromium_env import ensure_chromium_env
    return ensure_chromium_env()['lib_dirs_added']


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


#: Console-error substrings that are KNOWN, app-authored degradation notices
#: rather than defects. Kept deliberately SHORT and specific — this list is the
#: one place a real regression could hide, so every entry needs a reason.
#:
#: Measured on a healthy app (2026-07-28, live server + real Chromium): boot
#: produced 4 console errors, ALL of them `_trySSE` premature-close notices
#: from reconnecting to pre-existing background tasks. They are the frontend
#: correctly REPORTING a degraded transport and falling back to polling — a
#: working recovery path, not a broken page.
_BENIGN_CONSOLE_ERRORS = (
    'SSE PREMATURE CLOSE',        # transport degraded → poll fallback engaged
    'falling back to poll',       # the same path's follow-up line
)


def _attach_js_error_capture(pg):
    """Record uncaught JS exceptions + unexpected console errors on ``pg``.

    WHY THIS EXISTS
    ---------------
    Measured 2026-07-28: **no test in this repo listened to the browser
    console**. `grep -rn "on('pageerror'" tests/` returned zero. So a page
    could throw `TypeError: x is not a function` on every boot and the whole
    visual ring would still pass, because assertions only ever looked at the
    DOM nodes each test happened to name. An uncaught exception aborts the rest
    of that script — later handlers silently never bind — which is exactly the
    "click does nothing" bug class this project keeps rediscovering by hand.

    WHY IT CLASSIFIES INSTEAD OF COUNTING
    -------------------------------------
    A strict "zero console errors" gate measured 4 errors on a HEALTHY app, so
    it would be red on day one and promptly deleted. Instead:

      * ``pageerror``   → ALWAYS hard. An uncaught exception is never fine.
      * ``console.error`` → hard UNLESS it matches :data:`_BENIGN_CONSOLE_ERRORS`.
      * ``requestfailed`` → hard unless ``ERR_ABORTED``, which is the browser
        cancelling an in-flight preload/navigation, not a missing asset.
        (Verified: the ERR_ABORTED seen at boot is a pet sprite preload, and
        that file EXISTS on disk — treating it as a 404 would be a false
        accusation of the kind the charter warns about.)

    Findings are attached to the page as ``pg._tofu_js_errors``; the
    ``assert_no_js_errors`` fixture is what turns them into a failure, so
    capture stays cheap and always-on while enforcement is opt-in per test.
    """
    hard = []
    pg._tofu_js_errors = hard

    def _on_pageerror(exc):
        hard.append(f'uncaught exception: {str(exc)[:300]}')

    def _on_console(msg):
        if msg.type != 'error':
            return
        text = msg.text or ''
        if any(b in text for b in _BENIGN_CONSOLE_ERRORS):
            return
        hard.append(f'console.error: {text[:300]}')

    def _on_requestfailed(req):
        failure = req.failure or ''
        if 'ERR_ABORTED' in failure:
            return
        hard.append(f'request failed: {req.url[:200]} ({failure})')

    try:
        pg.on('pageerror', _on_pageerror)
        pg.on('console', _on_console)
        pg.on('requestfailed', _on_requestfailed)
    except Exception as e:  # pragma: no cover - defensive
        _conftest_logger.debug('js error capture not attached: %s', e)


def _dismiss_onboarding_modals(pg):
    """Close the first-run settings modal so it can't swallow clicks.

    NOT a test hack — it reproduces what a real user does. `_maybeAutoOpenSettings`
    (static/js/main/main_toolbar_ui.js) deliberately auto-opens Settings when the
    server has **zero API keys**, which is always true of the ephemeral test
    server. The modal is a full-viewport `.modal-overlay`, so every subsequent
    `click()` lands on the overlay instead of the target and times out after 30s.

    Measured 2026-07-28: this single seam accounted for **12 of the 12** visual
    failures across test_e2e_smoke.py and test_visual_e2e.py. Those had been
    invisible because the whole ring skipped on "chromium unavailable" until
    ff0a94f3 made the browser work.

    Deliberately does NOT use ``click(force=True)`` at the call sites: that
    would paper over the interception, leave the modal open, and let the next
    obscured element repeat the failure somewhere less obvious.

    WHY IT POLLS INSTEAD OF CLOSING ONCE (measured, after my first attempt
    failed): the open is not synchronous with load. The chain is
    ``_loadServerConfigAndPopulate`` (async fetch of the server config)
    ``→ _maybeAutoOpenSettings → setTimeout(..., 500)``. A single close right
    after ``domcontentloaded`` therefore runs BEFORE the timer fires and the
    modal opens again a moment later — which is exactly what happened: the
    first version of this helper changed nothing and the same 12 tests kept
    failing with the identical interception message. So we wait for the
    overlay to be reliably ABSENT rather than closing once and hoping.
    """
    import time
    deadline = time.monotonic() + 12.0
    closed_any = False
    try:
        while time.monotonic() < deadline:
            has_overlay = pg.evaluate(
                "() => !!document.querySelector('.modal-overlay.open')")
            if has_overlay:
                pg.evaluate(
                    "() => { if (typeof closeSettings === 'function') closeSettings();"
                    "  document.querySelectorAll('.modal-overlay.open')"
                    "    .forEach(el => el.classList.remove('open')); }")
                closed_any = True
                pg.wait_for_timeout(120)
                continue
            # Absent right now — but the 500ms auto-open timer may still be
            # pending. Settle past it before declaring the page usable.
            pg.wait_for_timeout(700)
            if not pg.evaluate(
                    "() => !!document.querySelector('.modal-overlay.open')"):
                if closed_any:
                    _conftest_logger.debug(
                        'onboarding modal dismissed (auto-open on a keyless '
                        'test server is expected product behaviour)')
                return
        _conftest_logger.warning(
            'onboarding overlay still reopening after 12s — clicks will be '
            'intercepted; check _maybeAutoOpenSettings')
    except Exception as e:
        _conftest_logger.warning(
            'onboarding modal dismiss failed (%s) — clicks may be intercepted', e)


@pytest.fixture()
def assert_no_js_errors(page):
    """Fail the test if the page raised uncaught JS errors (opt-in).

    Request it alongside ``page`` to bind the browser's own error channel into
    the assertion set::

        def test_something(page, assert_no_js_errors):
            ...

    Enforcement is a separate fixture from capture so that adopting it is a
    per-test decision: a test that legitimately drives an error path can keep
    using ``page`` alone instead of being forced to widen the benign list,
    which would weaken the signal for everyone else.
    """
    yield
    errors = getattr(page, '_tofu_js_errors', [])
    assert not errors, (
        'the page reported %d JavaScript error(s) — an uncaught exception '
        'aborts the rest of that script, so later handlers silently never '
        'bind:\n  %s' % (len(errors), '\n  '.join(errors[:10])))


@pytest.fixture()
def page(browser, live_server):
    """A fresh page navigated to the live app, with automatic cleanup of any
    conversation created during the test (snapshot-diff → browser-side
    ``deleteConversation`` + a server-side pattern purge as a safety net).
    """
    ctx = browser.new_context()
    pg = ctx.new_page()
    _attach_js_error_capture(pg)
    pg.goto(live_server, wait_until='domcontentloaded')
    try:
        pg.wait_for_function("typeof conversations !== 'undefined'", timeout=10000)
    except Exception as e:
        _conftest_logger.debug('page: conversations global not ready: %s', e)

    _dismiss_onboarding_modals(pg)

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
