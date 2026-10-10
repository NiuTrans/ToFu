"""Cooperative checkout exclusion, independent of authorization/presence.

Entry points:
- workspace_write_lease(path, *, timeout=0.0, cancel_event=None) excludes all
  cooperative holders during whole-checkout mutations/capture/publication.
- workspace_file_write_lease(path, *, timeout=0.0, cancel_event=None) excludes
  one canonical file using a shared checkout barrier and exclusive target lock.
- workspace_read_lease(path, *, timeout=0.0, cancel_event=None) explicitly
  protects a caller's stable checkout read. Opaque commands do not imply one.

Git worktrees share a common-dir but have distinct locks keyed by the real
checkout path. Locks are advisory: external editors and programs deliberately
closing inherited FDs are non-cooperating writers. Non-Git paths are no-ops.
There is no owner database, expiry-based stealing, or stale-lock deletion.

In-process gates distinguish writer ownership and reader counts. A writer may
reenter read without downgrading; the sole reader may upgrade and retain its
outer read claim. One exclusive POSIX flock covers both modes in this process:
flock is open-file-description scoped, so a competing descriptor would block
an in-process upgrade. Explicit readers share that descriptor locally; across
processes its holder type is unknown, not evidence of an active writer.
File barriers alone use SH: distinct files can overlap across processes while
whole-checkout leases still exclude them. Entries/FDs exist only while active.
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time


class WorkspaceBusyError(RuntimeError):
    """Lease unavailable (busy, unsupported, or unsafe lock metadata)."""


class WorkspaceLeaseCancelled(WorkspaceBusyError):
    """Admission cancelled before the protected operation could begin."""


_WRITE = 'write'
_FILE_BARRIER = 'file-write'
_READ = 'read'
_UNKNOWN = 'unknown'


def _metadata(path: Path) -> str:
    with path.open('r', encoding='utf-8') as stream:
        value = stream.read(8193)
    if len(value) > 8192 or not value.strip():
        raise WorkspaceBusyError(f'Invalid Git workspace metadata: {path}')
    return value.strip()


def _checkout_identity(path):
    """Resolve files/nonexistent descendants without Git env/config overrides."""
    current = Path(os.path.realpath(os.path.expanduser(os.fspath(path))))
    if not current.is_dir():
        current = current.parent
    for root in (current, *current.parents):
        marker = root / '.git'
        if marker.is_dir():
            git_dir = marker.resolve()
        elif marker.is_file():
            text = _metadata(marker)
            if not text.startswith('gitdir: '):
                raise WorkspaceBusyError(f'Invalid Git directory pointer: {marker}')
            git_dir = (root / text[8:]).resolve()
        elif marker.is_symlink():
            raise WorkspaceBusyError(f'Broken Git directory pointer: {marker}')
        else:
            continue
        common_file = git_dir / 'commondir'
        common = ((git_dir / _metadata(common_file)).resolve()
                  if common_file.is_file() else git_dir)
        if not common.is_dir():
            raise WorkspaceBusyError(f'Git common directory is unavailable: {common}')
        return str(root), common
    return None


def is_source_workspace_path(path) -> bool:
    """Runtime/user data beneath a Git root is not a source checkout writer."""
    from lib.git_checkpoint_policy import FORBIDDEN_CHECKPOINT_ROOTS
    identity = _checkout_identity(path)
    if identity is None:
        return True  # ordinary non-Git lease admission remains a no-op
    relative = Path(os.path.realpath(os.path.expanduser(os.fspath(path)))).relative_to(identity[0])
    return not relative.parts or relative.parts[0] not in FORBIDDEN_CHECKPOINT_ROOTS


_registry_lock = threading.Lock()
_registry = {}  # active holders/waiters only; removed at last departure
_active_descriptors = set()  # includes pending cross-process admission


def _after_fork():
    # Close, never LOCK_UN: the parent's open-file-description owns the flock.
    global _registry_lock, _registry, _active_descriptors
    for fd in _active_descriptors:
        try:
            os.close(fd)
        except OSError:
            pass
    _registry_lock = threading.Lock()
    _registry = {}
    _active_descriptors = set()


def _before_fork():
    _registry_lock.acquire()


def _after_fork_parent():
    _registry_lock.release()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_before_fork,
                        after_in_parent=_after_fork_parent,
                        after_in_child=_after_fork)


def workspace_lease_fds() -> tuple[int, ...]:
    """FDs the current holder's supervised subprocess must inherit.

    Derive these from current ownership, not the thread that opened the FD:
    another explicit reader can inherit the shared descriptor, and the last
    reader's release cannot leave a closed FD in the first reader's local state.
    Parent release is close-only; children retain exclusion until last close.
    """
    thread_id = threading.get_ident()
    with _registry_lock:
        return tuple(fd for entry in _registry.values()
                     if (entry['writer_thread'] == thread_id
                         or thread_id in entry['readers']
                         or thread_id in entry['file_writers'])
                     for fd in entry['fds'].values())


def _open_lock(common: Path, root: str):
    import fcntl  # Unsupported platforms fail closed for Git writes.
    lock_dir = common / 'tofu-workspace-leases'
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    if lock_dir.is_symlink() or not lock_dir.is_dir():
        raise WorkspaceBusyError(f'Unsafe workspace lock directory: {lock_dir}')
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(lock_dir / (digest + '.lock'), flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceBusyError(f'Unsafe workspace lock file for {root}')
    except BaseException:
        # Opening succeeded but descriptor validation did not transfer ownership.
        os.close(fd)
        raise
    return fd, fcntl


def _holder_identity(cancel_event):
    """Bounded diagnostic values only; never retain task/runtime/command bodies."""
    from .tool_admission import cancellation
    scoped = cancellation()
    task = getattr(cancel_event, 'task', None) or getattr(scoped, 'task', None) or {}
    tool = getattr(cancel_event, 'tool_name', '') or getattr(scoped, 'tool_name', '')

    def identifier(value):
        value = str(value or '')
        return value if re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value) else ''

    return {'pid': os.getpid(), 'thread': threading.get_ident(),
            'tool': identifier(tool), 'task': identifier(task.get('id')),
            'conversation': identifier(task.get('convId')),
            'since': time.monotonic()}


def _busy(root, active, entry=None):
    if active == _UNKNOWN:
        return WorkspaceBusyError(
            f'Workspace lease contention is active (holder type unknown): {root}')
    message = f'Workspace {active} is active: {root}'
    if entry is not None:
        # Called under the same condition as admission/release. Report actual
        # blockers, not the waiting caller or an invented cross-process owner.
        if entry['writer_thread'] is not None:
            owners = [(_WRITE, entry['writer_thread'])]
        elif active == _READ:
            owners = [(_READ, thread) for thread in entry['readers']]
        else:
            owners = [(_FILE_BARRIER, thread) for thread in entry['file_writers']]
        details = []
        for key in owners[:3]:
            holder = entry['holders'].get(key)
            if holder is None:
                continue
            fields = [f'pid={holder["pid"]}', f'thread={holder["thread"]}',
                      f'held_seconds={max(0.0, time.monotonic() - holder["since"]):.3f}']
            fields.extend(f'{name}={holder[name]}' for name in ('tool', 'task', 'conversation')
                          if holder[name])
            details.append(', '.join(fields))
        if details:
            message += ' [holder: ' + '; '.join(details) + ']'
    return WorkspaceBusyError(message)


def _check_cancelled(root, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise WorkspaceLeaseCancelled(f'Workspace lease admission cancelled: {root}')


def _try_acquire(entry, thread_id, mode):
    """Attempt admission under entry['cond']; busy markers name ACTUAL blockers."""
    if entry['acquiring_thread'] is not None:
        return _UNKNOWN

    if mode == _FILE_BARRIER:
        if entry['writer_thread'] == thread_id:
            entry['writer_depth'] += 1
            return 'enter'
        if entry['writer_thread'] is not None:
            return _WRITE
        if entry['readers']:
            return _READ
        return 'acquire'
    if mode == _WRITE:
        if entry['writer_thread'] == thread_id:
            entry['writer_depth'] += 1
            return 'enter'
        if entry['writer_thread'] is not None:
            return _WRITE
        if entry['file_writers']:
            return _WRITE
        if not entry['readers'] or (
                len(entry['readers']) == 1 and thread_id in entry['readers']):
            return 'acquire'
        return _READ
    if entry['writer_thread'] == thread_id:
        entry['reader_reentry'] += 1
        return 'enter'
    if entry['writer_thread'] is not None or entry['file_writers']:
        return _WRITE
    return 'acquire'


def _publish_hold(entry, thread_id, mode, holder):
    entry['holders'].setdefault((mode, thread_id), holder)
    if mode == _WRITE:
        entry['writer_thread'] = thread_id
        entry['writer_depth'] = 1
        # Preserve an upgrading reader's outer claim until its own exit.
    elif mode == _FILE_BARRIER:
        owners = entry['file_writers']
        owners[thread_id] = owners.get(thread_id, 0) + 1
    else:
        entry['readers'][thread_id] = entry['readers'].get(thread_id, 0) + 1


def _close_descriptor(fd):
    with _registry_lock:
        _active_descriptors.discard(fd)
        # No LOCK_UN: an inherited child FD must continue to exclude.
        os.close(fd)


def _acquire_process_lock(common, root, deadline, cancel_event, mode):
    fd = None
    try:
        with _registry_lock:
            fd, fcntl = _open_lock(common, root)
            _active_descriptors.add(fd)
        while True:
            _check_cancelled(root, cancel_event)
            try:
                physical_mode = fcntl.LOCK_SH if mode == _FILE_BARRIER else fcntl.LOCK_EX
                fcntl.flock(fd, physical_mode | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _busy(root, _UNKNOWN)
                time.sleep(min(0.05, remaining))
    except BaseException as error:
        if fd is not None:
            _close_descriptor(fd)
        if isinstance(error, (OSError, ImportError)):
            raise WorkspaceBusyError(
                f'Workspace lease unavailable for {root}: {error}') from error
        raise


def _enter(path, mode, timeout, cancel_event, _identity=None):
    """Acquire for this thread without holding its gate during process waits."""
    _check_cancelled(path, cancel_event)
    try:
        identity = _identity if _identity is not None else _checkout_identity(path)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot resolve workspace lease: {error}') from error
    if identity is None:
        return None, None, None, None
    root, common = identity
    key = (root, str(common))
    # Diagnostic collection is fallible and must precede acquiring any FD or
    # publishing registry ownership. It cannot strand a transferred flock.
    holder = _holder_identity(cancel_event)
    deadline = time.monotonic() + timeout
    thread_id = threading.get_ident()
    with _registry_lock:
        entry = _registry.setdefault(key, {
            'cond': threading.Condition(), 'users': 0,
            'writer_thread': None, 'writer_depth': 0,
            'readers': {}, 'reader_reentry': 0, 'file_writers': {},
            'fds': {}, 'acquiring_thread': None, 'holders': {},
        })
        entry['users'] += 1
    pending = False
    fd = None
    try:
        with entry['cond']:
            while True:
                _check_cancelled(root, cancel_event)
                outcome = _try_acquire(entry, thread_id, mode)
                if outcome in ('acquire', 'enter'):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _busy(root, outcome, entry)
                entry['cond'].wait(timeout=min(0.05, remaining))
            if outcome == 'enter':
                return entry, root, key, outcome
            if entry['fds']:
                _publish_hold(entry, thread_id, mode, holder)
                return entry, root, key, outcome
            entry['acquiring_thread'] = thread_id
            pending = True
        # Reserve admission, but let other local waiters observe/cancel/expire.
        fd = _acquire_process_lock(common, root, deadline, cancel_event, mode)
        with entry['cond']:
            _check_cancelled(root, cancel_event)
            _publish_hold(entry, thread_id, mode, holder)
            with _registry_lock:
                entry['fds'][_FILE_BARRIER if mode == _FILE_BARRIER else _WRITE] = fd
            fd = None  # transfer only after logical publication succeeded
            entry['acquiring_thread'] = None
            pending = False
            entry['cond'].notify_all()
        return entry, root, key, outcome
    except BaseException:
        if fd is not None:
            _close_descriptor(fd)
        if pending:
            with entry['cond']:
                # A first physical admission had no prior owners. Roll back
                # even a partially published logical claim before waking peers.
                if entry['writer_thread'] == thread_id:
                    entry['writer_thread'] = None
                    entry['writer_depth'] = 0
                entry['readers'].pop(thread_id, None)
                entry['file_writers'].pop(thread_id, None)
                entry['holders'].pop((mode, thread_id), None)
                entry['acquiring_thread'] = None
                entry['cond'].notify_all()
        with _registry_lock:
            entry['users'] -= 1
            if entry['users'] == 0:
                _registry.pop(key, None)
        raise


def _validated_timeout(timeout):
    timeout = float(timeout)
    if not math.isfinite(timeout) or not 0 <= timeout <= 600:
        raise ValueError('workspace lease timeout must be finite and in [0, 600]')
    return timeout


@contextlib.contextmanager
def _lease(path, mode, timeout, cancel_event, _identity=None):
    timeout = _validated_timeout(timeout)
    owner_pid = os.getpid()
    entry, root, key, action = _enter(path, mode, timeout, cancel_event, _identity)
    if entry is None:  # non-Git path: no-op
        yield
        return
    thread_id = threading.get_ident()
    try:
        yield
    finally:
        # At-fork cleanup already closed inherited FDs. Their numbers may now
        # name child-owned files, and inherited Conditions may be locked.
        if os.getpid() == owner_pid:
            with entry['cond']:
                _release(entry, thread_id, mode, action)
                entry['cond'].notify_all()
            with _registry_lock:
                entry['users'] -= 1
                last_departure = entry['users'] == 0
                if last_departure:
                    _registry.pop(key, None)
            if last_departure and mode in {_WRITE, _FILE_BARRIER}:
                # Optional wake only AFTER exclusion/registry admission ends.
                # Do not load integration on ordinary source writes or create a
                # worker in API-only replicas. This is a hint, not durability.
                control = sys.modules.get('lib.integration_control')
                if control is not None:
                    try:
                        control.notify_workspace_write_released()
                    except Exception:
                        from lib.log import get_logger
                        get_logger(__name__).warning('Integration release wake failed', exc_info=True)


def _release(entry, thread_id, mode, action):
    if action == 'enter':
        if mode in (_WRITE, _FILE_BARRIER):
            entry['writer_depth'] -= 1
        else:
            entry['reader_reentry'] -= 1
        return
    if mode == _WRITE:
        entry['writer_depth'] -= 1
        if entry['writer_depth'] == 0:
            entry['writer_thread'] = None
            entry['holders'].pop((_WRITE, thread_id), None)
    else:
        owners = entry['file_writers'] if mode == _FILE_BARRIER else entry['readers']
        depth = owners.get(thread_id, 0)
        if depth <= 1:
            owners.pop(thread_id, None)
            entry['holders'].pop((mode, thread_id), None)
        else:
            owners[thread_id] = depth - 1
    if (entry['writer_thread'] is None and not entry['readers']
            and not entry['file_writers'] and entry['reader_reentry'] == 0):
        with _registry_lock:
            for fd in entry['fds'].values():
                _active_descriptors.discard(fd)
                os.close(fd)
            entry['fds'].clear()


def _regular_file_destination(target):
    try:
        return stat.S_ISREG(os.stat(target).st_mode)
    except FileNotFoundError:
        return True  # Missing file destinations still have path identity.
    except OSError:
        return False


@contextlib.contextmanager
def workspace_file_write_lease(path, *, timeout=0.0, cancel_event=None):
    """Exclude a canonical file with SH checkout + EX target admission.

    Directories/unknown types fall back to whole-checkout EX. Non-Git paths
    remain no-ops. Reenter a same-thread whole writer without a second flock;
    never weaken an explicit stable whole read to overlap a file writer.
    One timeout budget covers both stages. This is cooperative exclusion,
    not containment of opaque processes or arbitrary external filesystem I/O.
    """
    deadline = time.monotonic() + _validated_timeout(timeout)
    _check_cancelled(path, cancel_event)
    lexical = os.path.abspath(os.path.expanduser(os.fspath(path)))
    target = os.path.realpath(lexical)
    try:
        identity = _checkout_identity(target)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot resolve workspace lease: {error}') from error
    _check_cancelled(target, cancel_event)
    if identity is None:
        yield
        return
    root, common = identity
    with _registry_lock:
        entry = _registry.get((root, str(common)))
    whole_writer = False
    if entry is not None:
        with entry['cond']:
            whole_writer = entry['writer_thread'] == threading.get_ident()
    regular = _regular_file_destination(target)

    def remaining():
        return max(0.0, deadline - time.monotonic())

    if whole_writer or not regular:
        with _lease(target, _WRITE, remaining(), cancel_event, _identity=identity):
            if os.path.realpath(lexical) != target:
                raise WorkspaceBusyError('File target changed while waiting for write admission; '
                                         're-resolve and retry: ' + lexical)
            yield
        return
    with _lease(root, _FILE_BARRIER, remaining(), cancel_event, _identity=identity):
        with _lease(target, _WRITE, remaining(), cancel_event, _identity=(target, common)):
            if os.path.realpath(lexical) != target or not _regular_file_destination(target):
                raise WorkspaceBusyError('File target changed while waiting for write admission; '
                                         're-resolve and retry: ' + lexical)
            yield


@contextlib.contextmanager
def workspace_runtime_setup_lease(path, *, timeout=0.0, cancel_event=None):
    """Serialize ignored dependency preparation without claiming source writes.

    A shared checkout barrier still fences worktree deletion/publication. The
    exclusive setup identity is a scheduling key, not a filesystem write target;
    only the trusted runtime preparer uses it for .venv/node_modules creation.
    """
    deadline = time.monotonic() + _validated_timeout(timeout)
    _check_cancelled(path, cancel_event)
    try:
        identity = _checkout_identity(path)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot resolve workspace lease: {error}') from error
    if identity is None:
        yield
        return
    root, common = identity
    with _lease(root, _FILE_BARRIER, max(0.0, deadline - time.monotonic()),
                cancel_event, _identity=identity):
        key = 'runtime-setup:' + root
        with _lease(key, _WRITE, max(0.0, deadline - time.monotonic()),
                    cancel_event, _identity=(key, common)):
            yield


def workspace_write_lease(path, *, timeout=0.0, cancel_event=None):
    """Exclude cooperative holders of the same real Git checkout.

    Reentrant on the owning thread; the sole reader may upgrade in place.
    Waits are finite (0..600 seconds), optionally cancellable, and fail-fast
    by default. Authorization/readonly checks remain the caller's authority.
    Lock files are never unlinked and ownership is never stolen.
    """
    return _lease(path, _WRITE, timeout, cancel_event)


def workspace_read_lease(path, *, timeout=0.0, cancel_event=None):
    """Explicitly protect stable reads; share with other in-process readers.

    Excluded by a local writer or another process's flock. Same finite timeout,
    cancellation, identity and fail-closed rules as the write lease. Opaque
    test/build/interpreter commands do not automatically request this API.
    """
    return _lease(path, _READ, timeout, cancel_event)
