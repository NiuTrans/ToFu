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
    if any(arg in ('-h', '--help') for arg in sys.argv[1:]):
        sys.stdout.write(
            'usage: python server.py [--host HOST] [--port PORT] [--no-tls] '
            '[--certfile FILE --keyfile FILE]\n\n'
            'Starts Tofu through the project-local manager. Operations: '
            'python serverctl.py {status,stop,restart,logs,doctor}\n')
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


def _install_numeric_thread_defaults() -> int:
    """Bound implicit BLAS/OpenMP pools before NumPy or ML imports.

    High-core personal hosts otherwise make OpenBLAS eagerly retain one native
    worker per visible CPU (64 in the measured deployment) even while Tofu is
    idle.  Tofu already owns request, DB, agent, and tool executors; an
    additional host-sized pool per numeric runtime causes oversubscription and
    needless thread stacks under memory pressure. Explicit library variables
    always win. ``TOFU_NUMERIC_THREADS`` changes only the zero-config default.
    """
    raw = os.environ.get('TOFU_NUMERIC_THREADS', '4')
    try:
        workers = int(raw or '4')
    except (TypeError, ValueError):
        workers = 4
    workers = max(1, min(32, workers))
    value = str(workers)
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ.setdefault(name, value)
    return workers


# Must run before the first ``lib`` import: route/plugin discovery eventually
# imports NumPy, at which point OpenBLAS has already fixed its native pool.
_NUMERIC_THREADS = _install_numeric_thread_defaults()

# Internal process marker (NOT a user knob — the owner directive 2026-08-05:
# plain `python server.py` carries everything). lib.database's local-primary
# migration gates on this: only the server's own boot may stop/start clusters
# and flip the primary. A side process that merely imports lib.database (agent
# probes, tooling) must never fire it — measured 2026-08-05: two bare imports
# each burned a full 46 GB dump+restore attempt. Must precede lib imports.
os.environ.setdefault('TOFU_SERVER_PROCESS', '1')

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
    header = '=== faulthandler armed pid=%d at %s ===\n' % (
        os.getpid(), time.strftime('%Y-%m-%d %H:%M:%S'))

    # Arm tmpfs first, before importing even the lightweight writable-path
    # resolver.  This preserves early native-crash capture while ensuring a
    # fresh/XDG or frozen install places its durable file beside all other logs
    # instead of writing into a source/read-only bundle.
    try:
        _fault_shm_log = open(_FAULT_SHM_PATH, 'w+', buffering=1)
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


# ── Faulthandler-sink hygiene + event-loop stall detection (pure helpers) ──
# These back the boot-time /dev/shm prune and the loop-stall watchdog wired up
# inside _serve(). Kept at module scope (not nested in _serve) so they are pure
# and unit-testable without a running loop — see tests/test_loop_stall_watchdog.py.
_FAULT_DUMP_PREFIX = 'tofu_faulthandler_'
_FAULT_DUMP_SUFFIX = '.log'


def _pid_alive(pid):
    """Best-effort liveness probe for *pid* (signal 0). Conservative: an
    ambiguous OSError (other than 'no such process') reports True so we never
    delete a dump whose owner might still be running."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False  # pid out of representable range → cannot be a live process
    except PermissionError:
        return True   # exists but owned by another user
    except OSError:
        return True   # ambiguous — err on the side of keeping
    return True


def _read_instance_lock_entry(lock_path):
    """Read the ``<pid>@<host>`` first line of the single-instance lock file.

    Returns ``(pid:int|None, host:str|None)``. A missing/empty/malformed file
    yields ``(None, None)`` (or ``(None, host)`` if only the pid is unparseable).
    """
    try:
        with open(lock_path, 'r') as f:
            entry = (f.readline() or '').strip()
    except OSError:
        return None, None
    if not entry or '@' not in entry:
        return None, None
    pid_str, _, host = entry.partition('@')
    host = host.strip() or None
    try:
        return int(pid_str), host
    except (ValueError, TypeError):
        return None, host


def _pid_is_live_server(pid):
    """True iff *pid* is alive AND its ``/proc/<pid>/cmdline`` still looks like
    our ``server.py``.

    A dead pid → False. A live pid whose cmdline is provably NOT ``server.py``
    (PID reuse) → False. If liveness or the cmdline cannot be established
    (no /proc, permission denied, empty cmdline) this conservatively returns
    True so we NEVER reclaim a lock whose owner might still be a running server.
    Mirrors stop.sh's ``kill -0`` + ``ps -o args`` server.py check.
    """
    if not _pid_alive(pid):
        return False
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', 'replace')
    except (OSError, ValueError):
        return True  # cannot inspect → assume a live server, refuse to reclaim
    if not cmdline.strip():
        return True  # ambiguous → conservative
    return 'server.py' in cmdline


# ── Loop-heartbeat sidecar (cross-process wedge detection for lock reclaim) ──
# A ``flock`` proves neither liveness nor HEALTH: a server whose event loop is
# wedged in a FUSE syscall (the proven root cause of the 5-minute restart
# stalls) is still alive, still ``server.py``, still holds the flock — so
# ``_pid_is_live_server`` reports True and the reclaim refuses, blocking the
# operator's restart. The fix is a second signal: the live loop persists a
# wall-clock heartbeat to a sidecar; a RESTARTING process reads it to tell a
# healthy holder (fresh heartbeat → refuse) from a wedged one (stale → reclaim).
#
# The sidecar lives on LOCAL disk, NOT under data/ (the FUSE mount that
# wedges): the reader runs in the restarting process DURING the exact FUSE
# stall we're detecting and must never block. Local xfs (``/tmp/tofu``) reads
# cannot block, and a loop wedged in a FUSE syscall simply stops REFRESHING
# the local file → its age grows → that IS the wedged signal. Wall-clock (not
# monotonic) because a DIFFERENT process interprets it.
_HEARTBEAT_FILE = 'server.heartbeat'


def _heartbeat_dir():
    """Local-disk directory for the loop-heartbeat sidecar (see block comment).

    Overridable via ``TOFU_HEARTBEAT_DIR``; defaults to ``<TOFU_DB_LOCAL_ROOT
    or /tmp/tofu>/heartbeat`` so it shares the same POSIX-correct local volume
    the DB local-primary split targets.
    """
    d = (os.environ.get('TOFU_HEARTBEAT_DIR', '') or '').strip()
    if d:
        return d
    root = (os.environ.get('TOFU_DB_LOCAL_ROOT', '') or '').strip() or '/tmp/tofu'
    return os.path.join(root, 'heartbeat')


def _heartbeat_path():
    """Absolute path of the heartbeat sidecar file."""
    return os.path.join(_heartbeat_dir(), _HEARTBEAT_FILE)


def _write_heartbeat(pid=None, ts=None, path=None, *, phase='serving'):
    """Atomically stamp ``{pid, ts}`` (wall-clock) into the sidecar.

    ``phase='booting'`` is stamped by the executable immediately after taking
    the instance lock, before importing the database or starting background
    writers.  A contender gives that phase a longer grace period than a stale
    serving-loop heartbeat, so a legitimate schema migration cannot be
    mistaken for a wedged server.  Best-effort: a write failure NEVER raises.
    Atomic (temp + ``os.replace``) means a concurrent reader never sees a
    half-written file. Returns True on success, False on any failure.
    """
    pid = os.getpid() if pid is None else pid
    ts = time.time() if ts is None else ts
    path = path or _heartbeat_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = '%s.%d.tmp' % (path, pid)
        with open(tmp, 'w') as f:
            f.write(json.dumps({'pid': pid, 'ts': ts, 'phase': phase}))
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError) as e:
        logging.getLogger('server').debug('[Heartbeat] write failed (%s) — '
                                          'letting the sidecar age', e)
        return False


def _read_heartbeat(path=None):
    """Read ``(pid:int|None, ts:float|None)`` from the sidecar.

    A missing / unreadable / unparseable file yields ``(None, None)`` — the
    normal case when no server is running and also the fail-safe for the
    reclaim decision (ambiguity → never claim wedge).
    """
    path = path or _heartbeat_path()
    try:
        with open(path) as f:
            data = json.loads(f.read() or '{}')
        pid = data.get('pid')
        ts = data.get('ts')
        return (int(pid) if pid is not None else None,
                float(ts) if ts is not None else None)
    except (OSError, ValueError, TypeError) as e:
        logging.getLogger('server').debug('[Heartbeat] read failed/absent: %s', e)
        return None, None


def _read_heartbeat_state(path=None):
    """Return the heartbeat record while keeping ``_read_heartbeat`` stable.

    Old sidecars without ``phase`` are serving heartbeats. Invalid records are
    ambiguous and therefore return ``None`` (the reclaim path fails safe).
    """
    path = path or _heartbeat_path()
    try:
        with open(path) as f:
            data = json.loads(f.read() or '{}')
        pid = int(data['pid'])
        ts = float(data['ts'])
        phase = str(data.get('phase') or 'serving')
        if phase not in ('booting', 'serving'):
            return None
        return {'pid': pid, 'ts': ts, 'phase': phase}
    except (OSError, KeyError, ValueError, TypeError):
        return None


def _heartbeat_stale_threshold():
    """Seconds after which a heartbeat proves the loop is wedged.

    Conservative: ``max(30s, 3 × TOFU_LOOP_HEARTBEAT_SECS)`` — well beyond any
    healthy GC pause or momentary busy stretch, so a genuinely-running server
    is never falsely reclaimed.
    """
    try:
        bump = float(os.environ.get('TOFU_LOOP_HEARTBEAT_SECS', '') or '1')
    except (ValueError, TypeError):
        bump = 1.0
    if bump <= 0:
        bump = 1.0
    return max(30.0, bump * 3.0)


def _boot_heartbeat_stale_threshold():
    """Grace for a lock holder that has not reached the serving loop yet.

    Production startup can legitimately spend tens of seconds migrating and
    verifying a 20 GB SQLite authority.  Keep the bound finite so a process
    genuinely wedged during import remains reclaimable without weakening the
    normal 30-second serving-loop detector.
    """
    try:
        value = float(os.environ.get('TOFU_BOOT_HEARTBEAT_GRACE_SECS', '') or '180')
    except (ValueError, TypeError):
        value = 180.0
    return max(60.0, min(900.0, value))


_SERVE_MODE_FILE = '.last_serve_mode'

def _serve_mode_path():
    """Absolute path of the serve-mode sidecar (data/.last_serve_mode)."""
    return os.path.join(_tofu_data_root(), _SERVE_MODE_FILE)


def _record_serve_mode(mode, path=None):
    """Persist the protocol we are ACTUALLY serving ('http'|'https') so the
    watchdog (deploy/tofu_guard.sh) can (a) probe /api/health with the right
    scheme and (b) replay the same TLS decision on auto-relaunch — a
    cron-env relaunch re-runs _detect_reverse_proxy blind and came up TLS
    behind a plain-HTTP proxy (the 2026-08-03 'socket hang up' incident).
    Best-effort: a write failure must never block startup."""
    if mode not in ('http', 'https'):
        raise ValueError('serve mode must be http|https, got %r' % (mode,))
    path = path or _serve_mode_path()
    try:
        from lib.json_store import write_text_atomic
        write_text_atomic(path, mode + '\n')
    except Exception as e:
        logging.getLogger('server').warning(
            '[TLS] could not record serve mode to %s: %s', path, e)


def _holder_wedge_age(pid, now=None, path=None):
    """Return the heartbeat AGE (seconds) iff the sidecar PROVES *pid*'s event
    loop is wedged, else None.

    "Proves" = the heartbeat belongs to *pid* (its recorded pid matches, so we
    never judge a live server by a stale file from a DIFFERENT process) AND its
    wall-clock age exceeds ``_heartbeat_stale_threshold()``. Every ambiguous
    case — missing / unparseable file, mismatched pid, or a future-dated ts
    (clock skew) — returns None so the caller keeps today's refuse-to-reclaim
    behaviour. The age is returned (not just a bool) so the caller can log the
    concrete staleness.
    """
    state = _read_heartbeat_state(path)
    if state is None or state['pid'] != pid:
        return None
    now = time.time() if now is None else now
    age = now - state['ts']
    threshold = (_boot_heartbeat_stale_threshold()
                 if state['phase'] == 'booting'
                 else _heartbeat_stale_threshold())
    if age < 0 or age <= threshold:
        return None
    return age


def _reclaim_stale_instance_lock(lock_path, hostname, logger):
    """Decide whether a flock-contended instance lock is a STALE *local* lock we
    may reclaim, and if so unlink it so a fresh inode can be flock'd.

    Robustness rationale (the crux of the OOM-restart bug): ``flock`` is bound
    to an open file *description*, NOT to process liveness. When the previous
    server is SIGKILL'd (e.g. OOM) its atexit/lock-release never runs, and
    orphaned child processes may keep the fd — and thus the flock — open
    indefinitely; on a FUSE mount the advisory lock is not reliably released on
    unclean death either. So a contended flock does NOT prove "a server is
    running". We mirror stop.sh: read the recorded ``<pid>@<host>`` and ONLY
    when ``host == this machine`` AND that pid is not a live ``server.py`` do we
    ``unlink`` the lock path. Unlinking yields a brand-new inode on the retry;
    the orphan's surviving fd points at the now-unlinked OLD inode, so its
    lingering flock is harmless and our flock on the new inode succeeds.

    Cross-host staleness is deliberately NOT handled here (that is the PG
    heartbeat-takeover's domain) — a foreign-host lock is left untouched and the
    caller refuses to start.

    Returns True iff a stale local lock was unlinked (caller should retry the
    flock), else False.
    """
    pid, host = _read_instance_lock_entry(lock_path)
    if pid is None and host is None:
        logger.critical('[Lock] contended instance lock has no readable <pid>@<host> entry — '
                        'refusing to reclaim (a live peer may hold it)')
        return False
    if host and host != hostname:
        logger.critical('[Lock] instance lock held by another host: pid=%s host=%s (we are %s) — '
                        'refusing to reclaim a foreign lock (cross-host is PG-heartbeat territory)',
                        pid, host, hostname)
        return False
    if pid is not None and _pid_is_live_server(pid):
        # A live local server.py normally means "genuinely running" — refuse.
        # BUT a loop wedged in a FUSE syscall is ALSO live+server.py yet cannot
        # serve or release its lock (the 5-minute-restart-stall root cause). The
        # heartbeat sidecar is the tie-breaker: only when it PROVES this pid's
        # loop has been silent past the stale threshold do we treat the holder
        # as wedged and reclaim. Fresh / missing / ambiguous heartbeat → keep
        # the refuse (fail-safe: never reclaim a possibly-healthy server).
        wedge_age = _holder_wedge_age(pid)
        if wedge_age is None:
            logger.critical('[Lock] instance lock held by a LIVE local server (pid=%s host=%s) — '
                            'another instance is genuinely running', pid, host)
            return False
        logger.critical('[Lock] instance lock held by a WEDGED local server '
                        '(pid=%s host=%s) — loop heartbeat stale %.1fs (threshold=%.1fs); '
                        'reclaiming so a fresh instance can start', pid, host,
                        wedge_age, _heartbeat_stale_threshold())
    else:
        logger.warning('[Lock] reclaiming stale lock pid=%s host=%s (dead)', pid, host)
    try:
        os.unlink(lock_path)
    except OSError as e:
        logger.critical('[Lock] failed to unlink stale lock %s: %s', lock_path, e)
        return False
    return True


def _acquire_instance_lock(lock_path, logger, hostname=None, allow_reclaim=True,
                           *, mark_booting=False):
    """Acquire the exclusive single-instance lock at *lock_path*.

    Returns ``(ok, fd)``: ``(True, <open flocked fd>)`` on success — the caller
    MUST keep the fd open for the whole process lifetime — or ``(False, None)``
    when a live instance genuinely holds it. On a platform without ``fcntl`` /
    with an unopenable lock dir it degrades to best-effort ``(True, fd|None)``
    so a missing lock never blocks startup.

    Self-healing: on flock contention we do NOT assume a live server (see
    ``_reclaim_stale_instance_lock`` for why). If the recorded owner is a dead
    LOCAL pid we unlink the stale lock and retry ONCE on a fresh inode. A
    single bounded retry (``allow_reclaim=False``) guarantees no reclaim loop;
    if the retry still fails we log CRITICAL and refuse (caller surfaces the
    ``TOFU_SKIP_LOCK=1`` escape hatch).
    """
    if hostname is None:
        import socket as _s
        hostname = _s.gethostname()
    try:
        import fcntl
    except ImportError:
        logger.warning('[Lock] fcntl unavailable on this platform — skipping instance lock')
        try:
            return True, open(lock_path, 'a+')
        except OSError:
            return True, None
    try:
        if not os.path.exists(lock_path):
            open(lock_path, 'a').close()
        fd = open(lock_path, 'r+')
    except OSError as e:
        logger.warning('[Lock] cannot open lock file %s (%s) — proceeding without instance lock', lock_path, e)
        return True, None
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        fd.close()
        if allow_reclaim and _reclaim_stale_instance_lock(lock_path, hostname, logger):
            ok2, fd2 = _acquire_instance_lock(
                lock_path, logger, hostname=hostname, allow_reclaim=False,
                mark_booting=mark_booting)
            if ok2 and fd2 is not None:
                logger.info('[Lock] reclaimed stale lock and acquired fresh instance lock (pid=%d)', os.getpid())
            else:
                logger.critical('[Lock] reclaimed stale lock but STILL could not acquire flock — '
                                'refusing to start. Set TOFU_SKIP_LOCK=1 to override.')
            return ok2, fd2
        return False, None
    try:
        fd.seek(0)
        fd.truncate()
        fd.write('%d@%s\n' % (os.getpid(), hostname))
        fd.flush()
    except OSError as e:
        logger.debug('[Lock] could not stamp lock identity: %s', e)
    if mark_booting:
        _write_heartbeat(pid=os.getpid(), phase='booting')
    return True, fd


def _parse_fault_dump_pid(basename):
    """Extract the pid from ``tofu_faulthandler_<pid>.log`` (else None)."""
    if not basename.startswith(_FAULT_DUMP_PREFIX) or not basename.endswith(_FAULT_DUMP_SUFFIX):
        return None
    core = basename[len(_FAULT_DUMP_PREFIX):-len(_FAULT_DUMP_SUFFIX)]
    try:
        return int(core)
    except (ValueError, TypeError):
        return None


def _prune_stale_fault_dumps(directory='/dev/shm', keep_basename='',
                             pid_alive=_pid_alive, logger=None):
    """Delete ``tofu_faulthandler_<pid>.log`` files in *directory* whose pid is
    no longer alive. Never touches *keep_basename* (our own live sink) or files
    that don't match the naming pattern. Returns the number removed.

    server.py opens one such file on every boot but historically never removed
    old ones, so the /dev/shm sink accumulated thousands of dead-pid files."""
    import glob as _glob
    removed = 0
    pattern = os.path.join(directory, _FAULT_DUMP_PREFIX + '*' + _FAULT_DUMP_SUFFIX)
    for path in _glob.glob(pattern):
        base = os.path.basename(path)
        if keep_basename and base == keep_basename:
            continue
        pid = _parse_fault_dump_pid(base)
        if pid is None or pid_alive(pid):
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError as _rm_err:
            if logger is not None:
                logger.debug('[LoopWatch] could not prune %s: %s', path, _rm_err)
    return removed


def _fault_dump_limits():
    """Return bounded per-file and stale-dump retention settings."""
    def _integer(name, default, minimum, maximum):
        try:
            value = int(os.environ.get(name, '') or default)
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    return {
        'active_bytes': _integer(
            'TOFU_FAULT_DUMP_MAX_BYTES', 16 * 1024 * 1024,
            1 * 1024 * 1024, 256 * 1024 * 1024),
        'stale_files': _integer(
            'TOFU_FAULT_DUMP_FILES', 8, 1, 64),
        'stale_bytes': _integer(
            'TOFU_FAULT_DUMP_TOTAL_BYTES', 64 * 1024 * 1024,
            4 * 1024 * 1024, 1024 * 1024 * 1024),
    }


def _trim_fault_sink_if_oversize(sink, max_bytes, *, header=''):
    """Reuse a live fd while bounding repeated recoverable stall dumps.

    Replacing the path is unsafe because the C-level faulthandler retains the
    old descriptor.  Truncating only after its one-shot timer is cancelled
    preserves descriptor identity and keeps the next capture on the named
    file.  Returns True when a trim occurred.
    """
    if sink is None or max_bytes <= 0:
        return False
    try:
        sink.flush()
        if os.fstat(sink.fileno()).st_size <= max_bytes:
            return False
        sink.seek(0)
        sink.truncate(0)
        if header:
            sink.write(header)
        sink.flush()
        return True
    except (OSError, ValueError):
        return False


def _reset_fault_sink(sink, *, header=''):
    """Keep only the newest manual durable dump on a live sink fd."""
    if sink is None:
        return False
    try:
        sink.flush()
        sink.seek(0)
        sink.truncate(0)
        if header:
            sink.write(header)
        sink.flush()
        return True
    except (OSError, ValueError):
        return False


def _prune_fault_dump_budget(directory, *, keep_basename='', pid_alive=_pid_alive,
                             max_dead_files=8, max_dead_bytes=64 * 1024 * 1024):
    """Keep newest dead-process evidence within count and byte budgets.

    Live process files, the current sink and unrelated files are never
    touched.  Unlike the historical prune-all helper, retaining the newest
    dead files means the next boot does not erase the crash it is meant to
    diagnose.  Returns the number of old dead dumps removed.
    """
    import glob as _glob
    pattern = os.path.join(
        directory, _FAULT_DUMP_PREFIX + '*' + _FAULT_DUMP_SUFFIX)
    dead = []
    for path in _glob.glob(pattern):
        base = os.path.basename(path)
        if keep_basename and base == keep_basename:
            continue
        pid = _parse_fault_dump_pid(base)
        if pid is None or pid_alive(pid):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        dead.append((stat.st_mtime_ns, base, path, stat.st_size))

    removed = 0
    kept_files = 0
    kept_bytes = 0
    for _mtime, _base, path, size in sorted(dead, reverse=True):
        fits = (kept_files < max(0, int(max_dead_files))
                and kept_bytes + size <= max(0, int(max_dead_bytes)))
        if fits:
            kept_files += 1
            kept_bytes += size
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


from lib.server_runtime_probes import stall_pressure_context


def _stall_pressure_context():
    """Compatibility hook consumed by the extracted loop watchdog owner."""
    return stall_pressure_context()


def _loop_stall_decide(age, threshold, already_dumped):
    """Pure decision for the loop-stall watchdog.

    Given the heartbeat *age* (seconds since the last on-loop bump), the stall
    *threshold*, and whether we've *already_dumped* for the current stall
    episode, return ``(should_dump, next_already_dumped)``. Emits at most one
    dump per contiguous stall episode and re-arms once the loop recovers."""
    if threshold <= 0:
        return (False, already_dumped)   # watchdog disabled
    if age <= threshold:
        return (False, False)            # healthy → re-arm for the next episode
    if already_dumped:
        return (False, True)             # still stalled, already captured
    return (True, True)                  # stalled and not yet captured → dump


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


def _extract_loop_top_frame(frame, project_root=None):
    """Pure: given the event-loop thread's current frame, return a one-line
    ``file:line in func`` locator for the STALL culprit.

    Walks OUTWARD from the innermost frame and returns the first frame whose
    file lives under *project_root* (our own code) — i.e. the deepest
    application frame, skipping stdlib/site-packages leaf frames like
    ``ssl.read`` so the audit line names ``segment_backfill.py:257`` rather
    than a generic C-level socket read. Falls back to the innermost frame when
    none match (all-stdlib stall). Returns ``''`` when *frame* is None.

    Kept pure + arg-injected (no globals) so a unit test can build a synthetic
    frame chain and assert the culprit is picked without a real stall.
    """
    if frame is None:
        return ''
    if project_root is None:
        project_root = os.path.dirname(os.path.abspath(__file__))
    innermost = None
    f = frame
    while f is not None:
        code = f.f_code
        fname = code.co_filename
        if innermost is None:
            innermost = '%s:%d in %s' % (fname, f.f_lineno, code.co_name)
        try:
            in_project = os.path.abspath(fname).startswith(project_root + os.sep)
        except Exception:
            in_project = False
        if in_project and 'site-packages' not in fname:
            return '%s:%d in %s' % (fname, f.f_lineno, code.co_name)
        f = f.f_back
    return innermost or ''


def _should_arm_ctimer(threshold, sink):
    """Pure gate for the GIL-INDEPENDENT capture path.

    ``faulthandler.dump_traceback_later`` runs from a dedicated C timer thread
    that does NOT acquire the GIL, so it fires even when the loop is wedged
    inside a single monolithic GIL-holding C call (the documented ``json.dumps``
    / catastrophic-regex pit) — the exact case the Python-thread watcher, which
    must take the GIL to run, is BLIND to. Arm it only when the watchdog is
    enabled (*threshold* > 0) AND we have a sink with a real file descriptor
    (``dump_traceback_later`` requires an fd — an in-memory buffer has none)."""
    if threshold is None or threshold <= 0:
        return False
    if sink is None:
        return False
    try:
        sink.fileno()
    except Exception:
        return False
    return True



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
# All .so files (C extensions, libpython, libc) are dlopen'd via mmap with
# demand-paged code segments. When those files live on a FUSE mount, a
# transient stall during a lazy page-in delivers SIGBUS (unrecoverable).
# MCL_CURRENT pins already-mapped pages; MCL_FUTURE pins every future mmap
# at load time, collapsing the dangerous demand-fault window to zero.
#
# BUT pinned pages are unreclaimable and are charged against the cgroup
# memory limit. On a memory-constrained container (e.g. an exported copy
# on a small box) pinning the whole C-extension working set can push RSS
# past memory.max → the OOM killer SIGKILLs the process at boot (a bare
# "Killed" with no traceback). mlockall only HELPS on a FUSE mount and is
# only SAFE with headroom under the cgroup limit, so we gate on both the
# limit AND live usage: on a SHARED cgroup the ceiling can be the whole
# machine yet already ~full, and pinning there both adds unreclaimable pages
# and inflates our oom_score so the killer targets us first — so we also skip
# when the cgroup is already past TOFU_MLOCK_MAX_USAGE_PCT (default 85%) full.
# Override: TOFU_MLOCK=1 forces it on, =auto enables the legacy headroom-gated
# mode.  The production default is OFF: MCL_FUTURE locks every later mmap and
# allocation, so a healthy-looking boot can grow into tens of GiB of
# unreclaimable memory hours later.  A one-shot startup headroom check cannot
# make that safe.  Operators with a proven FUSE SIGBUS workload can still opt
# into the bounded-by-cgroup legacy policy explicitly with TOFU_MLOCK=auto.
def _tofu_path_is_fuse(_path):
    """Best-effort: True if *_path* sits on a FUSE filesystem (stdlib-only)."""
    try:
        _path = os.path.abspath(_path)
        _best_mp, _best_fstype = '', ''
        with open('/proc/self/mountinfo', 'r') as _f:
            for _line in _f:
                # mountinfo: "... <mount point> ... - <fstype> <source> ..."
                _halves = _line.split(' - ')
                if len(_halves) != 2:
                    continue
                _left = _halves[0].split()
                _right = _halves[1].split()
                if len(_left) < 5 or not _right:
                    continue
                _mp, _fstype = _left[4], _right[0]
                if (_path == _mp or _path.startswith(_mp.rstrip('/') + '/')) \
                        and len(_mp) >= len(_best_mp):
                    _best_mp, _best_fstype = _mp, _fstype
        return _best_fstype.startswith('fuse')
    except OSError:
        return False


def _tofu_cgroup_mem_limit_bytes():
    """cgroup memory limit in bytes, or None if unlimited/unknown (stdlib-only)."""
    for _p in ('/sys/fs/cgroup/memory.max',                    # cgroup v2
               '/sys/fs/cgroup/memory/memory.limit_in_bytes'):  # cgroup v1
        try:
            with open(_p, 'r') as _f:
                _raw = _f.read().strip()
        except OSError:
            continue
        if _raw == 'max':
            return None
        try:
            _val = int(_raw)
        except ValueError:
            continue
        # cgroup v1 reports a huge sentinel (~PAGE_COUNTER_MAX) for "unlimited"
        if _val <= 0 or _val >= (1 << 62):
            return None
        return _val
    return None


def _tofu_cgroup_mem_usage_bytes():
    """Current cgroup memory usage in bytes, or None if unknown (stdlib-only).

    Includes reclaimable page cache on purpose: a shared cgroup running at the
    cache edge is exactly the contended, spike-prone state where adding
    unreclaimable pinned pages is net-harmful (see _tofu_should_mlock).
    """
    for _p in ('/sys/fs/cgroup/memory.current',                    # cgroup v2
               '/sys/fs/cgroup/memory/memory.usage_in_bytes'):      # cgroup v1
        try:
            with open(_p, 'r') as _f:
                _raw = _f.read().strip()
        except OSError:
            continue
        try:
            _val = int(_raw)
        except ValueError:
            continue
        if _val < 0:
            return None
        return _val
    return None


def _tofu_should_mlock():
    """Decide whether mlockall is worth it. Returns (do_it, reason)."""
    _mode = os.environ.get('TOFU_MLOCK', 'off').strip().lower()
    if _mode in ('0', 'off', 'false', 'no'):
        return False, 'disabled via TOFU_MLOCK=%s' % _mode
    if _mode in ('1', 'on', 'true', 'yes', 'force'):
        return True, 'forced via TOFU_MLOCK=%s' % _mode
    # auto: pin only where the SIGBUS risk is real (project dir OR the conda
    # env holding the .so files is on FUSE) AND there is enough memory
    # headroom that pinning won't trip the OOM killer.
    _on_fuse = (_tofu_path_is_fuse(os.path.dirname(os.path.abspath(__file__)))
                or _tofu_path_is_fuse(sys.prefix))
    if not _on_fuse:
        return False, 'not on FUSE (no SIGBUS risk to mitigate)'
    _limit = _tofu_cgroup_mem_limit_bytes()
    if _limit is None:
        return True, 'on FUSE, cgroup memory unlimited'
    try:
        _min_gb = float(os.environ.get('TOFU_MLOCK_MIN_LIMIT_GB', '8'))
    except ValueError:
        _min_gb = 8.0
    _gib = float(1 << 30)
    if _limit < _min_gb * _gib:
        return False, ('on FUSE but cgroup limit %.1fGiB < %.1fGiB — skipping to avoid '
                       'OOM (set TOFU_MLOCK=1 to force)' % (_limit / _gib, _min_gb))
    # The cgroup limit is generous, but on a SHARED cgroup that ceiling can be
    # the whole machine and already ~full of siblings + FUSE page/slab cache.
    # Pinning here adds unreclaimable pages AND inflates our own oom_score, so
    # the OOM killer picks us first (highest-RSS process in the group). Gate on
    # LIVE headroom: skip if usage already sits above TOFU_MLOCK_MAX_USAGE_PCT
    # (default 85%) of the limit. Unknown usage → proceed (matches prior behaviour).
    _usage = _tofu_cgroup_mem_usage_bytes()
    if _usage is not None and _usage > 0:
        try:
            _max_pct = float(os.environ.get('TOFU_MLOCK_MAX_USAGE_PCT', '85'))
        except ValueError:
            _max_pct = 85.0
        _used_pct = 100.0 * _usage / float(_limit)
        if _used_pct >= _max_pct:
            return False, ('on FUSE but cgroup %.1f%% full (%.1f/%.1fGiB) >= %.0f%% — '
                           'skipping to avoid OOM on a contended shared cgroup '
                           '(set TOFU_MLOCK=1 to force)'
                           % (_used_pct, _usage / _gib, _limit / _gib, _max_pct))
        return True, ('on FUSE, cgroup limit %.1fGiB >= %.1fGiB and %.1f%% used < %.0f%%'
                      % (_limit / _gib, _min_gb, _used_pct, _max_pct))
    return True, 'on FUSE, cgroup limit %.1fGiB >= %.1fGiB (usage unknown)' % (_limit / _gib, _min_gb)


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
# On 2026-07-31 10:33:27 a boot died with
#   ImportError: /lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found
#       (required by .../lxml/../../.././libicuuc.so.75)
# The system /lib64 copy (2019) exports no GLIBCXX_3.4.30; the conda copy does.
# The crash is therefore "the libstdc++.so.6 soname was bound to the system copy
# before libicuuc loaded", and it is NOT reproducible from the environment we
# can still observe: the platform's own LD_PRELOAD (dolphinfs client, set by
# /etc/profile.d/pc_env.sh) was measured clean 10/10, as were an RTLD_GLOBAL
# dlopen of the system copy and a NEEDED-chain pull via libjvm — the loader
# happily maps two libstdc++ copies side by side. Only an explicit LD_PRELOAD
# of the system copy reproduces it.
#
# The failing process died before anything recorded its environment, so the
# trigger is still unknown and cannot be recovered after the fact (ImportError
# is a clean exit, so no core is written). This line closes that gap: the
# binding is already decided HERE — measured, the healthy boot shows the conda
# path and the failing shape shows /usr/lib64, both before any heavy import —
# so recording it now makes the next occurrence diagnosable instead of a
# standing start. Diagnostic only: it changes no behaviour.
#
# Note on the two-branch read: whether libstdc++ is ALREADY mapped this early
# depends on whether something preloaded it. Under the platform's own preload
# (production always has it — /etc/profile.d/pc_env.sh exports it
# unconditionally) it is mapped, and so it is in the failing shape. With no
# preload at all it is not yet mapped, and reporting a bare "not-yet-mapped"
# would record nothing about the binding that is about to be chosen. So when
# it is absent we resolve the soname the way the loader will (ctypes, which
# performs the same search) and label the value as resolved-not-yet-bound. The
# distinction is kept in the output rather than flattened, because "nothing had
# claimed the soname yet" and "this copy owns it" are different facts.
try:
    _stdcxx_paths = []
    with open('/proc/self/maps', 'r') as _mf:
        for _line in _mf:
            if 'libstdc++' in _line:
                _p = _line.rsplit(' ', 1)[-1].strip()
                if _p and _p not in _stdcxx_paths:
                    _stdcxx_paths.append(_p)
    if _stdcxx_paths:
        _stdcxx_state = 'mapped=' + ','.join(_stdcxx_paths)
    else:
        # Not yet bound — ask the loader which copy it WOULD pick.
        try:
            import ctypes as _fx_ctypes
            _fx_ctypes.CDLL('libstdc++.so.6')
            _probe = [l.rsplit(' ', 1)[-1].strip()
                      for l in open('/proc/self/maps') if 'libstdc++' in l]
            _seen = []
            for _p in _probe:
                if _p and _p not in _seen:
                    _seen.append(_p)
            _stdcxx_state = ('would-resolve=' + ','.join(_seen)) if _seen \
                else 'unresolvable'
        except Exception as _fx_e:
            _stdcxx_state = 'probe-failed:%s' % (str(_fx_e)[:80],)
    os.write(2, ('[boot] libstdc++ soname -> %s | LD_PRELOAD=%s | LD_LIBRARY_PATH=%s\n' % (
        _stdcxx_state,
        (os.environ.get('LD_PRELOAD') or '<unset>'),
        (os.environ.get('LD_LIBRARY_PATH') or '<unset>'),
    )).encode(errors='replace'))
except Exception:
    # Forensics must never be able to break a boot it only observes.
    pass

# Retained so the crash hook can attach it to the CRITICAL record. stderr alone
# is NOT enough: measured 2026-07-31, seven GLIBCXX crashes landed in
# logs/error.log (written by the logging handlers) while server_15000.log — the
# only file the watchdog redirects stderr into — had not been touched since
# 10:33. Boots started by anything other than the watchdog send stderr to a
# terminal or pipe that nobody keeps, so a stderr-only forensic line is absent
# from precisely the crash reports an operator actually reads.
try:
    _TOFU_LINKAGE_FORENSICS = (
        'libstdc++ soname -> %s | LD_PRELOAD=%s | LD_LIBRARY_PATH=%s' % (
            _stdcxx_state,
            (os.environ.get('LD_PRELOAD') or '<unset>'),
            (os.environ.get('LD_LIBRARY_PATH') or '<unset>')))
except Exception:
    _TOFU_LINKAGE_FORENSICS = 'libstdc++ soname -> unavailable'

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
    env_path = os.path.join(_PROJ_DIR, '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if key not in os.environ:
                os.environ[key] = value

_load_dotenv()


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
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

# LOG_DIR must be WRITABLE. In a frozen desktop build BASE_DIR is the read-only
# bundle root, so route logs to the writable root (see lib/runtime_paths).
from lib.runtime_paths import data_root as _tofu_data_root, logs_root as _tofu_logs_root
LOG_DIR = _tofu_logs_root()
os.makedirs(LOG_DIR, exist_ok=True)

# Acquire the process authority before importing ``lib.log_aggregates`` or
# ``lib.database``.  Both can start database-backed background work during
# module initialisation, so taking this lock in the old ``__main__`` block was
# too late: a restart could migrate/verify the canonical database in two live
# processes before one eventually lost the TCP bind race.
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

_LOG_FMT = '%(asctime)s [%(levelname)s] %(name)s [%(threadName)s]: %(message)s'
_LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'
_formatter = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)

# 'tofu_search' is the extracted search/fetch library (sibling package). Its
# loggers carry first-class business diagnostics — the per-engine result
# counts, the streaming-fetch race-to-N decisions, the LLM content-filter
# reductions, and the step-by-step pipeline timing breakdown that explains WHY
# a search took N seconds. Treat it as business (→ app.log INFO, error.log
# WARNING+), NOT vendor: routing it to vendor.log at WARNING-only (the old
# behaviour) discarded all the INFO pipeline detail an operator needs to
# diagnose a slow/failed search.
_BIZ_PREFIXES = ('lib.', 'routes.', 'server', 'tofu_search')

class _BizOnly(logging.Filter):
    def filter(self, record):
        return record.name.startswith(_BIZ_PREFIXES)

class _VendorOnly(logging.Filter):
    def filter(self, record):
        return (not record.name.startswith(_BIZ_PREFIXES)
                and not record.name.startswith('frontend')
                and record.name != 'werkzeug'
                and record.name != 'hypercorn'
                and not record.name.startswith('hypercorn.'))

class _FrontendOnly(logging.Filter):
    """The browser-console relay (/api/v1/logs/client → logger 'frontend').
    Owns its own file: the full client stream (INFO included) is far too
    chatty for app.log, and client warnings must not cry wolf in error.log."""
    def filter(self, record):
        return (record.name == 'frontend'
                or record.name.startswith('frontend.'))

class _BizAndServerOnly(logging.Filter):
    def filter(self, record):
        return (record.name.startswith(_BIZ_PREFIXES)
                or record.name == 'hypercorn'
                or record.name.startswith('hypercorn.'))

class _AccessOnly(logging.Filter):
    def filter(self, record):
        return (record.name == 'hypercorn.access'
                or record.name == 'werkzeug')

class _QuietPollFilter(logging.Filter):
    _NOISY_PATHS = ('/api/chat/poll/', '/api/chat/stream/', '/api/browser/commands')
    def filter(self, record):
        msg = record.getMessage()
        if any(p in msg for p in self._NOISY_PATHS) and '200' in msg:
            return False
        return True


class _SizeAndTimeRotatingFileHandler(TimedRotatingFileHandler):
    """Daily rotation with a per-file ceiling and a whole-family budget.

    ``TimedRotatingFileHandler`` alone lets one runaway day create an
    arbitrarily large file (9.1 GiB happened in production).  Multiple size
    rotations within a day receive numeric suffixes, while the usual date
    names and new-user behaviour remain intact.
    """

    def __init__(self, filename, *, max_bytes, total_budget_bytes, **kwargs):
        self.maxBytes = max(0, int(max_bytes))
        self.totalBudgetBytes = max(0, int(total_budget_bytes))
        super().__init__(filename, **kwargs)
        self._prune_total_budget()

    def shouldRollover(self, record):
        if super().shouldRollover(record):
            return 1
        if self.maxBytes <= 0:
            return 0
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, os.SEEK_END)
        rendered = '%s\n' % self.format(record)
        encoding = self.encoding or 'utf-8'
        try:
            incoming = len(rendered.encode(encoding, errors='replace'))
        except LookupError:
            incoming = len(rendered.encode('utf-8', errors='replace'))
        return 1 if self.stream.tell() + incoming >= self.maxBytes else 0

    def rotation_filename(self, default_name):
        candidate = super().rotation_filename(default_name)
        if not os.path.exists(candidate):
            return candidate
        # A time handler normally owns one file per date. Size rotation can
        # happen repeatedly on that date, so choose a lexically ordered unique
        # suffix instead of deleting/replacing the earlier chunk.
        sequence = 1
        while sequence <= 99999:
            numbered = f'{candidate}.{sequence:05d}'
            if not os.path.exists(numbered):
                return numbered
            sequence += 1
        return f'{candidate}.{time.time_ns()}'

    def doRollover(self):
        super().doRollover()
        self._prune_total_budget()

    def _prune_total_budget(self):
        """Delete oldest *rotated* chunks until the family fits its budget."""
        if self.totalBudgetBytes <= 0:
            return
        directory = os.path.dirname(self.baseFilename) or '.'
        prefix = os.path.basename(self.baseFilename) + '.'
        rotated = []
        total = 0
        try:
            total += os.path.getsize(self.baseFilename)
        except OSError:
            pass
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.join(directory, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            total += stat.st_size
            rotated.append((stat.st_mtime_ns, name, path, stat.st_size))
        for _mtime, _name, path, size in sorted(rotated):
            if total <= self.totalBudgetBytes:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue


_app_handler = _SizeAndTimeRotatingFileHandler(
    os.path.join(LOG_DIR, 'app.log'),
    when='midnight', backupCount=30, encoding='utf-8',
    max_bytes=64 * 1024 * 1024, total_budget_bytes=2 * 1024 * 1024 * 1024)
_app_handler.setFormatter(_formatter)
_app_handler.setLevel(logging.INFO)
_app_handler.addFilter(_BizOnly())

_access_handler = _SizeAndTimeRotatingFileHandler(
    os.path.join(LOG_DIR, 'access.log'),
    when='midnight', backupCount=14, encoding='utf-8',
    max_bytes=32 * 1024 * 1024, total_budget_bytes=512 * 1024 * 1024)
_access_handler.setFormatter(_formatter)
_access_handler.setLevel(logging.INFO)
_access_handler.addFilter(_AccessOnly())

_error_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'error.log'),
    maxBytes=5 * 1024 * 1024, backupCount=10, encoding='utf-8')
_error_handler.setFormatter(_formatter)
_error_handler.setLevel(logging.WARNING)
_error_handler.addFilter(_BizAndServerOnly())

_vendor_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, 'vendor.log'),
    maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
_vendor_handler.setFormatter(_formatter)
_vendor_handler.setLevel(logging.WARNING)
_vendor_handler.addFilter(_VendorOnly())

_frontend_handler = _SizeAndTimeRotatingFileHandler(
    os.path.join(LOG_DIR, 'frontend.log'),
    when='midnight', backupCount=14, encoding='utf-8',
    max_bytes=32 * 1024 * 1024, total_budget_bytes=256 * 1024 * 1024)
_frontend_handler.setFormatter(_formatter)
_frontend_handler.setLevel(logging.INFO)
_frontend_handler.addFilter(_FrontendOnly())

_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(_formatter)
_console_handler.setLevel(logging.WARNING)
_console_handler.addFilter(_BizAndServerOnly())

# ── Log fingerprint aggregation (epic pt_71eaaa8d5b8243e9) ──
# error.log 的去重层:文本文件仍是唯一权威源,本 handler 只把每条
# WARNING+ 记录归一成 (level, logger, 消息模板, 异常签名) 指纹在内存计数,
# 后台 daemon flusher 每 ~15s 批量 upsert 进 log_aggregates 表——DB 失败只
# 丢聚合、fail-open。与 error.log 共用 _BizAndServerOnly 过滤器,聚合覆盖面
# 恒等于 error.log。挂在 QueueListener 线程上(_real_log_handlers),归一化
# CPU 不占请求线程。TOFU_LOG_AGGREGATES=0 全关。
from lib.log_aggregates import (
    FingerprintHandler as _FingerprintHandler,
    enabled as _log_agg_enabled,
    get_default_store as _log_agg_store,
    start_flusher as _log_agg_start_flusher,
    stop_flusher as _log_agg_stop_flusher,
)
_log_agg_handler = _FingerprintHandler(_log_agg_store())
_log_agg_handler.setLevel(logging.WARNING)
_log_agg_handler.addFilter(_BizAndServerOnly())

# ── Non-blocking, memory-bounded logging ──
# The four file handlers + the stderr StreamHandler all do SYNCHRONOUS I/O
# under a per-handler lock. error.log lives on a FUSE/NFS mount (see
# _tofu_logs_root), so a WARNING/ERROR *storm* (e.g. a total upstream 502
# outage emitting thousands of lines) would serialize every logging thread
# behind slow network writes — INCLUDING the sync threads serving GET / and
# the health/conversation endpoints. That converts ANY log storm into a dead
# frontend ("backend alive, frontend can't be served"), independent of what
# caused the storm.
#
# Fix (structural, not a time-bound): the root logger gets a SINGLE
# QueueHandler whose emit() is just a non-blocking queue.put() — it never
# touches the disk or the handler locks. A dedicated background thread
# (QueueListener) drains the queue and performs the actual file/stderr I/O.
# So a request/serving thread that logs during a storm returns immediately;
# only the listener thread ever blocks on the slow mount. The queue is bounded:
# a log storm may shed diagnostics, but can never retain LogRecords until OOM.
import queue as _queue_mod
from logging.handlers import QueueHandler, QueueListener


def _log_queue_capacity() -> int:
    """Bound pending LogRecord memory without requiring install-time tuning."""
    try:
        value = int(os.environ.get('TOFU_LOG_QUEUE_MAX', '') or '20000')
    except (TypeError, ValueError):
        value = 20000
    return max(1000, min(500000, value))


class _BoundedQueueHandler(QueueHandler):
    """Shed a full async queue and summarize drops after it recovers.

    The stdlib handler routes ``queue.Full`` through ``handleError``. During a
    storm that emits a traceback per dropped record and creates another storm,
    so Full is an expected overload signal here rather than an exception.
    """

    def __init__(self, log_queue):
        super().__init__(log_queue)
        self._drop_lock = threading.Lock()
        self._dropped_pending = 0
        self._dropped_total = 0

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except _queue_mod.Full:
            with self._drop_lock:
                self._dropped_pending += 1
                self._dropped_total += 1
            return

        with self._drop_lock:
            dropped = self._dropped_pending
            dropped_total = self._dropped_total
        if not dropped:
            return
        notice = logging.LogRecord(
            name='server.logging', level=logging.WARNING,
            pathname=__file__, lineno=0,
            msg=('Async log queue recovered; shed %d record(s) while full '
                 '(capacity=%d, total_shed=%d).'),
            args=(dropped, self.queue.maxsize, dropped_total), exc_info=None,
        )
        try:
            self.queue.put_nowait(self.prepare(notice))
        except _queue_mod.Full:
            return
        with self._drop_lock:
            # Concurrent drops after the snapshot remain pending.
            self._dropped_pending = max(0, self._dropped_pending - dropped)


class _BoundedQueueListener(QueueListener):
    """QueueListener whose shutdown is bounded even if a sink is wedged."""

    def stop(self, timeout=5.0):
        thread = self._thread
        if thread is None:
            return True
        try:
            wait_s = max(0.0, float(timeout))
        except (TypeError, ValueError):
            wait_s = 5.0
        try:
            self.queue.put(self._sentinel, timeout=min(1.0, wait_s))
        except _queue_mod.Full:
            # At shutdown an undrainable queue is already lossy. Free exactly
            # one slot so the daemon listener can observe the sentinel.
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except _queue_mod.Empty:
                pass
            try:
                self.queue.put_nowait(self._sentinel)
            except _queue_mod.Full:
                return False
        thread.join(wait_s)
        if thread.is_alive():
            return False
        self._thread = None
        return True

_real_log_handlers = [_app_handler, _access_handler, _error_handler,
                      _vendor_handler, _frontend_handler, _console_handler]

# Under pytest, keep logging SYNCHRONOUS: caplog and the tests that assert a
# log line landed in a file handler (e.g. test_log_pytest_sink_isolation) read
# handler output immediately after logger.error(), which an async listener
# thread would race. The queue's whole point is production request-thread
# latency; the test process is single-purpose, so direct handlers are correct
# there. Detect pytest via the env var it always sets for a collected session.
_LOG_UNDER_PYTEST = bool(os.environ.get('PYTEST_CURRENT_TEST')) or (
    'pytest' in sys.modules)

if _log_agg_enabled() and not _LOG_UNDER_PYTEST:
    # pytest 同步模式下不挂:测试进程里聚合属噪音(测试会自建实例);
    # 生产下它跑在 QueueListener 的 drain 线程上,emit 只做内存计数。
    _real_log_handlers.append(_log_agg_handler)

_LOG_QUEUE = None
_log_listener = None

if _LOG_UNDER_PYTEST:
    logging.basicConfig(
        level=logging.INFO,
        handlers=list(_real_log_handlers),
    )
else:
    # SINGLE QueueHandler on the root logger. Its emit() is just a
    # non-blocking bounded-queue put — it never touches the disk or the
    # per-handler locks. A dedicated background thread (QueueListener) drains
    # the queue and performs the actual file/stderr I/O, so a request/serving
    # thread that logs during a storm returns immediately. A bounded queue is
    # essential: an upstream/FUSE outage must not turn retained LogRecords into
    # an unbounded heap and let the kernel kill the server.
    _LOG_QUEUE = _queue_mod.Queue(maxsize=_log_queue_capacity())
    _queue_handler = _BoundedQueueHandler(_LOG_QUEUE)
    # CRITICAL: give the QueueHandler an explicit ``%(message)s`` formatter so
    # basicConfig() does NOT attach its default BASIC_FORMAT
    # (``LEVEL:name:message``) to it. QueueHandler.prepare() renders its
    # formatter into record.msg before enqueueing; if that were BASIC_FORMAT,
    # each real file handler would then format the ALREADY-formatted string a
    # SECOND time → doubled ``[ERROR] name: ERROR:name:msg`` lines. With
    # ``%(message)s`` the enqueued text is just the rendered message (+ any
    # exc traceback, which Formatter appends and prepare() then clears from
    # exc_info so it isn't duplicated), and the real handlers apply the full
    # timestamp/level/name/thread layout exactly once — byte-identical to the
    # old synchronous output. levelname/name/threadName/created stay on the
    # record (prepare only rewrites msg/args/exc_info), so the real formatter
    # still has every field.
    _queue_handler.setFormatter(logging.Formatter('%(message)s'))
    logging.basicConfig(
        level=logging.INFO,
        handlers=[_queue_handler],
    )
    # respect_handler_level=True so each real handler still applies its own
    # setLevel()/filters on the listener thread exactly as before.
    _log_listener = _BoundedQueueListener(
        _LOG_QUEUE, *_real_log_handlers, respect_handler_level=True)


def _start_logging_runtime():
    """Start log I/O owners from Quart's serving lifecycle."""
    if _LOG_UNDER_PYTEST or _log_listener is None:
        return False
    thread = getattr(_log_listener, '_thread', None)
    if thread is not None and thread.is_alive():
        return False
    _log_listener.start()
    if _log_agg_enabled():
        _log_agg_start_flusher()
    return True


def _stop_logging_runtime(*, timeout=5.0, final_flush=True):
    """Bound and stop aggregate/log threads; safe to call repeatedly."""
    if _LOG_UNDER_PYTEST or _log_listener is None:
        return True
    aggregate_stopped = True
    if _log_agg_enabled():
        aggregate_stopped = _log_agg_stop_flusher(
            final_flush=final_flush, timeout=timeout)
    listener_stopped = _log_listener.stop(timeout=timeout)
    return bool(aggregate_stopped and listener_stopped)


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

from lib.database import init_db, warmup_db


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
    )


def create_production_app(config: dict | None = None):
    """Create an ASGI app with the process-wide production lifespan attached."""
    production_app = create_app(config)
    register_server_runtime_lifecycle(production_app)
    return production_app


# ── Flask secret key (reuse server.py logic) ──
def _load_or_create_flask_secret_key():
    from lib.config_dir import config_path as _cfg_path
    _env_key = os.environ.get('FLASK_SECRET_KEY', '').strip()
    if _env_key:
        return _env_key
    _key_file = _cfg_path('flask_secret_key')
    try:
        if os.path.isfile(_key_file):
            with open(_key_file, 'r', encoding='utf-8') as _kf:
                _existing = _kf.read().strip()
            if _existing:
                return _existing
    except Exception:
        pass
    _new_key = os.urandom(32).hex()
    try:
        os.makedirs(os.path.dirname(_key_file), exist_ok=True)
        _flag = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            _fd = os.open(_key_file, _flag, 0o600)
            try:
                os.write(_fd, _new_key.encode('utf-8'))
            finally:
                os.close(_fd)
        except (AttributeError, OSError):
            with open(_key_file, 'w', encoding='utf-8') as _kf:
                _kf.write(_new_key)
    except Exception as e:
        logging.getLogger('server').warning('[FlaskSecret] Failed to persist: %s', e)
    return _new_key


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


# ── Auth (legacy compat constants only) ──
# The active auth middleware lives in routes/api_v1/auth.py and is
# registered after blueprints are wired in. ``TUNNEL_TOKEN`` is kept
# only as a deprecated back-compat shim; new deployments mint API
# keys instead (see lib.api_keys.bootstrap_personal_key).
TUNNEL_TOKEN = os.environ.get('TUNNEL_TOKEN', '')
TUNNEL_COOKIE = '_tunnel_auth'
TUNNEL_COOKIE_MAX_AGE = 86400 * 30
if TUNNEL_TOKEN:
    logging.getLogger('server.auth').warning(
        '[Auth] TUNNEL_TOKEN is deprecated. Migrate to API keys '
        '(POST /api/v1/keys with admin scope). The shim remains '
        'for now but new code paths target the unified auth gate.')


from lib.log import get_logger

_lifecycle_log = get_logger('server.lifecycle')


# ── Install the tofu-search bridge (LLM + browser + auth seams) ──
# Must run before any search/fetch call; idempotent, re-synced on config reload.
#
# DEGRADE, don't die. This import pulls in tofu_search → trafilatura → lxml →
# libicuuc, and on 2026-07-31 that chain raised
#   ImportError: /lib64/libstdc++.so.6: version `GLIBCXX_3.4.30' not found
# eight times. Being a bare module-level import, it took the WHOLE SERVER down
# each time — chat, projects, the scheduler and every other subsystem died for
# a fault confined to web search. That blast radius is wrong regardless of what
# triggers the linkage fault: search is one optional capability, not a boot
# prerequisite.
#
# Scope check before narrowing this: three modules import tofu_search at module
# level (here, lib/paper/tools.py, lib/tasks_pkg/executor/_summary.py) and all
# three are on the boot chain, but THIS one is reached first — measured, it is
# the frame that actually raises. The other ten consumers import lazily inside
# functions and already fail per-call. So guarding here removes the only
# whole-process kill; if a future refactor removes this import, the next
# module-level one inherits the hazard (tests/test_startup_stdcxx_forensics.py
# asserts the guard stays).
#
# The bridge only INSTALLS seams (LLM/browser/auth) into tofu_search; without it
# the search tools still import fine and fail per-call instead, which is the
# degradation we want. The linkage forensics captured at boot are logged with
# the failure so the cause is diagnosable rather than a mystery.
try:
    from lib.search_bridge import install_search_bridge
    install_search_bridge()
except ImportError as _sb_err:
    _sb_msg = str(_sb_err)
    _sb_linkage = ''
    if 'GLIBCXX' in _sb_msg or 'libstdc++' in _sb_msg or 'symbol' in _sb_msg:
        _sb_linkage = ' | LINKAGE: %s' % (
            globals().get('_TOFU_LINKAGE_FORENSICS', 'unavailable'),)
    logging.getLogger('server').error(
        'Web search/fetch is DISABLED for this process — the tofu-search bridge '
        'could not be imported: %s%s. Every other subsystem is unaffected; '
        'search tools will fail per-call instead of taking down the server.',
        _sb_msg, _sb_linkage, exc_info=True)
except Exception as _sb_err:
    logging.getLogger('server').error(
        'Web search/fetch is DISABLED for this process — the tofu-search bridge '
        'failed to install: %s. Every other subsystem is unaffected.',
        _sb_err, exc_info=True)

# ── First-boot personal key bootstrap ──
# Only relevant when the auth gate is in ``private`` or ``multi-user``
# mode. In ``open`` mode (the default for personal installs) no
# credential is required and minting a key would just confuse the
# operator. When in private/multi-user mode and the key store is
# empty AND no TUNNEL_TOKEN is configured, mint a personal admin key
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
    if TUNNEL_TOKEN:
        return  # legacy mode — user explicitly chose a shared secret
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


def _load_static_bytes(filename):
    """Resolve *filename* strictly under STATIC_DIR and read it (SYNC, runs in a
    worker thread so the FUSE I/O never touches the event loop).

    Returns ``(data, mtime, etag)`` on success or ``None`` when the path is
    unsafe (traversal) or the file is absent/not-a-file. Raising is reserved for
    genuine I/O errors (surfaced as 500). The blocking calls — safe_join, the
    ``os.path.isfile`` stat, and the full ``open().read()`` — are exactly what
    would wedge the loop if run inline; here they are on the thread.
    """
    return _read_static_bytes(STATIC_DIR, filename)


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
)


# ═══════════════════════════════════════════════════════════════════════
#  Startup & Main
# ═══════════════════════════════════════════════════════════════════════

_server_log = logging.getLogger('server')

# Descriptor produced by recover_stale_tasks_on_startup(dispatch=False) during
# _init_database; consumed by _serve() to run the deferred BILLED boot dispatch
# (killed-recovery + autopilot-resume) on the SERVING loop, not the startup one.
_DEFERRED_BOOT_DISPATCH = None

# Frontend assets are a release artifact. Runtime startup never invokes Node
# or a Python JavaScript bundler; requests fail visibly with 503 when the graph
# is missing so source checkouts can still start for backend work.
def _check_frontend_artifact():
    try:
        from lib.vite_assets import validate_vite_artifact
        validate_vite_artifact()
    except Exception as artifact_error:
        _server_log.warning(
            'Prebuilt frontend unavailable; UI routes will return 503: %s',
            artifact_error)


def _init_database():
    """Initialize database (runs in app context)."""
    _boot('Initialising database…')
    init_db()
    warmup_db()
    # Existing row mirrors predate the fixed-width message_ts projection.
    # Converge them AFTER schema init on a delayed daemon thread so direct
    # ``python server.py`` keeps the same fast, dependency-free startup path.
    try:
        from lib.database.messages_rows import start_activity_projection_backfill
        start_activity_projection_backfill()
    except Exception as e:
        _server_log.warning('Message activity projection backfill failed to start: %s', e)
    try:
        from lib.database import heal_toast_corruption
        heal_toast_corruption()
    except Exception as e:
        _server_log.warning('TOAST auto-heal failed: %s', e)
    _boot('Database ready.')
    # Turn/attempt authority owns restart settlement for v2 work. This is a
    # DB-only transaction: it preserves the latest projection, emits a durable
    # terminal event and deliberately does NOT restart billable generation.
    try:
        from lib.turn_lifecycle import (
            cleanup_superseded_attempts, recover_running_attempts,
        )
        recover_running_attempts()
        cleanup_superseded_attempts()
    except Exception as e:
        _server_log.warning('Turn/attempt startup recovery failed: %s', e)
    # ── Clean-shutdown classification (OS-kill detection) ──
    # Read the marker LEFT BY THE PREVIOUS PROCESS, log/audit an unclean exit
    # loudly (the silent-OOM-SIGKILL incident), then re-arm the dirty-bit for
    # THIS process. Must run BEFORE recovery so it can tag interrupted turns
    # killed-vs-manual. Best-effort — never block boot.
    _prev_shutdown = None
    try:
        from lib.shutdown_marker import report_and_arm
        _prev_shutdown = report_and_arm()
    except Exception as e:
        _server_log.warning('Shutdown-marker classification failed: %s', e)
    # Run ONLY the synchronous DB cleanup here (dispatch=False). The BILLED
    # re-dispatch (killed-recovery + autopilot-resume) is DEFERRED and run from
    # the serving loop (_serve) after Hypercorn starts — never on the startup
    # event loop, where a spawned carrier would block asyncio.run()'s teardown
    # for the whole length of the carrier's run (the 297s-boot incident).
    global _DEFERRED_BOOT_DISPATCH
    _DEFERRED_BOOT_DISPATCH = None
    try:
        from lib.tasks_pkg import recover_stale_tasks_on_startup
        _DEFERRED_BOOT_DISPATCH = recover_stale_tasks_on_startup(
            prev_shutdown=_prev_shutdown, dispatch=False)
    except Exception as e:
        _server_log.warning('Stale task recovery failed: %s', e)

    # Orchestration run headers are durable, while their executor threads are
    # process-local. Any non-terminal row visible at this point belongs to the
    # previous process and cannot make further progress. Retire it explicitly
    # so Task Mode replays the preserved events and stops polling instead of
    # presenting an immortal "running" task.
    try:
        from lib.orchestration.run_service import OrchestrationRunService
        _retired_runs = OrchestrationRunService().retire_interrupted(error={
            'kind': 'worker_lost',
            'message': 'Run interrupted by a server restart before completion.',
            'source': 'orchestration.startup_recovery',
        })
        if _retired_runs:
            _server_log.warning(
                'Retired %d interrupted orchestration run(s)', _retired_runs)
    except Exception as e:
        _server_log.warning('Orchestration run recovery failed: %s', e)

    # ── Presence: reconcile the on-disk live-peer registry. A server that
    #    crashed mid-run left ghost peers marked "active" in each project's
    #    .tofu/presence/registry.json; with no live tasks yet, every persisted
    #    peer is a ghost and is reaped, so the "who's working" strip never lies
    #    after a restart. Then start the background sweep timer.
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.presence import reconcile_on_startup, start_sweeper
        _roots: list[str] = []
        try:
            db = get_thread_db(DOMAIN_CHAT)
            rows = db.execute(
                "SELECT DISTINCT json_extract(settings, '$.projectPath') AS p "
                "FROM conversations WHERE user_id=1 "
                "AND json_extract(settings, '$.projectPath') IS NOT NULL").fetchall()
            _roots = [r['p'] for r in rows if r['p']]
        except Exception as _re:
            _server_log.debug('Presence root discovery failed: %s', _re)
        reconcile_on_startup(_roots)
        start_sweeper()
    except Exception as e:
        _server_log.warning('Presence startup reconciliation failed: %s', e)

    # Resume swarm sub-agents that were mid-flight when the server stopped.
    # DB-backed round-level resume (see lib/swarm/persistence.py): rehydrates
    # each conversation-scoped session and re-spawns its unfinished agents
    # from their checkpointed message history.
    try:
        from lib.swarm.integration import rehydrate_swarms_on_startup
        rehydrate_swarms_on_startup()
    except Exception as e:
        _server_log.warning('Swarm rehydration failed: %s', e)


def _validate_imports():
    """Validate critical imports at startup."""
    _CRITICAL_IMPORTS = [
        'lib.tasks_pkg.orchestrator',
        'lib.tasks_pkg.executor',
        'tofu_search.fetch',
        'tofu_search.search',
        'lib.search_bridge',
        'lib.llm',
    ]
    _boot('Validating critical imports…')
    failures = []
    for mod_name in _CRITICAL_IMPORTS:
        _boot('  • importing %s', mod_name)
        try:
            __import__(mod_name)
        except ImportError as ie:
            failures.append((mod_name, ie))
            _server_log.error('Critical import failed: %s — %s', mod_name, ie)
    if failures:
        msgs = [f'  {m}: {e}' for m, e in failures]
        raise ImportError('Missing dependencies:\n' + '\n'.join(msgs))
    _boot('All critical imports validated.')

    # ── Eager-load heavy C extensions only when mlockall is enabled ──
    # These are the .so modules seen in past SIGBUS faulthandler dumps.
    # Loading them now (under mlockall MCL_FUTURE) ensures their code
    # pages are resident before any request arrives — the demand-fault
    # window that causes Bus errors on FUSE is eliminated.
    _NATIVE_PRELOADS = [
        'PIL._imaging',
        'lxml.etree',
        'greenlet._greenlet',
        'numpy.core._multiarray_umath',
        'markupsafe._speedups',
        'charset_normalizer.md',
    ]
    # These are optional — may not be installed in all environments.
    # yaml._yaml: only used by routes/api_docs.py::openapi_yaml, which already
    # degrades to JSON on ImportError — never a hard dependency.
    _NATIVE_PRELOADS_OPTIONAL = [
        'pymupdf._extra',
        'psycopg2._psycopg',
        'yaml._yaml',
    ]
    if _tofu_do_mlock:
        _boot('Eager-loading native extensions (FUSE SIGBUS mitigation)…')
        for _mod in _NATIVE_PRELOADS:
            try:
                __import__(_mod)
            except ImportError as _ie:
                _server_log.warning('Native preload failed (required): %s — %s', _mod, _ie)
        for _mod in _NATIVE_PRELOADS_OPTIONAL:
            try:
                __import__(_mod)
            except ImportError as _ie:
                _server_log.debug('Optional native preload %s unavailable: %s',
                                  _mod, _ie)  # optional — not all deployments have these
        _boot('Native extensions preloaded.')
    else:
        _boot('Native extension preload skipped (mlock disabled).')


def _start_background_workers(target_app=None):
    """Compatibility seam for the extracted lifecycle service owner."""
    from lib.server_background_services import start_background_services

    return start_background_services(
        target_app or app,
        load_saved_proxy_config=_load_saved_proxy_config,
        bootstrap_personal_key=_bootstrap_personal_key_if_needed,
        logger=_server_log,
    )


def _start_storage_sidecar():
    """Start the required storage authority and verify its ready handshake."""
    from lib.storage import start_storage

    _boot('Starting storage sidecar…')
    client = start_storage()
    health = client.health(deadline=2.0)
    if not health.get('ready'):
        raise RuntimeError('storage sidecar did not report ready')
    _server_log.info(
        '[Storage] required sidecar ready backend=%s protocol=%s',
        health.get('backend', 'unknown'), health.get('protocol', 'unknown'))
    _boot('Storage sidecar ready.')


from lib.server_network import (
    detect_reverse_proxy as _detect_reverse_proxy,
    find_free_port as _find_free_port,
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
            build_assets=_check_frontend_artifact,
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
        deferred_dispatch_provider=lambda: _DEFERRED_BOOT_DISPATCH,
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
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', 15000)))
    parser.add_argument('--certfile', default=os.environ.get('TLS_CERTFILE', ''))
    parser.add_argument('--keyfile', default=os.environ.get('TLS_KEYFILE', ''))
    parser.add_argument('--no-tls', action='store_true',
                        help='Disable TLS (HTTP/1.1 only, no HTTP/2 in browsers)')
    parser.add_argument(
        '--workers', type=int, default=1,
        help='Must be 1. Scale with one process per replica behind the task-'
             'affinity load balancer; programmatic Hypercorn ignores workers.')
    args = parser.parse_args()

    # hypercorn.asyncio.serve() explicitly ignores Config.workers. Silently
    # accepting --workers=N advertised isolation that did not exist and, worse,
    # multiple local processes cannot share the live task registry.
    if args.workers != 1:
        parser.error(
            '--workers must be 1. Run one chatui process per replica and use '
            'deploy/nginx-task-affinity.conf.example for horizontal scale.')

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

    # ── PG shutdown hook ──
    try:
        import atexit as _atexit
        from lib.database._core import stop_local_pg_if_owned
        _atexit.register(stop_local_pg_if_owned)
    except Exception as _e:
        _server_log.warning('[Server] PG shutdown hook failed: %s', _e)

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
            _server_log.warning('[Restart] Port %d still busy after wait — '
                                 'falling back to probe', port)
            port = _find_free_port(start=port)
            if port != args.port:
                _server_log.info('Port %d in use — using %d', args.port, port)
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
    from lib.env_compat import getenv_compat
    _tls_value = (getenv_compat('TOFU_TLS') or '').strip()
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
    if _invalid_tls_value:
        _server_log.warning(
            '[TLS] Ignoring unsupported TOFU_TLS=%r; expected 0/1, '
            'false/true, no/yes, or off/on. Using HTTP.',
            _invalid_tls_value)
    if bool(args.certfile) != bool(args.keyfile):
        _server_log.warning(
            '[TLS] Both --certfile/TLS_CERTFILE and --keyfile/TLS_KEYFILE '
            'are required; incomplete certificate configuration is ignored.')

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
        _tls_cert, _tls_key = _ensure_tls_certs(
            args.certfile,
            args.keyfile,
            bind_host=host,
            data_root=_tofu_data_root(),
            logger=logging.getLogger('server.tls'),
            boot=_boot,
        )

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
            tunnel_token=TUNNEL_TOKEN,
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
