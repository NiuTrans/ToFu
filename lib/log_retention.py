"""Bound every core runtime log family and expose retention evidence.

Responsibility
--------------
Apply :mod:`lib.log_policy` to application-owned rotating files, append-only
process-console/bootstrap files, PostgreSQL collector files, and the aggregate
``logs/`` budget.  The implementation is standard-library-only so it can run
from the server lifecycle manager and from an offline repair command even when
the application or storage sidecar cannot import.

Safety
------
Only regular files inside explicitly supplied directories are touched;
symlinks and subdirectories are never followed.  Active application handler
files are left to their owning handlers.  Append-only external streams use a
bounded copy-truncate rotation because their writers retain an open file
descriptor.  Unregistered files modified in the last day are reported but
never removed.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path

from lib.log_policy import (
    LOG_DIRECTORY_MODE,
    LOG_FILE_MODE,
    POLICY_BY_FILENAME,
    STREAM_POLICIES,
    LogStreamPolicy,
    maintenance_interval_seconds,
    policy_manifest,
    stream_backup_count,
    stream_family_budget_bytes,
    stream_max_bytes,
    total_log_budget_bytes,
)


SCHEMA_VERSION = 1
_RECENT_UNKNOWN_SECONDS = 24 * 60 * 60
_COPYTRUNCATE_STREAMS = frozenset({
    'audit', 'desktop_client_diag', 'server_console', 'server_manager',
    'supervisor_tofu', 'supervisor_watchdog', 'watchdog',
    'cgroup_pressure', 'storage_postgres', 'raw_sse_anomaly', 'raw_sse',
    'faulthandler_legacy',
})
_PROCESS_LOCK = threading.Lock()


def _mode_label(mode: int) -> str:
    return format(stat.S_IMODE(mode), '04o')


def ensure_private_log_file(path: str | os.PathLike[str], *,
                            create: bool = False) -> bool:
    """Make one regular log owner-readable/writable only, without following links.

    ``create=True`` prepares an append-only subprocess target before the child
    inherits it.  The function never truncates and returns whether it changed
    the mode.  Callers may treat an ``OSError`` as a sink failure; silently
    following a replacement symlink is intentionally not a fallback.
    """
    target = Path(path)
    flags = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    if create:
        flags |= os.O_WRONLY | os.O_APPEND | os.O_CREAT
    else:
        flags |= os.O_RDONLY
    descriptor = os.open(target, flags, LOG_FILE_MODE)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError('log target is not a regular file: %s' % target)
        changed = stat.S_IMODE(info.st_mode) != LOG_FILE_MODE
        if changed:
            try:
                os.fchmod(descriptor, LOG_FILE_MODE)
            except (AttributeError, OSError):
                os.chmod(target, LOG_FILE_MODE, follow_symlinks=False)
        return changed
    finally:
        os.close(descriptor)


def ensure_private_log_directory(path: str | os.PathLike[str]) -> bool:
    """Create one log root if needed and restrict it to its service owner."""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=LOG_DIRECTORY_MODE)
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError('log root is not a direct directory: %s' % target)
    changed = stat.S_IMODE(info.st_mode) != LOG_DIRECTORY_MODE
    if not changed:
        return False
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise OSError('log root changed while securing it: %s' % target)
            try:
                os.fchmod(descriptor, LOG_DIRECTORY_MODE)
            except (AttributeError, OSError):
                os.chmod(target, LOG_DIRECTORY_MODE, follow_symlinks=False)
        finally:
            os.close(descriptor)
    except (AttributeError, TypeError):
        os.chmod(target, LOG_DIRECTORY_MODE)
    return True


def _regular_file(path: Path, directory: Path) -> os.stat_result | None:
    """Return lstat for one direct, non-symlink regular file."""
    try:
        if path.parent.resolve() != directory.resolve():
            return None
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info


def _harden_file_permissions(path: Path, directory: Path, result: dict, *,
                             dry_run: bool) -> None:
    info = _regular_file(path, directory)
    if info is None or stat.S_IMODE(info.st_mode) == LOG_FILE_MODE:
        return
    action = {
        'kind': 'file',
        'path': str(path),
        'before_mode': _mode_label(info.st_mode),
        'after_mode': _mode_label(LOG_FILE_MODE),
        'dry_run': bool(dry_run),
    }
    if dry_run:
        result['permissions_hardened'].append(action)
        return
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return
            try:
                os.fchmod(descriptor, LOG_FILE_MODE)
            except (AttributeError, OSError):
                os.chmod(path, LOG_FILE_MODE, follow_symlinks=False)
        finally:
            os.close(descriptor)
        result['permissions_hardened'].append(action)
    except (AttributeError, OSError) as exc:
        _record_error(result, 'secure_file_mode', path, exc)


def _harden_directory_permissions(path: Path, result: dict, *,
                                  dry_run: bool) -> None:
    try:
        info = path.lstat()
    except OSError:
        return
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) == LOG_DIRECTORY_MODE):
        return
    action = {
        'kind': 'directory',
        'path': str(path),
        'before_mode': _mode_label(info.st_mode),
        'after_mode': _mode_label(LOG_DIRECTORY_MODE),
        'dry_run': bool(dry_run),
    }
    if dry_run:
        result['permissions_hardened'].append(action)
        return
    try:
        if ensure_private_log_directory(path):
            result['permissions_hardened'].append(action)
    except OSError as exc:
        _record_error(result, 'secure_directory_mode', path, exc)


def _harden_managed_permissions(directory: Path, result: dict, *,
                                dry_run: bool) -> None:
    try:
        paths = list(directory.iterdir())
    except OSError:
        return
    for path in paths:
        _priority, _protected, family = _classify_direct_file(path)
        if family != 'unmanaged':
            _harden_file_permissions(path, directory, result, dry_run=dry_run)



def _record_error(result: dict, operation: str, path: Path, exc: BaseException) -> None:
    result['errors'].append({
        'operation': operation,
        'path': str(path),
        'error': '%s: %s' % (type(exc).__name__, str(exc)[:300]),
    })


def _remove(path: Path, result: dict, *, reason: str, dry_run: bool) -> bool:
    try:
        size = path.lstat().st_size
        if path.is_symlink() or not path.is_file():
            return False
        if not dry_run:
            path.unlink()
        result['removed'].append({
            'path': str(path), 'bytes': size, 'reason': reason,
            'dry_run': bool(dry_run),
        })
        return True
    except OSError as exc:
        _record_error(result, 'remove', path, exc)
        return False


def _remove_inventory_row(row: tuple, result: dict, *, reason: str,
                          dry_run: bool) -> bool:
    """Remove a real row or record a removal from a dry-run virtual inventory."""
    path = row[2]
    if not dry_run:
        return _remove(path, result, reason=reason, dry_run=False)
    result['removed'].append({
        'path': str(path), 'bytes': int(row[3]), 'reason': reason,
        'dry_run': True,
    })
    return True


def _shift_numbered_backups(path: Path, backups: int, result: dict,
                              *, dry_run: bool) -> None:
    oldest = path.with_name(path.name + '.%d' % backups)
    already_planned = any(
        item.get('path') == str(oldest) for item in result.get('removed', ()))
    if (oldest.exists() or oldest.is_symlink()) and not already_planned:
        _remove(oldest, result, reason='numbered_backup_limit', dry_run=dry_run)
    if dry_run:
        return
    for index in range(backups - 1, 0, -1):
        source = path.with_name(path.name + '.%d' % index)
        target = path.with_name(path.name + '.%d' % (index + 1))
        try:
            if source.is_symlink():
                continue
            if source.is_file():
                os.replace(source, target)
        except OSError as exc:
                _record_error(result, 'shift_backup', source, exc)


def _bounded_complete_tail_range(
        source_fd: int, file_size: int, ceiling: int) -> tuple[int, int]:
    """Return a complete-line tail range after one ceiling-bounded scan."""
    snapshot_size = max(0, int(file_size))
    keep = min(snapshot_size, max(1, int(ceiling)))
    lower_bound = snapshot_size - keep
    if keep == 0:
        return 0, 0

    starts_at_boundary = lower_bound == 0
    if lower_bound:
        os.lseek(source_fd, lower_bound - 1, os.SEEK_SET)
        starts_at_boundary = os.read(source_fd, 1) == b'\n'

    os.lseek(source_fd, lower_bound, os.SEEK_SET)
    cursor = lower_bound
    remaining = keep
    first_newline = None
    last_newline = None
    last_byte = b''
    while remaining > 0:
        chunk = os.read(source_fd, min(1024 * 1024, remaining))
        if not chunk:
            break
        first = chunk.find(b'\n')
        if first_newline is None and first >= 0:
            first_newline = cursor + first
        last = chunk.rfind(b'\n')
        if last >= 0:
            last_newline = cursor + last
        last_byte = chunk[-1:]
        cursor += len(chunk)
        remaining -= len(chunk)

    # A writer-side truncate during the scan is treated as an incomplete
    # snapshot.  Callers revalidate identity/size before mutating anything.
    observed_end = cursor
    if last_byte == b'\n' and observed_end == snapshot_size:
        end = snapshot_size
    elif last_newline is not None:
        end = last_newline + 1
    else:
        return snapshot_size, snapshot_size

    if starts_at_boundary:
        start = lower_bound
    elif first_newline is not None:
        start = first_newline + 1
    else:
        start = end
    if start >= end:
        return end, end
    return start, end


def _planned_retained_tail_bytes(path: Path, info: os.stat_result,
                                 ceiling: int) -> int:
    """Preview the exact bounded tail size without copying the whole file."""
    source_fd = -1
    try:
        source_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return min(info.st_size, max(1, int(ceiling)))
        start, end = _bounded_complete_tail_range(
            source_fd, opened.st_size, ceiling)
        return max(0, end - start)
    except OSError:
        return min(info.st_size, max(1, int(ceiling)))
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def copytruncate_if_oversize(path: str | os.PathLike[str], *, max_bytes: int,
                             backup_count: int, result: dict | None = None,
                             dry_run: bool = False,
                             trigger_bytes: int | None = None) -> dict:
    """Rotate an append-only file while preserving its writer's descriptor.

    At most the newest ``max_bytes`` are copied to ``.1``.  This intentionally
    avoids copying a multi-gigabyte legacy file merely to delete it on the next
    budget pass.  The unavoidable copy-truncate race is restricted to external
    stdout/PG writers; application logging handlers perform atomic rollover.
    """
    output = result if result is not None else {
        'rotated': [], 'removed': [], 'errors': [],
    }
    target = Path(path)
    directory = target.parent
    info = _regular_file(target, directory)
    ceiling = max(1, int(max_bytes))
    trigger = ceiling if trigger_bytes is None else max(1, int(trigger_bytes))
    backups = max(1, int(backup_count))
    if info is None or info.st_size <= trigger:
        return output

    action = {
        'path': str(target), 'before_bytes': info.st_size,
        'retained_bytes': min(info.st_size, ceiling),
        'reason': 'active_file_ceiling', 'dry_run': bool(dry_run),
    }
    if dry_run:
        action['retained_bytes'] = _planned_retained_tail_bytes(
            target, info, ceiling)
        _shift_numbered_backups(target, backups, output, dry_run=True)
        output['rotated'].append(action)
        return output

    temporary_name = ''
    try:
        flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        source_fd = os.open(target, flags)
        try:
            try:
                import fcntl
                fcntl.flock(source_fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            opened = os.fstat(source_fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return output
            if opened.st_size <= trigger:
                return output
            action['before_bytes'] = opened.st_size
            tail_start, tail_end = _bounded_complete_tail_range(
                source_fd, opened.st_size, ceiling)
            action['retained_bytes'] = max(0, tail_end - tail_start)
            _shift_numbered_backups(target, backups, output, dry_run=False)
            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix='.%s.rotate-' % target.name, dir=str(directory))
            try:
                try:
                    os.fchmod(temporary_fd, LOG_FILE_MODE)
                except (AttributeError, OSError):
                    os.chmod(temporary_name, LOG_FILE_MODE,
                             follow_symlinks=False)
                os.lseek(source_fd, tail_start, os.SEEK_SET)
                remaining = max(0, tail_end - tail_start)
                while remaining > 0:
                    chunk = os.read(source_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(temporary_fd, view)
                        if written <= 0:
                            raise OSError('zero-length write during log rotation')
                        view = view[written:]
                    remaining -= len(chunk)
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            backup = target.with_name(target.name + '.1')
            os.replace(temporary_name, backup)
            temporary_name = ''
            # Re-check descriptor identity immediately before truncation.  A
            # concurrent owner rollover means our fd is stale; preserve it.
            current = _regular_file(target, directory)
            if current is not None and (
                    current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                os.ftruncate(source_fd, 0)
                os.fsync(source_fd)
            else:
                _remove(backup, output, reason='owner_rotated_concurrently',
                        dry_run=False)
                return output
        finally:
            os.close(source_fd)
        action['retained_bytes'] = backup.stat().st_size if backup.exists() else 0
        output['rotated'].append(action)
    except OSError as exc:
        _record_error(output, 'copytruncate', target, exc)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return output


def _compact_closed_file_if_oversize(
        path: Path, *, max_bytes: int, result: dict, dry_run: bool) -> None:
    """Atomically replace an inactive rotation with its complete-line tail."""
    directory = path.parent
    info = _regular_file(path, directory)
    ceiling = max(1, int(max_bytes))
    if info is None or info.st_size <= ceiling:
        return
    action = {
        'path': str(path),
        'before_bytes': info.st_size,
        'retained_bytes': _planned_retained_tail_bytes(path, info, ceiling),
        'reason': 'rotated_file_ceiling',
        'dry_run': bool(dry_run),
    }
    if dry_run:
        result['compacted'].append(action)
        return

    source_fd = -1
    temporary_fd = -1
    temporary_name = ''
    try:
        source_fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return
        tail_start, tail_end = _bounded_complete_tail_range(
            source_fd, opened.st_size, ceiling)
        action['before_bytes'] = opened.st_size
        action['retained_bytes'] = max(0, tail_end - tail_start)
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix='.%s.compact-' % path.name, dir=str(directory))
        try:
            try:
                os.fchmod(temporary_fd, LOG_FILE_MODE)
            except (AttributeError, OSError):
                os.chmod(temporary_name, LOG_FILE_MODE,
                         follow_symlinks=False)
            os.lseek(source_fd, tail_start, os.SEEK_SET)
            remaining = action['retained_bytes']
            while remaining > 0:
                chunk = os.read(source_fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(temporary_fd, view)
                    if written <= 0:
                        raise OSError(
                            'zero-length write during rotated log compaction')
                    view = view[written:]
                remaining -= len(chunk)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
            temporary_fd = -1
        os.utime(
            temporary_name,
            ns=(opened.st_atime_ns, opened.st_mtime_ns),
            follow_symlinks=False)
        current = _regular_file(path, directory)
        if current is None:
            return
        if ((current.st_dev, current.st_ino) !=
                (opened.st_dev, opened.st_ino)
                or current.st_size != opened.st_size
                or current.st_mtime_ns != opened.st_mtime_ns):
            return
        os.replace(temporary_name, path)
        temporary_name = ''
        action['retained_bytes'] = path.stat().st_size
        result['compacted'].append(action)
    except OSError as exc:
        _record_error(result, 'compact_rotated_file', path, exc)
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def append_bytes_locked(path: str | os.PathLike[str], payload: bytes,
                        *, mode: int = LOG_FILE_MODE) -> None:
    """Append one complete payload while coordinating with copy-truncate.

    Application-owned JSONL/raw diagnostic writers use this helper so the
    exclusive ``flock`` taken by :func:`copytruncate_if_oversize` cannot race
    their append. External stdout and PostgreSQL writers cannot participate;
    their residual copy-truncate race is documented and bounded separately.
    """
    if not isinstance(payload, bytes):
        raise TypeError('payload must be bytes')
    target = Path(path)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(target, flags, int(mode))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError('diagnostic target is not a regular file: %s' % target)
        try:
            os.fchmod(descriptor, int(mode))
        except (AttributeError, OSError):
            os.chmod(target, int(mode), follow_symlinks=False)
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError('zero-length append to diagnostic log')
            view = view[written:]
    finally:
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        os.close(descriptor)


def _family_files(directory: Path, policy: LogStreamPolicy) -> tuple[Path, list[Path]]:
    active = directory / policy.filename
    rotated: list[Path] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return active, rotated
    prefix = policy.filename + '.'
    for path in entries:
        if path.name.startswith(prefix) and _regular_file(path, directory) is not None:
            rotated.append(path)
    return active, rotated


def _prune_family(directory: Path, policy: LogStreamPolicy, result: dict,
                  *, now: float, dry_run: bool) -> None:
    if '*' in policy.filename:
        return
    active, rotated = _family_files(directory, policy)
    rows = []
    for path in rotated:
        info = _regular_file(path, directory)
        if info is not None:
            rows.append((info.st_mtime, path.name, path, info.st_size))

    cutoff = now - max(1, policy.retention_days) * 86_400
    kept = []
    for row in sorted(rows):
        if row[0] < cutoff:
            _remove(row[2], result, reason='retention_days', dry_run=dry_run)
        else:
            kept.append(row)

    # Simulate earlier dry-run removals by selecting from the retained set
    # rather than probing the unchanged filesystem again.
    backup_limit = stream_backup_count(policy.name)
    overflow = max(0, len(kept) - backup_limit)
    for row in kept[:overflow]:
        _remove(row[2], result, reason='backup_count', dry_run=dry_run)
    kept = kept[overflow:]

    # Age/count removals happen before compaction so maintenance never rewrites
    # a closed rotation that is about to be deleted.  Apply and dry-run still
    # share the same survivor set and exact complete-line tail calculation.
    compaction_start = len(result['compacted'])
    for row in kept:
        _compact_closed_file_if_oversize(
            row[2], max_bytes=stream_max_bytes(policy.name), result=result,
            dry_run=dry_run)
    compacted_sizes = {
        item['path']: int(item.get('retained_bytes') or 0)
        for item in result['compacted'][compaction_start:]
    }
    kept = [(
        row[0], row[1], row[2], compacted_sizes.get(str(row[2]), row[3]))
        for row in kept]

    active_size = 0
    info = _regular_file(active, directory)
    if info is not None:
        active_size = info.st_size
    budget = stream_family_budget_bytes(policy.name)
    total = active_size + sum(row[3] for row in kept)
    for row in kept:
        if total <= budget:
            break
        if _remove(row[2], result, reason='family_budget', dry_run=dry_run):
            total -= row[3]



_FAULT_PROCESS_RE = re.compile(r'^tofu_faulthandler_(\d+)\.log$')


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _fault_process_pid(path: Path) -> int | None:
    match = _FAULT_PROCESS_RE.fullmatch(path.name)
    return int(match.group(1)) if match else None


def _prune_process_fault_dumps(directory: Path, result: dict, *, now: float,
                               dry_run: bool) -> None:
    """Bound dead per-PID crash evidence while preserving every live sink."""
    policy = STREAM_POLICIES['faulthandler_process']
    try:
        paths = list(directory.iterdir())
    except OSError:
        return
    live_rows = []
    dead_rows = []
    for path in paths:
        pid = _fault_process_pid(path)
        info = _regular_file(path, directory)
        if pid is None or info is None:
            continue
        row = (info.st_mtime, path.name, path, info.st_size)
        (live_rows if _process_is_alive(pid) else dead_rows).append(row)

    cutoff = now - policy.retention_days * 86_400
    retained_dead = []
    for row in sorted(dead_rows):
        if row[0] < cutoff:
            _remove_inventory_row(
                row, result, reason='fault_retention_days', dry_run=dry_run)
        else:
            retained_dead.append(row)

    overflow = max(
        0, len(retained_dead) - stream_backup_count(policy.name))
    for row in retained_dead[:overflow]:
        _remove_inventory_row(
            row, result, reason='fault_file_count', dry_run=dry_run)
    retained_dead = retained_dead[overflow:]

    compaction_start = len(result['compacted'])
    for row in retained_dead:
        _compact_closed_file_if_oversize(
            row[2], max_bytes=stream_max_bytes(policy.name), result=result,
            dry_run=dry_run)
    compacted_sizes = {
        item['path']: int(item.get('retained_bytes') or 0)
        for item in result['compacted'][compaction_start:]
    }
    retained_dead = [(
        row[0], row[1], row[2], compacted_sizes.get(str(row[2]), row[3]))
        for row in retained_dead]

    budget = stream_family_budget_bytes(policy.name)
    total = sum(row[3] for row in live_rows + retained_dead)
    survivors = []
    for row in retained_dead:
        if total > budget and _remove_inventory_row(
                row, result, reason='fault_family_budget', dry_run=dry_run):
            total -= row[3]
            continue
        survivors.append(row)
    result['faulthandler_process'] = {
        'live_files': len(live_rows),
        'retained_dead_files': len(survivors),
        'after_bytes_estimate': total,
        'budget_bytes': budget,
        'over_budget_bytes': max(0, total - budget),
    }



def _classify_direct_file(path: Path) -> tuple[int, bool, str]:
    """Return ``(priority, protected_active, family_name)``."""
    policy = POLICY_BY_FILENAME.get(path.name)
    if policy is not None:
        return policy.priority, True, policy.name
    for filename, candidate in POLICY_BY_FILENAME.items():
        if '*' not in filename and path.name.startswith(filename + '.'):
            return candidate.priority, False, candidate.name
    if fnmatch.fnmatch(path.name, 'tofu_faulthandler_*.log'):
        pid = _fault_process_pid(path)
        policy = STREAM_POLICIES['faulthandler_process']
        return policy.priority, bool(pid and _process_is_alive(pid)), policy.name
    return 0, False, 'unmanaged'


def _directory_inventory(log_dir: Path, *, now: float) -> list[dict]:
    rows = []
    try:
        paths = list(log_dir.iterdir())
    except OSError:
        return rows
    for path in paths:
        info = _regular_file(path, log_dir)
        if info is None:
            continue
        priority, protected, family = _classify_direct_file(path)
        recent_unmanaged = family == 'unmanaged' and (
            now - info.st_mtime < _RECENT_UNKNOWN_SECONDS)
        rows.append({
            'path': str(path), 'name': path.name, 'bytes': info.st_size,
            'mtime': info.st_mtime, 'priority': priority,
            'family': family, 'protected': bool(protected or recent_unmanaged),
        })
    return rows


def _enforce_global_budget(log_dir: Path, result: dict, *, now: float,
                           dry_run: bool) -> None:
    rows = _directory_inventory(log_dir, now=now)
    budget = total_log_budget_bytes()
    # In apply mode the inventory is already the post-mutation filesystem.
    # A numbered path removed earlier may have been recreated by shifting .1 to
    # .2, so excluding historical action paths would undercount real bytes.
    planned_removed = ({item['path'] for item in result['removed']}
                       if dry_run else set())
    virtual_sizes = {row['path']: row['bytes'] for row in rows}
    if dry_run:
        for action in result.get('compacted', ()):
            if action['path'] in virtual_sizes:
                virtual_sizes[action['path']] = int(
                    action.get('retained_bytes') or 0)
        for action in result['rotated']:
            if action['path'] in virtual_sizes:
                virtual_sizes[action['path']] = int(
                    action.get('retained_bytes') or 0)
    total = sum(virtual_sizes[row['path']] for row in rows
                if row['path'] not in planned_removed)
    # Remove low-value families before high-value incident/error/audit evidence;
    # within a priority, oldest evidence goes first.
    candidates = sorted(
        (row for row in rows
         if not row['protected'] and row['path'] not in planned_removed),
        key=lambda row: (row['priority'], row['mtime'], row['name']),
    )
    for row in candidates:
        if total <= budget:
            break
        path = Path(row['path'])
        if _remove(path, result, reason='global_budget', dry_run=dry_run):
            total -= virtual_sizes[row['path']]
    result['budget_bytes'] = budget
    result['after_bytes_estimate'] = total
    result['over_budget_bytes'] = max(0, total - budget)
    result['unmanaged'] = [
        {'path': row['path'], 'bytes': row['bytes'],
         'recent_protected': row['protected']}
        for row in rows if row['family'] == 'unmanaged'
    ]


@contextlib.contextmanager
def _maintenance_lock(lock_path: Path | None):
    """Best-effort cross-process singleton, plus an in-process lock."""
    with _PROCESS_LOCK:
        if lock_path is None:
            yield True
            return
        file_handle = None
        acquired = False
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            file_handle = lock_path.open('a+b')
            try:
                import fcntl
                fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (ImportError, OSError):
                acquired = False
            yield acquired
        finally:
            if file_handle is not None:
                if acquired:
                    try:
                        import fcntl
                        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
                    except (ImportError, OSError):
                        pass
                file_handle.close()


def _write_report(data_dir: Path, report: dict) -> None:
    path = data_dir / 'log-maintenance-last.json'
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.log-maintenance-', dir=str(data_dir))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, separators=(',', ':'))
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def maintain_logs(log_dir: str | os.PathLike[str], *,
                  data_dir: str | os.PathLike[str] | None = None,
                  dry_run: bool = False, now: float | None = None) -> dict:
    """Apply all retention policies and return a machine-readable audit."""
    started = time.time() if now is None else float(now)
    root = Path(log_dir).resolve()
    data_root = Path(data_dir).resolve() if data_dir is not None else None
    result = {
        'schema_version': SCHEMA_VERSION,
        'timestamp': started,
        'log_dir': str(root),
        'dry_run': bool(dry_run),
        'skipped': False,
        'before_bytes': 0,
        'after_bytes_estimate': 0,
        'budget_bytes': total_log_budget_bytes(),
        'over_budget_bytes': 0,
        'rotated': [],
        'compacted': [],
        'removed': [],
        'permissions_hardened': [],
        'unmanaged': [],
        'errors': [],
        'policy': policy_manifest(),
    }
    if not dry_run:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _record_error(result, 'create_log_directory', root, exc)
            return result
    result['before_bytes'] = sum(
        row['bytes'] for row in _directory_inventory(root, now=started))
    # A dry run is genuinely read-only: it neither creates logs/ nor a lock
    # file in data/. Its inventory is an advisory point-in-time snapshot.
    lock_path = None if dry_run else (
        data_root / '.log-maintenance.lock' if data_root else
        root / '.log-maintenance.lock')
    with _maintenance_lock(lock_path) as acquired:
        if not acquired:
            result['skipped'] = True
            result['skip_reason'] = 'maintenance_already_running'
            return result

        _harden_directory_permissions(root, result, dry_run=dry_run)
        # Secure the point-in-time inventory before renames/deletions. This
        # keeps dry-run/apply permission plans identical; new rotation files
        # are independently created with LOG_FILE_MODE below.
        _harden_managed_permissions(root, result, dry_run=dry_run)

        # Prune the pre-existing backup families first, then rotate append-only
        # active files. This order makes dry-run and apply operate on the same
        # named targets: a dry run need not pretend that .1/.2 were renamed on
        # disk, while the subsequent rotation still leaves exactly the declared
        # number and byte budget of backups.
        for policy in STREAM_POLICIES.values():
            _prune_family(root, policy, result, now=started, dry_run=dry_run)
        _prune_process_fault_dumps(
            root, result, now=started, dry_run=dry_run)
        for name in sorted(_COPYTRUNCATE_STREAMS):
            policy = STREAM_POLICIES[name]
            copytruncate_if_oversize(
                root / policy.filename,
                max_bytes=stream_max_bytes(name),
                backup_count=stream_backup_count(name),
                result=result,
                dry_run=dry_run,
            )
        _enforce_global_budget(root, result, now=started, dry_run=dry_run)

    result['reclaimed_bytes_estimate'] = max(
        0, result['before_bytes'] - result['after_bytes_estimate'])
    if data_root is not None and not dry_run:
        try:
            _write_report(data_root, result)
        except OSError as exc:
            _record_error(result, 'write_report', data_root, exc)
    return result


class LogMaintenanceRuntime:
    """One upgradeable daemon for core and registered external log families.

    Standalone launchers may register an external append-only stream before a
    server runtime exists.  The same object can later be configured with the
    core ``logs/`` paths, avoiding two permanent 15-minute janitors in the
    serving process without weakening either retention policy.
    """

    def __init__(self, log_dir: str | None, data_dir: str | None,
                 *, interval_seconds: float | None = None):
        self.log_dir = None if log_dir is None else str(log_dir)
        self.data_dir = None if data_dir is None else str(data_dir)
        self.interval_seconds = (
            maintenance_interval_seconds() if interval_seconds is None
            else max(1.0, float(interval_seconds)))
        self._configuration_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: dict | None = None

    def configure_core(self, log_dir: str, data_dir: str) -> None:
        """Attach core paths and request their immediate first maintenance."""
        with self._configuration_lock:
            self.log_dir = str(log_dir)
            self.data_dir = str(data_dir)
        self._wake_event.set()

    def _maintain_once(self) -> None:
        # External descriptors can grow independently of the application log
        # handlers, so service them first. Each path is already independently
        # fail-open inside the helper.
        _maintain_registered_external_logs_once()
        with self._configuration_lock:
            log_dir = self.log_dir
            data_dir = self.data_dir
        if log_dir is None or data_dir is None:
            return
        try:
            self.last_result = maintain_logs(log_dir, data_dir=data_dir)
        except Exception as exc:  # maintenance must never kill the server
            logging.getLogger(__name__).warning(
                'runtime log maintenance failed: %s', exc)
            self.last_result = {
                'schema_version': SCHEMA_VERSION,
                'timestamp': time.time(),
                'errors': [{'operation': 'runtime',
                            'error': '%s: %s' % (type(exc).__name__, exc)}],
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            self._maintain_once()
            if self._stop_event.is_set():
                break
            self._wake_event.wait(self.interval_seconds)

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._wake_event.clear()
            thread = threading.Thread(
                target=self._run, name='tofu-log-maintenance', daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                if self._thread is thread:
                    self._thread = None
                raise
            return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        self._wake_event.set()
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        stopped = not thread.is_alive()
        if stopped:
            with self._lifecycle_lock:
                if self._thread is thread:
                    self._thread = None
        return stopped


_RUNTIME: LogMaintenanceRuntime | None = None
_RUNTIME_LOCK = threading.Lock()

# Some launchers redirect a child process into a file outside logs/. Register
# those exact files here; the shared runtime copy-truncates them without ever
# scanning their surrounding data/config directory.
_EXTERNAL_LOGS: dict[str, str] = {}
_EXTERNAL_LOCK = threading.Lock()


def _maintain_registered_external_logs_once() -> None:
    with _EXTERNAL_LOCK:
        registered = list(_EXTERNAL_LOGS.items())
    for path, stream_name in registered:
        try:
            ensure_private_log_file(path)
            copytruncate_if_oversize(
                path,
                max_bytes=stream_max_bytes(stream_name),
                backup_count=stream_backup_count(stream_name),
            )
        except Exception as exc:
            logging.getLogger(__name__).debug(
                'external log retention failed for %s: %s', path, exc)
            continue


def register_external_log(path: str | os.PathLike[str], stream_name: str) -> None:
    """Continuously bound one exact append-only file outside ``logs/``."""
    global _RUNTIME
    if stream_name not in STREAM_POLICIES:
        raise KeyError('unknown log stream: %s' % stream_name)
    normalized = str(Path(path).absolute())
    # Prepare a private inode, then apply the ceiling before a new child
    # inherits the descriptor. The subsequent plain ``open(..., 'a')`` keeps
    # this mode even under a permissive process umask.
    ensure_private_log_file(normalized, create=True)
    copytruncate_if_oversize(
        normalized, max_bytes=stream_max_bytes(stream_name),
        backup_count=stream_backup_count(stream_name))
    with _EXTERNAL_LOCK:
        _EXTERNAL_LOGS[normalized] = stream_name
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = LogMaintenanceRuntime(None, None)
        _RUNTIME.start()


def start_log_maintenance(log_dir: str, data_dir: str) -> bool:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = LogMaintenanceRuntime(log_dir, data_dir)
        else:
            _RUNTIME.configure_core(log_dir, data_dir)
        return _RUNTIME.start()


def stop_log_maintenance(timeout: float = 5.0) -> bool:
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
    return True if runtime is None else runtime.stop(timeout=timeout)


__all__ = [
    'LogMaintenanceRuntime', 'SCHEMA_VERSION', 'append_bytes_locked',
    'copytruncate_if_oversize', 'ensure_private_log_directory',
    'ensure_private_log_file', 'maintain_logs', 'register_external_log',
    'start_log_maintenance', 'stop_log_maintenance',
]
