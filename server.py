#!/usr/bin/env python3
"""Tofu Server — Quart + Hypercorn (ASGI).

App entry point. Uses:
  - Quart (async Flask from Pallets) as the application framework
  - Hypercorn as the ASGI server with HTTP/2 support
  - Optional TLS/HTTP2 for explicitly configured direct deployments

All existing Flask-style sync route handlers run unchanged in a thread pool.

Usage:
    python server.py                          # HTTP/1.1 (proxy-safe default)
    TOFU_TLS=1 python server.py               # HTTPS + HTTP/2 (auto-cert)
    python server.py --no-tls                 # Explicit HTTP/1.1
    python server.py --certfile cert.pem --keyfile key.pem   # custom cert
"""

import asyncio
import os
import sys
import json
import logging
import time
import threading
import faulthandler

from runtime_guards import (
    RESOURCE_BUDGET_ENV_KEYS,
    deployment_resource_default,
    install_runtime_resource_defaults,
)


def _delegate_executable_to_manager():
    """Fast-path a human ``python server.py`` into the sole lifecycle owner."""
    external_owner = (
        os.environ.get('TOFU_SERVER_WORKER') == '1'
        or os.environ.get('_TOFU_VIA_BOOTSTRAP') == '1'
        or os.environ.get('TOFU_RUN_SERVER') == '1'
        # In-place update/HEAD re-exec keeps the original worker PID and sets
        # this port handoff marker before execv. It is already the worker; a
        # manager handoff here would strand that PID holding the instance lock
        # while it waits for itself to become ready.
        or bool(os.environ.get('_TOFU_REEXEC_PORT'))
        or os.getpid() == 1  # Docker/container entrypoint owns this process
    )
    if __name__ != '__main__' or external_owner:
        return
    if '--version' in sys.argv[1:]:
        try:
            with open(os.path.join(os.path.dirname(__file__), 'VERSION'),
                      encoding='utf-8') as version_file:
                version = version_file.read().strip() or 'unknown'
        except OSError:
            version = 'unknown'
        sys.stdout.write(f'Tofu {version}\n')
        raise SystemExit(0)
    if any(arg in ('-h', '--help') for arg in sys.argv[1:]):
        sys.stdout.write(
            'usage: python server.py [--host HOST] [--port PORT] [--no-tls] '
            '[--certfile FILE --keyfile FILE]\n\n'
            'Starts Tofu through the project-local manager.\n'
            'Lifecycle operations: python serverctl.py --help\n'
            'Start diagnostics   : python serverctl.py doctor\n')
        raise SystemExit(0)

    # Preserve the established environment-selection contract without loading
    # the application first. The re-executed wrapper will immediately hand off
    # to serverctl; the eventual worker still runs the full native-path setup.
    project = os.path.dirname(os.path.abspath(__file__))
    marker = os.path.join(project, '.tofu_env.json')
    try:
        with open(marker, encoding='utf-8') as fh:
            cfg = json.load(fh)
        target = cfg.get('python') or ''
        prefix = cfg.get('env_prefix') or ''
        in_target = (
            os.path.realpath(sys.prefix) == os.path.realpath(prefix)
            if prefix else os.path.realpath(sys.executable) == os.path.realpath(target)
        )
        if target and os.access(target, os.X_OK) and not in_target \
                and os.environ.get('_TOFU_ENV_REEXEC') != '1':
            os.environ['_TOFU_ENV_REEXEC'] = '1'
            os.execv(target, [target, *sys.argv])
    except (OSError, ValueError, TypeError):
        pass

    try:
        from serverctl import managed_start
        code = managed_start(sys.argv[1:], wait=180.0, source='python-server.py')
    except Exception as exc:
        sys.stderr.write(
            '[server.py] Could not hand startup to the Tofu manager: %s\n'
            'Diagnose with: %s serverctl.py doctor\n' % (exc, sys.executable))
        code = 1
    raise SystemExit(code)


_delegate_executable_to_manager()


def _load_early_resource_environment() -> None:
    """Expose project resource/data knobs before native pools are imported."""
    if __name__ != '__main__':
        return
    from tofu_dotenv import read_dotenv_values

    project = os.path.dirname(os.path.abspath(__file__))
    os.environ.setdefault('TOFU_PROJECT_PATH', project)
    try:
        values = read_dotenv_values(os.path.join(project, '.env'))
    except OSError:
        # The canonical loader below emits the established actionable startup
        # error. Early policy installation must not replace it with a traceback.
        return
    early_names = RESOURCE_BUDGET_ENV_KEYS | {
        'TOFU_DEPLOYMENT_MODE',
        'TOFU_DATA_DIR',
        'TOFU_DATA_LAYOUT',
        'XDG_DATA_HOME',
        'LOCALAPPDATA',
    }
    for name in early_names:
        if name not in os.environ and name in values:
            os.environ[name] = values[name]


_load_early_resource_environment()

# Docker runs server.py as PID 1 and therefore has no Python parent that can
# materialize the adaptive personal budget. Freeze one probe before importing
# NumPy, routes, pools, or storage so every consumer (and the later Sidecar
# child) sees the same values for this boot.
_RESOURCE_BUDGET = (
    install_runtime_resource_defaults(os.environ)
    if __name__ == '__main__' else None)


def _install_numeric_thread_defaults() -> int:
    """Bound implicit BLAS/OpenMP pools before NumPy or ML imports.

    High-core personal hosts otherwise make OpenBLAS eagerly retain one native
    worker per visible CPU (64 in the measured deployment) even while Tofu is
    idle.  Tofu already owns request, DB, agent, and tool executors; an
    additional host-sized pool per numeric runtime causes oversubscription and
    needless thread stacks under memory pressure. ``TOFU_NUMERIC_THREADS`` is
    the process-wide ceiling; a smaller library-specific value remains valid,
    while a larger inherited host setting is clamped before imports.
    """
    default = deployment_resource_default(
        'TOFU_NUMERIC_THREADS', os.environ)
    raw = os.environ.get('TOFU_NUMERIC_THREADS', str(default))
    try:
        workers = int(raw or default)
    except (TypeError, ValueError):
        workers = default
    workers = max(1, min(32, workers))
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        try:
            library_workers = int(os.environ.get(name, '') or workers)
        except (TypeError, ValueError, OverflowError):
            library_workers = workers
        os.environ[name] = str(max(1, min(workers, library_workers)))
    return workers


# Must run before the first ``lib`` import: route/plugin discovery eventually
# imports NumPy, at which point OpenBLAS has already fixed its native pool.
_NUMERIC_THREADS = _install_numeric_thread_defaults()

# ── Capture C-level fatal signals (SIGSEGV / SIGABRT / SIGFPE / SIGILL / SIGBUS) ──
# These fire on heap corruption (e.g. `munmap_chunk(): invalid pointer`) from
# native extensions like urllib3's response decompressor. Without this the
# abort prints to fd 2 only and we lose the Python stack of every thread.
# Writing to a dedicated file (instead of stderr) ensures the trace survives
# even when stderr is the controlling terminal of a process that's about
# to die. all_threads=True captures every Python thread, not just the
# crashing one — essential for diagnosing concurrent-fetch races.
#
# Dual-sink strategy: write to BOTH a per-process FUSE-backed file (durable
# across box restarts) and a per-process tmpfs mirror (immune to FUSE stalls).
# Only a real ``python server.py`` process arms the sinks.  Historically this
# ran on every ``import server`` and appended to one shared file: test/tool
# imports alone produced 44k headers, while 1,598 stall dumps grew it to
# 125 MiB with no bound.
_fault_log = None
_fault_shm_log = None
_FAULT_LOG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'logs')
_FAULT_LOG_PATH = os.path.join(
    _FAULT_LOG_DIR, 'tofu_faulthandler_%d.log' % os.getpid())
_FAULT_SHM_PATH = '/dev/shm/tofu_faulthandler_%d.log' % os.getpid()


def _arm_faulthandler_sinks():
    """Open early crash sinks for the executable server, never an importer."""
    global _fault_log, _fault_shm_log, _FAULT_LOG_DIR, _FAULT_LOG_PATH
    try:
        from lib.log_policy import LOG_FILE_MODE as _fault_file_mode
    except Exception:
        _fault_file_mode = 0o600
    header = '=== faulthandler armed pid=%d at %s ===\n' % (
        os.getpid(), time.strftime('%Y-%m-%d %H:%M:%S'))

    # Arm tmpfs first, before importing even the lightweight writable-path
    # resolver.  This preserves early native-crash capture while ensuring a
    # fresh/XDG or frozen install places its durable file beside all other logs
    # instead of writing into a source/read-only bundle.
    try:
        _fault_shm_log = open(_FAULT_SHM_PATH, 'w+', buffering=1)
        try:
            os.fchmod(_fault_shm_log.fileno(), _fault_file_mode)
        except (AttributeError, OSError):
            os.chmod(_FAULT_SHM_PATH, _fault_file_mode)
        _fault_shm_log.write(header)
        faulthandler.enable(file=_fault_shm_log, all_threads=True)
    except (OSError, RuntimeError):
        if _fault_shm_log is not None:
            try:
                _fault_shm_log.close()
            except OSError:
                pass
        _fault_shm_log = None

    try:
        from lib.log import LOG_DIR as _writable_log_dir
        _FAULT_LOG_DIR = _writable_log_dir
        _FAULT_LOG_PATH = os.path.join(
            _FAULT_LOG_DIR, 'tofu_faulthandler_%d.log' % os.getpid())
    except Exception:
        pass
    try:
        os.makedirs(_FAULT_LOG_DIR, exist_ok=True)
        _fault_log = open(_FAULT_LOG_PATH, 'w+', buffering=1)
        try:
            os.fchmod(_fault_log.fileno(), _fault_file_mode)
        except (AttributeError, OSError):
            os.chmod(_FAULT_LOG_PATH, _fault_file_mode)
        _fault_log.write(header)
    except OSError:
        _fault_log = None

    # Fall back to the durable fd, then stderr, when tmpfs is unavailable.
    # ``w+`` lets the healthy heartbeat cap repeated non-fatal stall dumps
    # without replacing the descriptor retained by faulthandler.
    if _fault_shm_log is None:
        if _fault_log is not None:
            faulthandler.enable(file=_fault_log, all_threads=True)
        else:
            faulthandler.enable(all_threads=True)


if __name__ == '__main__':
    _arm_faulthandler_sinks()


# ── Single-instance startup lock + heartbeat wedge detection ──
# (moved to lib/server_boot/lock.py; re-exported for asgi.py and tests.)
from lib.server_boot.lock import (  # noqa: F401
    _acquire_instance_lock,
    _boot_heartbeat_stale_threshold,
    _heartbeat_dir,
    _heartbeat_path,
    _heartbeat_stale_threshold,
    _holder_wedge_age,
    _pid_alive,
    _pid_is_live_server,
    _read_heartbeat,
    _read_heartbeat_state,
    _read_instance_lock_entry,
    _reclaim_stale_instance_lock,
    _record_serve_mode,
    _serve_mode_path,
    _write_heartbeat,
)


# ── Faulthandler-sink hygiene + event-loop stall detection (pure helpers) ──
# The pure helpers live in lib/server_fault_watchdog.py and are re-exported
# here so ``server.<name>`` / ``from server import <name>`` keep working.
from lib.server_fault_watchdog import (  # noqa: F401
    _extract_loop_top_frame,
    _fault_dump_limits,
    _loop_stall_decide,
    _parse_fault_dump_pid,
    _prune_fault_dump_budget,
    _prune_stale_fault_dumps,
    _reset_fault_sink,
    _should_arm_ctimer,
    _stall_pressure_context,
    _trim_fault_sink_if_oversize,
)

def _port_bound(port, host='127.0.0.1', timeout=0.5):
    """True iff a TCP connection to host:port succeeds (listener present).
    Scheme-agnostic: works for TLS and plain-HTTP listeners alike."""
    import socket as _s
    try:
        with _s.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _listener_death_decide(was_bound, bound, misses, k):
    """Pure decision for the serve-listener watch (second layer after the
    loop heartbeat). Returns ``(was_bound, misses, should_exit)``.

    Arms only after the listener was seen bound at least once (the pre-serve
    startup window is not our watch); counts CONSECUTIVE misses from there;
    a single recovery resets the streak. The serve task dying while the
    loop stays alive (the 2026-08-03 11:14 state: no listener, live lock,
    FRESH heartbeat) is invisible to every external probe — the watchdog
    sees a live pid and yields forever — so the process must die loudly
    itself and hand the watchdog a clean, handleable death."""
    if bound:
        return (True, 0, False)
    if not was_bound:
        return (False, 0, False)
    misses += 1
    return (True, misses, misses >= k)


# One-shot boot cleanup: retain recent dead-process evidence but bound both the
# tmpfs and durable per-pid families.  The file opened for this process and any
# other genuinely live server are always preserved.
if _fault_shm_log is not None:
    try:
        _fault_limits = _fault_dump_limits()
        _pruned = _prune_fault_dump_budget(
            directory='/dev/shm',
            keep_basename=os.path.basename(_FAULT_SHM_PATH),
            max_dead_files=_fault_limits['stale_files'],
            max_dead_bytes=_fault_limits['stale_bytes'])
        if _pruned:
            sys.stderr.write('[boot] pruned %d over-budget faulthandler dump(s) from /dev/shm\n'
                             % _pruned)
    except Exception:
        pass   # cleanup is best-effort; never block boot on it
if _fault_log is not None:
    try:
        _fault_limits = _fault_dump_limits()
        _pruned = _prune_fault_dump_budget(
            directory=_FAULT_LOG_DIR,
            keep_basename=os.path.basename(_FAULT_LOG_PATH),
            max_dead_files=_fault_limits['stale_files'],
            max_dead_bytes=_fault_limits['stale_bytes'])
        if _pruned:
            sys.stderr.write('[boot] pruned %d over-budget durable fault dump(s)\n'
                             % _pruned)
    except Exception:
        pass

# ── Pin mapped pages into RAM (FUSE SIGBUS mitigation) ──
# The FUSE/cgroup headroom gate lives in lib/server_mlock.py; this module
# keeps the import-time decision + mlockall exec. Production default is
# OFF (TOFU_MLOCK=1 forces it on, =auto enables the legacy headroom-gated
# mode).
from lib.server_mlock import (  # noqa: F401
    _tofu_cgroup_mem_limit_bytes,
    _tofu_cgroup_mem_usage_bytes,
    _tofu_path_is_fuse,
    _tofu_should_mlock,
)

_tofu_do_mlock, _tofu_mlock_reason = _tofu_should_mlock()
if _tofu_do_mlock:
    try:
        import ctypes as _ctypes
        _MCL_CURRENT, _MCL_FUTURE = 1, 2
        _libc = _ctypes.CDLL('libc.so.6', use_errno=True)
        if _libc.mlockall(_MCL_CURRENT | _MCL_FUTURE) != 0:
            import errno as _errno
            _mlk_err = _ctypes.get_errno()
            # ENOMEM (12) = memlock rlimit too low — common in containers
            if _mlk_err == _errno.ENOMEM:
                os.write(2, b'[boot] mlockall skipped: memlock rlimit too low\n')
            else:
                os.write(2, (b'[boot] mlockall failed errno=%d\n' % _mlk_err))
        else:
            os.write(2, b'[boot] mlockall(MCL_CURRENT|MCL_FUTURE) OK '
                        b'\xe2\x80\x94 pages pinned\n')
    except Exception as _mlk_exc:
        try:
            os.write(2, (b'[boot] mlockall unavailable: %s\n'
                         % str(_mlk_exc).encode(errors='replace')))
        except OSError:
            pass
else:
    try:
        os.write(2, (b'[boot] mlockall skipped \xe2\x80\x94 %s\n'
                     % _tofu_mlock_reason.encode(errors='replace')))
    except OSError:
        pass

# ── Record process start time (same as server.py) ──
_PROC_T0 = time.time()
try:
    os.write(2, b'\033[36m[boot +  0.0s]\033[0m \xf0\x9f\xab\xa7 Tofu '
                b'async bootstrap \xe2\x80\x94 importing core libraries\xe2\x80\xa6\n')
except OSError:
    pass


# ── Native-linkage forensics: which libstdc++ owns the soname? ──
# The capture body lives in lib/server_linkage_forensics.py; the
# diagnostic line and exception guard stay here (source pin in
# tests/test_startup_stdcxx_forensics.py).
from lib.server_linkage_forensics import capture_linkage_forensics

try:
    _TOFU_LINKAGE_FORENSICS = capture_linkage_forensics()
except Exception:
    _TOFU_LINKAGE_FORENSICS = 'libstdc++ soname -> unavailable'
try:
    os.write(2, ('[boot] %s\n' % _TOFU_LINKAGE_FORENSICS).encode(errors='replace'))
except OSError:
    pass

# ── Auto-activate conda env (reuse server.py logic) ──
# This must happen before any third-party imports.
_PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PROJ_DIR)

# ── Dev fallback: locate a local tofu-search checkout when it isn't
# pip-installed (production installs it via requirements.txt). Set
# TOFU_SEARCH_PATH to the repo root of a sibling tofu-search clone.
_TOFU_SEARCH_PATH = os.environ.get('TOFU_SEARCH_PATH', '')
if _TOFU_SEARCH_PATH and os.path.isdir(_TOFU_SEARCH_PATH):
    sys.path.insert(0, _TOFU_SEARCH_PATH)


def _tofu_export_env_native_paths(env_prefix, backend):
    """Put the env's lib/ + bin/ on the search paths for CHILD processes.

    The headless-Chromium half (LD_LIBRARY_PATH + fontconfig) is delegated to
    chromium_env.ensure_chromium_env() — the single source of truth shared with
    bootstrap.py, tests/conftest.py and lib/motion_video. It resolves from
    sys.prefix, so it works even with no .tofu_env.json marker (a fresh clone,
    an exported bundle, or Docker), which is precisely the case the four
    hand-copied versions used to miss. See chromium_env.py's docstring.

    What stays here is the marker-specific part: PATH and the CONDA_* shims.
    """
    # Chromium's GUI libs + fonts. Passing env_prefix only ADDS a candidate;
    # resolution still works when the marker is absent or wrong.
    try:
        from chromium_env import ensure_chromium_env
        ensure_chromium_env(env_prefix=env_prefix)
    except Exception as _e:
        sys.stderr.write(f'[server.py] chromium env setup skipped: {_e}\n')

    if not env_prefix or not os.path.isdir(env_prefix):
        return
    env_bin = os.path.join(env_prefix, 'bin')
    if os.path.isdir(env_bin):
        _cur = os.environ.get('PATH', '')
        if env_bin not in _cur.split(os.pathsep):
            os.environ['PATH'] = (env_bin + os.pathsep + _cur) if _cur else env_bin
    # Only masquerade as a conda env when we ARE one. A uv venv
    # (backend='uv') is not conda; setting CONDA_PREFIX would make
    # bootstrap.py's _running_in_conda_env() misfire and route its pip
    # fallback down the conda-forge branch.
    if backend != 'uv':
        os.environ.setdefault('CONDA_PREFIX', env_prefix)


def _tofu_maybe_reexec_into_env():
    """Re-exec into Tofu's conda env if not already there."""
    marker = os.path.join(_PROJ_DIR, '.tofu_env.json')
    if not os.path.isfile(marker):
        # No marker (fresh clone, exported bundle, Docker, pip layout) — there
        # is nothing to re-exec into, but Chromium still needs its GUI libs and
        # fonts on the search paths. chromium_env resolves those from
        # sys.prefix, so this path is no longer a dead browser.
        _tofu_export_env_native_paths('', '')
        return
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except Exception:
        return
    target_py = cfg.get('python') or ''
    env_prefix = cfg.get('env_prefix') or ''
    backend = cfg.get('backend') or ''
    if not target_py or not os.access(target_py, os.X_OK):
        return
    # Are we ALREADY running inside the target env? Prefer a prefix check over a
    # bare interpreter-path comparison: a uv venv's bin/python is a symlink to a
    # base CPython, so realpath(target_py) can equal realpath(sys.executable)
    # even though we are NOT running with the venv's site-packages active —
    # comparing sys.prefix to env_prefix catches that. Fall back to the
    # interpreter-path compare when env_prefix is absent.
    already_in_env = False
    if env_prefix:
        try:
            already_in_env = (os.path.realpath(sys.prefix) == os.path.realpath(env_prefix))
        except OSError:
            already_in_env = (sys.prefix == env_prefix)
    else:
        try:
            already_in_env = os.path.realpath(target_py) == os.path.realpath(sys.executable)
        except OSError:
            already_in_env = (target_py == sys.executable)
    # Export the env's native-library search path BEFORE the already_in_env
    # early return. Chromium is a CHILD process and resolves libatk /
    # libatk-bridge / libnss out of $env_prefix/lib, which is not on the
    # default linker path. `python server.py` launched directly with the env
    # interpreter (the documented way, and how the supervisor runs it) takes
    # that early return, so while this lived inside the re-exec branch the
    # variables were only ever set on the path that re-execs — a directly
    # launched server left them unset and every Playwright launch died with
    # "libatk-1.0.so.0: cannot open shared object file".
    _tofu_export_env_native_paths(env_prefix, backend)
    if already_in_env:
        return
    if os.environ.get('_TOFU_ENV_REEXEC') == '1':
        return
    os.environ['_TOFU_ENV_REEXEC'] = '1'
    try:
        os.execv(target_py, [target_py, *sys.argv])
    except OSError:
        os.environ.pop('_TOFU_ENV_REEXEC', None)


_tofu_maybe_reexec_into_env()

# ── .env loading ──
def _load_dotenv():
    from tofu_dotenv import load_dotenv_file
    load_dotenv_file(os.path.join(_PROJ_DIR, '.env'))

try:
    _load_dotenv()
except OSError as _dotenv_error:
    if __name__ != '__main__':
        raise
    sys.stderr.write(
        f'[server.py] Invalid project .env: {_dotenv_error}\n'
        '[server.py] Fix or replace the project .env, then run '
        '`python serverctl.py doctor`.\n'
    )
    raise SystemExit(2) from None


# ═══════════════════════════════════════════════════════════════════════
#  Sync→loop boundary helpers (back-compatible test surface)
# ═══════════════════════════════════════════════════════════════════════
# Legacy synchronous routes cross Quart's async request/response boundary via
# ``lib.quart_sync``. Keep these module-level names for existing diagnostics and
# tests without mutating Quart's module or Request class.

def _resolve_sync_body_timeout():
    """Return the configured timeout for an explicit sync boundary."""
    from lib.quart_sync import sync_boundary_timeout
    return sync_boundary_timeout()


def _await_coro_on_loop(coro, main_loop, timeout):
    """Run an awaitable on ``main_loop`` from an executor thread."""
    from lib.quart_sync import await_on_loop
    return await_on_loop(coro, main_loop, timeout)


# ═══════════════════════════════════════════════════════════════════════
#  Logging (reuse server.py's architecture)
# ═══════════════════════════════════════════════════════════════════════

import mimetypes
mimetypes.init()
mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/javascript', '.mjs')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/json', '.json')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('font/ttf', '.ttf')
mimetypes.add_type('application/wasm', '.wasm')

BASE_DIR = _PROJ_DIR

# ── Logging setup (identical to server.py) ──
# LOG_DIR must be WRITABLE. In a frozen desktop build BASE_DIR is the read-only
# bundle root, so route logs to the writable root (see lib/runtime_paths).
from lib.runtime_paths import data_root as _tofu_data_root, logs_root as _tofu_logs_root
from lib.log_retention import (
    ensure_private_log_directory as _ensure_private_log_directory,
    register_external_log as _register_external_log,
    start_log_maintenance as _start_log_maintenance,
    stop_log_maintenance as _stop_log_maintenance,
)
LOG_DIR = _tofu_logs_root()
_ensure_private_log_directory(LOG_DIR)

# Acquire process authority before application assembly. This prevents two
# supervised server processes from racing startup work before one eventually
# loses the TCP bind race.
_instance_lock_fd = None
_instance_lock_bypassed = False
_lock_dir = None
_lock_path = None
if __name__ == '__main__':
    _lock_dir = _tofu_data_root()
    os.makedirs(_lock_dir, exist_ok=True)
    _lock_path = os.path.join(_lock_dir, '.server.lock')
    _early_lock_log = logging.getLogger('server')
    _lock_ok, _instance_lock_fd = _acquire_instance_lock(
        _lock_path, _early_lock_log, mark_booting=True)
    if not _lock_ok:
        _skip = (os.environ.get('TOFU_SKIP_LOCK', '') or '').strip()
        if _skip != '1':
            _message = (
                'Another server instance is already running from this project '
                'directory. Set TOFU_SKIP_LOCK=1 to force start.')
            _early_lock_log.critical(_message)
            try:
                sys.stderr.write('[server.py] ERROR: %s\n' % _message)
                sys.stderr.flush()
            except OSError:
                pass
            sys.exit(1)
        _instance_lock_bypassed = True
        _early_lock_log.warning(
            '[Lock] TOFU_SKIP_LOCK=1 — bypassing instance lock')

# ── Non-blocking, memory-bounded logging ──
# Filter/handler/queue classes and the one-shot construction live in
# lib/server_logging.py; server.py keeps the lifecycle control functions.
from lib.server_logging import (  # noqa: F401
    _BoundedQueueHandler,
    _BoundedQueueListener,
    _QuietPollFilter,
    _SizeAndTimeRotatingFileHandler,
    _log_queue_capacity,
    build_logging_runtime,
)

# Imported AFTER the instance lock above (the source-order pin in
# tests/test_instance_lock_wedge_reclaim.py).
from lib.log_aggregates import (
    enabled as _log_agg_enabled,
    get_default_store as _log_agg_store,
    start_flusher as _log_agg_start_flusher,
    stop_flusher as _log_agg_stop_flusher,
)

# Under pytest, keep logging SYNCHRONOUS: caplog and the tests that assert
# a log line landed in a file handler (e.g. test_log_pytest_sink_isolation)
# read handler output immediately after logger.error(), which an async
# listener thread would race. Detect pytest via the env var it always sets
# for a collected session.
_LOG_UNDER_PYTEST = bool(os.environ.get('PYTEST_CURRENT_TEST')) or (
    'pytest' in sys.modules)

(_formatter, _real_log_handlers, _LOG_QUEUE, _queue_handler,
 _coalescing_filter, _log_listener) = build_logging_runtime(
    log_dir=LOG_DIR,
    under_pytest=_LOG_UNDER_PYTEST,
    log_agg_enabled=_log_agg_enabled,
    log_agg_store=_log_agg_store,
)

# Individual handler handles retained for test/diagnostic introspection
# (tests/test_log_pytest_sink_isolation.py reads these by name).
_app_handler = _real_log_handlers[0]
_access_handler = _real_log_handlers[1]
_error_handler = _real_log_handlers[2]
_vendor_handler = _real_log_handlers[3]
_frontend_handler = _real_log_handlers[4]
_console_handler = _real_log_handlers[5]


def _start_logging_runtime():
    """Start storage-independent log I/O from Quart's serving lifecycle."""
    if _LOG_UNDER_PYTEST or _log_listener is None:
        return False
    thread = getattr(_log_listener, '_thread', None)
    if thread is not None and thread.is_alive():
        return False
    _log_listener.start()
    if _coalescing_filter is not None:
        # Quiet duplicate tails need one delayed checkpoint even if no later
        # event arrives. Arm its sink now; a short-lived worker is created only
        # after a delta is actually suppressed. Delivery bypasses the producer
        # filter and enters the already-bounded queue directly.
        _coalescing_filter.start_pending_flush(_queue_handler.emit)
    _external_console = (os.environ.get('TOFU_EXTERNAL_CONSOLE_LOG') or '').strip()
    if _external_console:
        try:
            _external_stream = (
                os.environ.get('TOFU_EXTERNAL_CONSOLE_STREAM')
                or 'server_console').strip()
            _register_external_log(_external_console, _external_stream)
        except Exception as exc:
            _server_log.warning(
                '[Logging] external console retention registration failed: %s',
                exc)
    _start_log_maintenance(LOG_DIR, _tofu_data_root())
    return True


def _start_log_aggregate_runtime_after_recovery():
    """Start the adaptive database-backed log index after recovery settles.

    Logging must capture recovery diagnostics, so the listener starts at the
    beginning of the Quart lifespan. Its aggregate flusher is different: it
    writes through the same single-writer storage authority as recovery and
    used to contend with ``turn.recover`` every 15 seconds during startup. The
    worker retains that batching delay while rows are pending and sleeps to the
    hourly TTL boundary when idle.
    """
    if _LOG_UNDER_PYTEST or not _log_agg_enabled():
        return False
    return _log_agg_start_flusher()


def _stop_logging_runtime(*, timeout=5.0, final_flush=True):
    """Bound and stop aggregate/log threads; safe to call repeatedly."""
    if _LOG_UNDER_PYTEST or _log_listener is None:
        return True
    coalescer_stopped = True
    if _coalescing_filter is not None:
        coalescer_stopped = _coalescing_filter.stop_pending_flush(
            timeout=timeout, final_flush=True)
    maintenance_stopped = _stop_log_maintenance(timeout=timeout)
    # Drain the listener before the aggregate store's final flush. Otherwise a
    # record already in the queue can increment the in-memory fingerprint table
    # after its flusher has stopped and disappear from the DB acceleration view.
    listener_stopped = _log_listener.stop(timeout=timeout)
    aggregate_stopped = True
    if _log_agg_enabled():
        aggregate_stopped = _log_agg_stop_flusher(
            final_flush=final_flush, timeout=timeout)
    return bool(coalescer_stopped and maintenance_stopped
                and aggregate_stopped and listener_stopped)


if not _LOG_UNDER_PYTEST:
    # Drain + flush the queue on interpreter exit so the tail of the log isn't
    # lost if the process stops before Quart can run its shutdown lifespan.
    import atexit as _atexit_mod

    def _stop_log_listener():
        try:
            _stop_logging_runtime(final_flush=False)
        except Exception as exc:
            # atexit must not raise; direct stderr is still available if the
            # asynchronous listener itself is the failing component.
            try:
                sys.stderr.write(
                    '[Server] logging atexit cleanup failed: %s\n' % exc)
            except OSError:
                pass

    _atexit_mod.register(_stop_log_listener)

_NOISY_LIBS = (
    'courlan', 'htmldate', 'justext',
    'urllib3', 'requests', 'charset_normalizer',
    'websockets', 'websockets.client',
    'PIL', 'pymupdf',
    'httpcore', 'httpx',
)
for _lib_name in _NOISY_LIBS:
    logging.getLogger(_lib_name).setLevel(logging.WARNING)
logging.getLogger('trafilatura').setLevel(logging.ERROR)
for _sub in ('trafilatura.xml', 'trafilatura.core', 'trafilatura.htmlprocessing',
             'trafilatura.metadata'):
    logging.getLogger(_sub).setLevel(logging.ERROR)
logging.getLogger('hypercorn.access').addFilter(_QuietPollFilter())


# ── Crash visibility: route uncaught exceptions to the log files ──
# faulthandler (top of file) covers C-level fatal signals, but an uncaught
# *Python* exception in the main thread otherwise reaches only the default
# excepthook → stderr, never app.log / error.log. Install a hook that logs
# it at CRITICAL (with traceback) before delegating to whatever hook was
# already installed (e.g. the bootstrap-delegation hook that re-execs to
# bootstrap.py on ImportError) — so we add visibility without clobbering it.
_prev_excepthook = sys.excepthook

def _crash_excepthook(exc_type, exc_value, exc_tb):
    # Ctrl-C is a normal shutdown path, not a crash — don't scream about it.
    if not issubclass(exc_type, KeyboardInterrupt):
        try:
            # A dynamic-linker failure is unreadable without knowing WHICH copy
            # of the library won the soname and what injected it. The boot-time
            # forensics went to stderr, which is discarded on any boot the
            # watchdog did not start — so attach it to the crash record itself,
            # where it lands in logs/error.log next to the traceback.
            _extra = ''
            if issubclass(exc_type, ImportError):
                _msg = str(exc_value)
                if 'GLIBCXX' in _msg or 'libstdc++' in _msg or 'symbol' in _msg:
                    _extra = ' | LINKAGE: %s' % (
                        globals().get('_TOFU_LINKAGE_FORENSICS', 'unavailable'),)
            logging.getLogger('server').critical(
                'Uncaught exception — process is terminating%s' % _extra,
                exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass  # logging must never mask the original crash
    (_prev_excepthook or sys.__excepthook__)(exc_type, exc_value, exc_tb)

sys.excepthook = _crash_excepthook


# ── Crash visibility: background threads ──
# sys.excepthook covers ONLY the main thread. The entire task/orchestration
# system (run_task, swarm agents, scheduler ticks, timers) runs in daemon
# worker threads, where an uncaught exception otherwise reaches just the
# default threading hook → stderr, never app.log / error.log. Route it through
# our 'server' logger at CRITICAL (with traceback) so a silently-dying worker
# is always diagnosable. threading.excepthook exists since Py3.8.
_prev_thread_excepthook = threading.excepthook

def _thread_crash_excepthook(args):
    # SystemExit raised inside a thread is a normal stop signal, not a crash.
    if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
        try:
            logging.getLogger('server').critical(
                'Uncaught exception in background thread %r — thread is dying',
                getattr(args.thread, 'name', '?'),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass  # logging must never mask the original crash
    if _prev_thread_excepthook is not None:
        _prev_thread_excepthook(args)

threading.excepthook = _thread_crash_excepthook


# ── Boot progress ──
_BOOT_T0 = _PROC_T0
_boot_logger = logging.getLogger('server.boot')

def _boot(msg, *args):
    try:
        line = msg % args if args else msg
    except Exception:
        line = msg
    elapsed = time.time() - _BOOT_T0
    # The cosmetic console echo must NEVER be fatal. On an in-place restart
    # (os.execv) the child inherits fd 2 as a pipe whose reader has already
    # gone away, so this write raises BrokenPipeError — which, unguarded,
    # kills boot at the very first progress line before any module loads.
    # The authoritative boot record is the logger.info below (→ app.log).
    try:
        sys.stderr.write('\033[36m[boot +%5.1fs]\033[0m %s\n' % (elapsed, line))
        sys.stderr.flush()
    except OSError:
        pass
    _boot_logger.info('[boot +%.1fs] %s', elapsed, line)


_boot('🫧 Tofu (async) starting up — loading core modules…')

# ── Keep the unused/broken PyMuPDF layout backend out of ordinary boot ──
# pymupdf4llm auto-activates pymupdf.layout merely because it is installed.
# That backend is incompatible with our RapidOCR version and Tofu explicitly
# uses the classic Markdown implementation, yet activation still imported
# ONNX and retained ~63 native threads + tens of MiB before the first request.
# This stdlib-only policy must run before tofu_search imports pymupdf4llm.
# Structured Docling parsing remains opt-in and installs the ONNX session guard
# at its actual first use.
try:
    from runtime_guards import install_pymupdf_classic_policy
    install_pymupdf_classic_policy()
except Exception as _pdf_policy_err:  # never let an optional policy break boot
    _boot('PyMuPDF classic policy install skipped: %s', _pdf_policy_err)

# ── Deployment topology is validated before application imports ──
# Personal is one-process SQLite. Distributed requires external TLS-verified
# PostgreSQL/Redis secret files and an explicit replica identity; removed
# backend/ring switches are fatal instead of selecting a split-brain topology.
from runtime_guards import enforce_deployment_configuration
DEPLOYMENT_CONFIGURATION = enforce_deployment_configuration()

# ═══════════════════════════════════════════════════════════════════════
#  Quart App
# ═══════════════════════════════════════════════════════════════════════

#  static_folder=None DISABLES Quart's built-in /static/<path> view. That view
#  serves files via a NATIVE-ASYNC send_static_file → send_from_directory whose
#  is_file()/stat()/full-file-read run DIRECTLY on the event loop — one FUSE
#  stall there wedges the whole server (the proven root cause of the outage).
#  Our own executor-offloaded /static route below replaces it (see
#  _static_route). BASE_DIR/static lives on FUSE here.
from lib.app_factory import create_base_app

app = create_base_app(__name__)
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# Logging handlers are configured early so import/boot records are retained,
# but their worker threads belong to the native application lifespan.


async def _shutdown_logging_runtime():
    await asyncio.to_thread(_stop_logging_runtime)


def create_app(config: dict | None = None):
    """Create a fully assembled, independent Quart ASGI application."""
    from lib.app_assembly import create_application

    return create_application(
        __name__,
        static_dir=STATIC_DIR,
        logger=_lifecycle_log,
        secret_key=_load_or_create_flask_secret_key(),
        config=config,
        body_policy=_HTTP_BODY_POLICY,
        static_timeout=lambda: _STATIC_SEND_TIMEOUT,
        static_offload=lambda loop, filename: _static_offload(loop, filename),
        static_range_allows=lambda value, etag, mtime: _if_range_allows(
            value, etag, mtime),
        startup_handlers=(
            ('tofu.logging.startup', _start_logging_runtime),
        ),
        shutdown_handlers=(
            ('tofu.logging.shutdown', _shutdown_logging_runtime),
        ),
        distributed_preview_read_only=(
            DEPLOYMENT_CONFIGURATION.distributed_preview_read_only),
    )


def create_production_app(config: dict | None = None):
    """Create an ASGI app with the process-wide production lifespan attached."""
    production_app = create_app(config)
    register_server_runtime_lifecycle(production_app)
    return production_app


# ── Flask secret key (reuse server.py logic) ──
from lib.server_assembly import _load_or_create_flask_secret_key


# MAX_CONTENT_LENGTH is APP-GLOBAL, so it must fit the LARGEST legitimate
# body any route accepts — the video upload cap (512 MiB, TOFU_VIDEO_MAX_BYTES)
# plus multipart slack. Every OTHER route keeps the legacy 50 MiB ceiling via
# the per-route guard below — raising the global must not silently open
# big-body uploads on the whole API surface (owner ruling 2026-08-04).

# ── Long-lived response timeout + bounded request-body policy ──
# Policy, parsing and the Quart before-request hook are owned by the native
# assembly boundary; this module retains only the configured policy value.
from lib.http_body_policy import build_http_body_policy

_HTTP_BODY_POLICY = build_http_body_policy()
_HTTP_BODY_TIMEOUT_S = _HTTP_BODY_POLICY.body_timeout
_HTTP_UPLOAD_BODY_TIMEOUT_S = _HTTP_BODY_POLICY.upload_body_timeout


from lib.log import get_logger

_lifecycle_log = get_logger('server.lifecycle')


# tofu-search is an optional, heavyweight capability. ``lib.search_runtime``
# installs its configuration/browser/auth seams at the first valid search or
# fetch call; ordinary boot and non-search requests never import it.

# ── First-boot personal key bootstrap ──
# Only relevant when the auth gate is in ``private`` or ``multi-user``
# mode. In ``open`` mode (the default for personal installs) no
# credential is required and minting a key would just confuse the
# operator. When in private/multi-user mode and the key store is
# empty, mint a personal admin key
# so the local UI and SDK "just work". The plaintext is printed once
# to stderr and persisted (0600) at data/config/.first_run_token.
# Disable with TOFU_AUTO_KEY=0.
_BOOTSTRAP_TOKEN = ''
try:
    from lib.auth_mode import get_mode as _get_auth_mode
    _AUTH_MODE = _get_auth_mode()
except Exception as _e:
    logging.getLogger('server.boot').warning(
        '[AuthMode] could not resolve mode: %s', _e)
    _AUTH_MODE = 'open'


def _bootstrap_personal_key_if_needed():
    global _BOOTSTRAP_TOKEN
    if (os.environ.get('TOFU_AUTO_KEY', '1') or '1').strip() == '0':
        return
    if _AUTH_MODE == 'open':
        return  # gate is open — no credential needed at all
    try:
        from lib.api_keys import bootstrap_personal_key, has_any_key
    except Exception as _e:
        logging.getLogger('server.boot').warning(
            '[Auth] could not import bootstrap helpers: %s', _e)
        return
    if has_any_key():
        return
    plaintext = bootstrap_personal_key(name='personal')
    if plaintext:
        _BOOTSTRAP_TOKEN = plaintext

# ── Static file serving — executor-offloaded (FUSE-stall safe) ──
#
# Quart's built-in /static route was DISABLED (static_folder=None) because its
# native-async send_static_file runs is_file()/stat()/full-file-read directly on
# the event loop — one stall on the FUSE-backed static/ dir wedges the whole
# server. This replacement moves ALL blocking filesystem I/O into a worker
# thread under a hard timeout, so a FUSE stall degrades one request to a fast
# 503 while the loop keeps serving everyone else.
#
# Invariants (see the three sign-off requirements):
#   1. Path traversal: _load_static_bytes uses werkzeug.safe_join (the same
#      primitive the built-in route used) — never a hand-rolled os.path.join —
#      so '..'/absolute/escape resolves to None → 404, never a file leak.
#   2. 404 vs 503 stay DISTINCT: a genuinely-missing file returns 404 (so the
#      stale-bundle self-heal in _handle_404 / resolve_stale_bundle keeps
#      working); only an executor TIMEOUT (the FUSE-wedge signal) returns 503.
#   3. Caching preserved: we compute size+mtime+adler32 ETag in the thread and
#      build a conditional response on the loop (make_conditional → 304), and
#      add_cache_headers still stamps the immutable/max-age headers afterward.
_STATIC_SEND_TIMEOUT = float(os.environ.get('TOFU_STATIC_SEND_TIMEOUT', '') or '12')

from lib.static_serving import (
    if_range_allows as _if_range_allows,
    load_static_bytes as _read_static_bytes,
)
from lib.static_mirror import StaticViteMirror


_STATIC_VITE_MIRROR = StaticViteMirror.from_environment(STATIC_DIR)


def _load_static_bytes(filename):
    """Resolve *filename* strictly under STATIC_DIR and read it (SYNC, runs in a
    worker thread so the FUSE I/O never touches the event loop).

    Returns ``(data, mtime, etag)`` on success or ``None`` when the path is
    unsafe (traversal) or the file is absent/not-a-file. Raising is reserved for
    genuine I/O errors (surfaced as 500). The blocking calls — safe_join, the
    ``os.path.isfile`` stat, and the full ``open().read()`` — are exactly what
    would wedge the loop if run inline; here they are on the thread.
    """
    selected_static_dir = _STATIC_VITE_MIRROR.static_dir_for(filename)
    result = _read_static_bytes(selected_static_dir, filename)
    if result is None and selected_static_dir != STATIC_DIR:
        # A cache generation can disappear only through external cleanup.  The
        # repository remains authoritative, so preserve 404 semantics by
        # checking it before declaring the file absent.
        return _read_static_bytes(STATIC_DIR, filename)
    return result


async def _static_offload(loop, filename):
    """Offload the blocking static read to a worker thread.

    A one-line seam kept separate so the executor-offload is the SINGLE point a
    test can neuter to prove it is load-bearing (running _load_static_bytes
    inline here would put the FUSE-blocking read back on the loop — the exact
    regression this whole route prevents).
    """
    return await loop.run_in_executor(None, _load_static_bytes, filename)


# ── Proxy config ──
def _load_saved_proxy_config():
    """Apply persisted network settings during the serving lifecycle.

    Reading the settings file and mutating process-wide proxy state used to
    happen while importing ``server``. Keeping it behind this explicit startup
    function makes app imports safe for tests, schema tooling and desktop
    probes. The netpath prober starts only after this function runs.
    """
    try:
        from routes.config import _read_server_config
        from lib.proxy import set_bypass_domains, set_proxy_config

        saved_cfg = _read_server_config()
        saved_proxy_config = saved_cfg.get('proxy_config', {})
        if saved_proxy_config and any(
                saved_proxy_config.get(key)
                for key in ('http_proxy', 'https_proxy')):
            set_proxy_config(
                http_proxy=saved_proxy_config.get('http_proxy', ''),
                https_proxy=saved_proxy_config.get('https_proxy', ''),
            )
        saved_bypass_domains = saved_cfg.get('proxy_bypass_domains', [])
        if saved_bypass_domains:
            set_bypass_domains(saved_bypass_domains)
        # Ordered proxy pool (scoped subscription/global entries) is additive
        # over the legacy single-proxy environment fallback above.
        saved_pool = saved_cfg.get('proxy_pool') or []
        if saved_pool:
            from lib.proxy import set_proxy_pool
            set_proxy_pool(saved_pool)
    except Exception as exc:
        _lifecycle_log.warning('Failed to load proxy config: %s', exc)


from lib.app_assembly import configure_application

configure_application(
    app,
    static_dir=STATIC_DIR,
    logger=_lifecycle_log,
    secret_key=_load_or_create_flask_secret_key(),
    body_policy=_HTTP_BODY_POLICY,
    static_timeout=lambda: _STATIC_SEND_TIMEOUT,
    # Resolve compatibility seams per request so fault-injection tests and
    # embedders can replace them without rebuilding the route table.
    static_offload=lambda loop, filename: _static_offload(loop, filename),
    static_range_allows=lambda value, etag, mtime: _if_range_allows(
        value, etag, mtime),
    startup_handlers=(
        ('tofu.logging.startup', _start_logging_runtime),
    ),
    shutdown_handlers=(
        ('tofu.logging.shutdown', _shutdown_logging_runtime),
    ),
    distributed_preview_read_only=(
        DEPLOYMENT_CONFIGURATION.distributed_preview_read_only),
)


# ═══════════════════════════════════════════════════════════════════════
#  Startup & Main
# ═══════════════════════════════════════════════════════════════════════

_server_log = logging.getLogger('server')

from lib.server_assembly import (
    _check_frontend_artifact,
    _init_database,
    _run_boot_recovery_step,  # noqa: F401
    _start_storage_sidecar,
    _validate_imports,
    _validate_storage_cutover_boundary,
)
from lib import server_assembly as _server_assembly

_server_assembly.inject_runtime(
    boot=_boot,
    server_log=_server_log,
    deployment_configuration=DEPLOYMENT_CONFIGURATION,
    tofu_do_mlock=_tofu_do_mlock,
    project_root=_PROJ_DIR,
    # Resolve through the LIVE server module at call time: test_restart_smoke
    # re-executes server.py into a temp module and re-runs inject_runtime, so a
    # closure over _start_log_aggregate_runtime_after_recovery would go stale.
    start_log_aggregate_runtime_after_recovery=(
        lambda: (sys.modules.get('server') or sys.modules.get('__main__'))
        ._start_log_aggregate_runtime_after_recovery()),
)


def _start_background_workers(target_app=None):
    """Start the extracted lifecycle-owned background services."""
    from lib.server_background_services import start_background_services

    return start_background_services(
        target_app or app,
        process_role=DEPLOYMENT_CONFIGURATION.process_role,
        load_saved_proxy_config=_load_saved_proxy_config,
        bootstrap_personal_key=_bootstrap_personal_key_if_needed,
        logger=_server_log,
    )


from lib.server_network import (
    detect_reverse_proxy as _detect_reverse_proxy,
    find_free_port as _find_free_port,
    listener_configuration_error as _listener_configuration_error,
    resolve_tls_policy as _resolve_tls_policy,
    wait_port_free as _wait_port_free,
)
from lib.server_tls import ensure_tls_certificates as _ensure_tls_certs


from lib.server_shutdown import (
    graceful_shutdown_signals,
    http_keep_alive_timeout_seconds,
    request_graceful_shutdown as _request_graceful_shutdown,
    shutdown_hard_deadline_seconds as shutdown_hard_deadline_seconds,
    # Compatibility exports used by lifecycle tests and external launchers.
    start_shutdown_hard_deadline as _start_shutdown_hard_deadline,  # noqa: F401
)


def _prepare_frontend_runtime_assets():
    """Validate/build the frontend, then stage its Vite tree on local disk."""
    _check_frontend_artifact()
    status = _STATIC_VITE_MIRROR.prepare()
    if status.active:
        _server_log.info(
            '[Static] local Vite mirror ready: files=%d bytes=%d root=%s',
            status.file_count, status.total_bytes, status.static_dir)
    else:
        _server_log.warning(
            '[Static] local Vite mirror unavailable (%s); using %s',
            status.reason, status.static_dir)


def register_server_production_lifecycle(
        target_app, *, shutdown_requested=None, announce_ready=None):
    """Attach the shared production bootstrap/cleanup recipe to ``target_app``."""
    from lib.production_lifecycle import (
        ProductionStartupSteps,
        register_production_lifecycle,
    )

    return register_production_lifecycle(
        target_app,
        steps=ProductionStartupSteps(
            build_assets=_prepare_frontend_runtime_assets,
            validate_storage_boundary=_validate_storage_cutover_boundary,
            init_database=_init_database,
            start_storage=_start_storage_sidecar,
            validate_imports=_validate_imports,
            start_workers=_start_background_workers,
        ),
        shutdown_requested=shutdown_requested,
        logger=_server_log,
        boot=_boot,
        announce_ready=announce_ready,
        request_graceful_shutdown=_request_graceful_shutdown,
        process_role=DEPLOYMENT_CONFIGURATION.process_role,
    )


def register_server_runtime_lifecycle(
        target_app, *, shutdown_requested=None, announce_ready=None,
        host=None, port=None):
    """Attach serving-loop owners before the fail-fast production bootstrap."""
    from lib.server_runtime_lifecycle import register_runtime_lifecycle

    return register_runtime_lifecycle(
        target_app,
        production_registrar=register_server_production_lifecycle,
        shutdown_requested=shutdown_requested,
        announce_ready=announce_ready,
        host=host,
        port=port,
        hooks=sys.modules[__name__],
        fault_shm_log=_fault_shm_log,
        fault_log=_fault_log,
        logger=_server_log,
    )


if __name__ == '__main__':
    try:
        from hypercorn.asyncio import serve as hypercorn_serve
    except ImportError:
        sys.stderr.write(
            '\033[31m[server.py] ERROR: hypercorn is not installed.\n'
            '  Install with: pip install hypercorn\033[0m\n')
        sys.exit(1)

    import asyncio
    import argparse

    parser = argparse.ArgumentParser(description='Tofu Async Server')
    # Default to all interfaces (owner 2026-08-04): bootstrap.py / Docker /
    # install.sh already defaulted to 0.0.0.0, so direct `python server.py`
    # was the only outlier — and the desktop-agent LAN flow NEEDS the
    # server reachable off-loopback. Loopback is now the explicit choice
    # via --host 127.0.0.1 / BIND_HOST=127.0.0.1 (the packaged desktop app
    # pins that itself). The boot banner already warns loudly on open-auth
    # + non-loopback binds.
    parser.add_argument('--host', default=os.environ.get('BIND_HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=os.environ.get('PORT', '15000'))
    parser.add_argument('--certfile', default=os.environ.get('TLS_CERTFILE', ''))
    parser.add_argument('--keyfile', default=os.environ.get('TLS_KEYFILE', ''))
    parser.add_argument('--no-tls', action='store_true',
                        help='Disable TLS (HTTP/1.1 only, no HTTP/2 in browsers)')
    parser.add_argument(
        '--workers', type=int, default=1,
        help='Must be 1. Horizontal scaling remains closed during the '
             'distributed preview; see docs/EPIC_D_SCALE_ROLLOUT_RUNBOOK.md. '
             'Programmatic Hypercorn ignores workers.')
    args = parser.parse_args()

    # hypercorn.asyncio.serve() explicitly ignores Config.workers. Silently
    # accepting --workers=N advertised isolation that did not exist and, worse,
    # multiple local processes cannot share the live task registry.
    if args.workers != 1:
        parser.error(
            '--workers must be 1. Horizontal scaling is not enabled in the '
            'current distributed preview; see '
            'docs/EPIC_D_SCALE_ROLLOUT_RUNBOOK.md.')

    _tls_value = (os.environ.get('TOFU_TLS') or '').strip()
    _listener_error = _listener_configuration_error(
        port=args.port,
        no_tls=args.no_tls,
        tls_value=_tls_value,
        certfile=args.certfile,
        keyfile=args.keyfile,
    )
    if _listener_error:
        parser.error(_listener_error)

    host = args.host

    # The executable acquired this before any database-backed import.  Keep the
    # fd alive in module scope for the full process lifetime.
    if _instance_lock_bypassed:
        _boot('Instance lock explicitly bypassed (PID=%d)', os.getpid())
    else:
        _boot('Instance lock acquired before database imports (PID=%d)', os.getpid())

    # ── SIGTERM → graceful shutdown ──
    # Set a shutdown flag instead of calling sys.exit(0) from the signal
    # handler. sys.exit raises SystemExit in the main thread, which aborts
    # Hypercorn mid-serve and skips connection draining (graceful_timeout).
    # The flag is consumed by an asyncio.Event created inside the serving
    # loop (see _serve) and handed to hypercorn_serve(shutdown_trigger=…)
    # so in-flight requests / SSE streams drain cleanly.
    import threading as _threading
    _shutdown_requested = _threading.Event()
    from lib.compat import safe_signal
    def _signal_shutdown(signum, frame):
        # A SECOND signal while we're already draining = the user is impatient
        # (or a task is wedged). Honour it as an immediate force-quit escape
        # hatch instead of forcing them to wait out the drain window. os._exit
        # skips the atexit PG-stop hook, but mark_clean already ran on the first
        # signal so the next boot still classifies this as a clean exit.
        if _shutdown_requested.is_set():
            try:
                sys.stderr.write(
                    '\n\033[31m[Server] Force-quit — terminating now.\033[0m\n')
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(130)
        _server_log.info('[Server] Received signal %s — shutting down…', signum)
        # The logger line above lands in logs/app.log, NOT the terminal — so
        # from the user's seat Ctrl+C looked like a silent freeze. Echo a
        # visible notice to stderr (the terminal) that we're draining, and how
        # to bail out immediately.
        try:
            sys.stderr.write(
                '\n\033[33m[Server] Shutting down gracefully — draining in-flight '
                'requests…\n'
                '  Press Ctrl+C again to force-quit immediately.\033[0m\n')
            sys.stderr.flush()
        except Exception:
            pass
        # Arm the in-memory shutdown flag and hard deadline BEFORE the clean
        # marker's FUSE write.  The marker remains best-effort, while recovery
        # from a wedged storage call remains bounded.
        _request_graceful_shutdown(
            _shutdown_requested, logger=_server_log)
    # Passing a custom shutdown_trigger to hypercorn_serve suppresses
    # Hypercorn's own signal handlers, so we own these signals here and
    # funnel them into the same graceful-drain flag. SIGHUP is included so
    # closing a terminal the server was (wrongly) attached to drains cleanly
    # instead of killing it — see graceful_shutdown_signals().
    for _sig in graceful_shutdown_signals():
        safe_signal(_sig, _signal_shutdown)

    # A request/HEAD watcher may initiate an in-place restart from a worker
    # thread, but it must never call execv there.  Route the request through
    # Hypercorn's shutdown trigger so Quart releases every declared runtime
    # owner (especially the Storage Sidecar) before the main thread replaces
    # this process image.
    from lib.server_reexec import install_server_reexec_shutdown_requester

    def _request_reexec_shutdown(_reason):
        _request_graceful_shutdown(
            _shutdown_requested,
            logger=_server_log,
            reason='restart',
        )

    install_server_reexec_shutdown_requester(_request_reexec_shutdown)

    # ── Write-freshness snapshot on clean exit ──
    # Signal-path restarts (SIGTERM/SIGINT/SIGHUP drain → normal exit) run
    # atexit; the re-exec path does NOT (execv) and saves explicitly in
    # routes/api_v1/update.py::_perform_server_reexec instead.
    try:
        import atexit as _atexit2
        from lib import write_freshness as _wf_mod
        _atexit2.register(_wf_mod.save_snapshot)
    except Exception as _e:
        _server_log.warning('[Server] write-freshness snapshot hook failed: %s', _e)

    # On an in-place restart (re-exec), the previous image's listener may
    # still be draining on the original port for a fraction of a second.
    # _deferred_reexec stamps the port it was serving into _TOFU_REEXEC_PORT;
    # honor it by WAITING for that exact port to free up rather than letting
    # the connect-probe mistake our own lingering socket for a foreign one
    # and shift to the next port (15000 → 15001 → …).
    _reexec_port_env = (os.environ.get('_TOFU_REEXEC_PORT', '') or '').strip()
    os.environ.pop('_TOFU_REEXEC_PORT', None)
    if _reexec_port_env:
        try:
            port = int(_reexec_port_env)
        except (ValueError, TypeError) as _e:
            _server_log.debug('[Server] bad _TOFU_REEXEC_PORT %r, using %s: %s',
                              _reexec_port_env, args.port, _e)
            port = args.port
        if _wait_port_free(host, port):
            _server_log.info('[Restart] Reclaimed original port %d', port)
        else:
            _server_log.critical(
                '[Restart] Original port %d is still busy after the bounded '
                'wait; refusing to shift endpoints', port)
            raise SystemExit(1)
    else:
        if os.environ.get('TOFU_SERVER_WORKER') == '1':
            # The manager owns a stable configured endpoint. Moving to 15001+
            # hides a foreign listener and collides with the manager API; fail
            # clearly and let its conflict/crashloop state explain the cause.
            if not _wait_port_free(host, args.port, timeout=0.1):
                _server_log.critical(
                    '[Server] configured port %d is already in use; managed '
                    'workers never shift ports', args.port)
                raise SystemExit(1)
            port = args.port
        else:
            port = _find_free_port(start=args.port)
            if port != args.port:
                _server_log.info('Port %d in use — using %d', args.port, port)

    # Record the port we actually bound so an in-place restart (re-exec)
    # can reclaim it instead of re-probing. Read by _deferred_reexec in
    # routes/api_v1/update.py.
    os.environ['_TOFU_RUNTIME_PORT'] = str(port)
    # The effective bind host, for the LAN-discovery responder's honesty
    # guard: advertising http://<lan-ip> while bound loopback-only would
    # send every discovering agent to a dead address.
    os.environ['_TOFU_RUNTIME_HOST'] = host

    # ── TLS / HTTP/2 setup ──
    # Auto-detect cloud-IDE / notebook reverse-proxy environments.
    # These proxies provide their own HTTPS+HTTP/2 on the public URL and
    # connect to our backend over plain HTTP. Adding TLS on our side causes
    # "socket hang up" because the proxy doesn't expect a TLS handshake.
    _behind_proxy, _proxy_name = _detect_reverse_proxy()
    _vscode_proxy = os.environ.get('VSCODE_PROXY_URI', '')
    _use_tls, _tls_reason, _invalid_tls_value = _resolve_tls_policy(
        no_tls=args.no_tls,
        tls_value=_tls_value,
        certfile=args.certfile,
        keyfile=args.keyfile,
        behind_proxy=_behind_proxy,
    )
    _force_no_tls = _tls_reason in ('command-line-disabled', 'explicitly-disabled')
    if _invalid_tls_value:  # defensive: listener validation above owns this path
        parser.error(f'unsupported TOFU_TLS={_invalid_tls_value!r}')

    if not _use_tls:
        _tls_cert, _tls_key = None, None
        if _tls_reason == 'reverse-proxy':
            _boot('TLS disabled — %s proxy detected (provides its own HTTPS). '
                  'Force with TOFU_TLS=1.', _proxy_name or 'cloud IDE')
        elif _force_no_tls:
            _boot('TLS disabled (--no-tls or TOFU_TLS=0).')
        else:
            _boot('TLS disabled by proxy-safe default. Set TOFU_TLS=1 for '
                  'direct HTTPS/HTTP2 or configure a trusted TLS ingress.')
    else:
        try:
            _tls_cert, _tls_key = _ensure_tls_certs(
                args.certfile,
                args.keyfile,
                bind_host=host,
                data_root=_tofu_data_root(),
                logger=logging.getLogger('server.tls'),
                boot=_boot,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(f'TLS setup failed: {exc}')

    # Persist the protocol we are ACTUALLY serving (after the certs are
    # settled, so a cert-generation failure records 'http' correctly).
    _record_serve_mode('https' if (_tls_cert and _tls_key) else 'http')
    _has_tls = bool(_tls_cert and _tls_key)

    def _announce_ready(mcp_config, feishu_ok):
        from lib.server_boot_report import announce_server_ready
        return announce_server_ready(
            host=host,
            port=port,
            tls_enabled=_has_tls,
            configured_cert=bool(args.certfile),
            tls_requested=_use_tls,
            behind_proxy=_behind_proxy,
            force_no_tls=_force_no_tls,
            vscode_proxy=_vscode_proxy,
            feishu_ok=feishu_ok,
            mcp_config=mcp_config,
            auth_mode=_AUTH_MODE,
            bootstrap_token=_BOOTSTRAP_TOKEN,
            boot_started_at=_BOOT_T0,
            data_root=_tofu_data_root(),
            boot=_boot,
            logger=_server_log,
            boot_logger=_boot_logger,
        )

    # ── Configure Hypercorn at a testable transport boundary ──
    from lib.hypercorn_runtime import build_hypercorn_config
    hconfig = build_hypercorn_config(
        host, port,
        keep_alive_timeout=http_keep_alive_timeout_seconds(),
        tls_cert=_tls_cert if _has_tls else '',
        tls_key=_tls_key if _has_tls else '',
        logger=_server_log,
    )

    # ── Run ──
    async def _serve():
        register_server_runtime_lifecycle(
            app,
            shutdown_requested=_shutdown_requested,
            announce_ready=_announce_ready,
            host=host,
            port=port,
        )

        # Bridge the SIGTERM threading.Event to an async trigger Hypercorn
        # awaits. When set, Hypercorn stops accepting new connections and
        # drains in-flight ones within graceful_timeout. Poll cheaply (the
        # signal handler can't touch loop state directly from a thread).
        async def _shutdown_trigger():
            while not _shutdown_requested.is_set():
                await asyncio.sleep(0.25)

        await hypercorn_serve(app, hconfig, shutdown_trigger=_shutdown_trigger)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        _server_log.info('[Server] Received SIGINT — shutting down…')

    # Normal signal/manual shutdowns have no pending request and simply exit.
    # For an in-place restart, reaching this line proves Hypercorn stopped and
    # Quart's production shutdown stack completed; only now may exec preserve
    # the worker PID without preserving any child authority or live socket.
    from lib.server_reexec import execute_pending_server_reexec
    try:
        _reexec_attempted = execute_pending_server_reexec(
            lifecycle_stopped=True,
            logger=_server_log,
        )
    except RuntimeError as _reexec_error:
        _server_log.critical(
            '[Restart] Graceful re-exec handoff failed: %s; exiting so the '
            'lifecycle manager can restore service',
            _reexec_error,
        )
        raise SystemExit(1) from _reexec_error
    if _reexec_attempted:
        # os.execv cannot return on success.  This branch is defensive for an
        # injected/foreign implementation and must not leave a stopped server
        # process pretending to be healthy.
        _server_log.critical(
            '[Restart] execv returned without replacing the process image')
        raise SystemExit(1)
