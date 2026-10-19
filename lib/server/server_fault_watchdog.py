"""Fault-dump sinks, prune + loop-stall helpers (extracted from server.py).

Owns the early faulthandler dual-sink arming (``arm_faulthandler_sinks``),
safe retained-frame snapshots, the boot-time /dev/shm prune and helpers wired
up inside ``lib.server.server_loop_watchdog.py``. The decision helpers stay pure and
unit-testable without a running loop — see
``tests/cgroup_guard/test_loop_stall_watchdog.py``.

This module must NOT import ``server`` (server.py imports it for re-export).
``_pid_alive`` is duplicated here (it is also defined in the lock cluster)
because the prune helpers use it as a default argument and this module cannot
reach back into the composition root.
"""

import errno
import faulthandler
import logging
import os
import sys
import threading
from types import FrameType
import time
import typing

from lib.server.server_runtime_probes import stall_pressure_context

_FAULT_DUMP_PREFIX = 'tofu_faulthandler_'
_FAULT_DUMP_SUFFIX = '.log'


def format_frame_stack(frame: FrameType | None, *, max_frames: int = 128,
                       max_bytes: int = 64 * 1024) -> str:
    """Format retained frame metadata without linecache/source I/O."""
    parts: list[str] = []
    remaining = max(0, max_bytes)
    for _ in range(max(0, max_frames)):
        if frame is None:
            break
        code = frame.f_code
        line = (f'  File {code.co_filename[:4096]!r}, line {frame.f_lineno}, '
                f'in {code.co_name[:512]}\n')
        wire = line.encode('utf-8', errors='replace')
        if len(wire) > remaining:
            parts.append(wire[:remaining].decode('utf-8', errors='ignore'))
            remaining = 0
            break
        parts.append(line)
        remaining -= len(wire)
        frame = frame.f_back
    if frame is not None:
        marker = '...<stack truncated>\n'.encode()
        parts.append(marker[:remaining].decode())
    return ''.join(parts)


def capture_thread_stacks(*, max_bytes: int, max_frames: int = 128,
                          priority_thread_id: int | None = None) -> str:
    """Retain frames, then format within the existing fault-log byte budget.

    Loop-thread evidence comes first; other threads are captured up to the
    byte budget, without CPython faulthandler's fixed 100-thread ceiling.
    Strong frame references survive thread completion and generator teardown.
    No references escape this call and no frame locals are inspected.
    """
    frames = sys._current_frames()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    ordered = sorted(frames, key=lambda ident: (ident != priority_thread_id, ident))
    parts: list[str] = []
    remaining = max(0, max_bytes)
    marker = '...<thread snapshot truncated>\n'
    reserve = min(remaining, len(marker.encode()))
    remaining -= reserve
    captured = 0
    for ident in ordered:
        header = f'Thread {ident:#x} ({names.get(ident, "unknown")[:128]}):\n'
        encoded = header.encode('utf-8', errors='replace')
        if len(encoded) > remaining:
            break
        parts.append(header)
        remaining -= len(encoded)
        stack = format_frame_stack(frames[ident], max_frames=max_frames,
                                   max_bytes=remaining)
        parts.append(stack)
        remaining -= len(stack.encode('utf-8', errors='replace'))
        captured += 1
        if remaining <= 0:
            break
    if captured < len(ordered) or remaining <= 0:
        parts.append(marker.encode()[:remaining + reserve].decode())
    return ''.join(parts)

def _native_runtime_header():
    """Record bounded interpreter/core provenance before any native crash."""
    import json
    facts = {'python': sys.version, 'executable': sys.executable,
             'fatal_thread_scope': 'faulting-thread-only',
             'stall_thread_scope': 'retained-python-frames'}
    try:
        import resource
        facts['core_rlimit_bytes'] = list(resource.getrlimit(resource.RLIMIT_CORE))
    except (ImportError, OSError, ValueError):
        pass
    for name, path in (
            ('core_pattern', '/proc/sys/kernel/core_pattern'),
            ('core_filter', '/proc/self/coredump_filter')):
        try:
            with open(path, encoding='utf-8') as source:
                facts[name] = source.read(4096).strip()
        except OSError:
            pass
    return '=== native crash provenance ' + json.dumps(facts, ensure_ascii=True) + ' ===\n'


class FaultSinks(typing.NamedTuple):
    """Open crash-sink handles plus the paths they were opened at."""
    durable_dir: str
    durable_path: str
    shm_path: str
    durable_log: typing.Any
    shm_log: typing.Any

    @classmethod
    def unarmed(cls, *, pid, default_log_dir):
        """Paths without handles, for importers that must never arm sinks."""
        return cls(
            durable_dir=default_log_dir,
            durable_path=os.path.join(
                default_log_dir,
                _FAULT_DUMP_PREFIX + '%d' % pid + _FAULT_DUMP_SUFFIX),
            shm_path='/dev/shm/'
                     + _FAULT_DUMP_PREFIX + '%d' % pid + _FAULT_DUMP_SUFFIX,
            durable_log=None,
            shm_log=None,
        )


def arm_faulthandler_sinks(*, pid, default_log_dir):
    """Open early crash sinks for the executable server, never an importer.

    Captures C-level fatal signals (SIGSEGV/SIGABRT/SIGFPE/SIGILL/SIGBUS).
    These fire on heap corruption (e.g. ``munmap_chunk(): invalid pointer``)
    from native extensions like urllib3's response decompressor. Without this
    the abort prints to fd 2 only and we lose the Python stack of every
    thread. The signal handler captures only the faulting thread: traversing
    other live frames without the GIL can itself segfault. Complete native
    thread evidence belongs to the OS core; recoverable stalls use retained
    frame snapshots from ``capture_thread_stacks``.

    Dual-sink strategy: write to BOTH a per-process FUSE-backed file (durable
    across box restarts) and a per-process tmpfs mirror (immune to FUSE
    stalls). Only the real executable arms the sinks; historically this ran on
    every ``import server`` and appended to one shared file: test/tool imports
    alone produced 44k headers, while 1,598 stall dumps grew it to 125 MiB
    with no bound.
    """
    try:
        from lib.logging_pkg.log_policy import LOG_FILE_MODE as fault_file_mode
    except Exception as exc:
        # Early-boot fallback: the manifest may not be importable yet.
        logging.getLogger(__name__).debug(
            'log policy unavailable, using default fault file mode: %s', exc)
        fault_file_mode = 0o600
    header = '=== faulthandler armed pid=%d at %s ===\n' % (
        pid, time.strftime('%Y-%m-%d %H:%M:%S'))
    header += _native_runtime_header()

    sinks = FaultSinks.unarmed(pid=pid, default_log_dir=default_log_dir)
    shm_log = None
    durable_log = None

    # Arm tmpfs first, before importing even the lightweight writable-path
    # resolver.  This preserves early native-crash capture while ensuring a
    # fresh/XDG or frozen install places its durable file beside all other
    # logs instead of writing into a source/read-only bundle.
    try:
        shm_log = open(sinks.shm_path, 'w+', buffering=1)
        try:
            os.fchmod(shm_log.fileno(), fault_file_mode)
        except (AttributeError, OSError):
            os.chmod(sinks.shm_path, fault_file_mode)
        shm_log.write(header)
        faulthandler.enable(file=shm_log, all_threads=False)
    except (OSError, RuntimeError):
        if shm_log is not None:
            try:
                shm_log.close()
            except OSError:
                pass
        shm_log = None

    durable_dir = sinks.durable_dir
    durable_path = sinks.durable_path
    try:
        from lib.log import LOG_DIR as writable_log_dir
        durable_dir = writable_log_dir
        durable_path = os.path.join(
            durable_dir,
            _FAULT_DUMP_PREFIX + '%d' % pid + _FAULT_DUMP_SUFFIX)
    except Exception as exc:
        # Keep the caller-provided default dir when lib.log cannot resolve yet.
        logging.getLogger(__name__).debug(
            'writable log dir resolution failed, keeping default: %s', exc)
    try:
        os.makedirs(durable_dir, exist_ok=True)
        durable_log = open(durable_path, 'w+', buffering=1)
        try:
            os.fchmod(durable_log.fileno(), fault_file_mode)
        except (AttributeError, OSError):
            os.chmod(durable_path, fault_file_mode)
        durable_log.write(header)
    except OSError:
        durable_log = None

    # Fall back to the durable fd, then stderr, when tmpfs is unavailable.
    # ``w+`` lets the healthy heartbeat cap repeated non-fatal stall dumps
    # without replacing the descriptor retained by faulthandler.
    if shm_log is None:
        if durable_log is not None:
            faulthandler.enable(file=durable_log, all_threads=False)
        else:
            faulthandler.enable(all_threads=False)

    return FaultSinks(
        durable_dir=durable_dir,
        durable_path=durable_path,
        shm_path=sinks.shm_path,
        durable_log=durable_log,
        shm_log=shm_log,
    )


def _port_bound(port, host='127.0.0.1', timeout=0.5):
    """True iff a TCP connection to host:port succeeds (listener present).
    Local resource failures propagate: an undispatched probe cannot observe
    the listener. Scheme-agnostic for TLS and plain-HTTP listeners."""
    from tofu_db._loopback import create_connection
    try:
        with create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        if exc.errno in {
            errno.EADDRNOTAVAIL, errno.EADDRINUSE, errno.EMFILE, errno.ENFILE,
            errno.ENOBUFS, errno.ENOMEM,
        }:
            raise
        return False


def _listener_death_decide(was_bound, bound, misses, k):
    """Pure decision for the serve-listener watch (second layer after the
    loop heartbeat). Returns ``(was_bound, misses, listener_unavailable)``.

    Arms only after the listener was seen bound at least once (the pre-serve
    startup window is not our watch); counts CONSECUTIVE misses from there;
    a single recovery resets the streak. Connection failure is evidence of
    unavailability, not proof that owned work has stopped. The lifecycle owner
    reports the outage and keeps probing without exiting the process."""
    if bound is None:
        # Unknown observations neither establish binding nor complete a
        # consecutive loss streak. Loop-stall detection remains independent.
        return (was_bound, 0, False)
    if bound:
        return (True, 0, False)
    if not was_bound:
        return (False, 0, False)
    misses += 1
    return (True, misses, misses >= k)


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
    try:
        from lib.logging_pkg.log_policy import (
            stream_backup_count, stream_family_budget_bytes, stream_max_bytes,
        )
        return {
            'active_bytes': stream_max_bytes('faulthandler_process'),
            'stale_files': stream_backup_count('faulthandler_process'),
            'stale_bytes': stream_family_budget_bytes('faulthandler_process'),
        }
    except Exception as exc:
        # Early/frozen boot fallback: keep crash capture available even if the
        # shared manifest cannot import yet.
        logging.getLogger(__name__).debug(
            'fault budget manifest unavailable: %s', exc)

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
        # This module lives in lib/server/, so the project root is two levels up.
        project_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
    innermost = None
    f = frame
    while f is not None:
        code = f.f_code
        fname = code.co_filename
        if innermost is None:
            innermost = '%s:%d in %s' % (fname, f.f_lineno, code.co_name)
        try:
            in_project = os.path.abspath(fname).startswith(project_root + os.sep)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                'fault frame path classification failed: %s', exc)
            in_project = False
        if in_project and 'site-packages' not in fname:
            return '%s:%d in %s' % (fname, f.f_lineno, code.co_name)
        f = f.f_back
    return innermost or ''
