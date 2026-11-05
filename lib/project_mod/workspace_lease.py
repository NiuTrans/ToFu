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
- set_admission_wait_observer(observer) installs the process-wide sink that
  receives one AdmissionWait per acquisition that had to wait. Higher layers
  (the task engine) install it; this module never imports them.
- workspace_claims_lease(..., purpose=) names the request (its claims and a
  bounded summary). Wait lines and AdmissionWait name both the waiting and the
  blocking request.

Callers are Tofu's own whole-workspace operations (delivery, integration
snapshots, linear checkpoints, dependency setup, Git maintenance). Model tool
calls take no lease (docs/runbooks/GIT_WORKSPACE_GUARD.md, "Tool calls take
no workspace leases").
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

Admission order: a blocked request queues on its key. A fresh request may not
overtake an earlier queued request of a conflicting mode while every holder
blocking that request is younger than _QUEUE_ORDER_MAX_HOLDER_AGE_SECONDS;
reentrant requests are never held back. A writer is therefore not starved by
overlapping shared holders, compatible work is not frozen behind a writer that
waits for a long-running holder, and queue order cannot close a lock-order
cycle for longer than that fixed bound.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time

from lib.tools.execution_timing import timing_phase


class WorkspaceBusyError(RuntimeError):
    """Lease unavailable (busy, unsupported, or unsafe lock metadata)."""


class WorkspaceLeaseCancelled(WorkspaceBusyError):
    """Admission cancelled before the protected operation could begin."""


class WorkspaceLeaseNamespaceError(WorkspaceBusyError):
    """The declared write scope covers internal lease storage, independent of holders."""


_WRITE = 'write'
_FILE_BARRIER = 'file-write'
_READ = 'read'
_UNKNOWN = 'unknown'
_QUEUED = 'queued'

# Queue order holds a fresh compatible request back only while every holder
# blocking the earlier request is younger than this. Behind an older holder the
# earlier request cannot be admitted anyway, so compatible work proceeds
# instead of freezing behind it. The same bound ends any wait that queue order
# alone imposes, so it cannot become a lock-order cycle. A fixed scheduling
# constant, independent of hardware.
_QUEUE_ORDER_MAX_HOLDER_AGE_SECONDS = 2.0


@dataclass(frozen=True)
class AdmissionWait:
    """One acquisition that could not be admitted on its first probe.

    Instants are this process's ``time.monotonic()`` seconds; observers project
    them onto their own clocks. Identity fields are bounded identifiers; the
    lock root and claim labels (``scope:basename[rule]``) carry basenames only,
    never full paths. The holder is the one observed blocking this wait for the
    longest stretch; it is empty when no local holder was known (another
    process holds the flock). It may be an earlier queued request (mode
    ``queued-<mode>``; its held time is then its time queued). Its command is a
    bounded one-line summary, present only when the holder's task has the
    waiter's owner.
    """

    started_monotonic: float
    ended_monotonic: float
    mode: str
    lock_domain: str
    lock_root: str
    outcome: str
    waiter_tool: str = ''
    tool_call_id: str = ''
    round_num: int | None = None
    holder_tool: str = ''
    holder_task: str = ''
    holder_conversation: str = ''
    holder_held_ms: int | None = None
    waiter_claims: str = ''
    holder_mode: str = ''
    holder_claims: str = ''
    holder_command: str = ''


_admission_wait_observer = None


def set_admission_wait_observer(observer):
    """Install the process-wide admission-wait sink; ``None`` uninstalls it.

    Called as ``observer(task, wait)`` on the waiting thread once its
    acquisition ends (admitted, cancelled or refused), never for an immediate
    admission. It runs outside every lease condition but may hold the admitted
    lease, so it must not acquire workspace leases. Its failures are swallowed:
    evidence never decides admission.
    """
    global _admission_wait_observer
    if observer is not None and not callable(observer):
        raise TypeError('admission-wait observer must be callable or None')
    _admission_wait_observer = observer

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
_registry_condition = threading.Condition(_registry_lock)
_opening_descriptors = 0  # fork waits for open -> registration/close
_registry = {}  # active holders/waiters only; removed at last departure
_active_descriptors = set()  # includes pending cross-process admission

_lease_scope_state = threading.local()  # bounded depth + pending hint, never ownership
_admission_wait_state = threading.local()  # armed flag + this thread's open wait record
_admission_request_state = threading.local()  # this thread's outermost named lease request

@contextlib.contextmanager
def _timed_registry():
    """Measure admission bookkeeping wait, never protected filesystem work.

    Fork waits for in-flight descriptor creation before taking its snapshot;
    registry ownership itself never needs to cover lockfile filesystem I/O.
    """
    lock = _registry_lock
    acquired = False
    try:
        with timing_phase('workspace_registry_wait'):
            lock.acquire()
            acquired = True
        yield
    finally:
        if acquired:
            lock.release()


def _after_fork():
    # Close, never LOCK_UN: the parent's open-file-description owns the flock.
    global _registry_lock, _registry_condition, _opening_descriptors
    global _registry, _active_descriptors, _lease_scope_state
    global _admission_wait_state, _admission_publications_lock, _admission_publications
    global _admission_request_state
    for fd in _active_descriptors:
        try:
            os.close(fd)
        except OSError:
            pass
    _registry_lock = threading.Lock()
    _registry_condition = threading.Condition(_registry_lock)
    _opening_descriptors = 0
    _registry = {}
    _active_descriptors = set()
    _lease_scope_state = threading.local()
    _admission_wait_state = threading.local()
    _admission_request_state = threading.local()
    _admission_publications_lock = threading.Lock()
    _admission_publications = {}


def _before_fork():
    # CPython ignores exceptions from at-fork callbacks and still forks. An
    # interrupted barrier must therefore finish, not expose untracked FDs.
    # Task cancellation remains with the surrounding command lifecycle.
    while True:
        try:
            _registry_lock.acquire()
            break
        except BaseException:
            continue  # Only an interrupted acquire; no ownership was published.
    # Condition.wait releases registry ownership, allowing pending opens to
    # register/close and unrelated existing holders to finish. Hold the registry
    # across the actual fork only after every open descriptor is accounted for.
    while _opening_descriptors:
        try:
            _registry_condition.wait()
        except BaseException:
            continue  # Only the at-fork wait; callback errors cannot safely abort fork.


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
    with _timed_registry():
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
    fd = None
    try:
        try:
            fd = os.open(digest + '.lock', flags, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceBusyError(f'Unsafe workspace lock file for {root}')
    except BaseException:
        # Includes directory-close failure after opening: no descriptor may
        # escape the pending-open barrier without registration or retirement.
        if fd is not None:
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


def _bounded_identifier(value):
    value = str(value or '')
    return value if re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value) else ''


def _holder_identity(cancel_event):
    """Bounded diagnostic values; never retain task/runtime/command bodies."""
    from .tool_admission import cancellation
    scoped = cancellation()
    task = getattr(cancel_event, 'task', None) or getattr(scoped, 'task', None) or {}
    tool = getattr(cancel_event, 'tool_name', '') or getattr(scoped, 'tool_name', '')
    # Acquisitions outside any tool scope (settlement, delivery, checkpoints)
    # carry no tool/task; the thread name is what names their subsystem.
    return {'pid': os.getpid(), 'thread': threading.get_ident(),
            'thread_name': _bounded_identifier(threading.current_thread().name),
            'tool': _bounded_identifier(tool), 'task': _bounded_identifier(task.get('id')),
            'conversation': _bounded_identifier(task.get('convId')),
            'owner': _bounded_identifier(task.get('_userId')),
            'request': getattr(_admission_request_state, 'request', None)}


@dataclass(frozen=True)
class _LeaseRequest:
    """What one lease transaction asked for, as wait diagnostics name it.

    ``claims`` keeps at most ``_REQUEST_CLAIMS_SHOWN`` (scope, path) pairs;
    ``total`` counts every claim. ``purpose`` is the caller's bounded one-line
    summary, never a body.
    """

    claims: tuple
    total: int
    purpose: str


_REQUEST_CLAIMS_SHOWN = 4
_PURPOSE_CHARS = 160


def _describe_request(claims, purpose):
    claims = tuple(claims)
    shown = tuple((str(scope), os.path.abspath(os.path.expanduser(os.fspath(path))))
                  for scope, path in claims[:_REQUEST_CLAIMS_SHOWN])
    text = ' '.join(''.join(char if char.isprintable() else ' '
                            for char in str(purpose or '')).split())
    if len(text) > _PURPOSE_CHARS:
        text = text[:_PURPOSE_CHARS - 1] + '…'
    return _LeaseRequest(shown, len(claims), text)


@contextlib.contextmanager
def _lease_request(claims, *, purpose=''):
    """Name this thread's outermost lease request for holder/waiter diagnostics.

    Nested public scopes re-enter keys the outer transaction already admitted,
    so the outermost request names them. Naming never decides admission.
    """
    state = _admission_request_state
    if getattr(state, 'request', None) is not None:
        yield
        return
    try:
        request = _describe_request(claims, purpose)
    except Exception as error:
        # Diagnostics never decide admission.
        from lib.log import get_logger
        get_logger(__name__).debug('[WorkspaceLease] request description dropped: %s', error)
        request = None
    state.request = request
    try:
        yield
    finally:
        state.request = None


def _path_label(path):
    """A path's basename, bounded and restricted to model-safe characters."""
    name = os.path.basename(path.rstrip(os.sep)) or os.sep
    return re.sub(r'[^\w .@+=,~-]', '?', name)[:96]


def _request_label(request, *, full_paths=False):
    """'scope:name,...' for a named request; '' when none was named."""
    if request is None or not request.claims:
        return ''
    parts = [f'{scope}:{path if full_paths else _path_label(path)}'
             for scope, path in request.claims]
    if request.total > len(request.claims):
        parts.append(f'+{request.total - len(request.claims)} more')
    return ','.join(parts)


def _purpose(holder):
    request = holder.get('request')
    return request.purpose if request is not None else ''


def _same_owner(holder, viewer):
    """Whether *holder*'s command summary may be shown to *viewer*.

    TODO(enterprise): owner identity is the task's ``_userId``; a holder of
    another owner is named only by its ids, lock and claim labels.
    """
    return holder.get('owner', '') == viewer.get('owner', '')


def _holder_mode_label(holder):
    mode = holder.get('mode', '')
    return 'queued-' + mode if holder.get('queued') and mode else mode


def _quoted(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


# A wait longer than this is invisible to every other subsystem: no process
# exists, no output arrives, and the round still looks alive. Log it once, then
# at a bounded interval so rotation cannot swallow a genuine 20-minute stall.
_ADMISSION_NOTICE_SECONDS = 2.0
_ADMISSION_NOTICE_INTERVAL_SECONDS = 60.0


def _log_admission_wait(root, mode, active, entry, waited_s, waiter):
    """Bounded diagnostic; never raises into admission or retains command text."""
    try:
        from lib.log import get_logger
        request = waiter.get('request')
        get_logger(__name__).warning(
            '[WorkspaceLease] wait %.0fs mode=%s root=%s waiter(pid=%s thread=%s '
            'thread_name=%s tool=%s task=%s conv=%s claims=%s cmd=%s)%s',
            waited_s, mode, root,
            waiter.get('pid'), waiter.get('thread'), waiter.get('thread_name') or '-',
            waiter.get('tool') or '-',
            waiter.get('task') or '-', waiter.get('conversation') or '-',
            _request_label(request, full_paths=True) or '-',
            _quoted(request.purpose) if request is not None and request.purpose else '-',
            _holder_summary(entry, active, mode))
    except Exception as error:
        # Diagnostics never decide admission.
        from lib.log import get_logger
        get_logger(__name__).debug('[WorkspaceLease] admission-wait notice dropped: %s', error)


def _blocking_owners(entry, active):
    """Every (mode, thread) hold that blocks under the caller's condition."""
    if entry['writer_thread'] is not None:
        return [(_WRITE, entry['writer_thread'])]
    if active == _READ:
        return [(_READ, thread) for thread in entry['readers']]
    if entry['file_writers']:
        return [(_FILE_BARRIER, thread) for thread in entry['file_writers']]
    return []


def _blocking_holders(entry, active, mode=None):
    """The ACTUAL blockers (at most three) under the caller's condition.

    A request held back by queue order is blocked by the earlier queued request
    it may not overtake, recomputed for the calling thread and its *mode*.
    """
    if entry is None:
        return []
    if active == _QUEUED:
        request = _queued_ahead(entry, threading.get_ident(), mode, time.monotonic())
        return [request] if request is not None else []
    return [holder for holder in (entry['holders'].get(key)
                                  for key in _blocking_owners(entry, active)[:3])
            if holder is not None]


def _holder_fields(holder, show_command):
    age = f'{max(0.0, time.monotonic() - holder["since"]):.1f}'
    fields = [f'pid={holder["pid"]}', f'thread={holder["thread"]}',
              f'queued_seconds={age}' if holder.get('queued') else f'held_seconds={age}']
    if holder.get('mode'):
        fields.append(f'mode={_holder_mode_label(holder)}')
    fields.extend(f'{name}={holder[name]}'
                  for name in ('thread_name', 'tool', 'task', 'conversation')
                  if holder.get(name))
    claims = _request_label(holder.get('request'), full_paths=True)
    if claims:
        fields.append(f'claims={claims}')
    purpose = _purpose(holder)
    if purpose and show_command:
        fields.append('cmd=' + _quoted(purpose))
    return ', '.join(fields)


def _holder_summary(entry, active, mode=None, viewer=None):
    """Render the ACTUAL blockers under the caller's condition; '' if unknown.

    ``viewer`` is the waiting identity when the text returns to a model; a
    blocker's command summary is then shown only to the blocker's owner.
    """
    details = [_holder_fields(holder, viewer is None or _same_owner(holder, viewer))
               for holder in _blocking_holders(entry, active, mode)]
    return (' [holder: ' + '; '.join(details) + ']') if details else ''

def _lease_domain(root):
    """(domain name, path) of a lock key for refusal messages."""
    if root.startswith('tree:'):
        return 'Workspace tree lifecycle', root[len('tree:'):]
    if root.startswith('git-metadata:'):
        return 'Git common-directory metadata', root[len('git-metadata:'):]
    return 'Workspace', root


def _busy(root, active, entry=None, mode=None, viewer=None):
    domain, root = _lease_domain(root)
    if active == _UNKNOWN:
        return WorkspaceBusyError(
            f'{domain} lease contention is active (holder type unknown): {root}')
    if active == _QUEUED:
        message = f'{domain} admission is queued behind an earlier conflicting request: {root}'
    else:
        message = f'{domain} {active} is active: {root}'
    # Called under the same condition as admission/release. Report actual
    # blockers, not the waiting caller or an invented cross-process owner.
    message += _holder_summary(entry, active, mode, viewer)
    return WorkspaceBusyError(message)


def _check_cancelled(root, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise WorkspaceLeaseCancelled(f'Workspace lease admission cancelled: {root}')


def _try_acquire(entry, thread_id, mode):
    """Attempt admission under entry['cond']; busy markers name ACTUAL blockers."""
    if entry['acquiring_thread'] is not None:
        return _UNKNOWN
    if entry['writer_thread'] == thread_id:
        if mode == _READ:
            entry['reader_reentry'] += 1
        else:
            entry['writer_depth'] += 1
        return 'enter'
    if entry['writer_thread'] is not None:
        return _WRITE
    if mode == _FILE_BARRIER:
        if entry['readers']:
            return _READ
    elif mode == _WRITE:
        if entry['file_writers']:
            return _WRITE
        if entry['readers'] and not (
                len(entry['readers']) == 1 and thread_id in entry['readers']):
            return _READ
    elif entry['file_writers']:
        return _WRITE
    # A thread already holding this key keeps its admission (an upgrading sole
    # reader, a nested file barrier); waiting behind a request that waits for
    # this very thread could not end.
    if (entry['queued'] and thread_id not in entry['readers']
            and thread_id not in entry['file_writers']
            and _queued_ahead(entry, thread_id, mode, time.monotonic()) is not None):
        return _QUEUED
    return 'acquire'


def _modes_conflict(first, second):
    return not (first == second and first in (_FILE_BARRIER, _READ))


def _conflicting_holds(entry, mode, thread_id):
    return [holder for (held, owner), holder in entry['holders'].items()
            if owner != thread_id and _modes_conflict(held, mode)]


def _queued_ahead(entry, thread_id, mode, now):
    """The earliest queued request a fresh *mode* request may not overtake.

    Called under entry['cond'] for a thread holding nothing on this key. An
    earlier queued request of a conflicting mode keeps precedence only while
    every holder blocking it is younger than _QUEUE_ORDER_MAX_HOLDER_AGE_SECONDS:
    behind an older holder it cannot be admitted anyway. Precedence follows
    strictly earlier queue times, so queued requests never hold each other back.
    """
    own = entry['queued'].get(thread_id)
    arrived = own['since'] if own is not None else now
    for owner, request in entry['queued'].items():
        if (owner == thread_id or request['since'] >= arrived
                or not _modes_conflict(request['mode'], mode)):
            continue
        if all(now - holder['since'] < _QUEUE_ORDER_MAX_HOLDER_AGE_SECONDS
               for holder in _conflicting_holds(entry, request['mode'], owner)):
            return request
    return None


def _publish_hold(entry, thread_id, mode, holder):
    # Held time starts at admission, not when this holder began its own wait.
    entry['holders'].setdefault((mode, thread_id),
                                {**holder, 'mode': mode, 'since': time.monotonic()})
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
    with _timed_registry():
        _active_descriptors.discard(fd)
        # No LOCK_UN: an inherited child FD must continue to exclude.
        os.close(fd)


def _acquire_process_lock(common, root, deadline, cancel_event, mode):
    global _opening_descriptors
    fd = None
    opening = False
    try:
        # Cross-process waits have no local holder entry: state that honestly
        # rather than reporting a fabricated blocker.
        waiter = _holder_identity(cancel_event)
        waited_since = next_notice = None
        try:
            with _timed_registry():
                _opening_descriptors += 1
                opening = True
            # Slow mkdir/stat/open on a network volume must not stop unrelated
            # admission, release, cancellation or registry inspection.
            with timing_phase('workspace_lock_io'):
                fd, fcntl = _open_lock(common, root)
        finally:
            if opening:
                # Cleanup must not itself enter fallible diagnostic callbacks.
                # A returned FD is tracked even if the timing finalizer raised;
                # the outer exception path then closes it before propagation.
                pending_error = sys.exc_info()[1]
                interrupted = None
                while True:
                    try:
                        _registry_lock.acquire()
                        break
                    except BaseException as error:
                        # Finish the one owned reservation before propagating
                        # interruption; leaving its count behind fences all forks.
                        if interrupted is None:
                            interrupted = error
                try:
                    try:
                        if fd is not None:
                            _active_descriptors.add(fd)
                    except BaseException:
                        if fd is not None:
                            _active_descriptors.discard(fd)
                            os.close(fd)
                            fd = None
                        raise
                    finally:
                        _opening_descriptors -= 1
                        _registry_condition.notify_all()
                finally:
                    _registry_lock.release()
                if interrupted is not None and pending_error is None:
                    raise interrupted
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
                _note_admission_blocked(root, mode, _UNKNOWN, None)
                now = time.monotonic()
                if waited_since is None:
                    waited_since = next_notice = now + _ADMISSION_NOTICE_SECONDS
                elif now >= next_notice:
                    next_notice = now + _ADMISSION_NOTICE_INTERVAL_SECONDS
                    _log_admission_wait(root, mode, _UNKNOWN, None,
                                        now - (waited_since - _ADMISSION_NOTICE_SECONDS),
                                        waiter)
                with timing_phase('workspace_flock_wait'):
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
    with _timed_registry():
        if any(other[0] == root and other != key for other in _registry):
            raise WorkspaceBusyError('Workspace metadata rebound during an active lease: ' + root)
        entry = _registry.setdefault(key, {
            'cond': threading.Condition(), 'users': 0,
            'writer_thread': None, 'writer_depth': 0,
            'readers': {}, 'reader_reentry': 0, 'file_writers': {},
            'fds': {}, 'acquiring_thread': None, 'holders': {},
            'queued': {},  # blocked requests in arrival order: thread -> identity
        })
        entry['users'] += 1
    pending = False
    fd = None
    snapshot = None
    waited_locally = False
    try:
        with entry['cond']:
            waited_since = next_notice = None
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
                    raise _busy(root, outcome, entry, mode, holder)
                waited_locally = True
                now = time.monotonic()
                entry['queued'].setdefault(
                    thread_id, {**holder, 'mode': mode, 'since': now, 'queued': True})
                _note_admission_blocked(root, mode, outcome, entry)
                if waited_since is None:
                    waited_since = next_notice = now + _ADMISSION_NOTICE_SECONDS
                elif now >= next_notice:
                    next_notice = now + _ADMISSION_NOTICE_INTERVAL_SECONDS
                    _log_admission_wait(root, mode, outcome, entry,
                                        now - (waited_since - _ADMISSION_NOTICE_SECONDS),
                                        holder)
                with timing_phase('workspace_local_wait'):
                    entry['cond'].wait(timeout=0.05 if remaining is None else min(0.05, remaining))
            entry['queued'].pop(thread_id, None)
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
            with _timed_registry():
                entry['fds'][_FILE_BARRIER if mode == _FILE_BARRIER else _WRITE] = fd
            entry['acquiring_thread'] = None
            entry['cond'].notify_all()
            fd = None  # transfer only after the publication transaction succeeded
            pending = False
        return entry, root, key, outcome
    except BaseException:
        with entry['cond']:
            entry['queued'].pop(thread_id, None)
            if fd is not None:
                with _timed_registry():
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
        with _timed_registry():
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


def _waiting_task(cancel_event):
    """The live task dict that owns this wait, or None for task-less callers."""
    from .tool_admission import cancellation
    for control in (cancel_event, cancellation()):
        task = getattr(control, 'task', None)
        if isinstance(task, dict):
            return task
    return None


class _AdmissionWaitRecord:
    """One acquisition's first blocked probe and its longest-observed blocker.

    A blocker is a published holder dict (stable for one hold, so identity marks
    a handoff) or ``None`` when no local holder is known: another process owns
    the flock, or another local thread is still acquiring it.
    """

    __slots__ = ('started', 'root', 'mode', 'request', '_holder', '_first', '_last',
                 'dominant', 'dominant_last', '_dominant_span')

    def __init__(self, started, root, mode, holder, request=None):
        self.started, self.root, self.mode, self.request = started, root, mode, request
        self._holder, self._first, self._last = holder, started, started
        self.dominant, self.dominant_last, self._dominant_span = None, started, -1.0

    def observe(self, now, holder):
        if holder is not self._holder:
            self._settle()
            self._holder, self._first = holder, now
        self._last = now

    def _settle(self):
        span = self._last - self._first
        if span > self._dominant_span:
            self.dominant, self.dominant_last, self._dominant_span = (
                self._holder, self._last, span)

    def finish(self):
        self._settle()


def _note_admission_blocked(root, mode, active, entry):
    """Record one blocked probe of this thread's acquisition as wait evidence.

    Local waits call this under the entry condition, so the blockers read are
    current; cross-process flock waits pass no entry (their holder is unknowable
    here). Only an armed thread (observer installed, task present) records.
    """
    state = _admission_wait_state
    if not getattr(state, 'armed', False):
        return
    holders = _blocking_holders(entry, active, mode)
    holder = holders[0] if holders else None
    now = time.monotonic()
    if state.record is None:
        state.record = _AdmissionWaitRecord(
            now, root, mode, holder, getattr(_admission_request_state, 'request', None))
    else:
        state.record.observe(now, holder)


class _AdmissionPublication:
    """One acquisition in progress on a task."""

    __slots__ = ('since',)

    def __init__(self, since):
        self.since = since


# Every acquisition in progress, per waiting task: id(task) -> (task, the value
# the task held before the first began, its acquisitions oldest first). Parallel
# calls of one task share its dict and end in any order, so the marker is
# derived from the acquisitions still in progress, never restored LIFO.
_admission_publications_lock = threading.Lock()
_admission_publications = {}
_NO_MARKER = object()


def _open_admission_publication(task):
    from .tool_admission import WORKSPACE_ADMISSION_KEY
    publication = _AdmissionPublication(time.monotonic())
    with _admission_publications_lock:
        entry = _admission_publications.setdefault(
            id(task), (task, task.get(WORKSPACE_ADMISSION_KEY, _NO_MARKER), []))
        entry[2].append(publication)
        _show_admission_marker(task)
    return publication


def _close_admission_publication(task, publication):
    from .tool_admission import WORKSPACE_ADMISSION_KEY
    with _admission_publications_lock:
        _task, original, publications = _admission_publications[id(task)]
        publications.remove(publication)
        if publications:
            _show_admission_marker(task)
            return
        del _admission_publications[id(task)]
        if original is _NO_MARKER:
            task.pop(WORKSPACE_ADMISSION_KEY, None)
        else:
            task[WORKSPACE_ADMISSION_KEY] = original


def _show_admission_marker(task):
    """Replace (never mutate) the task's marker; caller holds the lock.

    Readers on other threads hold a stable snapshot of the oldest acquisition
    still in progress.
    """
    from .tool_admission import WORKSPACE_ADMISSION_KEY
    publications = _admission_publications[id(task)][2]
    marker = {'since': publications[0].since, 'depth': len(publications)}
    if task.get(WORKSPACE_ADMISSION_KEY) != marker:
        task[WORKSPACE_ADMISSION_KEY] = marker


_LOCK_KEY_DOMAINS = ('tree:', 'git-metadata:', 'runtime-setup:')


def _lock_root_label(root):
    """(domain, basename) of a lock key; full paths never leave this module."""
    for prefix in _LOCK_KEY_DOMAINS:
        if root.startswith(prefix):
            domain, path = prefix[:-1], root[len(prefix):]
            break
    else:
        domain, path = 'workspace', root
    return domain, os.path.basename(path.rstrip(os.sep)) or os.sep


def _waiter_call(cancel_event):
    """(toolCallId, roundNum) of the waiting tool occurrence, when one is bound."""
    from .tool_admission import cancellation
    for control in (cancel_event, cancellation()):
        runtime = getattr(control, 'runtime_context', None)
        call_id = getattr(runtime, 'tool_call_id', None)
        if isinstance(call_id, str) and 0 < len(call_id) <= 256:
            round_num = getattr(runtime, 'round_num', None)
            return call_id, round_num if type(round_num) is int else None
    return '', None


def _report_admission_wait(task, cancel_event, record, outcome, ended):
    observer = _admission_wait_observer
    if observer is None:
        return
    try:
        record.finish()
        holder = record.dominant or {}
        domain, root = _lock_root_label(record.root)
        call_id, round_num = _waiter_call(cancel_event)
        waiter = _holder_identity(cancel_event)
        observer(task, AdmissionWait(
            started_monotonic=record.started,
            ended_monotonic=max(record.started, ended),
            mode=record.mode, lock_domain=domain, lock_root=root, outcome=outcome,
            waiter_tool=waiter['tool'],
            tool_call_id=call_id, round_num=round_num,
            holder_tool=holder.get('tool', ''), holder_task=holder.get('task', ''),
            holder_conversation=holder.get('conversation', ''),
            holder_held_ms=(max(0, int((record.dominant_last - holder['since']) * 1000))
                            if holder else None),
            waiter_claims=_request_label(record.request),
            holder_mode=_holder_mode_label(holder),
            holder_claims=_request_label(holder.get('request')),
            holder_command=_purpose(holder) if holder and _same_owner(holder, waiter) else ''))
    except Exception:
        # The observer contract forbids raising; evidence never decides admission.
        from lib.log import get_logger
        get_logger(__name__).warning('[WorkspaceLease] admission-wait evidence dropped',
                                     exc_info=True)


class _AdmittedWaitReport:
    """An admitted wait's evidence, sent only once the caller owns the release.

    The acquisition has already ended (its instant is captured); only delivery
    is deferred, so an observer can never run while an acquired lease has no
    release owner.
    """

    __slots__ = ('_pending',)

    def __init__(self):
        self._pending = None

    def send(self):
        pending, self._pending = self._pending, None
        if pending is not None:
            _report_admission_wait(*pending)


@contextlib.contextmanager
def _publish_admission_wait(cancel_event):
    """Mark a task as WAITING for admission for exactly the acquisition span.

    The wait produces no process and no output, so every silence-based liveness
    decision would otherwise read it as a wedge
    (``lib.tasks_pkg.explained_wait`` owns that classification). Publishing it
    here, the wait owner, covers every task-bound internal caller (dependency
    setup, integration snapshots); a caller-side copy would cover only one.

    The same owner records timing evidence: an acquisition whose first probe was
    blocked is reported once to the admission-wait observer
    (``set_admission_wait_observer``). A refused or cancelled acquisition holds
    nothing and is reported here; an admitted one is handed back through the
    yielded ``_AdmittedWaitReport``, which ``_lease`` sends inside the scope that
    owns the release. An immediate admission records nothing.
    """
    admitted = _AdmittedWaitReport()
    task = _waiting_task(cancel_event)
    if task is None:
        yield admitted
        return
    publication = _open_admission_publication(task)
    state = _admission_wait_state
    outer = (getattr(state, 'armed', False), getattr(state, 'record', None))
    state.armed, state.record = _admission_wait_observer is not None, None
    outcome = 'error'
    try:
        yield admitted
        outcome = 'admitted'
    except WorkspaceLeaseCancelled:
        outcome = 'cancelled'
        raise
    except WorkspaceBusyError:
        outcome = 'refused'
        raise
    finally:
        record = state.record
        state.armed, state.record = outer
        _close_admission_publication(task, publication)
        if record is not None:
            pending = (task, cancel_event, record, outcome, time.monotonic())
            if outcome == 'admitted':
                admitted._pending = pending
            else:
                _report_admission_wait(*pending)


@contextlib.contextmanager
def _lease(path, mode, timeout, cancel_event, _identity=None, _binding=None,
           _validation_path=None):
    timeout = _validated_timeout(timeout)
    owner_pid = os.getpid()
    with _publish_admission_wait(cancel_event) as admitted_wait:
        entry, root, key, action = _enter(path, mode, timeout, cancel_event, _identity,
                                         _binding, _validation_path)
    if entry is None:  # non-Git path: no-op
        admitted_wait.send()
        yield
        return
    thread_id = threading.get_ident()
    state = _lease_scope_state
    state.depth = getattr(state, 'depth', 0) + 1
    try:
        admitted_wait.send()
        yield
    finally:
        # At-fork cleanup already closed inherited FDs. Their numbers may now
        # name child-owned files, and inherited Conditions may be locked.
        if os.getpid() == owner_pid:
            with entry['cond']:
                _release(entry, thread_id, mode, action)
                entry['cond'].notify_all()
            with _timed_registry():
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
        with _timed_registry():
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
        raise WorkspaceLeaseNamespaceError('Internal workspace lease namespace cannot be mutated: ' + target)


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
    with _lease_request((('root', path),)), _checkout_fence(path, deadline, cancel_event) as chain:
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

    with _lease_request((('file', path),)), _checkout_fence(target, deadline, cancel_event) as chain:
        if not chain or not is_source_workspace_path(target):
            with _plain_file_transaction(lexical, target, deadline, cancel_event, chain):
                yield
            return
        binding = chain[-1]
        root, common = binding[:2]
        if target in (os.path.realpath(os.path.join(root, '.git')),
                      os.path.join(binding[2], 'commondir')):
            raise WorkspaceBusyError('Git workspace identity metadata cannot be rebound by a file lease: ' + target)
        with _timed_registry():
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
    with _lease_request((('runtime-setup', path),)), \
            _checkout_fence(path, deadline, cancel_event) as chain:
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
    with _lease_request((('metadata', path),)), \
            _checkout_fence(path, deadline, cancel_event) as chain:
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
        stack.enter_context(_lease_request((('tree', path),)))
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


def _physical_claim_order(item):
    return item[0], str(item[3][1]), item[1]


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
    ordered = sorted(physical.values(), key=_physical_claim_order)
    return ordered, evidence


@contextlib.contextmanager
def workspace_claims_lease(claims, *, timeout=0.0, cancel_event=None, purpose=''):
    """Atomically admit a complete ordered multi-target effect set.

    This is the batching boundary for Tofu's own multi-target transactions
    (delivery, publication, reclamation). The existing registry/flocks still
    own every descriptor. Public single-target scopes remain usable;
    multi-target callers must not acquire their hidden barriers one logical
    scope at a time.

    ``purpose`` (a bounded one-line summary) only names the request in wait
    diagnostics; it never changes admission.
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
    with contextlib.ExitStack() as lifetime, contextlib.ExitStack() as stack:
        lifetime.enter_context(_lease_request(claims, purpose=purpose))
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
            stack.enter_context(apis[scope](
                path, timeout=_wait_remaining(deadline), cancel_event=cancel_event))
        yield
