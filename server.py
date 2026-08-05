#!/usr/bin/env python3
"""Tofu Server — Quart + Hypercorn (HTTP/2, ASGI).

App entry point. Uses:
  - Quart (async Flask from Pallets) as the application framework
  - Hypercorn as the ASGI server with HTTP/2 support
  - Auto-generated self-signed TLS for zero-config HTTP/2 in browsers

All existing Flask-style sync route handlers run unchanged in a thread pool.

Usage:
    python server.py                          # HTTPS + HTTP/2 (auto-cert)
    python server.py --no-tls                 # HTTP/1.1 only
    python server.py --certfile cert.pem --keyfile key.pem   # custom cert
"""

import asyncio
import os
import sys
import json
import logging
import time
import signal
import threading
import faulthandler

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
# Dual-sink strategy: write to BOTH the FUSE-backed logs/ (durable across
# box restarts, but may be truncated by the very FUSE stall that caused the
# crash) AND a tmpfs mirror in /dev/shm (immune to FUSE stalls, but lost on
# box reboot). On crash, check /dev/shm first for the clean copy.
_fault_log = None
try:
    _FAULT_LOG_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'logs', 'faulthandler.log')
    os.makedirs(os.path.dirname(_FAULT_LOG_PATH), exist_ok=True)
    _fault_log = open(_FAULT_LOG_PATH, 'a', buffering=1)  # line-buffered
    _fault_log.write('\n=== faulthandler armed pid=%d at %s ===\n'
                     % (os.getpid(), time.strftime('%Y-%m-%d %H:%M:%S')))
except OSError:
    pass

# Prefer tmpfs for the live faulthandler sink (survives FUSE stalls intact);
# fall back to the FUSE log, then stderr.
_fault_shm_log = None
try:
    _FAULT_SHM_PATH = '/dev/shm/tofu_faulthandler_%d.log' % os.getpid()
    _fault_shm_log = open(_FAULT_SHM_PATH, 'w', buffering=1)
    _fault_shm_log.write('=== faulthandler armed pid=%d at %s ===\n'
                         % (os.getpid(), time.strftime('%Y-%m-%d %H:%M:%S')))
    faulthandler.enable(file=_fault_shm_log, all_threads=True)
except OSError:
    _fault_shm_log = None

if _fault_shm_log is None:
    # tmpfs unavailable — use the FUSE log (better than nothing)
    if _fault_log is not None:
        faulthandler.enable(file=_fault_log, all_threads=True)
    else:
        faulthandler.enable(all_threads=True)


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


def _write_heartbeat(pid=None, ts=None, path=None):
    """Atomically stamp ``{pid, ts}`` (wall-clock) into the sidecar.

    Best-effort: a wedged loop failing to write is precisely the signal we
    want, so a write failure NEVER raises — it just lets the file age. Atomic
    (temp + ``os.replace``) so a concurrent reader never sees a half-written
    file. Returns True on success, False on any failure.
    """
    pid = os.getpid() if pid is None else pid
    ts = time.time() if ts is None else ts
    path = path or _heartbeat_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = '%s.%d.tmp' % (path, pid)
        with open(tmp, 'w') as f:
            f.write(json.dumps({'pid': pid, 'ts': ts}))
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
    hb_pid, hb_ts = _read_heartbeat(path)
    if hb_pid is None or hb_ts is None or hb_pid != pid:
        return None
    now = time.time() if now is None else now
    age = now - hb_ts
    if age < 0 or age <= _heartbeat_stale_threshold():
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


def _acquire_instance_lock(lock_path, logger, hostname=None, allow_reclaim=True):
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
            ok2, fd2 = _acquire_instance_lock(lock_path, logger, hostname=hostname, allow_reclaim=False)
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


def _stall_pressure_context():
    """Best-effort host-pressure snapshot for a LoopWatch stall line.

    Returns a compact string like ``load1=7.20 cgmem=91.4%`` (either part may
    be absent) or ``''``. Why it exists: the stall top-frame can only say WHAT
    the loop was doing; the dominant stall class is the loop idle in
    ``select()`` because the HOST is starved (cgroup memory pressure relief,
    a sibling's 46 GB dump, a 300 MB JSON parse on a shared box). Without the
    pressure reading IN the stall line, classifying a stall means hand-
    correlating two log files (the 2026-08-05 audit did exactly that). Cheap
    local reads only (cgroupfs + /proc) — NEVER FUSE: the watcher fires
    precisely when the loop may be wedged on a syscall, so the probe itself
    must be syscall-cheap. Never raises.
    """
    parts = []
    try:
        with open('/proc/loadavg') as _f:
            parts.append('load1=%s' % _f.read().split()[0])
    except Exception:
        pass
    try:
        from lib.cgroup_guard import pressure as _cg_pressure
        _p = _cg_pressure()
        if _p:
            parts.append('cgmem=%.1f%%' % _p['pct'])
    except Exception:
        pass
    return ' '.join(parts)


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



# One-shot boot cleanup: prune dead-pid faulthandler dumps from the tmpfs sink
# so it stays bounded (the file we just opened for THIS pid is preserved).
if _fault_shm_log is not None:
    try:
        _pruned = _prune_stale_fault_dumps(
            directory='/dev/shm',
            keep_basename=os.path.basename(_FAULT_SHM_PATH))
        if _pruned:
            sys.stderr.write('[boot] pruned %d stale faulthandler dump(s) from /dev/shm\n'
                             % _pruned)
    except Exception:
        pass   # cleanup is best-effort; never block boot on it

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
# Override: TOFU_MLOCK=1 forces it on, =0 forces it off (default 'auto').
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
    _mode = os.environ.get('TOFU_MLOCK', 'auto').strip().lower()
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
#  Sync→loop body-read bounded wait (extracted for testability)
# ═══════════════════════════════════════════════════════════════════════
# ``_run_coro_sync`` (installed by the Flask→Quart shim below) bridges a sync
# route handler running in an executor thread to the MAIN event loop to read
# the request body (get_json / form / files / data). If the loop is EVER wedged
# (a blocking call slipped onto it, a FUSE/PG stall), an UNBOUNDED
# ``future.result()`` there blocks the worker thread FOREVER; every subsequent
# request-body read queues behind it and the whole sync-executor pool is
# exhausted — the "whole site frozen, must restart" failure mode. Bounding the
# wait severs that one failure mode WITHOUT hurting legitimate slow large
# uploads. These two helpers are module-level (not closure-local) so a unit test
# can exercise the bound directly, mirroring the ``timeout=30`` contract the
# sibling ``_sync_safe`` wrapper already carries.

def _resolve_sync_body_timeout():
    """Seconds to wait for a cross-thread request-body read before aborting.

    Reads ``TOFU_SYNC_BODY_TIMEOUT`` (default 300s — generous so a genuine slow
    upload is never cut short; this is a backstop against an infinitely wedged
    loop, NOT a tight per-request budget). A value ``<= 0`` opts out (unbounded,
    the legacy behaviour) and returns ``None``.
    """
    raw = os.environ.get('TOFU_SYNC_BODY_TIMEOUT', '') or '300'
    try:
        val = float(raw)
    except (ValueError, TypeError) as e:
        logging.getLogger('server').debug(
            '[Server] bad TOFU_SYNC_BODY_TIMEOUT=%r, using 300s: %s', raw, e)
        return 300.0
    return None if val <= 0 else val


def _await_coro_on_loop(coro, main_loop, timeout):
    """Run ``coro`` on ``main_loop`` from a sync thread, bounded by ``timeout``.

    On timeout the coroutine is best-effort cancelled, an ERROR is logged (per
    CLAUDE.md §2.2), and ``concurrent.futures.TimeoutError`` propagates so the
    handler fails fast instead of hanging the worker thread indefinitely.
    """
    from concurrent.futures import TimeoutError as _FuturesTimeoutError
    future = asyncio.run_coroutine_threadsafe(coro, main_loop)
    try:
        return future.result(timeout=timeout)
    except _FuturesTimeoutError:
        future.cancel()
        logging.getLogger('server').error(
            '[Server] _run_coro_sync timed out after %ss waiting on the main '
            'event loop for a request-body read — the loop is likely wedged. '
            'Aborting this read instead of hanging the worker thread (raise '
            'TOFU_SYNC_BODY_TIMEOUT if this is a genuine slow upload).', timeout)
        raise


# ═══════════════════════════════════════════════════════════════════════
#  Framework Compatibility Shim
# ═══════════════════════════════════════════════════════════════════════
# Quart is API-compatible with Flask but lives under `quart.*` imports.
# Our routes and lib/ code import from flask. We install a shim so that
# `from flask import *` resolves to Quart's equivalents at runtime.
# This is the official Quart migration approach.

def _install_flask_shim():
    """Make `from flask import X` resolve to Quart equivalents.

    Quart is a superset of Flask's API. This shim allows all existing
    route code to work without changing any import statements.

    Key difference: Quart makes send_from_directory, send_file, and
    make_response async. When sync route handlers (running in Quart's
    thread pool) call these, they get coroutine objects. We wrap them
    with sync-safe versions that detect this and await appropriately.
    """
    try:
        import quart
    except ImportError:
        sys.stderr.write(
            '\033[31m[server.py] ERROR: quart is not installed.\n'
            '  Install with: pip install quart hypercorn cryptography\033[0m\n')
        sys.exit(1)

    import asyncio
    import functools
    import inspect

    # Recover the GENUINE async helpers. If server.py is imported/exec'd
    # more than once in the same process (e.g. a test re-imports it via
    # importlib), ``quart.make_response`` etc. are already our sync-safe
    # wrappers from the first install. Capturing those as the "originals"
    # and wrapping them again would corrupt ``_orig_make_response_async``
    # (it would point at a sync-safe wrapper instead of the real async
    # ``quart.make_response``), so error handlers that
    # ``await _orig_make_response_async(...)`` would route through the
    # thread-bridge and deadlock. ``_sync_safe`` stashes the genuine async
    # function on ``.__wrapped__``; unwrap through it so a re-install
    # always starts from the real async helpers.
    def _genuine(fn):
        while getattr(fn, '_quart_async_wrapper', False):
            fn = getattr(fn, '__wrapped__', fn)
        return fn

    _orig_send_from_directory = _genuine(quart.send_from_directory)
    _orig_send_file = _genuine(quart.send_file)
    _orig_make_response = _genuine(quart.make_response)

    def _sync_safe(async_fn):
        """Wrap an async function to be callable from sync code in a thread."""
        @functools.wraps(async_fn)
        def wrapper(*args, **kwargs):
            coro = async_fn(*args, **kwargs)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                # We're in a thread with an event loop running elsewhere.
                # Use the Quart-provided mechanism to run coroutines from
                # sync code within a request context.
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=30)
            else:
                return asyncio.run(coro)
        # Also make it awaitable for async callers
        wrapper._async = async_fn
        wrapper.__wrapped__ = async_fn
        # Mark it so Quart's ensure_async can detect the dual nature
        wrapper._quart_async_wrapper = True
        return wrapper

    # Quart 0.19.x's send_file / send_from_directory still use the
    # pre-Flask-2.0 kwarg name `attachment_filename`; modern route code
    # uses Flask's `download_name`. Normalize so callers can use the
    # current Flask spelling regardless of the installed Quart version.
    def _compat_download_name(async_fn):
        @functools.wraps(async_fn)
        def adapter(*args, **kwargs):
            if 'download_name' in kwargs:
                params = inspect.signature(async_fn).parameters
                if 'download_name' not in params and 'attachment_filename' in params:
                    kwargs['attachment_filename'] = kwargs.pop('download_name')
            return async_fn(*args, **kwargs)
        # Mark so _genuine() unwraps through this adapter on re-install,
        # recovering the real async helper rather than stopping here.
        adapter.__wrapped__ = async_fn
        adapter._quart_async_wrapper = True
        return adapter

    # Replace in quart module so `from flask import send_from_directory`
    # gets the sync-safe version
    quart.send_from_directory = _sync_safe(_compat_download_name(_orig_send_from_directory))
    quart.send_file = _sync_safe(_compat_download_name(_orig_send_file))
    quart.make_response = _sync_safe(_orig_make_response)

    # Expose originals at module level for async code that needs to await directly
    global _orig_make_response_async
    _orig_make_response_async = _orig_make_response

    # ── Patch Request async methods/properties for sync route handlers ──
    # In Quart, get_json(), form, files, data, and json are async. Sync
    # route handlers (run in executor threads via run_sync) get coroutine
    # objects instead of values. Monkey-patch the Request class to run
    # the coroutine on the MAIN event loop — NOT a fresh child loop.
    #
    # The naive ``asyncio.run(coro)`` here is wrong: it spins up a new
    # loop in the worker thread, and the coroutine then awaits hypercorn's
    # request body Future, which lives on the main loop. Cross-loop
    # awaits never wake up — symptom: large POST bodies hang server-side
    # until the client times out, while small bodies (already inlined
    # into the ASGI scope before dispatch) work fine. Fix: schedule via
    # ``run_coroutine_threadsafe`` on the loop saved by
    # ``hub.set_loop`` at startup.
    from quart.wrappers import Request as _QuartRequest

    _orig_get_json = _QuartRequest.get_json

    def _run_coro_sync(coro):
        """Run a coroutine from a sync context (executor thread).

        The cross-thread wait is bounded by ``TOFU_SYNC_BODY_TIMEOUT`` (default
        300s, see :func:`_resolve_sync_body_timeout`) so a wedged event loop can
        never hang a worker thread forever and exhaust the sync-executor pool.
        On timeout the coroutine is cancelled and
        ``concurrent.futures.TimeoutError`` propagates instead of blocking
        indefinitely. Delegates to the module-level :func:`_await_coro_on_loop`
        so the bound is unit-testable.
        """
        if not inspect.iscoroutine(coro):
            return coro
        try:
            from lib.push import hub as _push_hub
            main_loop = getattr(_push_hub, '_loop', None)
        except Exception:
            main_loop = None
        if main_loop is not None and main_loop.is_running():
            return _await_coro_on_loop(
                coro, main_loop, _resolve_sync_body_timeout())
        return asyncio.run(coro)

    def _sync_safe_get_json(self, *args, **kwargs):
        return _run_coro_sync(_orig_get_json(self, *args, **kwargs))

    # Stash the genuine async original ON the wrapper so async handlers can
    # recover it regardless of how many times the shim is (re)installed or
    # which module object holds it (test harnesses sometimes exec server.py as
    # a second module). Always unwrap to the FIRST genuine coroutine fn.
    _genuine_get_json = getattr(_orig_get_json, '_genuine_async_get_json', _orig_get_json)
    _sync_safe_get_json._genuine_async_get_json = _genuine_get_json
    _QuartRequest.get_json = _sync_safe_get_json

    # Patch async properties: form, files, data, json
    _orig_form_prop = _QuartRequest.form
    _orig_files_prop = _QuartRequest.files
    _orig_data_prop = _QuartRequest.data

    def _make_sync_safe_property(orig_prop):
        _fget = orig_prop.fget
        @property
        def _prop(self):
            return _run_coro_sync(_fget(self))
        return _prop

    _QuartRequest.form = _make_sync_safe_property(_orig_form_prop)
    _QuartRequest.files = _make_sync_safe_property(_orig_files_prop)
    _QuartRequest.data = _make_sync_safe_property(_orig_data_prop)

    # json property delegates to the already-patched sync get_json
    @property
    def _json_prop(self):
        return self.get_json()
    _QuartRequest.json = _json_prop

    # Install the shim: make `import flask` resolve to quart
    sys.modules['flask'] = quart
    # Also shim sub-modules that code might import from
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        quart_sub = f'quart.{attr}'
        flask_sub = f'flask.{attr}'
        if quart_sub in sys.modules:
            sys.modules[flask_sub] = sys.modules[quart_sub]

    # Werkzeug exceptions are used directly in some places
    # Quart re-exports them, but ensure werkzeug is still importable
    import importlib.util
    if importlib.util.find_spec('werkzeug') is None:
        logging.getLogger(__name__).debug('werkzeug not importable; relying on quart re-exports')


_install_flask_shim()


# ── Now safe to import Quart (which the routes will see as 'flask') ──
import quart  # noqa: F401  — kept so quart.* monkeypatches in _install_flask_shim resolve
from quart import Quart, redirect, request

# ═══════════════════════════════════════════════════════════════════════
#  Logging (reuse server.py's architecture)
# ═══════════════════════════════════════════════════════════════════════

import mimetypes
mimetypes.init()
mimetypes.add_type('text/javascript', '.js')
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

_app_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'app.log'),
    when='midnight', backupCount=30, encoding='utf-8')
_app_handler.setFormatter(_formatter)
_app_handler.setLevel(logging.INFO)
_app_handler.addFilter(_BizOnly())

_access_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'access.log'),
    when='midnight', backupCount=14, encoding='utf-8')
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

_frontend_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, 'frontend.log'),
    when='midnight', backupCount=14, encoding='utf-8')
_frontend_handler.setFormatter(_formatter)
_frontend_handler.setLevel(logging.INFO)
_frontend_handler.addFilter(_FrontendOnly())

_console_handler = logging.StreamHandler(sys.stderr)
_console_handler.setFormatter(_formatter)
_console_handler.setLevel(logging.WARNING)
_console_handler.addFilter(_BizAndServerOnly())

# ── Non-blocking logging: QueueHandler + QueueListener ──
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
# only the listener thread ever blocks on the slow mount. The queue is
# unbounded (put_nowait never blocks/drops), so no log line is lost — a burst
# just grows the in-memory queue and the listener catches up.
import queue as _queue_mod
from logging.handlers import QueueHandler, QueueListener

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

_LOG_QUEUE = None
_log_listener = None

if _LOG_UNDER_PYTEST:
    logging.basicConfig(
        level=logging.INFO,
        handlers=list(_real_log_handlers),
    )
else:
    # SINGLE QueueHandler on the root logger. Its emit() is just a
    # non-blocking SimpleQueue.put() — it never touches the disk or the
    # per-handler locks. A dedicated background thread (QueueListener) drains
    # the queue and performs the actual file/stderr I/O, so a request/serving
    # thread that logs during a storm returns immediately; only the listener
    # thread ever blocks on the slow FUSE mount. SimpleQueue is unbounded
    # (put never blocks/drops), so no line is lost — a burst just grows the
    # in-memory queue and the listener catches up.
    _LOG_QUEUE = _queue_mod.SimpleQueue()
    _queue_handler = QueueHandler(_LOG_QUEUE)
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
    _log_listener = QueueListener(
        _LOG_QUEUE, *_real_log_handlers, respect_handler_level=True)
    _log_listener.start()

    # Drain + flush the queue on interpreter exit so the tail of the log isn't
    # lost if the process stops while lines are still queued.
    # QueueListener.stop() enqueues a sentinel and joins the drain thread.
    import atexit as _atexit_mod

    def _stop_log_listener():
        try:
            if _log_listener is not None:
                _log_listener.stop()
        except Exception:
            pass  # shutdown best-effort — never raise from an atexit hook

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

# ── Cap onnxruntime threads BEFORE any import can create an InferenceSession ──
# pymupdf4llm → pymupdf_layout → onnxruntime (also rapidocr/cadtrans) spawns one
# worker per HOST cpu and pins each via pthread_setaffinity_np. On a cpuset-
# restricted host (containers, YARN/Hope, exported cluster deployments) the pins
# fail with EINVAL → a stderr storm during `python server.py`. The guard must
# run before the critical-import chain (tofu_search.fetch imports pymupdf4llm),
# so install it here, first thing after the boot banner.
try:
    from lib.onnx_thread_guard import install_onnx_thread_guard
    install_onnx_thread_guard()
except Exception as _onnx_guard_err:  # never let the guard itself break boot
    _boot('onnx thread guard install skipped: %s', _onnx_guard_err)

from lib.database import close_db, init_db, warmup_db


# ═══════════════════════════════════════════════════════════════════════
#  Quart App
# ═══════════════════════════════════════════════════════════════════════

# Flask 3.1+ / newer Quart dropped PROVIDE_AUTOMATIC_OPTIONS from default
# config, but add_url_rule (called during __init__ for the static route)
# still reads it → KeyError.  Inject it into the class defaults before
# instantiation so it's present from the very first add_url_rule call.
_orig_default_config = Quart.default_config
if 'PROVIDE_AUTOMATIC_OPTIONS' not in _orig_default_config:
    Quart.default_config = {**_orig_default_config, 'PROVIDE_AUTOMATIC_OPTIONS': True}

#  static_folder=None DISABLES Quart's built-in /static/<path> view. That view
#  serves files via a NATIVE-ASYNC send_static_file → send_from_directory whose
#  is_file()/stat()/full-file-read run DIRECTLY on the event loop — one FUSE
#  stall there wedges the whole server (the proven root cause of the outage).
#  Our own executor-offloaded /static route below replaces it (see
#  _static_route). BASE_DIR/static lives on FUSE here.
app = Quart(__name__, static_folder=None)
STATIC_DIR = os.path.join(BASE_DIR, 'static')


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


app.secret_key = _load_or_create_flask_secret_key()
# MAX_CONTENT_LENGTH is APP-GLOBAL, so it must fit the LARGEST legitimate
# body any route accepts — the video upload cap (512 MiB, TOFU_VIDEO_MAX_BYTES)
# plus multipart slack. Every OTHER route keeps the legacy 50 MiB ceiling via
# the per-route guard below — raising the global must not silently open
# big-body uploads on the whole API surface (owner ruling 2026-08-04).
app.config['MAX_CONTENT_LENGTH'] = 520 * 1024 * 1024

# Per-route request-body caps: first matching path prefix wins.
_ROUTE_BODY_CAPS = (
    ('/api/v1/videos/upload', 512 * 1024 * 1024),
)
_DEFAULT_BODY_CAP = 50 * 1024 * 1024


@app.before_request
async def _enforce_route_body_caps():
    cl = request.content_length or 0
    if cl <= 0:
        return None
    cap = _DEFAULT_BODY_CAP
    for _prefix, _cap in _ROUTE_BODY_CAPS:
        if request.path.startswith(_prefix):
            cap = _cap
            break
    if cl > cap:
        from lib.api_response import api_payload_too_large
        logging.getLogger('server').warning(
            '[BodyCap] %s %s rejected: Content-Length=%d > cap=%d',
            request.method, request.path, cl, cap)
        return api_payload_too_large(cap)
    return None
# ── Disable response/body timeouts for long-lived SSE streams ──
# Quart's defaults (60s) silently kill /api/chat/stream connections during
# long LLM responses, causing the UI to "stop updating without refresh".
# SSE clients keep their own keepalive (15s comment ping in chat_stream),
# so we set these to None to defer entirely to the SSE layer.
app.config['RESPONSE_TIMEOUT'] = None
app.config['BODY_TIMEOUT'] = None


# ── Compression (Quart-native, no flask-compress dependency) ──
# Quart does not use flask-compress. Instead, we add a simple
# after_request hook for gzip. For heavy production use, Hypercorn
# + a reverse proxy handle this better.
_COMPRESS_MIMETYPES = frozenset([
    'text/html', 'text/css', 'text/javascript',
    'application/javascript', 'application/json',
])
_COMPRESS_MIN_SIZE = 256

import gzip as _gzip

from lib.ttl_cache import TTLCache

try:
    import brotli as _brotli
except ImportError as _e:
    _brotli = None
    # _lifecycle_log is only bound further down this module (line ~1755); on a
    # brotli-less box this except fires DURING import, before that binding —
    # a bare reference here was a startup NameError. Import the logger locally.
    from lib.log import get_logger as _get_logger
    _get_logger('server.lifecycle').info(
        '[Compress] brotli unavailable (%s) — gzip only', _e)

# Compressed-artifact cache for CONTENT-ADDRESSED immutable assets.
#
# Without it every single page load re-compresses the whole JS bundle: measured
# 1711 KB → 463 KB costs ~55 ms of executor CPU, paid again for every visitor
# and every hard refresh. The bundle filename carries a content hash and the
# ETag carries mtime+size+adler32, so the compressed bytes are a pure function
# of the ETag — cache them and the cost is paid once per build instead of once
# per request. This is what makes it affordable to spend a HIGHER brotli quality
# on these bodies (see _BR_QUALITY_CACHED).
_COMPRESS_CACHE = TTLCache(ttl=6 * 3600, max_size=48)
# Don't let one pathological body evict the whole cache / balloon RSS.
_COMPRESS_CACHE_MAX_BYTES = 8 * 1024 * 1024

# Two-tier brotli quality, justified by the cache above:
#   cached  — compressed once per build, so buy the extra ratio (387 KB @ ~92 ms)
#   uncached— on the request's critical path, so stay cheap (~26 ms for 1.7 MB;
#             microseconds for a typical API JSON body)
_BR_QUALITY_CACHED = 9
_BR_QUALITY_LIVE = 4


def _compress_bytes(data, encoding, quality):
    """Compress *data* with *encoding*. Runs in a worker thread (CPU-bound)."""
    if encoding == 'br':
        return _brotli.compress(data, quality=quality)
    return _gzip.compress(data, 6)


@app.after_request
async def _compress_response(response):
    """gzip/brotli compression for eligible responses.

    Immutable content-addressed static assets are compressed ONCE and served
    from ``_COMPRESS_CACHE`` thereafter; everything else is compressed live at
    a cheaper setting so the per-request cost stays negligible.
    """
    # Skip SSE (buffering breaks streaming), small responses, already encoded
    if (response.content_type
            and 'text/event-stream' in response.content_type):
        return response
    if response.content_encoding:
        return response
    # Never compress partial / range / non-200 responses. Gzipping a 206 while
    # keeping its Content-Range header (and rewriting Content-Length to the
    # compressed slice length) hands the client a body it decodes as a corrupt
    # byte-range — the empirically-confirmed cause of vendor .js "failed to
    # load" on clients that issue Range requests for scripts (mobile
    # Safari/Chrome, some tablet browsers). Only whole 200 bodies are safe.
    if response.status_code != 200 or 'Content-Range' in response.headers:
        return response
    accept_enc = request.headers.get('Accept-Encoding', '')
    # Prefer brotli when the client advertises it: measured on the real
    # 1711 KB bundle, br q=9 lands 387 KB vs gzip's 463 KB (-16% transfer).
    if _brotli is not None and 'br' in accept_enc:
        encoding = 'br'
    elif 'gzip' in accept_enc:
        encoding = 'gzip'
    else:
        return response
    mime = (response.content_type or '').split(';')[0].strip()
    if mime not in _COMPRESS_MIMETYPES:
        return response
    data = await response.get_data()
    if len(data) < _COMPRESS_MIN_SIZE:
        return response

    # Cache key: the ETag identifies the exact bytes, so a hit means the
    # compressed body is still valid. Only immutable content-addressed static
    # assets are cached — a dynamic API body would just churn the cache.
    etag = response.headers.get('ETag', '')
    cache_key = None
    if etag and len(data) <= _COMPRESS_CACHE_MAX_BYTES and request.path.startswith('/static/'):
        cache_key = (etag, encoding)

    compressed = _COMPRESS_CACHE.get(cache_key) if cache_key else None
    if compressed is None:
        # Compression is CPU-bound; running it inline would block the event
        # loop (and every other connection / SSE keepalive) for the duration.
        # Offload to the sync executor so a multi-MB body doesn't stall the
        # whole server.
        quality = _BR_QUALITY_CACHED if cache_key else _BR_QUALITY_LIVE
        loop = asyncio.get_running_loop()
        compressed = await loop.run_in_executor(
            None, _compress_bytes, data, encoding, quality)
        if cache_key:
            _COMPRESS_CACHE.set(cache_key, compressed)

    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers['Content-Encoding'] = encoding
    response.headers['Content-Length'] = len(compressed)
    response.headers.pop('Vary', None)
    response.headers['Vary'] = 'Accept-Encoding'
    return response


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


# ── Method Override + CloudIDE JSON fix ──
@app.before_request
async def method_override():
    override = request.args.get('_method')
    if override:
        request.scope['method'] = override.upper()
    # CloudIDE sometimes double-encodes JSON bodies (sends a JSON string
    # whose value is itself a JSON object). Detect and unwrap in-place so
    # downstream ``request.get_json()`` returns the correct dict.
    ct = request.content_type or ''
    if request.method in ('POST', 'PUT') and 'json' in ct:
        raw = (await request.get_data()).decode('utf-8', errors='replace')
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, str):
                    corrected = json.dumps(json.loads(data)).encode('utf-8')
                    request._body = corrected
            except (json.JSONDecodeError, TypeError) as e:
                _lifecycle_log.debug('[method_override] body unwrap skipped: %s', e)


# ── Request lifecycle logging ──
from lib.log import (get_logger, set_req_id, req_id as _get_req_id,
                     resolve_inbound_rid as _resolve_inbound_rid)

_lifecycle_log = get_logger('server.lifecycle')
_QUIET_PREFIXES = ('/api/browser/', '/api/desktop/', '/static/', '/api/task/')
_SLOW_THRESHOLD_S = 2.0
# Responses at/above this size are flagged even when the server returned
# quickly — the "fast but heavy" case (e.g. a multi-MB conversation fetch)
# that otherwise only shows up as a client-side timeout over a slow proxy.
_HEAVY_RESPONSE_BYTES = 1_048_576  # 1 MiB


def _response_size(response):
    """Return the response body size in bytes from Content-Length, else None.

    Streaming / chunked responses (SSE, ``Content-Range`` partials) carry no
    reliable ``Content-Length``; we return None so the caller omits the size
    rather than blocking to materialize the body.
    """
    try:
        raw = response.headers.get('Content-Length')
    except Exception:
        return None
    if raw is None:
        return None
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return None
    return n if n >= 0 else None


def _fmt_size(n):
    """Format a byte count as a compact human string ('2.8MB'); '' if unknown."""
    if not isinstance(n, int) or n < 0:
        return ''
    if n < 1024:
        return '%dB' % n
    if n < 1024 * 1024:
        return '%.1fKB' % (n / 1024)
    return '%.1fMB' % (n / (1024 * 1024))


@app.before_request
async def _assign_req_id_and_log():
    rid = _resolve_inbound_rid(
        request.headers.get('X-Request-ID'),
        request.args.get('_rid'),
    )
    set_req_id(rid)
    request._start_time = time.time()
    path = request.path
    is_quiet = any(path.startswith(p) for p in _QUIET_PREFIXES)
    level = logging.DEBUG if is_quiet else logging.INFO
    _lifecycle_log.log(level, '[%s] → %s %s', rid, request.method, path)


@app.after_request
async def _log_response(response):
    rid = _get_req_id()
    path = request.full_path.rstrip('?')
    status = response.status_code
    is_quiet = any(path.startswith(p) for p in _QUIET_PREFIXES)

    size = _response_size(response)
    size_str = _fmt_size(size)

    # Elapsed is only meaningful when before_request stamped _start_time. Some
    # early-error / middleware paths reach after_request without it — the old
    # `time.time() - getattr(request, '_start_time', time.time())` evaluated
    # the left clock BEFORE the default on the right, yielding a slightly
    # NEGATIVE span that polluted the log. Detect the absence explicitly (emit
    # size only), and clamp against clock skew when present.
    _start = getattr(request, '_start_time', None)
    if _start is None:
        elapsed = None
        timing = '(%s)' % size_str if size_str else '(elapsed n/a)'
    else:
        elapsed = max(0.0, time.time() - _start)
        timing = '(%.3fs, %s)' % (elapsed, size_str) if size_str else '(%.3fs)' % elapsed

    if status >= 500:
        _lifecycle_log.error('[%s] ← %s %s %d %s', rid, request.method, path, status, timing)
    elif status >= 400:
        if status == 404 and request.path.startswith('/.well-known/'):
            _lifecycle_log.debug('[%s] ← %s %s %d %s', rid, request.method, path, status, timing)
        else:
            _lifecycle_log.warning('[%s] ← %s %s %d %s', rid, request.method, path, status, timing)
    elif elapsed is not None and elapsed >= _SLOW_THRESHOLD_S and not is_quiet:
        _lifecycle_log.warning('[%s] ← %s %s %d SLOW %s', rid, request.method, path, status, timing)
    elif size is not None and size >= _HEAVY_RESPONSE_BYTES and not is_quiet:
        # Fast but heavy: the server was quick, yet a multi-MB body will feel
        # slow to the client over a constrained proxy. Surface it at WARN so
        # "fast server, heavy experience" is traceable in server logs.
        _lifecycle_log.warning('[%s] ← %s %s %d HEAVY %s', rid, request.method, path, status, timing)
    elif not is_quiet:
        _lifecycle_log.info('[%s] ← %s %s %d %s', rid, request.method, path, status, timing)
    else:
        _lifecycle_log.debug('[%s] ← %s %s %d %s', rid, request.method, path, status, timing)

    response.headers['X-Request-ID'] = rid
    return response


@app.teardown_request
async def _clear_req_id(exc):
    if exc:
        rid = _get_req_id()
        # Client disconnect mid-request (CancelledError during body read) is
        # benign — log at debug. Real handler exceptions are already logged
        # by _handle_uncaught with full context, so reaching teardown with
        # any other exception means the framework swallowed it; warn so it's
        # still visible without the alarming ERROR + traceback.
        if isinstance(exc, asyncio.CancelledError):
            _lifecycle_log.debug('[%s] Request teardown: client disconnected', rid)
        else:
            _lifecycle_log.warning('[%s] Request teardown with exception: %s', rid, exc)
    set_req_id(None)


# ── DB teardown ──
app.teardown_appcontext(close_db)


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

# ── Register all Blueprints ──
from routes import register_all
register_all(app)


# ── Unified auth gate (single middleware) ──
# Replaces the legacy dual scheme (tunnel_auth + bearer_auth). One
# before_request hook resolves an AuthContext from any of:
#   - Authorization: Bearer / x-api-key header
#   - tofu_session cookie  (set on first browser visit via ?token=…)
#   - X-Tunnel-Token / TUNNEL_TOKEN  (deprecated back-compat shim)
# Public routes (static, /, /api/health, /api/v1/capabilities, etc.)
# bypass the gate — see _PUBLIC_EXACT in routes/api_v1/auth.py.
from routes.api_v1.auth import attach_rate_headers, auth_before_request
app.before_request(auth_before_request)
app.after_request(attach_rate_headers)


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


_bootstrap_personal_key_if_needed()


# ── Billing janitor: release stale credit reservations ──
# Spawns one daemon thread that sweeps the ledger every 5 minutes.
# A no-op if multi-user mode never gets used (the sweep just finds 0
# rows). Disabled with TOFU_BILLING_JANITOR=0.
try:
    from lib.billing.janitor import start_janitor as _start_billing_janitor
    _start_billing_janitor()
except Exception as _e:
    logging.getLogger('server.boot').warning(
        '[Billing] janitor failed to start: %s', _e)


# ── Static file cache headers ──
@app.after_request
async def add_cache_headers(response):
    if request.path.startswith('/static/'):
        # A 3xx here is the stale-bundle self-heal redirect (see _handle_404).
        # It MUST NOT inherit the immutable long-cache below — the redirect
        # target changes on every rebuild, so freezing it would permanently
        # pin a client to one now-stale mapping. Keep it uncached.
        if 300 <= response.status_code < 400:
            response.headers['Cache-Control'] = 'no-store'
            return response
        if request.path.endswith('.js'):
            response.content_type = 'text/javascript; charset=utf-8'
        elif request.path.endswith('.css'):
            response.content_type = 'text/css; charset=utf-8'
        if '/vendor/' in request.path or '/bundle-' in request.path:
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif request.path.endswith(('.js', '.css')):
            if 'v=' in request.query_string.decode('ascii', errors='ignore'):
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
            else:
                response.headers['Cache-Control'] = 'public, max-age=300, must-revalidate'
        else:
            response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


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


def _load_static_bytes(filename):
    """Resolve *filename* strictly under STATIC_DIR and read it (SYNC, runs in a
    worker thread so the FUSE I/O never touches the event loop).

    Returns ``(data, mtime, etag)`` on success or ``None`` when the path is
    unsafe (traversal) or the file is absent/not-a-file. Raising is reserved for
    genuine I/O errors (surfaced as 500). The blocking calls — safe_join, the
    ``os.path.isfile`` stat, and the full ``open().read()`` — are exactly what
    would wedge the loop if run inline; here they are on the thread.
    """
    from werkzeug.utils import safe_join
    from zlib import adler32
    full = safe_join(STATIC_DIR, filename)
    if full is None:
        return None  # traversal / absolute path → treat as not found (never leak)
    if not os.path.isfile(full):
        return None
    with open(full, 'rb') as f:
        data = f.read()
    st = os.stat(full)
    etag = '%d-%d-%d' % (int(st.st_mtime), st.st_size, adler32(data) & 0xFFFFFFFF)
    return data, st.st_mtime, etag


async def _static_offload(loop, filename):
    """Offload the blocking static read to a worker thread.

    A one-line seam kept separate so the executor-offload is the SINGLE point a
    test can neuter to prove it is load-bearing (running _load_static_bytes
    inline here would put the FUSE-blocking read back on the loop — the exact
    regression this whole route prevents).
    """
    return await loop.run_in_executor(None, _load_static_bytes, filename)


def _if_range_allows(if_range, etag, mtime):
    """RFC 9110 §13.1.5 conditional-range gate: return True iff a Range MAY be
    honoured given the request's ``If-Range`` validator.

    A conditional-range request carries the validator the client holds for its
    partial copy. If that validator no longer matches (the file changed since
    the client's partial), the Range MUST be ignored and the FULL current
    representation served — otherwise the client stitches a slice of the NEW
    file onto its OLD partial → silent corruption. Returns True when If-Range is
    absent/empty, its etag equals ours, or its HTTP-date is >= our mtime.
    """
    if if_range is None or (if_range.etag is None and if_range.date is None):
        return True
    if if_range.etag is not None:
        return if_range.etag == etag.strip('"')
    return mtime <= if_range.date.timestamp()


@app.route('/static/<path:filename>')
async def _static_route(filename):
    """Executor-offloaded, FUSE-stall-safe replacement for the built-in static
    view. All blocking FS I/O runs in a thread under a hard timeout."""
    from quart import abort, Response as _Resp
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            _static_offload(loop, filename),
            timeout=_STATIC_SEND_TIMEOUT)
    except asyncio.TimeoutError:
        # FUSE wedge: the file read did not complete in time. Return a fast 503
        # so the loop is NOT blocked (distinct from a 404 — a missing file is
        # not a stalled disk). This is the whole point of the offload.
        _lifecycle_log.critical(
            '[Static] read timed out after %.1fs for %s — FUSE stall suspected; '
            'returning 503 (loop preserved)', _STATIC_SEND_TIMEOUT, filename)
        abort(503)
    except OSError as e:
        _lifecycle_log.error('[Static] I/O error serving %s: %s', filename, e)
        abort(500)

    if result is None:
        # Missing / unsafe path → REAL 404 so _handle_404's stale-bundle
        # self-heal (resolve_stale_bundle) can redirect a stale bundle hash.
        abort(404)

    data, mtime, etag = result
    total = len(data)
    ctype, _ = mimetypes.guess_type(filename)
    ctype = ctype or 'application/octet-stream'

    # Range / partial-content (HTTP 206) — handled HERE, not via Quart's
    # make_conditional. Quart's _process_range_request emits a Content-Range
    # whose end byte is off-by-one (it passes end-1 to a werkzeug ContentRange
    # that already renders an exclusive stop), so a resumable-download client
    # that trusts Content-Range would miscount. werkzeug's
    # Range.range_for_length() correctly resolves closed (bytes=0-9), suffix
    # (bytes=-500), and open (bytes=500-) forms to a half-open (begin, end),
    # or None when unsatisfiable → 416. We slice the already-in-memory bytes
    # (no extra FS I/O) and set the header ourselves.
    # If-Range gate (RFC 9110 §13.1.5): honour the range only when the client's
    # conditional-range validator still matches (see _if_range_allows).
    range_ok = _if_range_allows(request.if_range, etag, mtime)

    req_range = request.range
    if range_ok and req_range is not None and req_range.units == 'bytes' and len(req_range.ranges) == 1:
        resolved = req_range.range_for_length(total)
        if resolved is None:
            r416 = _Resp(b'', status=416,
                         mimetype=ctype)
            r416.headers['Content-Range'] = 'bytes */%d' % total
            r416.headers['Accept-Ranges'] = 'bytes'
            return r416
        begin, end = resolved  # half-open: [begin, end)
        part = _Resp(data[begin:end], status=206, mimetype=ctype)
        part.headers['Content-Range'] = 'bytes %d-%d/%d' % (begin, end - 1, total)
        part.headers['Accept-Ranges'] = 'bytes'
        part.set_etag(etag)
        part.last_modified = mtime
        return part

    # No range → full body + conditional 304 on the loop with NO filesystem I/O
    # (size/mtime/etag were computed in the thread). mimetype is content-type
    # only; add_cache_headers overrides .js/.css content-type + Cache-Control.
    resp = _Resp(data, mimetype=ctype)
    resp.set_etag(etag)
    resp.last_modified = mtime
    resp.headers['Accept-Ranges'] = 'bytes'
    await resp.make_conditional(request, accept_ranges=True, complete_length=total)
    return resp


# ── Proxy config (reuse server.py logic) ──
try:
    from routes.config import _read_server_config
    from lib.proxy import set_bypass_domains, set_proxy_config
    _saved_cfg = _read_server_config()
    _saved_pc = _saved_cfg.get('proxy_config', {})
    if _saved_pc and any(_saved_pc.get(k) for k in ('http_proxy', 'https_proxy')):
        set_proxy_config(
            http_proxy=_saved_pc.get('http_proxy', ''),
            https_proxy=_saved_pc.get('https_proxy', ''),
        )
    _saved_proxy = _saved_cfg.get('proxy_bypass_domains', [])
    if _saved_proxy:
        set_bypass_domains(_saved_proxy)
except Exception as _e:
    _lifecycle_log.warning('Failed to load proxy config: %s', _e)


# ── Adaptive direct-vs-proxy path prober ──
try:
    from lib.netpath import start_prober as _start_netpath_prober
    _start_netpath_prober()
except Exception as _e:
    _lifecycle_log.warning('Failed to start netpath prober: %s', _e)


# ── Global error handlers ──
from lib.api_response import (
    api_internal_error,
    api_method_not_allowed,
    api_not_found,
    api_payload_too_large,
    api_service_unavailable,
)


def _is_api_request():
    return request.path.startswith('/api/')


def _ws_safe_method_path():
    """Return (method, path) tolerating contexts where request is unavailable."""
    try:
        return request.method, request.path
    except RuntimeError:
        from quart import websocket as _ws
        try:
            return 'WS', _ws.path
        except RuntimeError:
            return '?', '?'


@app.errorhandler(404)
async def _handle_404(exc):
    # Self-heal a stale content-hashed bundle request. A client holding an old
    # index.html (bfcache / long-lived tab / caching proxy defeating no-cache)
    # asks for a bundle-/feature-<hash>.js whose hash was deleted on the last
    # rebuild → 404 → LoadGuard banner. Redirect it to the current bundle of
    # the same kind so the stale page self-heals with zero user action. Only a
    # genuinely-built bundle name of a DIFFERENT hash is redirected; any other
    # miss falls through to a real 404 (never masked). See routes/common.py.
    if request.path.startswith('/static/js/'):
        from lib.js_bundler import resolve_stale_bundle
        requested = request.path.rsplit('/', 1)[-1]
        current = resolve_stale_bundle(requested)
        if current:
            _lifecycle_log.warning(
                '[StaleBundle] Self-healing stale request: %s -> %s (client held old index.html)',
                requested, current)
            resp = redirect('/static/js/' + current, code=302)
            # Never let this mapping be cached — the target changes each rebuild.
            resp.headers['Cache-Control'] = 'no-store'
            return resp

    if request.path.startswith('/.well-known/'):
        _lifecycle_log.debug('404 (well-known probe): %s', request.path)
    else:
        _lifecycle_log.warning('404 Not Found: %s %s', request.method, request.path)
    if _is_api_request():
        return api_not_found('Not Found: %s' % request.path)
    return await _orig_make_response_async(
        '<h2>404 — Not Found</h2><p>The requested URL was not found.</p>', 404)


@app.errorhandler(413)
async def _handle_413(exc):
    if _is_api_request():
        return api_payload_too_large(app.config['MAX_CONTENT_LENGTH'])
    return await _orig_make_response_async('<h2>413 — Payload Too Large</h2>', 413)


@app.errorhandler(405)
async def _handle_405(exc):
    if _is_api_request():
        return api_method_not_allowed()
    return await _orig_make_response_async('<h2>405 — Method Not Allowed</h2>', 405)


@app.errorhandler(500)
async def _handle_500(exc):
    rid = _get_req_id() or '-'
    method, path = _ws_safe_method_path()
    _lifecycle_log.error('500 ISE: [%s] %s %s', rid, method, path, exc_info=exc)
    if path.startswith('/api/'):
        return api_internal_error(exc, log_traceback=False)
    return await _orig_make_response_async(
        f'<h2>500</h2><p>Request ID: <code>{rid}</code></p>', 500)


@app.errorhandler(Exception)
async def _handle_uncaught(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    rid = _get_req_id() or '-'
    method, path = _ws_safe_method_path()
    # ── Transient DB-pool overload → 503, not 500 ──────────────────────
    # A saturated connection pool (reconnection burst after a restart) is a
    # transient overload, not a server bug. Shed load with 503 + Retry-After
    # so polling clients back off instead of retrying harder and amplifying
    # the storm. Logged at WARNING (not ERROR-with-traceback) — the pool
    # snapshot is the diagnostic, a stack trace here is just noise ×N.
    from lib.database import PoolExhaustedError
    if isinstance(exc, PoolExhaustedError):
        _lifecycle_log.warning('[%s] 503 pool-exhausted: %s %s (active=%d/%d '
                               'pooled=%d tracked=%d)', rid, method, path,
                               exc.active, exc.max_conns, exc.pooled, exc.tracked)
        if path.startswith('/api/'):
            return api_service_unavailable(
                'Server busy (database pool saturated) — retry shortly',
                retry_after=2, kind='overloaded')
        return await _orig_make_response_async(
            f'<h2>503</h2><p>Server busy — retry shortly. '
            f'Request ID: <code>{rid}</code></p>', 503)
    _lifecycle_log.error('[%s] Uncaught: %s %s: %s', rid, method, path, exc, exc_info=True)
    if path.startswith('/api/'):
        return api_internal_error(exc, log_traceback=False)
    return await _orig_make_response_async(
        f'<h2>500</h2><p>Request ID: <code>{rid}</code></p>', 500)


# ═══════════════════════════════════════════════════════════════════════
#  Startup & Main
# ═══════════════════════════════════════════════════════════════════════

_server_log = logging.getLogger('server')

# Descriptor produced by recover_stale_tasks_on_startup(dispatch=False) during
# _init_database; consumed by _serve() to run the deferred BILLED boot dispatch
# (killed-recovery + autopilot-resume) on the SERVING loop, not the startup one.
_DEFERRED_BOOT_DISPATCH = None

# ── JS bundle ──
# Built during server STARTUP (see _startup / _build_js_bundle), NOT at import
# time. Importing this module must have no side effect on the live static/js/
# artifact — otherwise every test-suite worker that imports `server` (e.g. 96
# pytest-xdist workers) races to rebuild the production bundle into the shared
# tree, clobbering the hash-named file mid-write. The build is idempotent and
# lock-guarded, so running it once from the real startup path is sufficient;
# a plain `import server` no longer touches the bundle.
def _build_js_bundle():
    """Build the JS bundle. Called from the server startup path only."""
    try:
        from lib.js_bundler import build_bundle
        build_bundle()
    except Exception as _bundle_err:
        _server_log.warning('JS bundle build failed: %s', _bundle_err)


def _init_database():
    """Initialize database (runs in app context)."""
    _boot('Initialising database…')
    init_db()
    warmup_db()
    try:
        from lib.database import heal_toast_corruption
        heal_toast_corruption()
    except Exception as e:
        _server_log.warning('TOAST auto-heal failed: %s', e)
    _boot('Database ready.')
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

    # ── Eager-load heavy C extensions so mlockall pins their pages ──
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


def _start_background_workers():
    """Launch optional background threads.

    Feature background workers (e.g. the trading intel + autopilot threads)
    now start via the ``tofu.startup`` entry-point group, run from
    ``routes.register_all`` after blueprints are mounted. Core no longer
    imports any optional feature here.
    """
    # Crash-resume: re-spawn motion-video jobs left ``running`` on disk by a
    # process that died mid-render. The stage-graph checkpoint + per-scene mp4
    # skip make the re-run resume rather than restart (owner correctness
    # contract, docs/PRODUCTION_PIPELINE_DESIGN.md). Best-effort — never blocks
    # startup.
    try:
        from lib.motion_video.engine import resume_interrupted_jobs
        n = resume_interrupted_jobs()
        if n:
            _server_log.info('[Server] resumed %d interrupted motion job(s)', n)
    except Exception as e:
        _server_log.warning('[Server] motion job resume failed: %s', e)
    # Auto-research counterpart (R4): same checkpointed stage-graph contract —
    # a job left 'running' on disk resumes from its last completed stage, so
    # an already-harvested corpus is not re-crawled.
    try:
        from lib.research import resume_interrupted_research
        n = resume_interrupted_research()
        if n:
            _server_log.info('[Server] resumed %d interrupted research job(s)', n)
    except Exception as e:
        _server_log.warning('[Server] research job resume failed: %s', e)
    # Podcast counterpart (P-UX4): a 'generating' cache row can only belong
    # to the process that just died — flip them to 'interrupted' so the tab
    # says "被重启打断" instead of pretending nothing happened.
    try:
        from lib.paper.podcast_engine import mark_interrupted_podcasts
        mark_interrupted_podcasts()
    except Exception as e:
        _server_log.warning('[Server] podcast interrupted sweep failed: %s', e)
    # Desktop-agent LAN discovery responder (design §11.2.1 rung B): ON by
    # default since 2026-08-04 (TOFU_DESKTOP_LAN_DISCOVERY=0 disables), and
    # skipped when the effective bind is loopback-only (advertising a LAN
    # url the server cannot be reached at would be a lie). Without THIS
    # call the responder class was dead code — only tests ever instantiated
    # it (owner review 2026-08-03).
    try:
        from lib.desktop.pairing import maybe_start_responder
        _lan_responder = maybe_start_responder(
            int(os.environ.get('_TOFU_RUNTIME_PORT') or '15000'),
            bind_host=os.environ.get('_TOFU_RUNTIME_HOST') or '')
        if _lan_responder is not None:
            _server_log.info('[Server] LAN discovery responder up on UDP '
                             '15001 (%s)', _lan_responder.url)
    except Exception as e:
        _server_log.warning('[Server] LAN discovery responder failed: %s', e)
    return


def _detect_reverse_proxy():
    """Detect whether we're running behind an HTTPS-terminating reverse proxy.

    Cloud IDEs / notebook platforms (VS Code port forwarding, GitHub
    Codespaces, Gitpod, JupyterHub-fronted environments like Meituan
    Codelab) expose the server on a public ``https://`` URL while talking
    to our backend over plain HTTP. If we also enable TLS on our side the
    proxy's plain-HTTP request hits our TLS listener and the connection is
    reset — the browser/proxy reports ``socket hang up``.

    Detection is signal-based, not host-name based, so it survives exports
    to any host: we look for env vars these platforms inject into the
    launch environment. Returns ``(behind_proxy: bool, proxy_name: str)``;
    ``proxy_name`` is ``''`` when nothing is detected.
    """
    # Ordered most-specific → most-generic so the friendliest name wins.
    if os.environ.get('VSCODE_PROXY_URI'):
        return True, 'VS Code'
    if os.environ.get('CODESPACES'):
        return True, 'Codespaces'
    if os.environ.get('GITPOD_WORKSPACE_URL'):
        return True, 'Gitpod'
    # JupyterHub-fronted platforms (Meituan Codelab, Binder, generic
    # JupyterHub) always terminate HTTPS at the hub and proxy plain HTTP
    # to single-user servers. JUPYTERHUB_* is set by the hub spawner;
    # JUPYTER_SERVER_URL / JPY_* cover bare notebook/lab proxying.
    if (os.environ.get('JUPYTERHUB_USER')
            or os.environ.get('JUPYTERHUB_SERVICE_PREFIX')
            or os.environ.get('JUPYTERHUB_API_URL')):
        return True, 'JupyterHub'
    # Codelab-specific belt-and-suspenders: the hub injects CODELAB_API_URL
    # even in shells where JUPYTERHUB_* was not exported.
    if os.environ.get('CODELAB_API_URL'):
        return True, 'Codelab'
    return False, ''


def _find_free_port(start=15000, end=15100):
    import socket
    for p in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            result = s.connect_ex(('localhost', p))
            s.close()
            if result != 0:
                return p
        except Exception:
            return p
    return start


def _wait_port_free(host, port, timeout=10.0):
    """Block until ``host:port`` can be bound, or ``timeout`` seconds elapse.

    Uses a real ``bind()`` probe rather than ``connect_ex`` so the server's
    OWN lingering listener — which is briefly still present right after an
    in-place re-exec restart — is correctly WAITED OUT instead of being
    mistaken for a foreign process (the connect-probe would see it as "in
    use" and silently shift the port). Returns True once the port is bindable.

    Args:
        host: Bind host (``0.0.0.0`` / ``::`` normalized to all-interfaces).
        port: Port to wait for.
        timeout: Max seconds to wait before giving up.

    Returns:
        True if the port became bindable within ``timeout``, else False.
    """
    import socket
    import time as _t
    bind_host = '' if host in ('', '0.0.0.0', '::') else host
    deadline = _t.time() + timeout
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_host, port))
            return True
        except OSError as e:
            if _t.time() >= deadline:
                _server_log.debug('[Port] %s:%d still busy after %.1fs wait: %s',
                                  bind_host or '*', port, timeout, e)
                return False
            _t.sleep(0.25)
        finally:
            s.close()


def _ensure_tls_certs(certfile='', keyfile=''):
    """Ensure TLS certificates exist for HTTP/2 support.

    Browsers only negotiate HTTP/2 over TLS (ALPN). Without certs the
    server falls back to HTTP/1.1 and we lose the multiplexing benefit.

    Strategy:
      1. If user provides --certfile/--keyfile, use those.
      2. If certs already exist at data/certs/tofu.{pem,key}, reuse them.
      3. Otherwise, auto-generate via the `cryptography` library (pure Python).

    Disable with TOFU_TLS=0 or --no-tls.

    Returns:
        (certfile_path, keyfile_path) or (None, None) if TLS unavailable.
    """
    _tls_log = logging.getLogger('server.tls')

    if certfile and keyfile:
        if os.path.isfile(certfile) and os.path.isfile(keyfile):
            _tls_log.info('[TLS] Using provided certs: %s, %s', certfile, keyfile)
            return certfile, keyfile
        _tls_log.warning('[TLS] Provided cert/key files not found: %s, %s', certfile, keyfile)

    cert_dir = os.path.join(_tofu_data_root(), 'certs')
    cert_path = os.path.join(cert_dir, 'tofu.pem')
    key_path = os.path.join(cert_dir, 'tofu.key')

    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        _tls_log.info('[TLS] Reusing existing self-signed certs at %s', cert_dir)
        return cert_path, key_path

    _boot('Generating self-signed TLS certificate for HTTP/2…')
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
        import ipaddress
        import socket

        os.makedirs(cert_dir, exist_ok=True)
        hostname = socket.gethostname()

        # Generate RSA 2048-bit key
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Build X.509 certificate — valid 10 years, SAN for local access
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, f'Tofu Server ({hostname})'),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName('localhost'),
                    x509.DNSName(hostname),
                    x509.IPAddress(ipaddress.IPv4Address('127.0.0.1')),
                    x509.IPAddress(ipaddress.IPv4Address('0.0.0.0')),
                ]),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        # Write key (mode 0600)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(_fd, key_pem)
        finally:
            os.close(_fd)

        # Write cert
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        with open(cert_path, 'wb') as f:
            f.write(cert_pem)

        _tls_log.info('[TLS] Generated self-signed cert at %s (valid 10 years)', cert_dir)
        _boot('TLS certificate ready (self-signed, valid 10 years).')
        return cert_path, key_path
    except ImportError:
        _tls_log.warning('[TLS] cryptography library not installed — '
                         'falling back to HTTP/1.1. Install: pip install cryptography')
        return None, None
    except Exception as e:
        _tls_log.warning('[TLS] Certificate generation failed: %s — falling back to HTTP/1.1', e)
        return None, None


def graceful_shutdown_signals():
    """Signals that must funnel into the graceful-drain path, not kill us.

    SIGTERM/SIGINT are the deliberate stop signals. SIGHUP is included on
    purpose: when the server is (wrongly) launched attached to a terminal —
    e.g. ``python server.py`` in a code-server terminal — closing that
    terminal makes the kernel deliver SIGHUP to the foreground process group,
    whose default disposition is *terminate*. That single hangup would take
    the server down along with the terminal session. Routing SIGHUP through
    the same graceful-drain handler makes "close the terminal" a clean,
    connection-draining shutdown instead of an abrupt death — defence in depth
    for the case the supervisor launch path is bypassed. Only signals that
    actually exist on this platform are returned (Windows has no SIGHUP).

    Returns:
        A list of signal numbers to register the graceful handler on.
    """
    names = ('SIGTERM', 'SIGINT', 'SIGHUP')
    out = []
    for name in names:
        sig = getattr(signal, name, None)
        if sig is not None:
            out.append(sig)
    return out


if __name__ == '__main__':
    try:
        from hypercorn.config import Config as HypercornConfig
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
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()

    host = args.host

    # ── Instance lock — prevent multiple servers on the same project dir ──
    # Self-healing: a contended flock does NOT prove a live server (an OOM
    # SIGKILL skips atexit/lock-release, and an orphaned child — or an
    # unclean death on a FUSE mount — can keep the fd's flock held). So on
    # contention we reclaim a stale LOCAL lock (dead recorded pid) by
    # unlinking + retrying on a fresh inode, exactly as stop.sh does. See
    # _acquire_instance_lock / _reclaim_stale_instance_lock.
    _lock_dir = _tofu_data_root()
    os.makedirs(_lock_dir, exist_ok=True)
    _lock_path = os.path.join(_lock_dir, '.server.lock')

    _lock_ok, _instance_lock_fd = _acquire_instance_lock(_lock_path, _server_log)
    if not _lock_ok:
        _skip = (os.environ.get('TOFU_SKIP_LOCK', '') or '').strip()
        if _skip != '1':
            _server_log.critical('Another server instance is already running from this project directory.\n'
                                 '  Set TOFU_SKIP_LOCK=1 to force start.')
            sys.exit(1)
        _server_log.warning('[Lock] TOFU_SKIP_LOCK=1 — bypassing instance lock')

    _boot('Instance lock acquired (PID=%d)', os.getpid())

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
        # Flip the clean-shutdown dirty-bit so the next boot classifies this as
        # a controlled exit, NOT an OS kill. One atomic write; never raises.
        try:
            from lib.shutdown_marker import mark_clean
            mark_clean('signal')
        except Exception as _sm_e:
            _server_log.warning('[Server] mark_clean(signal) failed: %s', _sm_e)
        _shutdown_requested.set()
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
    _force_tls = (getenv_compat('TOFU_TLS') or '').strip() == '1'
    _force_no_tls = (args.no_tls
                     or (getenv_compat('TOFU_TLS') or '').strip() == '0')
    # Auto-detect cloud-IDE / notebook reverse-proxy environments.
    # These proxies provide their own HTTPS+HTTP/2 on the public URL and
    # connect to our backend over plain HTTP. Adding TLS on our side causes
    # "socket hang up" because the proxy doesn't expect a TLS handshake.
    _behind_proxy, _proxy_name = _detect_reverse_proxy()
    _vscode_proxy = os.environ.get('VSCODE_PROXY_URI', '')

    if _force_no_tls:
        _tls_cert, _tls_key = None, None
        _boot('TLS disabled (--no-tls or TOFU_TLS=0).')
    elif _behind_proxy and not _force_tls:
        _tls_cert, _tls_key = None, None
        _boot('TLS auto-disabled — %s proxy detected (provides its own HTTPS). '
              'Force with TOFU_TLS=1.', _proxy_name or 'cloud IDE')
    else:
        _tls_cert, _tls_key = _ensure_tls_certs(args.certfile, args.keyfile)

    # Persist the protocol we are ACTUALLY serving (after the certs are
    # settled, so a cert-generation failure records 'http' correctly).
    _record_serve_mode('https' if (_tls_cert and _tls_key) else 'http')

    # ── Init DB + validate imports in app context ──
    async def _startup():
        async with app.app_context():
            _build_js_bundle()
            _init_database()
            # Checkpoint: a ^C/SIGTERM during DB init/recovery set the flag —
            # stop doing further startup work (heavy imports, MCP) and let the
            # post-startup checkpoint take us to a clean exit before serving.
            if _shutdown_requested.is_set():
                _server_log.info('[Server] Shutdown requested during DB init — '
                                 'skipping remaining startup phases.')
                return {}, False
            _validate_imports()
            _start_background_workers()

            # Shared-cgroup memory-pressure defenses (① self-check + ② monitor).
            # Both graceful no-ops off-cgroup. See lib/cgroup_guard.py.
            try:
                from lib import cgroup_guard
                cgroup_guard.startup_self_check()
                cgroup_guard.start_monitor()
            except Exception as e:
                _server_log.warning('[cgroup] pressure defenses failed to start: %s', e)

            if _shutdown_requested.is_set():
                _server_log.info('[Server] Shutdown requested during import '
                                 'validation — skipping MCP + background starts.')
                return {}, False

            # ── MCP auto-connect ──
            _boot('Configuring MCP auto-connect…')
            mcp_config = {}
            try:
                from lib.mcp.client import get_bridge
                from lib.mcp.config import load_mcp_config
                mcp_config = load_mcp_config()
                enabled = sum(1 for c in (mcp_config or {}).values()
                              if c.get('enabled', True))
                import threading

                def _mcp_auto():
                    # Pre-warm vendored launchers (pip install off the event
                    # loop) so a later App-Store install click is just the
                    # fast handshake, never a cold pip that would freeze the
                    # MCP loop. Runs even with zero configured servers — that
                    # is exactly the fresh-install case we want fast.
                    try:
                        from lib.mcp.client import prewarm_all_vendored
                        warmed = prewarm_all_vendored()
                        if warmed:
                            _server_log.info('[MCP] Pre-warm: %s', warmed)
                    except Exception as e:
                        _server_log.warning('[MCP] Pre-warm failed: %s', e)
                    if enabled <= 0:
                        return
                    try:
                        bridge = get_bridge()
                        result = bridge.connect_all()
                        total = sum(len(v) for v in result.values())
                        _server_log.info('[MCP] Auto-connect: %d servers, %d tools', len(result), total)
                        # MCP auto-connect runs on THIS background thread and
                        # finishes seconds after boot — after a user may have
                        # already opened a conversation and latched its tool
                        # schema WITHOUT the (not-yet-connected) MCP tools. That
                        # incomplete latch would then diverge on the next round,
                        # surfacing a spurious "tools changed" banner. Clearing
                        # every latch here mirrors the deliberate MCP-mutation
                        # path in routes/api_v1/mcp.py: the next round of each
                        # conversation re-latches from the now-complete tool
                        # surface. Cost is self-limiting — a conversation whose
                        # effective tool set is unchanged re-latches
                        # byte-identically (no cache rebuild); only ones that
                        # genuinely gained MCP tools pay a one-time rebuild.
                        if total > 0:
                            try:
                                from lib.tools import clear_all_tool_list_latches
                                n = clear_all_tool_list_latches()
                                if n:
                                    _server_log.info(
                                        '[MCP] Auto-connect cleared %d '
                                        'tool-schema latch(es) — MCP tools '
                                        'now included next round', n)
                            except Exception as e:
                                _server_log.warning(
                                    '[MCP] tool-latch invalidation after '
                                    'auto-connect failed: %s', e)
                    except Exception as e:
                        _server_log.error('[MCP] Auto-connect failed: %s', e, exc_info=True)

                threading.Thread(target=_mcp_auto, name='mcp-auto-connect', daemon=True).start()
            except Exception as e:
                _server_log.warning('[MCP] Auto-connect setup failed: %s', e)

            # ── Local health checker ──
            try:
                from lib.llm_dispatch.health_local import start_local_health_checker
                start_local_health_checker()
            except Exception as e:
                _server_log.warning('[HealthLocal] Failed: %s', e)

            # ── Local engine auto-discovery (well-known loopback ports) ──
            try:
                from lib.llm_dispatch.autodiscover_local import start_local_autodiscovery
                start_local_autodiscovery()
            except Exception as e:
                _server_log.warning('[AutoDiscover] Failed: %s', e)

            # ── FS keepalive ──
            try:
                from lib.fs_keepalive import start_fs_keepalive
                start_fs_keepalive()
            except Exception as e:
                _server_log.warning('FS keepalive failed: %s', e)

            # ── code-server fileWatcher excludes sync ──
            # Mirror the project's canonical watcherExclude globs into the
            # User-scope code-server settings so opening a PARENT dir as the
            # workspace root can't recurse into swebench_workdir/ and OOM the
            # host via fileWatcher workers (see lib/code_server_excludes.py).
            try:
                from lib.code_server_excludes import start_code_server_excludes_sync
                start_code_server_excludes_sync()
            except Exception as e:
                _server_log.warning('code-server excludes sync failed: %s', e)

            # ── Cross-DC detection ──
            try:
                from lib.cross_dc import init_cross_dc_detection
                init_cross_dc_detection()
            except Exception as e:
                _server_log.warning('Cross-DC detection failed: %s', e)

            # ── Feishu Bot ──
            feishu_ok = False
            try:
                from lib.feishu import start_bot as start_feishu_bot, ENABLED as FEISHU_ENABLED
                if FEISHU_ENABLED:
                    feishu_ok = start_feishu_bot()
            except Exception as e:
                _server_log.warning('Feishu Bot failed: %s', e)

            return mcp_config, feishu_ok

    mcp_config, feishu_ok = asyncio.run(_startup())

    # ── Shutdown-during-startup checkpoint ──
    # A ^C / SIGTERM received while _startup() ran only SET the flag (nothing
    # awaited it yet). Honor it NOW: do not print the Ready banner or begin
    # serving — go straight to a clean exit. The atexit PG-stop hook still runs.
    if _shutdown_requested.is_set():
        _server_log.info('[Server] Shutdown requested during startup — '
                         'exiting before serving (no Ready).')
        sys.exit(0)

    # ── Banner ──
    from lib.version import __version__ as _ver
    _mcp_count = len(mcp_config)
    _has_tls = bool(_tls_cert and _tls_key)
    _proto = 'https' if _has_tls else 'http'
    if _has_tls:
        _h2_status = 'HTTP/2 + HTTP/1.1 (TLS, auto-cert)'
    elif _behind_proxy:
        _h2_status = 'HTTP/1.1 (proxy provides HTTP/2)'
    elif _force_no_tls:
        _h2_status = 'HTTP/1.1 only (TLS disabled)'
    else:
        _h2_status = 'HTTP/1.1 (TLS unavailable — pip install cryptography)'
    _banner_lines = [
        '=' * 56,
        f'  🫧 Tofu Server  v{_ver}  [ASYNC]',
    ]
    if _behind_proxy and _vscode_proxy:
        _public_url = _vscode_proxy.replace('{{port}}', str(port))
        _banner_lines.append(f'  {_public_url}')
    _banner_lines.extend([
        f'  {_proto}://{host}:{port}',
        f'  Protocol: {_h2_status}',
        '  Server: Hypercorn (ASGI)',
    ])
    if _has_tls and not args.certfile:
        _banner_lines.append('  🔐  Self-signed cert (accept once in browser)')
    if feishu_ok:
        _banner_lines.append('  💬  Feishu Bot: ON')
    if _mcp_count > 0:
        _banner_lines.append(f'  🔌  MCP Apps: {_mcp_count} server(s)')
    if TUNNEL_TOKEN:
        _banner_lines.append('  🔒  Tunnel Auth: ON (deprecated — prefer API keys)')
    # Auth mode banner. Always show so the operator knows whether the
    # API surface is gated. Loud warning if open + non-loopback bind.
    if _AUTH_MODE == 'open':
        _banner_lines.append('  🔓  Auth: OPEN — no token required')
        if host not in ('127.0.0.1', 'localhost', '::1'):
            _banner_lines.append(
                f'  ⚠️   Bound to {host}: API is reachable on the LAN '
                'WITHOUT auth.')
            _banner_lines.append(
                '      Switch to private mode in Settings → API Keys, '
                'or set TOFU_AUTH_MODE=private.')
    elif _AUTH_MODE == 'private':
        _banner_lines.append('  🔒  Auth: PRIVATE — Bearer token required')
    elif _AUTH_MODE == 'multi-user':
        _banner_lines.append('  👥  Auth: MULTI-USER — Bearer token required')
    if _BOOTSTRAP_TOKEN:
        _banner_lines.append('  🔑  Personal admin key minted (first boot)')
        _banner_lines.append(f'      Token: {_BOOTSTRAP_TOKEN}')
        _banner_lines.append(
            f'      Open: {_proto}://{host}:{port}/?token={_BOOTSTRAP_TOKEN}')
        _banner_lines.append(
            '      Saved to data/config/.first_run_token (chmod 0600)')
        _banner_lines.append(
            '      (auto-cleared when this bootstrap key is revoked)')
    # Loopback bind behind a cloud-IDE proxy (measured 2026-08-05): the SSO
    # edge 401s every cookieless /api/* call, and the loopback bind kills
    # the direct-LAN route too — so a desktop agent on another machine has
    # NO path in at all. The banner must say so: the failure is otherwise
    # invisible (zero agent requests ever reach the access log).
    if host in ('127.0.0.1', 'localhost', '::1') and (
            os.environ.get('VSCODE_PROXY_URI') or '').strip():
        _banner_lines.append(
            '  ⚠️   Bound to loopback behind a cloud-IDE proxy: remote '
            'desktop agents can NEVER reach this server')
        _banner_lines.append(
            '      (the SSO edge rejects cookieless clients). Unset '
            'BIND_HOST / use --host 0.0.0.0 and restart to allow')
        _banner_lines.append(
            '      direct LAN attach, or rely on the agent-side ssh '
            'self-tunnel.')
    _banner_lines.append('  ⏱  Boot time: %.1fs' % (time.time() - _BOOT_T0))
    _banner_lines.append('=' * 56)
    _banner = '\n'.join(_banner_lines)
    _server_log.info('Server starting\n%s', _banner)
    # ── Cache-fix generation self-report (deploy-acceptance ground truth) ──
    # Print the IMPORTED module's CACHE_FIX_GEN — the bytecode ACTUALLY loaded
    # into this process, not the on-disk source. The prefix-cache deploy verdict
    # (tests/cache_acceptance_check.py) parses this from the boot window to prove
    # the running code carries the whole cache-fix chain. Disk-freshness alone
    # can't prove this (Python compiles .py at import and never re-reads it).
    try:
        from lib.llm.cache import CACHE_FIX_GEN as _cfg
        # Bind the self-report to THIS process (pid + bootId). A restart storm
        # produces many short-lived replicas whose boot lines land in the same
        # app.log time window; without the pid tag a DEAD replica that printed
        # gen=5 then lost the port race could have its gen credited to the OLD
        # process still holding :15000 (a cross-attribution false-green). The
        # deploy verdict matches this pid against the actual :15000 listener PID.
        try:
            from lib import boot_identity as _bi
            _bid = _bi.BOOT_ID
        except Exception:
            _bid = '?'
        _boot('[CacheFixGen] CACHE_FIX_GEN=%d pid=%d bootId=%s (in-memory)'
              % (_cfg, os.getpid(), _bid))
        # Also self-report the RESOLVED mid-anchor layout mode. The cache-cost
        # acceptance analyzer reads this to POSITIVELY confirm the running
        # process placed no mid stepping-stone (mode=drop) — so a post-restart
        # cache_mid_out_of_window=0 is attributable to the fix, not merely to
        # traffic too short to have armed a mid. In-memory (post-import) value,
        # so it reflects the bytecode + env actually loaded.
        try:
            from lib.llm.cache import _mid_placement_mode as _mpm
            _boot('[CacheMidMode] TOFU_CACHE_MID_MODE=%s pid=%d bootId=%s (in-memory)'
                  % (_mpm(), os.getpid(), _bid))
        except Exception as _mpm_e:
            _boot_logger.warning('[CacheMidMode] self-report failed: %s', _mpm_e)
    except Exception as _cfg_e:  # never let a diagnostic line block boot
        _boot_logger.warning('[CacheFixGen] self-report failed: %s', _cfg_e)

    # ── Source-tree fingerprint (restart-applied-my-edits ground truth) ──
    # Warm + freeze the code fingerprint at boot so it reflects the on-disk
    # source THIS process actually loaded (HEAD + uncommitted tracked edits).
    # The restart UI captures the OLD process's digest and only declares
    # "your changes are live" when the NEW process reports a DIFFERENT one.
    try:
        from lib import boot_identity as _bi2
        _fp = _bi2.code_fingerprint()
        _boot('[CodeFingerprint] head=%s dirty=%s digest=%s'
              % (_fp.get('head'), _fp.get('dirty'), _fp.get('digest')))
    except Exception as _fp_e:  # never let a diagnostic line block boot
        _boot_logger.warning('[CodeFingerprint] self-report failed: %s', _fp_e)

    # Clear the re-exec marker (pt_aa3cd224b3b346e7): boot is done —
    # tofu_guard may resume judging this process by the normal signals
    # (listener / health / instance lock). Best-effort: a leftover marker
    # just yields for the remainder of its 300s TTL, never blocks boot.
    try:
        os.unlink(os.path.join(_tofu_data_root(), '.reexec_in_progress'))
    except FileNotFoundError:
        pass
    except Exception as _mkc_e:
        _boot_logger.debug('[Update] re-exec marker clear failed: %s', _mkc_e)

    _boot('Ready — handing off to Hypercorn.')
    try:
        sys.stderr.write('\n' + _banner + '\n\n')
        sys.stderr.flush()
    except OSError:
        pass  # cosmetic console echo; broken inherited pipe must not block boot

    # ── Configure Hypercorn ──
    hconfig = HypercornConfig()
    hconfig.bind = [f'{host}:{port}']
    hconfig.accesslog = logging.getLogger('hypercorn.access')
    hconfig.errorlog = logging.getLogger('hypercorn.error')
    # SSE streams can last minutes (long LLM responses with tool use).
    # Default keep_alive_timeout=5s is fine (it's idle-between-requests),
    # but we increase it to avoid edge cases where a proxy holds a
    # connection just past the threshold. Graceful timeout for shutdown.
    hconfig.keep_alive_timeout = 600
    # Shutdown drain window for in-flight connections. Kept short so Ctrl+C
    # feels responsive on a local dev server (a second Ctrl+C force-quits —
    # see _signal_shutdown). Override via TOFU_GRACEFUL_TIMEOUT.
    try:
        hconfig.graceful_timeout = float(
            os.environ.get('TOFU_GRACEFUL_TIMEOUT', '') or '3')
    except (ValueError, TypeError):
        hconfig.graceful_timeout = 3.0

    # ── Listen backlog ──
    # Hypercorn's default (100) is small: if the event loop briefly stalls
    # (CPU starvation from sibling processes, a burst of slow handlers), the
    # kernel accept queue fills and further connections are dropped/reset —
    # the page "goes dark" even though the process is alive and the port is
    # bound. A larger backlog lets transient stalls QUEUE instead of failing,
    # so the browser reconnect succeeds once the loop catches up. The kernel
    # still caps this at net.core.somaxconn. Override via TOFU_LISTEN_BACKLOG.
    try:
        _listen_backlog = int(os.environ.get('TOFU_LISTEN_BACKLOG', '0') or '0')
    except (ValueError, TypeError) as _e:
        _server_log.debug('[Server] bad TOFU_LISTEN_BACKLOG, defaulting: %s', _e)
        _listen_backlog = 0
    if _listen_backlog <= 0:
        _listen_backlog = 1024
    hconfig.backlog = _listen_backlog

    if _has_tls:
        hconfig.certfile = _tls_cert
        hconfig.keyfile = _tls_key

    # ── Run ──
    async def _serve():
        loop = asyncio.get_running_loop()

        # Uncaught exceptions in coroutines / callbacks never reach
        # sys.excepthook — asyncio routes them here. Default behavior only
        # logs to the root 'asyncio' logger at ERROR; funnel through our
        # 'server' logger at ERROR with the traceback so they land in
        # error.log with full context.
        def _loop_exception_handler(_loop, ctx):
            msg = ctx.get('message') or 'Unhandled exception in event loop'
            exc = ctx.get('exception')
            _server_log.error('[asyncio] %s', msg,
                              exc_info=exc if exc else False)
        loop.set_exception_handler(_loop_exception_handler)

        # ── Event-loop BLOCKING guard (sub-stall early warning) — OPT-IN ──
        # The always-on stall watchdog (LoopWatch, 5s) is the safe 24/7 net:
        # it's a separate sampling thread with ZERO per-step instrumentation
        # and dumps once per stall — no overhead, no log flood, and it already
        # names the culprit top frame via _extract_loop_top_frame.
        #
        # THIS guard is the finer, sub-stall detector: it catches a SINGLE
        # on-loop step that hogs the loop past TOFU_LOOP_SLOW_CALLBACK_SECS
        # BEFORE it snowballs. It relies on ``loop.set_debug(True)``, and here
        # is the cost that makes it UNSAFE as a default on a high-concurrency
        # SSE/WebSocket service: in CPython, debug mode makes EVERY call_soon
        # run format_helpers.extract_stack() (a Python stack walk) per Handle,
        # and the slow-callback timing/logging is gated on ``self._debug``.
        # On a long-connection storm the per-schedule stack-walk cost is real,
        # and a burst of just-over-threshold steps would flood the 'asyncio'
        # logger into error.log (log I/O can itself back-pressure the loop —
        # the diagnostic aggravating the very stall it hunts).
        #
        # So this guard is DEFAULT OFF. Enable it deliberately for a diagnostic
        # window via TOFU_LOOP_DEBUG_GUARD=1 (threshold via
        # TOFU_LOOP_SLOW_CALLBACK_SECS, default 1.0s). When on, a rate-limiting
        # filter caps the 'asyncio' warnings so it can't flood the log even if
        # many steps trip at once. Normal production pays NOTHING and keeps the
        # cheap LoopWatch net.
        _debug_guard = (os.environ.get('TOFU_LOOP_DEBUG_GUARD', '') or '').strip().lower()
        _guard_on = _debug_guard in ('1', 'true', 'yes', 'on')
        try:
            _slow_cb = float(os.environ.get('TOFU_LOOP_SLOW_CALLBACK_SECS', '') or '1.0')
        except (ValueError, TypeError) as _e:
            _server_log.debug('[Server] bad TOFU_LOOP_SLOW_CALLBACK_SECS, using 1.0: %s', _e)
            _slow_cb = 1.0
        if _guard_on and _slow_cb > 0:
            loop.slow_callback_duration = _slow_cb
            loop.set_debug(True)
            _asyncio_log = logging.getLogger('asyncio')
            _asyncio_log.setLevel(logging.WARNING)
            # Rate-limit so a burst of just-over-threshold steps can't flood
            # error.log (and back-pressure the loop via log I/O). Token-ish:
            # at most _burst warnings per _window seconds, then a single
            # suppression note. Cheap, allocation-free, no lock (loop thread
            # touches it; the 'asyncio' logger emits from the loop thread).
            class _SlowCallbackRateLimit(logging.Filter):
                def __init__(self, burst=20, window=10.0):
                    super().__init__()
                    self._burst = burst
                    self._window = window
                    self._win_start = 0.0
                    self._count = 0
                    self._suppressed = 0

                def filter(self, record):
                    now = time.monotonic()
                    if now - self._win_start >= self._window:
                        if self._suppressed:
                            record.msg = ('%s [+%d more slow-callback warnings '
                                          'suppressed in the last %.0fs]'
                                          % (record.getMessage(), self._suppressed,
                                             self._window))
                            record.args = ()
                        self._win_start = now
                        self._count = 0
                        self._suppressed = 0
                    if self._count < self._burst:
                        self._count += 1
                        return True
                    self._suppressed += 1
                    return False

            _asyncio_log.addFilter(_SlowCallbackRateLimit())
            _server_log.info('[Server] Loop blocking-guard armed '
                             '(slow_callback_duration=%.1fs, rate-limited) — a '
                             'single on-loop step over this logs "Executing … '
                             'took N seconds". DIAGNOSTIC MODE (debug loop).', _slow_cb)
        else:
            _server_log.info('[Server] Loop blocking-guard OFF (default) — cheap '
                             'LoopWatch 5s net remains active. Set '
                             'TOFU_LOOP_DEBUG_GUARD=1 to enable sub-stall detection.')

        # ── Size the default executor ──
        # Every sync route handler runs in this loop's default executor via
        # Quart's run_sync. Python's default ThreadPoolExecutor is capped at
        # min(32, os.cpu_count()+4) — too small once long-lived sync handlers
        # (chat_send, upload, PDF parse) and per-stream poll storms coexist:
        # the pool saturates and new requests queue behind it. Size it
        # explicitly. Override via TOFU_SYNC_WORKERS.
        from concurrent.futures import ThreadPoolExecutor
        try:
            _sync_workers = int(os.environ.get('TOFU_SYNC_WORKERS', '0') or '0')
        except (ValueError, TypeError) as _e:
            _server_log.debug('[Server] bad TOFU_SYNC_WORKERS, auto-sizing: %s', _e)
            _sync_workers = 0
        if _sync_workers <= 0:
            _sync_workers = min(128, (os.cpu_count() or 4) * 8)
        _executor = ThreadPoolExecutor(max_workers=_sync_workers,
                                       thread_name_prefix='tofu-sync')
        loop.set_default_executor(_executor)
        _server_log.info('[Server] Sync route executor sized to %d threads', _sync_workers)

        # ── Dedicated agent-worker executor ──
        # spawn_task() runs run_task on a thread; if it shared the default
        # executor with sync route handlers, long agent runs would starve
        # request handling (and vice-versa). Give agent workers their own
        # pool so the two cannot deadlock each other. Sized to the
        # in-flight ceiling + headroom; override via TOFU_AGENT_WORKERS.
        try:
            _agent_workers = int(os.environ.get('TOFU_AGENT_WORKERS', '') or '0')
        except (ValueError, TypeError) as _e:
            _server_log.debug('[Server] bad TOFU_AGENT_WORKERS, auto-sizing: %s', _e)
            _agent_workers = 0
        if _agent_workers <= 0:
            _agent_workers = min(256, (os.cpu_count() or 4) * 16)
        _agent_executor = ThreadPoolExecutor(
            max_workers=_agent_workers, thread_name_prefix='tofu-agent')
        try:
            from lib.tasks_pkg import set_agent_executor, set_serving_loop
            set_agent_executor(_agent_executor)
            # F3 (pt_1acd0bcdb2174566): let spawn_task hop onto THIS loop from
            # loop-less worker threads (queue dispatch / reaper successors)
            # instead of degrading to untracked daemon threads.
            set_serving_loop(loop)
            _server_log.info('[Server] Agent-worker executor sized to %d threads',
                             _agent_workers)
        except Exception as _ae_err:
            _server_log.warning('[Server] could not install agent executor: %s',
                                _ae_err)

        from lib.push import hub as _push_hub
        _push_hub.set_loop(loop)

        # ── Periodic finished-task reaper ──
        # The headless agent-API path (agent/run, compat adapters,
        # /api/v1/chat) never calls cleanup_old_tasks() opportunistically
        # the way the UI chat routes do, so on a headless-only deployment
        # the in-memory task registry would grow without bound. Run a
        # cheap sweep on the loop every TOFU_TASK_CLEANUP_INTERVAL seconds
        # (default 60). Finished-only + TTL-bounded — never touches a
        # running task. Disable with the interval set to 0.
        try:
            _cleanup_interval = int(
                os.environ.get('TOFU_TASK_CLEANUP_INTERVAL', '') or '60')
        except (ValueError, TypeError) as _e:
            _server_log.debug('[Server] bad TOFU_TASK_CLEANUP_INTERVAL, using 60: %s', _e)
            _cleanup_interval = 60

        async def _task_reaper():
            from lib.tasks_pkg import cleanup_old_tasks
            while not _shutdown_requested.is_set():
                await asyncio.sleep(_cleanup_interval)
                try:
                    await asyncio.to_thread(cleanup_old_tasks)
                except Exception as _reap_err:
                    _server_log.warning('[Server] task reaper sweep failed: %s',
                                        _reap_err)

        if _cleanup_interval > 0:
            loop.create_task(_task_reaper())
            _server_log.info('[Server] Finished-task reaper every %ds',
                             _cleanup_interval)


        # ── Event-loop stall watchdog ──
        # We have no supervisor and faulthandler only fires on C-level fatal
        # signals, so a wedged event loop (a blocking call on the loop thread,
        # a starved executor, a FUSE/PG stall) currently goes SILENT: the port
        # stops accept()ing while the process stays alive, and we get no stack
        # to diagnose it. This turns that into a captured all-thread dump.
        #
        # TWO complementary capture paths:
        #
        #  (A) GUARANTEED, GIL-INDEPENDENT — faulthandler.dump_traceback_later.
        #      The async heartbeat acts as a watchdog PET: on each bump it
        #      cancels + re-arms a C-timer set to fire in _stall_threshold s.
        #      While the loop is healthy the timer is petted before it fires;
        #      when the loop wedges — even inside a single monolithic
        #      GIL-holding C call (the documented json.dumps / catastrophic-
        #      regex pit) — the timer's DEDICATED C THREAD fires WITHOUT taking
        #      the GIL and writes an all-thread dump to the FUSE-resilient
        #      /dev/shm sink. This is the path that covers the one root cause
        #      the project has proven can happen.
        #
        #  (B) COMPLEMENTARY, human-readable — an off-loop daemon thread watches
        #      the heartbeat timestamp and, on a stall, emits an ERROR log line
        #      (with measured duration) + a dump to the FUSE log sink. It works
        #      for GIL-RELEASING stalls (blocking syscalls: FUSE/PG) but is
        #      BLIND to a GIL-held wedge (it must take the GIL to run) — hence
        #      it is a signal, never the sole guarantee. Path (A) is.
        #
        # One dump per stall episode; both re-arm on recovery.
        # Set TOFU_LOOP_STALL_SECS=0 to disable both.
        try:
            _stall_threshold = float(
                os.environ.get('TOFU_LOOP_STALL_SECS', '') or '5')
        except (ValueError, TypeError) as _e:
            _server_log.debug('[Server] bad TOFU_LOOP_STALL_SECS, using 5.0: %s', _e)
            _stall_threshold = 5.0
        try:
            _stall_bump_interval = float(
                os.environ.get('TOFU_LOOP_HEARTBEAT_SECS', '') or '1')
        except (ValueError, TypeError) as _e:
            _server_log.debug('[Server] bad TOFU_LOOP_HEARTBEAT_SECS, using 1.0: %s', _e)
            _stall_bump_interval = 1.0
        if _stall_bump_interval <= 0:
            _stall_bump_interval = 1.0

        # The C-timer must fire AFTER a healthy heartbeat would have petted it,
        # so its timeout must exceed the bump interval; guarantee headroom.
        _ctimer_timeout = max(_stall_threshold, _stall_bump_interval * 2.0)
        _arm_ctimer = _should_arm_ctimer(_stall_threshold, _fault_shm_log)

        _loop_heartbeat = {'ts': time.monotonic()}
        _listen_state = {'was_bound': False, 'misses': 0}
        _LISTENER_LOSS_K = 5   # consecutive 1s-bump misses ⇒ serve death
        # Probe the bind host; a wildcard bind answers on loopback too.
        _listen_probe_host = host if host not in ('0.0.0.0', '::', '') else '127.0.0.1'

        async def _loop_heartbeat_task():
            # Pet path (A): re-arm the GIL-independent C-timer on every bump.
            while not _shutdown_requested.is_set():
                _loop_heartbeat['ts'] = time.monotonic()
                # Persist a wall-clock heartbeat to the local-disk sidecar so a
                # RESTARTING process can tell this loop is alive AND healthy. A
                # wedge (FUSE syscall) stops these bumps → the file ages → the
                # lock-reclaim path treats us as wedged and can take over.
                _write_heartbeat()
                # Serve-listener liveness (second layer): a Hypercorn
                # serve-task death with a LIVE loop is invisible to every
                # external probe — the watchdog sees a live pid + fresh
                # heartbeat and yields forever (the 2026-08-03 11:14 state).
                # Die loudly instead so the watchdog gets a clean death it
                # can relaunch from. A broken probe never kills the server.
                try:
                    _bound = await asyncio.to_thread(
                        _port_bound, port, _listen_probe_host)
                except Exception:
                    _bound = True
                _was, _miss, _serve_dead = _listener_death_decide(
                    _listen_state['was_bound'], _bound,
                    _listen_state['misses'], _LISTENER_LOSS_K)
                _listen_state['was_bound'], _listen_state['misses'] = _was, _miss
                if _serve_dead:
                    _server_log.critical(
                        '[LoopWatch] serve listener on :%d lost for %d consecutive '
                        'checks while the loop is alive — serve task is dead; '
                        'exiting so the watchdog relaunches', port, _miss)
                    try:
                        from lib.log import audit_log as _audit
                        _audit('serve_listener_death', port=port, misses=_miss,
                               pid=os.getpid())
                    except Exception:
                        pass
                    os._exit(1)
                if _arm_ctimer:
                    try:
                        faulthandler.cancel_dump_traceback_later()
                        # exit=False: capture the hang, do NOT abort the process.
                        faulthandler.dump_traceback_later(
                            _ctimer_timeout, repeat=False,
                            file=_fault_shm_log, exit=False)
                    except Exception as _ct_err:
                        _server_log.warning('[LoopWatch] could not arm C-timer: %s', _ct_err)
                await asyncio.sleep(_stall_bump_interval)
            if _arm_ctimer:
                try:
                    faulthandler.cancel_dump_traceback_later()
                except Exception:
                    pass

        def _loop_stall_watch():
            # Poll fast enough to notice a stall promptly, but never faster
            # than a fraction of the threshold. Runs off-loop as a daemon.
            poll = max(0.5, min(_stall_bump_interval, _stall_threshold / 2.0))
            already_dumped = False
            while not _shutdown_requested.is_set():
                _shutdown_requested.wait(poll)
                if _shutdown_requested.is_set():
                    break
                age = time.monotonic() - _loop_heartbeat['ts']
                should_dump, already_dumped = _loop_stall_decide(
                    age, _stall_threshold, already_dumped)
                if not should_dump:
                    continue
                # Structured, grep-able culprit line so the NEXT stall needs no
                # stack-diving: pull the event-loop thread's current frame and
                # name the deepest application frame (skips stdlib leaves like
                # ssl.read → names segment_backfill.py:257). audit_log is
                # thread-safe; best-effort — a failure must not skip the dump.
                _top_frame = ''
                try:
                    import sys as _sys
                    _loop_tid = threading.main_thread().ident
                    _frames = _sys._current_frames()
                    _top_frame = _extract_loop_top_frame(_frames.get(_loop_tid))
                except Exception as _tf_err:
                    _server_log.debug('[LoopWatch] top-frame extract failed: %s', _tf_err)
                _pressure = _stall_pressure_context()
                try:
                    from lib.log import audit_log as _audit_log
                    _audit_log('event_loop_stall', duration=round(age, 1),
                               threshold=_stall_threshold, top_frame=_top_frame,
                               pressure=_pressure, pid=os.getpid())
                except Exception as _al_err:
                    _server_log.debug('[LoopWatch] audit_log failed: %s', _al_err)
                _server_log.error(
                    '[LoopWatch] event loop STALLED ~%.1fs (threshold=%.1fs) at %s%s — '
                    'dumping all-thread stacks to faulthandler sinks',
                    age, _stall_threshold, _top_frame or '?',
                    (' [' + _pressure + ']') if _pressure else '')
                for _sink in (_fault_shm_log, _fault_log):
                    if _sink is None:
                        continue
                    try:
                        _sink.write('\n=== LOOP STALL pid=%d age=%.1fs at %s ===\n'
                                    % (os.getpid(), age, time.strftime('%Y-%m-%d %H:%M:%S')))
                        _sink.flush()
                        faulthandler.dump_traceback(file=_sink, all_threads=True)
                        _sink.flush()
                    except Exception as _dump_err:
                        _server_log.warning('[LoopWatch] dump to sink failed: %s', _dump_err)

        if _stall_threshold > 0:
            loop.create_task(_loop_heartbeat_task())
            _stall_thread = threading.Thread(
                target=_loop_stall_watch, name='tofu-loopwatch', daemon=True)
            _stall_thread.start()
            _server_log.info(
                '[Server] Loop-stall watchdog armed (threshold=%.1fs, heartbeat=%.1fs, '
                'GIL-independent C-timer=%s @ %.1fs)',
                _stall_threshold, _stall_bump_interval,
                'on' if _arm_ctimer else 'off', _ctimer_timeout)
        else:
            _server_log.info('[Server] Loop-stall watchdog disabled (TOFU_LOOP_STALL_SECS=0)')

        # ── Write-freshness token replay ──
        # Restore the read/write fingerprints saved by the previous image
        # (re-exec or clean exit) so the shared-tree overwrite guard is NOT
        # fail-open in the post-restart window. Must run BEFORE the
        # deferred boot dispatch below (which spawns tasks that write).
        try:
            from lib import write_freshness as _wf
            _wf.load_snapshot()
        except Exception as _wf_err:
            _server_log.warning('[Server] write-freshness snapshot replay failed: %s',
                                _wf_err)

        # ── HEAD-moved auto-restart watcher (opt-in) ──
        # The "effective" contract for agent work on a shared checkout: a
        # commit only counts once the RUNNING process serves it. With
        # TOFU_AUTO_RESTART=1 this daemon re-execs the server when the
        # checked-out HEAD moves while idle (no in-flight tasks, shutdown
        # not requested) — the same guard the manual restart endpoint uses.
        try:
            from lib.auto_restart import maybe_start_auto_restart_watch
            if maybe_start_auto_restart_watch(shutdown_requested=_shutdown_requested):
                _server_log.info('[Server] Auto-restart watcher armed (TOFU_AUTO_RESTART=1)')
        except Exception as _ar_err:
            _server_log.warning('[Server] Auto-restart watcher setup failed: %s', _ar_err)

        # ── Deferred BILLED boot dispatch ──
        # killed-recovery + autopilot-resume were split out of the startup path
        # (they SPAWN carriers). Run them HERE, on the SERVING loop, so a
        # re-dispatched carrier is scheduled on THIS loop (which keeps running
        # under Hypercorn) — NOT the startup loop, whose asyncio.run() teardown
        # would otherwise block until the carrier finished (the 297s boot).
        # Gated on the shutdown flag: a ^C during startup skips it entirely.
        if _DEFERRED_BOOT_DISPATCH is not None and not _shutdown_requested.is_set():
            async def _run_deferred_boot_dispatch():
                try:
                    from lib.tasks_pkg import run_deferred_boot_dispatch
                    await asyncio.to_thread(
                        run_deferred_boot_dispatch, _DEFERRED_BOOT_DISPATCH,
                        should_continue=lambda: not _shutdown_requested.is_set(),
                        stop_event=_shutdown_requested)
                except Exception as _dbd_err:
                    _server_log.warning('[Server] deferred boot dispatch failed: %s',
                                        _dbd_err)
            loop.create_task(_run_deferred_boot_dispatch())

        # ── Orphaned-queue re-dispatch (message_queue) ──
        #   A human message QUEUED while a task was running lives ONLY in
        #   message_queue (never in conversations.messages). Nothing drains it
        #   on a fresh boot for a conv whose running task died with the process
        #   → the message is shown in the queue bar but never processed = total
        #   loss. Drain it on the SERVING loop (blocking DB + spawn work → a
        #   thread), gated on the shutdown flag so a ^C during boot skips it.
        #   Runs AFTER recover_stale_tasks_on_startup cleared dead activeTaskId
        #   pointers, so it cannot double-dispatch (plus a per-conv live guard).
        async def _run_orphan_queue_redispatch():
            if _shutdown_requested.is_set():
                return
            try:
                from lib.message_queue import redispatch_orphaned_queue_on_startup
                spawned = await asyncio.to_thread(redispatch_orphaned_queue_on_startup)
                if spawned:
                    _server_log.info('[Server] orphaned-queue redispatch spawned '
                                     '%d task(s) from stranded queue rows',
                                     len(spawned))
            except Exception as _oq_err:
                _server_log.warning('[Server] orphaned-queue redispatch failed: %s',
                                    _oq_err)
        loop.create_task(_run_orphan_queue_redispatch())

        # Bridge the SIGTERM threading.Event to an async trigger Hypercorn
        # awaits. When set, Hypercorn stops accepting new connections and
        # drains in-flight ones within graceful_timeout. Poll cheaply (the
        # signal handler can't touch loop state directly from a thread).
        async def _shutdown_trigger():
            while not _shutdown_requested.is_set():
                await asyncio.sleep(0.25)

        await hypercorn_serve(app, hconfig, shutdown_trigger=_shutdown_trigger)

        # ── Shutdown quiesce (ordering fix) ──
        # Hypercorn has drained HTTP; now signal every RUNNING agent task to
        # abort and give the agent-worker pool a BOUNDED window to stop, BEFORE
        # the atexit stop_local_pg_if_owned hook stops PG. Without this, live
        # carriers keep hitting get_thread_db while PG is shutting down → the
        # "database system is shutting down" + "cannot schedule new futures
        # after interpreter shutdown" cascade. Best-effort, time-boxed.
        try:
            from lib.tasks_pkg import quiesce_running_tasks
            _n_quiesced = quiesce_running_tasks(reason='server_shutdown')
        except Exception as _q_err:
            _server_log.warning('[Server] task quiesce failed: %s', _q_err)
            _n_quiesced = 0
        try:
            _drain_secs = float(os.environ.get('TOFU_SHUTDOWN_DRAIN_SECS', '') or '3')
        except (ValueError, TypeError):
            _drain_secs = 3.0
        if _n_quiesced and _drain_secs > 0:
            _server_log.info('[Server] Draining %d aborted task(s) up to %.0fs '
                             'before PG stop…', _n_quiesced, _drain_secs)
            # Terminal feedback so the post-HTTP drain isn't a silent wait; a
            # Ctrl+C here hits _signal_shutdown (flag already set) → force-quit.
            try:
                sys.stderr.write(
                    '\033[33m[Server] Waiting up to %.0fs for %d running task(s) '
                    'to stop (Ctrl+C to skip)…\033[0m\n'
                    % (_drain_secs, _n_quiesced))
                sys.stderr.flush()
            except Exception:
                pass
            _deadline = time.monotonic() + _drain_secs
            try:
                from lib.tasks_pkg import tasks as _tasks, tasks_lock as _tasks_lock
                while time.monotonic() < _deadline:
                    with _tasks_lock:
                        _still = sum(1 for _t in _tasks.values()
                                     if _t.get('status') == 'running')
                    if _still == 0:
                        break
                    await asyncio.sleep(0.25)
            except Exception as _dr_err:
                _server_log.warning('[Server] shutdown drain wait failed: %s', _dr_err)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        _server_log.info('[Server] Received SIGINT — shutting down…')
