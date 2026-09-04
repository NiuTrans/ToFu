"""Shared pytest fixtures with one isolated Sidecar authority per worker.

Writable application state and logs are rooted in disposable directories
before the first project import. API fixtures exercise the native Quart app
against the same supervised ``storage.v1`` process used in production; domain
tests that need their own lifetime opt into a focused Sidecar plugin fixture.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading

import pytest

_conftest_logger = logging.getLogger('tests.conftest')

# Pytest is frequently launched from a terminal created by the running Tofu
# server. Never let that production process identity, port, manager address,
# or storage credential become an implicit test input. Focused tests explicitly
# set the values they own after collection.
_INHERITED_LIFECYCLE_ENV_NAMES = (
    'PYTEST_CURRENT_TEST',
    'PORT',
    '_TOFU_RUNTIME_PORT',
    '_TOFU_REEXEC_PORT',
    'TOFU_DATA_DIR',
    'TOFU_PROJECT_ROOT',
    'TOFU_PROJECT_PATH',
    'TOFU_MANAGED_BY',
    'TOFU_SERVER_WORKER',
    'TOFU_RESTART_GATE_PASSED',
    'TOFU_LIFECYCLE_GATE_PASSED',
    'TOFU_ALLOW_LIFECYCLE_TEST',
    'TOFU_LIFECYCLE_TEST_ROOT',
    'TOFU_LIFECYCLE_TEST_PORT',
    'TOFU_LIFECYCLE_TEST_TARGET_PID',
    'TOFU_TESTING',
    'TOFU_EXTERNAL_CONSOLE_LOG',
    'TOFU_EXTERNAL_CONSOLE_STREAM',
    'TOFU_STORAGE_CONNECTION_FILE',
    'TOFU_STORAGE_TOKEN',
    'TOFU_STORAGE_PARENT_PID',
    'TOFU_STORAGE_PROJECT_ROOT',
    'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE',
    # Resource-budget provenance is written by the running server into every
    # child it spawns. A pytest process launched from a tofu shell inherits a
    # stale policy marker that makes install_process_resource_defaults discard
    # explicit test/project overrides (TOFU_MALLOC_ARENA_MAX,
    # TOFU_PROCESS_RSS_RECYCLE_MB, TOFU_STORAGE_RPC_CAPACITY, ...) and re-probe
    # host-dependent defaults. Scrub it so tests start hermetic, exactly like
    # the other lifecycle authority knobs above.
    'TOFU_RESOURCE_BUDGET_POLICY_VERSION',
    'TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS',
    'TOFU_SUPERVISOR_CONF',
    'TOFU_SUPERVISOR_CONF_DIR',
    'TOFU_SUPERVISOR_HOME',
    'TOFU_SUPERVISOR_HOST',
    'TOFU_SUPERVISOR_PORT',
    'TOFU_SUPERVISOR_PROJECTS',
    'TOFU_SUPERVISOR_PYTHON',
    'TOFU_SUPERVISOR_USER',
    'TOFU_HEARTBEAT_DIR',
    'TOFU_PYTEST_RUN_ROOT',
)


def _clear_inherited_lifecycle_environment(environment) -> tuple[str, ...]:
    """Remove production lifecycle authority from a pytest process."""
    removed = []
    for name in _INHERITED_LIFECYCLE_ENV_NAMES:
        if name in environment:
            environment.pop(name, None)
            removed.append(name)
    return tuple(removed)


_CLEARED_INHERITED_LIFECYCLE_ENV_NAMES = (
    _clear_inherited_lifecycle_environment(os.environ))

# A normal session fixture removes its roots below. SIGKILL/OOM cannot run
# Python finalizers, so every new-format root also carries its creating PID and
# the next pytest process reclaims only exact-format roots whose owner is dead.
# Both traversal and deletion are bounded: a hostile/shared /tmp must never
# turn test startup into an unbounded filesystem sweep.
_PYTEST_ROOT_PATTERN = re.compile(
    r'^tofu-test-(?:data|storage)(?:-[A-Za-z0-9_-]+)?-pid-'
    r'(?P<pid>[1-9][0-9]*)-[A-Za-z0-9_]+$')
_PYTEST_ROOT_SCAN_LIMIT = 4096
_PYTEST_ROOT_RECLAIM_LIMIT = 128
_PYTEST_ROOT_UID = str(os.getuid()) if hasattr(os, 'getuid') else 'user'
_PYTEST_ROOT_PARENT = (
    Path(tempfile.gettempdir()).resolve()
    / f'tofu-pytest-runs-{_PYTEST_ROOT_UID}'
)
try:
    _PYTEST_ROOT_PARENT.mkdir(mode=0o700, parents=True, exist_ok=True)
except OSError as exc:
    raise pytest.UsageError(
        f'cannot create isolated pytest root parent: {exc}') from exc
if _PYTEST_ROOT_PARENT.is_symlink() or not _PYTEST_ROOT_PARENT.is_dir():
    raise pytest.UsageError(
        f'pytest root parent is not a safe directory: {_PYTEST_ROOT_PARENT}')
if (hasattr(os, 'getuid')
        and _PYTEST_ROOT_PARENT.stat().st_uid != os.getuid()):
    raise pytest.UsageError(
        f'pytest root parent has a foreign owner: {_PYTEST_ROOT_PARENT}')
try:
    _PYTEST_ROOT_PARENT.chmod(0o700)
except OSError as exc:
    raise pytest.UsageError(
        f'cannot make pytest root parent private: {exc}') from exc


def _pytest_root_owner_is_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # A permission failure proves neither death nor ownership. Retaining a
        # disposable directory is safer than deleting another user's live run.
        return True
    return True


def _reclaim_stale_pytest_roots(
    temp_root: str | os.PathLike[str] | None = None,
    *,
    current_pid: int | None = None,
    reclaim_limit: int = _PYTEST_ROOT_RECLAIM_LIMIT,
) -> dict[str, object]:
    """Remove bounded, exact-format test roots owned by dead processes."""
    root = Path(temp_root or _PYTEST_ROOT_PARENT).resolve()
    this_pid = os.getpid() if current_pid is None else int(current_pid)
    delete_budget = max(0, min(_PYTEST_ROOT_RECLAIM_LIMIT, int(reclaim_limit)))
    scanned = 0
    matched = 0
    removed: list[str] = []
    errors: list[str] = []
    try:
        entries = os.scandir(root)
    except OSError as exc:
        return {
            'scanned': 0, 'matched': 0, 'removed': [],
            'errors': [f'{type(exc).__name__}: {exc}'],
        }
    with entries:
        for entry in entries:
            scanned += 1
            if scanned > _PYTEST_ROOT_SCAN_LIMIT:
                break
            match = _PYTEST_ROOT_PATTERN.fullmatch(entry.name)
            if match is None:
                continue
            matched += 1
            owner_pid = int(match.group('pid'))
            if owner_pid == this_pid or _pytest_root_owner_is_alive(owner_pid):
                continue
            if len(removed) >= delete_budget:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                shutil.rmtree(entry.path)
                removed.append(entry.name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if len(errors) < 8:
                    errors.append(
                        f'{entry.name}: {type(exc).__name__}: {exc}')
    return {
        'scanned': scanned,
        'matched': matched,
        'removed': removed,
        'errors': errors,
    }


_startup_root_reclaim = _reclaim_stale_pytest_roots()
if _startup_root_reclaim['removed']:
    _conftest_logger.info(
        'reclaimed %d crashed pytest root(s)',
        len(_startup_root_reclaim['removed']))

# Test storage isolation must happen before the FIRST project import.  In
# particular, importing tofu_search.config below can transitively import
# lib.config_dir, whose CONFIG_DIR is frozen at module import time.  Setting
# TOFU_DATA_DIR later in _initialize_test_application() is therefore too late
# and previously let API tests write webhook subscriptions into the user's
# real data/config directory.  Every pytest process (including each xdist
# worker) gets fresh roots; an ambient production setting is intentionally not
# inherited by tests.
_PYTEST_WORKER = os.environ.get('PYTEST_XDIST_WORKER', '')
_PYTEST_SUFFIX = f'-{_PYTEST_WORKER}' if _PYTEST_WORKER else ''
_PYTEST_ID = (_PYTEST_WORKER or 'controller').upper()
_PYTEST_DATA_ROOT_KEY = f'TOFU_PYTEST_DATA_ROOT_{_PYTEST_ID}'
_PYTEST_DATA_ROOT = os.environ.get(_PYTEST_DATA_ROOT_KEY)
if not _PYTEST_DATA_ROOT:
    _PYTEST_DATA_ROOT = tempfile.mkdtemp(
        prefix=f'tofu-test-data{_PYTEST_SUFFIX}-pid-{os.getpid()}-',
        dir=str(_PYTEST_ROOT_PARENT))
    os.environ[_PYTEST_DATA_ROOT_KEY] = _PYTEST_DATA_ROOT
os.environ['TOFU_DATA_DIR'] = _PYTEST_DATA_ROOT
os.environ['TOFU_PYTEST_RUN_ROOT'] = _PYTEST_DATA_ROOT
# Pytest may load this file as top-level ``conftest`` while guard tests import
# ``tests.conftest``.  Without an alias, Python executes the module twice: the
# second import replaces TOFU_DATA_DIR after route modules have frozen paths,
# splitting one worker across two supposedly-isolated roots.
_THIS_CONFTEST = sys.modules.get(__name__)
if _THIS_CONFTEST is not None:
    sys.modules.setdefault('conftest', _THIS_CONFTEST)
    sys.modules.setdefault('tests.conftest', _THIS_CONFTEST)
_PYTEST_STORAGE_ROOT_KEY = f'TOFU_PYTEST_STORAGE_ROOT_{_PYTEST_ID}'
_PYTEST_STORAGE_ROOT = os.environ.get(_PYTEST_STORAGE_ROOT_KEY)
if not _PYTEST_STORAGE_ROOT:
    _PYTEST_STORAGE_ROOT = tempfile.mkdtemp(
        prefix=f'tofu-test-storage{_PYTEST_SUFFIX}-pid-{os.getpid()}-',
        dir=str(_PYTEST_ROOT_PARENT))
    os.environ[_PYTEST_STORAGE_ROOT_KEY] = _PYTEST_STORAGE_ROOT
os.environ['TOFU_STORAGE_PROJECT_ROOT'] = _PYTEST_STORAGE_ROOT
os.environ['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'


def _cleanup_owned_pytest_roots(roots: tuple[str, ...]) -> int:
    """Remove only this process's captured roots after cooperative exit."""
    removed = 0
    for raw_path in roots:
        path = Path(raw_path)
        match = _PYTEST_ROOT_PATTERN.fullmatch(path.name)
        if (match is None
                or int(match.group('pid')) != os.getpid()
                or path.parent.resolve() != _PYTEST_ROOT_PARENT
                or path.is_symlink()
                or not path.is_dir()):
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError:
            # The next run's dead-owner sweep is the bounded backstop.
            continue
    return removed


_PYTEST_OWNED_ROOTS = (_PYTEST_STORAGE_ROOT, _PYTEST_DATA_ROOT)
atexit.register(_cleanup_owned_pytest_roots, _PYTEST_OWNED_ROOTS)
os.environ['TOFU_DEPLOYMENT_MODE'] = 'personal'
os.environ['TOFU_PROCESS_ROLE'] = 'all'
for _distributed_only_name in (
    'TOFU_DISTRIBUTED_PREVIEW_MODE',
    'TOFU_POSTGRES_DSN_FILE', 'TOFU_REDIS_URL_FILE', 'TOFU_REPLICA_ID',
):
    os.environ.pop(_distributed_only_name, None)
os.environ.setdefault('_TOFU_ENV_REEXEC', '1')
os.environ.setdefault('TOFU_DISABLE_ENV_REEXEC', '1')
os.environ.setdefault('TOFU_MLOCK', '0')
os.environ.setdefault('TRADING_ENABLED', '0')
os.environ.setdefault('PPTX_TRANSLATE_ENABLED', '0')
os.environ.setdefault('TOFU_BROWSER_POLL_WAIT', '0.2')
os.environ.setdefault('TOFU_DESKTOP_POLL_WAIT', '0.2')
os.environ.setdefault('TOFU_DISABLE_SCHEDULER', '1')
os.environ.setdefault('TOFU_MODEL_CATALOG_SYNC', '0')
os.environ.setdefault('TOFU_NETPATH', 'off')
os.environ['LLM_API_KEY'] = 'test-key-placeholder'
os.environ['LLM_API_KEYS'] = 'test-key-placeholder'
os.environ.setdefault('TOFU_AUTH_MODE', 'open')

# tofu_search's public facade imports pymupdf4llm.  Keep its optional, known-
# incompatible layout/OCR backend from creating a host-sized ONNX pool merely
# to collect tests.  The classic Markdown extractor remains available; ONNX is
# guarded and loaded lazily only when a test actually asks for Docling.
try:
    from runtime_guards import install_pymupdf_classic_policy
    install_pymupdf_classic_policy()
except Exception:
    pass

import tofu_search.config as _config

@pytest.fixture
def anyio_backend():
    """Run AnyIO-marked route contracts on Quart's asyncio backend only."""
    return 'asyncio'


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


def _ensure_quart_default_config():
    """Keep isolated mini-app tests runnable on the pre-0.20 Quart dev env.

    Production and CI install the ``quart>=0.20`` floor from requirements.txt.
    Some developer environments can still carry Quart 0.19 alongside Flask
    3.1; that pair reads ``PROVIDE_AUTOMATIC_OPTIONS`` in ``Quart.__init__``
    before an instance can set it. The production app factory already guards
    its instance, while direct route-contract mini-apps need this one
    process-local test bootstrap.
    """
    from quart import Quart

    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {
            **Quart.default_config,
            'PROVIDE_AUTOMATIC_OPTIONS': True,
        }


_ensure_quart_default_config()


# ─── Fail-closed test storage authority ──────────────────────────────
def _storage_is_test_safe() -> tuple[bool, str]:
    """Prove this worker can only address its disposable project root."""
    configured = os.environ.get('TOFU_STORAGE_PROJECT_ROOT', '').strip()
    if not configured:
        return False, 'TOFU_STORAGE_PROJECT_ROOT is missing'
    if os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') != '1':
        return False, 'the explicit test-authority gate is disabled'
    actual = Path(configured).resolve()
    expected = Path(_PYTEST_STORAGE_ROOT).resolve()
    if actual != expected:
        return False, f'configured root {actual} differs from worker root {expected}'
    repository = Path(__file__).resolve().parents[1]
    if actual == repository or repository in actual.parents:
        return False, f'test authority is inside the source checkout: {actual}'
    return True, f'isolated Sidecar project root {actual}'


def _assert_isolated_storage(context: str = '') -> None:
    """Abort before app boot if a test could reach persistent project data."""
    safe, detail = _storage_is_test_safe()
    if safe:
        _conftest_logger.debug('[storage-guard] OK (%s): %s', context, detail)
        return
    raise pytest.UsageError(
        f'Tofu test storage guard refused {context or "operation"}: {detail}')
# ─── Module-load: make Quart's app_context() usable as a SYNC context ──
#
# Native Quart's ``app.app_context()`` returns an ``AppContext`` that only
# implements ``__aenter__``/``__aexit__`` (async).
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


# ─── Background event-storage worker isolation ──────────────────────
# ``append_persistent_event`` lazily starts a Sidecar batcher; production also
# owns a process-lifetime maintenance daemon. Tests can rebind the Sidecar, so a
# pytest worker deliberately rebinds DB_PATH / SQLite ownership in several
# tests.  Letting the daemon survive across that boundary makes it write with
# the previous test's owner claim, and it can still log after pytest has closed
# its capture stream.  Quiesce only modules that are already imported (do not
# import the event stack for unrelated tests), before AND after every test.
def _stop_test_event_storage_workers(reason: str) -> None:
    import sys

    event_log = sys.modules.get('lib.tasks_pkg.event_log')
    if event_log is None:
        return
    try:
        event_log.stop_storage_maintenance(timeout=3.0)
    except Exception as e:
        _conftest_logger.debug(
            'event maintenance stop failed %s: %s', reason, e)
    try:
        event_log.stop_sidecar_batcher(timeout=3.0)
    except Exception as e:
        _conftest_logger.debug('event batcher stop failed %s: %s', reason, e)


@pytest.fixture(autouse=True)
def _isolate_event_storage_workers():
    _stop_test_event_storage_workers('(test setup)')
    try:
        yield
    finally:
        _stop_test_event_storage_workers('(test teardown)')


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


# ─── Foreign running-loop shield ────────────────────────────────────────
@pytest.fixture(autouse=True)
def _shield_private_loop_helpers(request):
    """Detach pytest-playwright's running-loop marker for synchronous tests.

    The session-scoped playwright driver runs its event loop inside a greenlet
    yet leaves it registered as THIS thread's running loop for the rest of the
    session. Every private-loop helper in the suite (each test file's own
    ``_run_async``: ``new_event_loop().run_until_complete(...)``) then fails
    with "Cannot run the event loop while another loop is running" — but only
    when a browser test ran earlier in the same worker, so the breakage only
    surfaces in large serial batches; xdist lanes never see it. conftest's
    ``_run_coro`` already bridges the same case for the sync test client with
    a helper thread; here we simply detach the foreign marker for the duration
    of tests that never touch the browser loop, and restore it afterwards.
    Tests using playwright fixtures or pytest-asyncio/anyio markers manage
    their own loops and are left alone.
    """
    playwright_fixtures = frozenset({
        'page', 'context', 'browser', 'browser_context', 'browser_type',
        'browser_name', 'browser_channel',
    })
    if (playwright_fixtures.intersection(request.fixturenames)
            or request.node.get_closest_marker('asyncio') is not None
            or request.node.get_closest_marker('anyio') is not None):
        yield
        return
    import asyncio
    import asyncio.events
    foreign = asyncio.events._get_running_loop()
    if foreign is None:
        yield
        return
    asyncio.events._set_running_loop(None)
    try:
        yield
    finally:
        asyncio.events._set_running_loop(foreign)



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
    'lib/message_queue.py',
    'lib/tasks_pkg/compaction/_persist/_splitters.py',
    'lib/tools/conversation.py',
    'lib/scheduler/manager.py',
    'lib/project_mod/config.py',
    'routes/conversations.py',
    # Discovered by tests/test_nc_guard_registry.py (the self-enforcing
    # meta-guard) — each is byte-patched IN PLACE by an NC with a finally
    # restore, so they need the same crash-heal backstop:
    #   static/styles.css — test_memory_modal_specificity.py (CSS-cascade NC)
    #                        + test_mobile_tofu_touch_polish.py (touch-padding NC)
    'static/styles.css',
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


@pytest.fixture(autouse=True)
def _isolate_open_mode_rate_limit(monkeypatch):
    """Do not let unrelated API tests exhaust the production open-mode RPM.

    The API suite shares one app and one per-IP rate-limit store per xdist
    worker. At the production default of 120 RPM, ordinary CRUD setup traffic
    eventually turns every later test into HTTP 429. A few limiter tests also
    deliberately remove the variable after probing it, so a session-only
    setting is insufficient. Reset at every test boundary; limiter-specific
    cases remain free to override/delete it inside their own scope.
    """
    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', '0')


# ─── Session-level isolated application authority ────────────────────
@pytest.fixture(scope="session", autouse=True)
def _configure_test_env():
    """Keep all writable application state under this worker's temp roots."""
    _assert_isolated_storage('session fixture')
    try:
        yield
    finally:
        try:
            from lib.storage import stop_storage
            stop_storage()
        except Exception as exc:
            _conftest_logger.debug(
                'test storage Sidecar cleanup failed: %s', exc)
        _cleanup_owned_pytest_roots(_PYTEST_OWNED_ROOTS)


@pytest.fixture(scope="session")
def flask_app(_configure_test_env):
    """Return the native Quart app over the supervised test Sidecar."""
    _assert_isolated_storage('flask_app fixture')
    import server
    from lib.storage import start_storage

    start_storage()
    server.app.config.update(TESTING=True)
    return server.app


# ─── Per-test auth-mode override via marker ───────────────────────────
def pytest_configure(config):
    """Register markers after proving the worker authority is isolated."""
    _assert_isolated_storage('pytest_configure')
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


_PYTEST_XDIST_DEFAULT_CEILING = 4


def _selected_test_file_count(config) -> int | None:
    """Return an explicit test-file count, or None for tier/directory runs."""
    selected_files = {
        os.path.abspath(str(argument).split('::', 1)[0])
        for argument in getattr(config, 'args', ())
        if str(argument).split('::', 1)[0].endswith('.py')
    }
    return len(selected_files) or None


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """Derive ``-n auto`` from the shared personal-computer resource probe.

    The runtime's useful-parallelism budget already accounts for affinity,
    cgroup CPU, memory capacity, and current memory headroom. Tests add a hard
    four-worker default ceiling because every worker imports the application
    graph and may spawn Node/browser children. Explicit ``-n N`` / ``JOBS=N``
    remains the dedicated-host override.
    """
    explicit_auto_workers = os.environ.get('PYTEST_XDIST_AUTO_NUM_WORKERS', '')
    if explicit_auto_workers.strip():
        try:
            return max(1, int(explicit_auto_workers))
        except (TypeError, ValueError, OverflowError):
            _conftest_logger.warning(
                'ignoring invalid PYTEST_XDIST_AUTO_NUM_WORKERS=%r',
                explicit_auto_workers,
            )
    try:
        from runtime_guards import deployment_resource_default

        useful_parallelism = deployment_resource_default(
            'TOFU_MAX_INFLIGHT_TASKS')
        worker_count = max(
            1, min(_PYTEST_XDIST_DEFAULT_CEILING, useful_parallelism))
    except Exception as exc:
        _conftest_logger.warning(
            'pytest worker resource probe failed; falling back to one worker: %s',
            exc,
        )
        worker_count = 1
    selected_file_count = _selected_test_file_count(config)
    if selected_file_count is not None:
        worker_count = min(worker_count, selected_file_count)
    return worker_count


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
    reclaim = _reclaim_stale_pytest_roots()
    if reclaim['errors']:
        _conftest_logger.debug(
            'stale pytest root reclaim had bounded errors: %s',
            reclaim['errors'])
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
# The app is native Quart, so ``app.test_client()`` is a
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
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Playwright's synchronous API owns an event loop on the pytest main
        # thread.  Quart's legacy sync adapter must not try to nest another
        # loop there (``Cannot run the event loop while another loop is
        # running``); drive this one request on a private helper thread.
        import threading
        result = []
        error = []

        def _drive():
            try:
                result.append(asyncio.run(coro))
            except BaseException as exc:  # preserve the original traceback
                error.append(exc)

        thread = threading.Thread(target=_drive, name='pytest-quart-sync-bridge')
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0]
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
    """Return a ready sync client with its own cookie jar (per test).

    Quart's in-process client does not enter the serving lifespan, so it does
    not run the production ``before_serving`` hook that starts the mandatory
    Storage Sidecar.  Start/handshake it explicitly before APIs migrated to
    named storage operations (users, billing, artifacts) can be exercised.
    """
    from lib.storage import start_storage
    start_storage()
    return _SyncClient(flask_app.test_client())


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
    import time

    # Keystone guard: a live Hypercorn server + Playwright browser runs the
    # destructive E2E cleanup fixtures — refuse to boot against production.
    _assert_isolated_storage('live_server fixture')

    # Hypercorn's programmatic ASGI runner does not consistently enter Quart's
    # production serving lifecycle in every supported version.  Complete the
    # mandatory storage handshake here so browser requests never race an
    # unstarted process-wide Sidecar.
    from lib.storage import start_storage
    start_storage()

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
            # Hypercorn/Quart can leave websocket queue readers pending after
            # the shutdown trigger resolves. Closing the loop underneath them
            # produces noisy ``Queue.get`` unraisable exceptions and, in a
            # longer mixed suite, leaks their process-global state. Drain all
            # loop-owned tasks before closing the loop.
            pending = [task for task in asyncio.all_tasks(loop)
                       if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
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
    """Session-scoped Playwright browser (Chromium by default).

    Self-bootstraps ``LD_LIBRARY_PATH`` (rootless conda-forge GUI libs) before
    launch so it succeeds on shared/HPC nodes without sudo. Skips — with the
    concrete missing-lib reason — only when launch still fails on a host where
    the libs genuinely aren't reachable (the per-machine fallback).
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        pytest.skip(f'playwright not installed: {e}')

    browser_name = (os.environ.get('TOFU_E2E_BROWSER') or 'chromium').lower()
    if browser_name not in {'chromium', 'firefox', 'webkit'}:
        pytest.fail('TOFU_E2E_BROWSER must be chromium, firefox, or webkit; '
                    f'got {browser_name!r}')
    if browser_name == 'chromium':
        _ensure_chromium_library_path()

    pw = sync_playwright().start()
    try:
        launcher = getattr(pw, browser_name)
        launch_opts = {'headless': True}
        if browser_name == 'chromium':
            # Required in this container; avoid swiftshader on headless nodes.
            launch_opts['args'] = ['--no-sandbox', '--disable-gpu']
            executable = os.environ.get('TOFU_E2E_EXECUTABLE_PATH', '').strip()
            if executable:
                launch_opts['executable_path'] = executable
        b = launcher.launch(**launch_opts)
    except Exception as e:
        pw.stop()
        pytest.skip(f'{browser_name} build unavailable / failed to launch '
                    f'(run `playwright install {browser_name}`; on a rootless '
                    f'Chromium host install GUI libs via conda-forge): {e}')
    try:
        yield b
    finally:
        try:
            b.close()
        finally:
            pw.stop()


def _conv_ids_in_page(pg):
    """Best-effort snapshot through the rendered ESM-owned sidebar."""
    try:
        ids = pg.evaluate(
            "[...document.querySelectorAll('.conv-item[data-conv-id]')]"
            ".map(item => item.dataset.convId).filter(Boolean)")
        return set(ids or [])
    except Exception:
        return set()


def _conv_global_ready(pg):
    """True iff the Vite app and its conversation rail are ready RIGHT NOW.

    Distinguishes a genuinely-empty sidebar (global present, length 0) from
    the not-yet-loaded race (global undefined) — the latter makes an empty
    ``_conv_ids_in_page`` baseline UNtrustworthy. See the ``page`` fixture
    cleanup for why this matters (2026-06-28 mass-deletion guard)."""
    try:
        return bool(pg.evaluate(
            "window.TofuModules?.version === 3 && !!document.querySelector('#convList')"))
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
    # Reload/navigation can cancel the old document's health probes while its
    # push socket closes.  This is an app-authored degradation verdict, not an
    # uncaught JavaScript exception; journey tests separately require the new
    # document to settle online with no banner.
    'BACKEND OFFLINE confirmed',
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
                pg.evaluate("""async () => {
                  const close = window.TofuModules?.resolveAction?.('closeSettings');
                  if (typeof close === 'function') await close();
                  // The onboarding close action records the durable dismissal;
                  // merely stripping `.open` makes it reappear after reload.
                  document.getElementById('obCloseX')?.click();
                  document.querySelectorAll('.modal-overlay.open')
                    .forEach((element) => element.classList.remove('open'));
                }""")
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
        pg.wait_for_function(
            "window.TofuModules && window.TofuModules.version === 3",
            timeout=10000)
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
                pg.evaluate("""async (conversationId) => {
                  const remove = window.TofuModules?.resolveAction?.('deleteConversation');
                  if (typeof remove === 'function') await remove(conversationId);
                }""", cid)
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
