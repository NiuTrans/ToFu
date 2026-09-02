"""Fault-dump prune + loop-stall pure helpers (extracted from server.py).

These back the boot-time /dev/shm prune and the loop-stall watchdog wired up
inside ``lib.server_loop_watchdog.py``. Kept as pure module-level helpers so
they are unit-testable without a running loop — see
``tests/test_loop_stall_watchdog.py``.

This module must NOT import ``server`` (server.py imports it for re-export).
``_pid_alive`` is duplicated here (it is also defined in the lock cluster)
because the prune helpers use it as a default argument and this module cannot
reach back into the composition root.
"""

import logging
import os

from lib.server_runtime_probes import stall_pressure_context

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
        from lib.log_policy import (
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
        # This module lives in lib/, so the project root is one level up.
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
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
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return True
