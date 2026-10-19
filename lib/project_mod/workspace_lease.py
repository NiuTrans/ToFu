"""Cooperative checkout exclusion, independent of authorization/presence.

Entry points:
- workspace_write_lease(path, *, timeout=0.0, cancel_event=None) excludes all
  cooperative holders during whole-checkout mutations/capture/publication.
- workspace_file_write_lease(path, *, timeout=0.0, cancel_event=None) excludes
  one canonical file using a shared checkout barrier and exclusive target lock.
- workspace_read_lease(path, *, timeout=0.0, cancel_event=None) explicitly
  protects a caller's stable checkout read. Opaque commands do not imply one.

- workspace_git_metadata_lease serializes common-directory index/refs effects
  while allowing file edits in the checkout to proceed.
- workspace_tree_write_lease fences real recursive/lifecycle targets, including
  prospective/non-Git directories. Acquire tree before checkout/file scopes.
- lease_claim_order((scope, target)) orders multi-claim wrappers consistently.
Git worktrees share a common-dir but have distinct locks keyed by the real
checkout path. Checkout leases never imply common-directory metadata ownership;
Git's native locks/CAS and explicit publication transactions own shared refs.
Locks are advisory: external editors and programs deliberately
closing inherited FDs are non-cooperating writers. Non-Git file transactions
use canonical targets in the neutral namespace; non-Git checkout scopes remain
no-ops. Explicit destructive tree scopes also protect non-Git directories.
There is no owner database, expiry-based stealing, or stale-lock deletion.

In-process gates distinguish writer ownership and reader counts. A writer may
reenter read without downgrading; the sole reader may upgrade and retain its
outer read claim. One exclusive POSIX flock covers both modes in this process:
flock is open-file-description scoped, so a competing descriptor would block
an in-process upgrade. Explicit readers share that descriptor locally; across
processes its holder type is unknown, not evidence of an active writer.
File barriers alone use SH: distinct files can overlap across processes while
whole-checkout leases still exclude them. Entries/FDs exist only while active.

Checkout operations retain shared real-path lifecycle ancestor barriers, not
ancestor checkout root claims. Parent checkout mutations therefore commute with
nested edits; explicit tree leases fence destructive descendants (including
external linked worktrees and their Git administration) without scans. Complete checkout/Git/common-dir bindings
are revalidated after admission; metadata rebinding fails visibly. Persistent
zero-byte lockfiles are one inode per historical root/file/setup key, not per
checkout alone, and are never unlinked or migrated by release.
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

_NEUTRAL_BINDING = object()  # Physical tree keys are not Git checkout identities.

def _metadata(path: Path) -> str:
    with path.open('r', encoding='utf-8') as stream:
        value = stream.read(8193)
    if len(value) > 8192 or not value.strip():
        raise WorkspaceBusyError(f'Invalid Git workspace metadata: {path}')
    return value.strip()


def _binding_at_root(root):
    """Read a root's complete binding once; no timestamp-based identity cache."""
    marker = root / '.git'
    try:
        marker_info = marker.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None  # A path below an existing file must still search its ancestors.
    marker_is_symlink = stat.S_ISLNK(marker_info.st_mode)
    if marker_is_symlink:
        try:
            marker_info = marker.stat()
        except FileNotFoundError as error:
            raise WorkspaceBusyError(f'Broken Git directory pointer: {marker}') from error
    pointer = None
    if stat.S_ISDIR(marker_info.st_mode):
        # Discovery roots are already real paths. Non-symlink .git directories
        # need no repeated full-prefix readlink walk; final topology validation
        # still detects root relocation/rebinding before protected work.
        git_dir = marker.resolve() if marker_is_symlink else marker
        git_info = marker_info
    elif stat.S_ISREG(marker_info.st_mode):
        pointer = _metadata(marker)
        if not pointer.startswith('gitdir: '):
            raise WorkspaceBusyError(f'Invalid Git directory pointer: {marker}')
        git_dir = (root / pointer[8:]).resolve()
        git_info = git_dir.stat()
    else:
        raise WorkspaceBusyError(f'Invalid Git workspace marker type: {marker}')
    if not stat.S_ISDIR(git_info.st_mode):
        raise WorkspaceBusyError(f'Git directory is unavailable: {git_dir}')
    common_file = git_dir / 'commondir'
    try:
        common_marker = common_file.lstat()
    except FileNotFoundError:
        common_marker = None
    if common_marker is None:
        common_pointer, common, common_info = None, git_dir, git_info
    else:
        if stat.S_ISLNK(common_marker.st_mode):
            try:
                common_marker = common_file.stat()
            except FileNotFoundError as error:
                raise WorkspaceBusyError(f'Invalid Git common-directory pointer: {common_file}') from error
        if not stat.S_ISREG(common_marker.st_mode):
            raise WorkspaceBusyError(f'Invalid Git common-directory pointer: {common_file}')
        common_pointer = _metadata(common_file)
        common = (git_dir / common_pointer).resolve()
        common_info = git_info if common == git_dir else common.stat()
    if not stat.S_ISDIR(common_info.st_mode):
        raise WorkspaceBusyError(f'Git common directory is unavailable: {common}')
    root_info = root.stat()
    return (str(root), common, str(git_dir), (root_info.st_dev, root_info.st_ino),
            (git_info.st_dev, git_info.st_ino), (common_info.st_dev, common_info.st_ino),
            pointer, common_pointer)


def _checkout_binding(path):
    """Discover existing ancestor checkouts without Git env/config overrides."""
    current = Path(os.path.realpath(os.path.expanduser(os.fspath(path))))
    if not current.is_dir():
        current = current.parent
    for root in (current, *current.parents):
        binding = _binding_at_root(root)
        if binding is not None:
            return binding
    return None


def _checkout_identity(path):
    """Compatibility projection for Git target/index resolution."""
    binding = _checkout_binding(path)
    return binding[:2] if binding is not None else None


def _checkout_chain(path):
    """One real-path/ancestor walk, outermost first; no persistent cache/scan."""
    current = Path(os.path.realpath(os.path.expanduser(os.fspath(path))))
    if not current.is_dir():
        current = current.parent
    chain = []
    for root in (current, *current.parents):
        binding = _binding_at_root(root)
        if binding is not None:
            chain.append(binding)
    return tuple(reversed(chain))


def _validate_binding(path, expected):

    if expected is _NEUTRAL_BINDING:
        return  # Lock inode validated separately; public caller validates topology.
    # The fence captured each known root. Check that binding directly here;
    # _validate_chain still rediscovers the full topology immediately before body.
    try:
        actual = (_binding_at_root(Path(expected[0])) if expected is not None
                  else _checkout_binding(path))
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot revalidate workspace lease: {error}') from error
    if actual != expected:
        raise WorkspaceBusyError('Workspace identity changed while waiting for admission; '
                                 're-resolve and retry: ' + os.fspath(path))


def lease_claim_order(claim):
    """Deterministic TREE/metadata/ROOT/FILE order for multi-claim wrappers.

    Ancestor lifecycle barriers are acquired in canonical path order. Public
    tree ancestors come first; checkout groups retain root-before-file order.
    Invalid metadata fails closed before any caller acquires a claim.
    """
    scope, path = claim
    if scope not in ('root', 'file', 'metadata', 'tree'):
        raise ValueError('workspace lease claim scope must be root, file, metadata or tree')
    target = os.path.realpath(os.path.expanduser(os.fspath(path)))
    try:
        chain = _checkout_chain(target)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot resolve workspace lease order: {error}') from error
    checkout = chain[-1][0] if chain else target
    common = str(chain[-1][1]) if chain else str(_tree_lock_namespace())
    # Semantic ordering is alias-independent. Multi-target callers also use
    # workspace_claims_lease to order the implicit physical barriers together.
    if scope == 'tree':
        return 0, target, 0, '', target
    if scope == 'metadata':
        return 1, common, 0, '', common
    return 1, common, 1, checkout, (scope != 'root', target)


def is_source_workspace_path(path) -> bool:
    """Runtime/user data beneath a Git root is not a source checkout writer."""
    from lib.git_checkpoint_policy import FORBIDDEN_CHECKPOINT_ROOTS
    identity = _checkout_identity(path)
    if identity is None:
        return True  # Non-Git Edit still needs canonical-file transaction protection.
    relative = Path(os.path.realpath(os.path.expanduser(os.fspath(path)))).relative_to(identity[0])
    return not relative.parts or relative.parts[0] not in FORBIDDEN_CHECKPOINT_ROOTS


_registry_lock = threading.Lock()
_registry = {}  # active holders/waiters only; removed at last departure
_active_descriptors = set()  # includes pending cross-process admission

_lease_scope_state = threading.local()  # bounded depth + pending hint, never ownership

def _after_fork():
    # Close, never LOCK_UN: the parent's open-file-description owns the flock.
    global _registry_lock, _registry, _active_descriptors, _lease_scope_state
    for fd in _active_descriptors:
        try:
            os.close(fd)
        except OSError:
            pass
    _registry_lock = threading.Lock()
    _registry = {}
    _active_descriptors = set()
    _lease_scope_state = threading.local()


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
    directory_fd = os.open(lock_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = os.open(digest + '.lock', flags, 0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceBusyError(f'Unsafe workspace lock file for {root}')
    except BaseException:
        # Opening succeeded but descriptor validation did not transfer ownership.
        os.close(fd)
        raise
    return fd, fcntl


def _validate_lock_fd(common, root, fd):
    """A removed/rebound namespace is not admission on the current namespace."""
    lock_dir = common / 'tofu-workspace-leases'
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()
    try:
        directory = lock_dir.lstat()
        actual = (lock_dir / (digest + '.lock')).lstat()
        held = os.fstat(fd)
        safe = (stat.S_ISDIR(directory.st_mode) and stat.S_ISREG(actual.st_mode)
                and actual.st_nlink == held.st_nlink == 1
                and (actual.st_dev, actual.st_ino) == (held.st_dev, held.st_ino))
    except OSError as error:
        raise WorkspaceBusyError(f'Cannot validate workspace lock binding for {root}: {error}') from error
    if not safe:
        raise WorkspaceBusyError('Workspace lock metadata rebound during admission: ' + root)


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

    domain = 'Workspace'
    if root.startswith('tree:'):
        domain, root = 'Workspace tree lifecycle', root[len('tree:'):]
    elif root.startswith('git-metadata:'):
        domain, root = 'Git common-directory metadata', root[len('git-metadata:'):]
    if active == _UNKNOWN:
        return WorkspaceBusyError(
            f'{domain} lease contention is active (holder type unknown): {root}')
    message = f'{domain} {active} is active: {root}'
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
                _validate_lock_fd(common, root, fd)
                return fd
            except BlockingIOError:
                remaining = _wait_remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise _busy(root, _UNKNOWN)
                time.sleep(0.05 if remaining is None else min(0.05, remaining))
    except BaseException as error:
        if fd is not None:
            _close_descriptor(fd)
        if isinstance(error, (OSError, ImportError)):
            raise WorkspaceBusyError(
                f'Workspace lease unavailable for {root}: {error}') from error
        raise


def _enter(path, mode, timeout, cancel_event, _identity=None, _binding=None,
           _validation_path=None):
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
    validation_path = path if _validation_path is None else _validation_path
    binding = _binding if _binding is not None else _checkout_binding(validation_path)
    # Diagnostic collection is fallible and must precede acquiring any FD or
    # publishing registry ownership. It cannot strand a transferred flock.
    holder = _holder_identity(cancel_event)
    deadline = _wait_deadline(timeout)
    thread_id = threading.get_ident()
    with _registry_lock:
        if any(other[0] == root and other != key for other in _registry):
            raise WorkspaceBusyError('Workspace metadata rebound during an active lease: ' + root)
        entry = _registry.setdefault(key, {
            'cond': threading.Condition(), 'users': 0,
            'writer_thread': None, 'writer_depth': 0,
            'readers': {}, 'reader_reentry': 0, 'file_writers': {},
            'fds': {}, 'acquiring_thread': None, 'holders': {},
        })
        entry['users'] += 1
    pending = False
    fd = None
    snapshot = None
    waited_locally = False
    try:
        with entry['cond']:
            while True:
                _check_cancelled(root, cancel_event)
                # Even reentry mutates depths. Every logical admission is a
                # transaction, including publication on an already-owned FD.
                snapshot = {name: (dict(entry[name]) if isinstance(entry[name], dict)
                                   else entry[name]) for name in (
                    'writer_thread', 'writer_depth', 'readers', 'reader_reentry',
                    'file_writers', 'holders')}
                outcome = _try_acquire(entry, thread_id, mode)
                if outcome in ('acquire', 'enter'):
                    break
                snapshot = None  # A blocked probe published no ownership.
                remaining = _wait_remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise _busy(root, outcome, entry)
                waited_locally = True
                entry['cond'].wait(timeout=0.05 if remaining is None else min(0.05, remaining))
            # Fresh reservations are always validated after physical admission,
            # before publication/body. Existing-FD/reentry and local-wait paths
            # also validate here; no identity is cached across acquisitions.
            if waited_locally or entry['fds'] or outcome == 'enter':
                _validate_binding(validation_path, binding)
            for held_fd in entry['fds'].values():
                _validate_lock_fd(common, root, held_fd)
            if outcome == 'enter':
                _check_cancelled(root, cancel_event)
                return entry, root, key, outcome
            if entry['fds']:
                _publish_hold(entry, thread_id, mode, holder)
                _check_cancelled(root, cancel_event)
                return entry, root, key, outcome
            entry['acquiring_thread'] = thread_id
            pending = True
        # Reserve admission, but let other local waiters observe/cancel/expire.
        fd = _acquire_process_lock(common, root, deadline, cancel_event, mode)
        with entry['cond']:
            _check_cancelled(root, cancel_event)
            _validate_binding(validation_path, binding)
            _publish_hold(entry, thread_id, mode, holder)
            _check_cancelled(root, cancel_event)
            with _registry_lock:
                entry['fds'][_FILE_BARRIER if mode == _FILE_BARRIER else _WRITE] = fd
            entry['acquiring_thread'] = None
            entry['cond'].notify_all()
            fd = None  # transfer only after the publication transaction succeeded
            pending = False
        return entry, root, key, outcome
    except BaseException:
        with entry['cond']:
            if fd is not None:
                with _registry_lock:
                    for descriptor_key, value in list(entry['fds'].items()):
                        if value == fd:
                            entry['fds'].pop(descriptor_key)
                _close_descriptor(fd)
            if snapshot is not None:
                # Other readers may publish between exception unwinding and
                # this condition hold. Restore only this transaction's owner.
                for name in ('readers', 'file_writers'):
                    if thread_id in snapshot[name]:
                        entry[name][thread_id] = snapshot[name][thread_id]
                    else:
                        entry[name].pop(thread_id, None)
                for owner_key in [key for key in entry['holders'] if key[1] == thread_id]:
                    entry['holders'].pop(owner_key)
                entry['holders'].update({key: value for key, value in snapshot['holders'].items()
                                         if key[1] == thread_id})
                if entry['writer_thread'] == thread_id:
                    for name in ('writer_thread', 'writer_depth', 'reader_reentry'):
                        entry[name] = snapshot[name]
            if pending:
                entry['acquiring_thread'] = None
            entry['cond'].notify_all()
        with _registry_lock:
            entry['users'] -= 1
            if entry['users'] == 0:
                _registry.pop(key, None)
        raise


def _validated_timeout(timeout):
    if timeout is None:
        return None
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError('workspace lease timeout must be nonnegative and finite or None')
    return timeout


def _wait_deadline(timeout):
    """None owns indefinite cancellable waiting; numeric zero remains fail-fast."""
    timeout = _validated_timeout(timeout)
    return None if timeout is None else time.monotonic() + timeout


def _wait_remaining(deadline):
    return None if deadline is None else max(0.0, deadline - time.monotonic())


@contextlib.contextmanager
def _lease(path, mode, timeout, cancel_event, _identity=None, _binding=None,
           _validation_path=None):
    timeout = _validated_timeout(timeout)
    owner_pid = os.getpid()
    entry, root, key, action = _enter(path, mode, timeout, cancel_event, _identity,
                                     _binding, _validation_path)
    if entry is None:  # non-Git path: no-op
        yield
        return
    thread_id = threading.get_ident()
    state = _lease_scope_state
    state.depth = getattr(state, 'depth', 0) + 1
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
                state.pending_wake = True
            state.depth -= 1
            if state.depth == 0 and getattr(state, 'pending_wake', False):
                # The complete physical scope—not its first released key—must
                # be gone before optional callbacks attempt a competing writer.
                state.pending_wake = False
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


def _tree_lock_namespace():
    """One per-user physical namespace; ownership still lives in _registry.

    Checkout common directories cannot fence arbitrary filesystem ancestors:
    an external linked worktree may live outside every discovered checkout.
    Keep these persistent zero-byte keys outside removable workspace trees.
    """
    common = Path('/tmp') / ('tofu-workspace-tree-' + str(os.getuid()))
    try:
        common.mkdir(mode=0o700, exist_ok=True)
        info = common.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise WorkspaceBusyError('Unsafe workspace tree lock namespace: ' + str(common))
    except OSError as error:
        raise WorkspaceBusyError('Cannot open workspace tree lock namespace: ' + str(common)) from error
    return common


@contextlib.contextmanager
def _tree_fence(paths, exclusive, binding, validation_path, deadline, cancel_event):
    common = _tree_lock_namespace()
    targets = set()
    for path in paths:
        target = Path(path)
        targets.update(str(parent) for parent in (target, *target.parents))
    with contextlib.ExitStack() as stack:
        for target in sorted(targets):
            key = 'tree:' + target
            stack.enter_context(_lease(key, _WRITE if target == exclusive else _FILE_BARRIER,
                _wait_remaining(deadline), cancel_event,
                _identity=(key, common), _binding=_NEUTRAL_BINDING,
                _validation_path=validation_path))
        yield


@contextlib.contextmanager
def _metadata_fence(paths, exclusive, deadline, cancel_event):
    """Physical Git-administration effects share one neutral namespace.

    Raw index/ref edits advertise canonical ancestors, even for externally
    located Git common dirs with no discoverable .git marker. Ordinary source
    paths do not include their sibling .git directory and remain parallel.
    """
    common = _tree_lock_namespace()
    targets = set()
    for path in paths:
        target = Path(path)
        targets.update(str(parent) for parent in (target, *target.parents))
    with contextlib.ExitStack() as stack:
        for target in sorted(targets):
            key = 'git-metadata:' + target
            mode = _WRITE if target == exclusive else _FILE_BARRIER
            stack.enter_context(_lease(key, mode,
                _wait_remaining(deadline), cancel_event,
                _identity=(key, common), _binding=_NEUTRAL_BINDING,
                _validation_path=target))
        yield


def _protect_lease_namespace(target, *, tree=False):
    namespace = os.path.realpath(_tree_lock_namespace())
    shared = os.path.commonpath((target, namespace))
    if shared == namespace or (tree and shared == target):
        raise WorkspaceBusyError('Internal workspace lease namespace cannot be mutated: ' + target)


@contextlib.contextmanager
def _checkout_fence(path, deadline, cancel_event):
    """Fence destructive ancestors, not ordinary ancestor checkout writers."""
    _check_cancelled(path, cancel_event)
    try:
        chain = _checkout_chain(path)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot resolve workspace lease: {error}') from error
    _check_cancelled(path, cancel_event)
    with contextlib.ExitStack() as stack:
        if chain:
            binding = chain[-1]
            # Real filesystem ancestors cover external linked worktrees and
            # their administrative directories without discovering descendants.
            paths = (os.path.realpath(os.fspath(path)), binding[0], binding[2],
                     str(binding[1]), str(binding[1] / 'tofu-workspace-leases'))
            stack.enter_context(_tree_fence(paths, None, binding, path,
                                           deadline, cancel_event))
            stack.enter_context(_metadata_fence((os.path.realpath(os.fspath(path)),),
                                                None, deadline, cancel_event))
        yield chain


def _validate_chain(path, chain):
    try:
        actual = _checkout_chain(path)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot revalidate workspace topology: {error}') from error
    if actual != chain:
        raise WorkspaceBusyError('Workspace identity changed while waiting for admission; '
                                 're-resolve and retry: ' + os.fspath(path))


@contextlib.contextmanager
def _root_lease(path, mode, timeout, cancel_event):
    deadline = _wait_deadline(timeout)
    with _checkout_fence(path, deadline, cancel_event) as chain:
        if not chain:
            yield
            return
        binding = chain[-1]
        # Protect this checkout's files/index, not every linked worktree. The
        # common directory remains fenced against destructive tree retirement.
        with _lease(path, mode, _wait_remaining(deadline), cancel_event,
                    _identity=binding[:2], _binding=binding):
            _validate_chain(path, chain)
            yield


def _regular_file_destination(target):
    try:
        return stat.S_ISREG(os.stat(target).st_mode)
    except FileNotFoundError:
        return True  # Missing file destinations still have path identity.
    except OSError:
        return False


@contextlib.contextmanager
def _plain_file_transaction(lexical, target, deadline, cancel_event, chain=()):
    """Same-file exclusion without inventing a non-Git checkout identity."""
    if not _regular_file_destination(target):
        with workspace_tree_write_lease(target,
                timeout=_wait_remaining(deadline), cancel_event=cancel_event):
            yield
        return
    common = _tree_lock_namespace()
    with contextlib.ExitStack() as stack:
        stack.enter_context(_tree_fence((target,), None, None, target, deadline, cancel_event))
        stack.enter_context(_metadata_fence((target,), None, deadline, cancel_event))
        with _lease(target, _WRITE, _wait_remaining(deadline), cancel_event,
                    _identity=(target, common), _binding=_NEUTRAL_BINDING,
                    _validation_path=target):
            _check_cancelled(target, cancel_event)
            _validate_chain(target, chain)
            if os.path.realpath(lexical) != target or not _regular_file_destination(target):
                raise WorkspaceBusyError('File target changed while waiting for admission: ' + lexical)
            try:
                if os.stat(target).st_nlink != 1:
                    raise WorkspaceBusyError('Hardlinked file has ambiguous write identity: ' + target)
            except FileNotFoundError:
                pass  # Creation still belongs to the canonical pathname.
            yield


def _protect_file_identity_metadata(target):
    """Raw Git pointer edits can split lock namespaces for the same working file.

    Use admitted Git lifecycle operations rather than a regular-file transaction
    for checkout markers and worktree common-directory pointers. This check is
    before non-Git/runtime exemptions; no descendant scan or owner cache exists.
    """
    path = Path(target)
    if ((path.name == '.git' and not path.is_dir())
            or (path.name == 'commondir' and (path.parent.parent.name == 'worktrees'
                                            or (path.parent / 'HEAD').is_file()))):
        raise WorkspaceBusyError('Git workspace identity metadata cannot be rebound by a file lease; '
                                 'use an admitted Git lifecycle operation: ' + target)


@contextlib.contextmanager
def workspace_file_write_lease(path, *, timeout=0.0, cancel_event=None):
    """Exclude a canonical file with SH checkout + EX target admission.

    Directories/unknown types fall back to whole-checkout EX (tree exclusion
    outside Git). Non-Git regular files also serialize only their canonical
    target. Reenter a same-thread whole writer without a second flock;
    never weaken an explicit stable whole read to overlap a file writer.
    One timeout budget covers both stages. This is cooperative exclusion,
    not containment of opaque processes or arbitrary external filesystem I/O.
    """
    deadline = _wait_deadline(timeout)
    _check_cancelled(path, cancel_event)
    lexical = os.path.abspath(os.path.expanduser(os.fspath(path)))
    target = os.path.realpath(lexical)
    _protect_lease_namespace(target)

    _protect_file_identity_metadata(target)
    def remaining():
        return _wait_remaining(deadline)

    with _checkout_fence(target, deadline, cancel_event) as chain:
        if not chain or not is_source_workspace_path(target):
            with _plain_file_transaction(lexical, target, deadline, cancel_event, chain):
                yield
            return
        binding = chain[-1]
        root, common = binding[:2]
        if target in (os.path.realpath(os.path.join(root, '.git')),
                      os.path.join(binding[2], 'commondir')):
            raise WorkspaceBusyError('Git workspace identity metadata cannot be rebound by a file lease: ' + target)
        with _registry_lock:
            entry = _registry.get((root, str(common)))
        whole_writer = False
        if entry is not None:
            with entry['cond']:
                whole_writer = entry['writer_thread'] == threading.get_ident()
        regular = _regular_file_destination(target)

        def validate_target():
            _check_cancelled(target, cancel_event)
            _validate_chain(target, chain)
            if os.path.realpath(lexical) != target or not _regular_file_destination(target):
                raise WorkspaceBusyError('File target changed while waiting for write admission; '
                                         're-resolve and retry: ' + lexical)
            try:
                if os.stat(target).st_nlink != 1:
                    raise WorkspaceBusyError('Hardlinked file has ambiguous write identity: ' + target)
            except FileNotFoundError:
                pass  # A missing regular-file destination keeps canonical path identity.

        if whole_writer or not regular:
            with _lease(target, _WRITE, remaining(), cancel_event,
                        _identity=binding[:2], _binding=binding):
                _validate_chain(target, chain)
                if os.path.realpath(lexical) != target:
                    raise WorkspaceBusyError('File target changed while waiting for write admission; '
                                             're-resolve and retry: ' + lexical)
                if regular:
                    validate_target()
                yield
            return
        with _lease(root, _FILE_BARRIER, remaining(), cancel_event,
                    _identity=binding[:2], _binding=binding):
            with _lease(target, _WRITE, remaining(), cancel_event,
                        _identity=(target, common), _binding=binding):
                validate_target()
                yield


@contextlib.contextmanager
def workspace_runtime_setup_lease(path, *, timeout=0.0, cancel_event=None):
    """Serialize ignored dependency preparation without claiming source writes.

    A shared checkout barrier still fences worktree deletion/publication. The
    exclusive setup identity is a scheduling key, not a filesystem write target;
    only the trusted runtime preparer uses it for .venv/node_modules creation.
    """
    deadline = _wait_deadline(timeout)
    _check_cancelled(path, cancel_event)
    with _checkout_fence(path, deadline, cancel_event) as chain:
        if not chain:
            yield
            return
        binding = chain[-1]
        root, common = binding[:2]
        with _lease(root, _FILE_BARRIER, _wait_remaining(deadline),
                    cancel_event, _identity=binding[:2], _binding=binding):
            key = 'runtime-setup:' + root
            with _lease(key, _WRITE, _wait_remaining(deadline),
                        cancel_event, _identity=(key, common), _binding=binding,
                        _validation_path=path):
                _validate_chain(path, chain)
                yield


def workspace_write_lease(path, *, timeout=0.0, cancel_event=None):
    """Exclude cooperative holders of the same real Git checkout.

    Reentrant on the owning thread; the sole reader may upgrade in place.
    Waits accept an exact finite nonnegative bound or None, are cancellable, and fail-fast
    by default. Authorization/readonly checks remain the caller's authority.
    Lock files are never unlinked and ownership is never stolen.
    """
    return _root_lease(path, _WRITE, timeout, cancel_event)


def workspace_read_lease(path, *, timeout=0.0, cancel_event=None):
    """Explicitly protect stable reads; share with other in-process readers.

    Excluded by a local writer or another process's flock. Same optional timeout,
    cancellation, identity and fail-closed rules as the write lease. Opaque
    test/build/interpreter commands do not automatically request this API.
    """
    return _root_lease(path, _READ, timeout, cancel_event)


@contextlib.contextmanager
def workspace_git_metadata_lease(path, *, timeout=0.0, cancel_event=None):
    """Serialize index/refs-only commands by canonical Git common directory.

    Files remain parallel. The checkout shared barrier still excludes genuine
    transformations and stable snapshots of this checkout. Callers own Git
    target resolution and authorization; this is not command classification.
    """
    deadline = _wait_deadline(timeout)
    with _checkout_fence(path, deadline, cancel_event) as chain:
        if not chain:
            yield
            return
        binding = chain[-1]
        root, common = binding[:2]
        with _metadata_fence((str(common),), str(common), deadline, cancel_event):
            with _lease(root, _FILE_BARRIER, _wait_remaining(deadline),
                        cancel_event, _identity=binding[:2], _binding=binding):
                _validate_chain(path, chain)
                yield


def _tree_target_node(target):
    try:
        info = os.stat(target)
        return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def workspace_tree_write_lease(path, *, timeout=0.0, cancel_event=None):
    """Fence a real recursive destructive/lifecycle target and descendants.

    Unlike root leases this also works on non-Git/prospective directories.
    Descendants advertise shared ancestor keys, including checkout, Git-dir
    and common-dir paths, without scans or a second ownership registry.
    """
    deadline = _wait_deadline(timeout)
    _check_cancelled(path, cancel_event)
    lexical = os.path.abspath(os.path.expanduser(os.fspath(path)))
    target = os.path.realpath(lexical)
    _protect_lease_namespace(target, tree=True)
    try:
        chain = _checkout_chain(target)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot resolve workspace tree lease: {error}') from error
    binding = chain[-1] if chain else None
    try:
        target_node = _tree_target_node(target)
    except OSError as error:
        raise WorkspaceBusyError(f'Cannot bind workspace tree target: {error}') from error
    with contextlib.ExitStack() as stack:
        stack.enter_context(_tree_fence((target,), target, binding, target, deadline, cancel_event))
        stack.enter_context(_metadata_fence((target,), None, deadline, cancel_event))
        _check_cancelled(path, cancel_event)
        _validate_chain(target, chain)
        try:
            actual_node = _tree_target_node(target)
        except OSError as error:
            raise WorkspaceBusyError(f'Cannot revalidate workspace tree target: {error}') from error
        if os.path.realpath(lexical) != target or actual_node != target_node:
            raise WorkspaceBusyError('Tree target changed while waiting for admission: ' + lexical)
        yield


def _physical_claim_plan(claims, neutral):
    """Expand implicit barriers once; merge stronger claims on identical keys.

    Logical ordering alone cannot prevent TREE(A)+ROOT(B) from inverting
    ROOT(A)+TREE(B). All callers sharing a multi-target transaction admit the
    complete physical set before public-scope validation or filesystem reads.
    """
    physical = {}
    evidence = []

    def add(phase, key, mode, common, binding, path):
        identity = (key, common)
        address = (key, str(common))
        previous = physical.get(address)
        if previous is None or mode == _WRITE:
            physical[address] = (phase, key, mode, identity, binding, path)

    def ancestors(paths, prefix, exclusive=None):
        targets = set()
        for path in paths:
            target = Path(path)
            targets.update(str(parent) for parent in (target, *target.parents))
        for target in targets:
            add(0 if prefix == 'tree:' else 1, prefix + target,
                _WRITE if target == exclusive else _FILE_BARRIER,
                neutral, _NEUTRAL_BINDING, target)

    for scope, path in claims:
        lexical = os.path.abspath(os.path.expanduser(os.fspath(path)))
        target = os.path.realpath(lexical)
        if scope == 'tree':
            _protect_lease_namespace(target, tree=True)
        elif scope == 'file':
            _protect_lease_namespace(target)
        chain = _checkout_chain(target)
        binding = chain[-1] if chain else None
        node = _tree_target_node(target) if scope == 'tree' else None
        evidence.append((scope, lexical, target, chain, node))

        if scope == 'file':
            _protect_file_identity_metadata(target)
        if scope == 'tree':
            ancestors((target,), 'tree:', target)
            ancestors((target,), 'git-metadata:')
            continue
        if not chain:
            if scope != 'file':
                continue  # No invented Git checkout/metadata identity.
            if _regular_file_destination(target):
                ancestors((target,), 'tree:')
                ancestors((target,), 'git-metadata:')
                add(3, target, _WRITE, neutral, _NEUTRAL_BINDING, target)
            else:
                ancestors((target,), 'tree:', target)
                ancestors((target,), 'git-metadata:')
            continue
        root, common = binding[:2]
        lifecycle_paths = (target, root, binding[2], str(common),
                           str(common / 'tofu-workspace-leases'))
        ancestors(lifecycle_paths, 'tree:')
        ancestors((target,), 'git-metadata:')
        if scope == 'metadata':
            ancestors((str(common),), 'git-metadata:', str(common))
            add(2, root, _FILE_BARRIER, common, binding, target)
        elif scope == 'root':
            add(2, root, _WRITE, common, binding, target)
        elif scope == 'file':
            if not is_source_workspace_path(target):
                if _regular_file_destination(target):
                    add(3, target, _WRITE, neutral, _NEUTRAL_BINDING, target)
                else:
                    ancestors((target,), 'tree:', target)
            elif _regular_file_destination(target):
                add(2, root, _FILE_BARRIER, common, binding, target)
                add(3, target, _WRITE, common, binding, target)
            else:
                add(2, root, _WRITE, common, binding, target)
        else:
            raise ValueError('Unknown workspace effect scope: ' + str(scope))
    ordered = sorted(physical.values(), key=lambda item: (item[0], str(item[3][1]), item[1]))
    return ordered, evidence


@contextlib.contextmanager
def workspace_claims_lease(claims, *, timeout=0.0, cancel_event=None):
    """Atomically admit a complete ordered multi-target effect set.

    This is the batching boundary for Edit and shell transactions.
    The existing registry/flocks still own every descriptor. Public single-
    target scopes remain usable; multi-target callers must not acquire their
    hidden barriers one logical scope at a time.
    """
    deadline = _wait_deadline(timeout)
    _check_cancelled('workspace effects', cancel_event)
    claims = tuple(claims)
    if not claims:
        yield
        return
    for scope, _path in claims:
        if scope not in {'root', 'file', 'metadata', 'tree'}:
            raise ValueError('Unknown workspace effect scope: ' + str(scope))
    neutral = _tree_lock_namespace()
    try:
        physical, evidence = _physical_claim_plan(claims, neutral)
    except (OSError, UnicodeError) as error:
        raise WorkspaceBusyError(f'Cannot resolve workspace effects: {error}') from error
    apis = {'root': workspace_write_lease, 'file': workspace_file_write_lease,
            'metadata': workspace_git_metadata_lease, 'tree': workspace_tree_write_lease}
    with contextlib.ExitStack() as stack:
        for _phase, key, mode, identity, binding, path in physical:
            stack.enter_context(_lease(key, mode, _wait_remaining(deadline),
                cancel_event, _identity=identity, _binding=binding, _validation_path=path))
        # Rebinding during a wait must not become a fresh baseline simply
        # because the validating single-target scope is entered afterwards.
        for scope, lexical, target, chain, node in evidence:
            _check_cancelled(target, cancel_event)
            _validate_chain(target, chain)
            if (os.path.realpath(lexical) != target
                    or (scope == 'tree' and _tree_target_node(target) != node)):
                raise WorkspaceBusyError('Workspace target changed while waiting for admission: ' + lexical)
        for scope, path in sorted(claims, key=lease_claim_order):
            stack.enter_context(apis[scope](path,
                timeout=_wait_remaining(deadline), cancel_event=cancel_event))
        yield
