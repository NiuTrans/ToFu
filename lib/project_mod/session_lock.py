"""Bounded reentrant per-session journal lock shared by recovery and writers.

Entry point: locked_session(session_dir). Identity is an explicit storage path.
Only currently held locks live in thread-local state; no persistent lock cache.
Acquire this lock before the modification cache lock. Nested same-thread use
shares the outer descriptor; other threads/processes wait at most the write
intent contract's lockWaitSeconds. Read-only history projections never take it.
"""
from contextlib import contextmanager
import os
import stat
import threading
import time

from lib.project_mod import post_commit
from lib.project_mod.tool_admission import cancellation
from lib.tools.execution_timing import timing_phase

_held = threading.local()
_descriptor_guard = threading.Lock()
_active_descriptors = set()
# abs session dir -> {thread ident: (thread name, tool name, task id, monotonic
# admission time)}. Diagnostics only: it never decides admission, and it is
# cleared at the last departure.
_holders = {}
# abs session dir -> wake-up events of this process's waiters, oldest first.
# Only the oldest waiter competes for the cross-process flock, so a later
# arrival never takes an earlier waiter's turn. An entry exists only while a
# waiter does.
_waiting = {}


def _operation():
    """(tool name, task id) of the project tool call running on this thread."""
    control = cancellation()
    if control is None:
        return None, None
    task = control.task if isinstance(control.task, dict) else {}
    return control.tool_name or None, task.get('id')


def _record_holder(directory, present):
    evidence = ((threading.current_thread().name, *_operation(), time.monotonic())
                if present else None)
    with _descriptor_guard:
        owned = _holders.setdefault(directory, {}) if present else _holders.get(directory)
        if owned is None:
            return
        if present:
            owned[threading.get_ident()] = evidence
        else:
            owned.pop(threading.get_ident(), None)
            if not owned:
                _holders.pop(directory, None)


def _holder_descriptions(directory):
    """Name each local holder by its tool call, not only its thread.

    Every task's tool pool names its first worker ``tool-parallel_0``, so a
    thread name alone cannot tell a sibling call from another conversation.
    """
    with _descriptor_guard:
        owned = dict(_holders.get(directory, {}))
    owned.pop(threading.get_ident(), None)
    _tool, own_task = _operation()
    now = time.monotonic()
    descriptions = []
    for _ident, (name, tool, task, since) in sorted(owned.items()):
        if tool is None:
            holder = f'thread {name} outside a project tool call'
        else:
            relation = ''
            if task is not None and own_task is not None:
                relation = ' in this task' if task == own_task else ' in another task'
            holder = f'{tool}{relation} (thread {name})'
        descriptions.append(f'{holder} for {now - since:.2f}s')
    return descriptions


def _after_fork_child():
    """Drop inherited ownership without unlocking the parent's file description.

    Parent threads do not exist in the child, so their places in line and their
    holder evidence are dropped too.
    """
    global _held
    try:
        for descriptor in _active_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _active_descriptors.clear()
        _held = threading.local()
        _waiting.clear()
        _holders.clear()
    finally:
        _descriptor_guard.release()


if hasattr(os, 'register_at_fork'):
    # Serialize fork with descriptor creation/removal. This includes locks held
    # by other threads, whose thread-local state is inaccessible in the child.
    os.register_at_fork(before=_descriptor_guard.acquire,
                        after_in_parent=_descriptor_guard.release,
                        after_in_child=_after_fork_child)


class SessionLockError(OSError):
    """Admission failed before a session mutation could start."""


class SessionLockBusy(SessionLockError):
    """Transient admission failure, distinct from unsafe lock identity/storage."""

    def __init__(self, waited_seconds, session_dir=None, holders=(), waiting_ahead=0):
        self.waited_seconds = waited_seconds
        self.session_dir = session_dir
        self.holders = tuple(holders)
        self.waiting_ahead = waiting_ahead
        scope = (f' Shared by every writer of this project journal ({session_dir}),'
                 ' not by the edited file.' if session_dir else '')
        holder = (' Held by ' + '; '.join(self.holders) + '.'
                  if self.holders else
                  ' No other thread in this process holds it, so another process '
                  'is committing.')
        line = ''
        if waiting_ahead == 1:
            line = ' 1 earlier operation of this process is still waiting ahead of this call.'
        elif waiting_ahead:
            line = (f' {waiting_ahead} earlier operations of this process are still '
                    'waiting ahead of this call.')
        super().__init__(
            f'Project journal lock is busy after {waited_seconds:.2f}s; '
            'no file mutation was performed. Wait for the active project operation, '
            'then re-read the file and recompute the edit before retrying.'
            + scope + holder + line)


def session_state(session_dir):
    """Return only this thread's currently owned admission, never cached ownership."""
    return getattr(_held, 'sessions', {}).get(os.path.abspath(session_dir))


def after_session_release(session_dir, callback, *, key=None):
    """Order optional projections under admission; execute only after release.

    Same-file notifications in this process cannot overtake earlier commits;
    independent files drain independently. This is not a cross-process fence
    for durable history. Reentrant callbacks follow the current notification.
    """
    state = session_state(session_dir)
    if state is None:
        raise RuntimeError('Post-commit effects require an active journal admission')
    ticket = post_commit.register(os.path.abspath(session_dir), key, callback)
    state['afterRelease'].append(ticket)


def _wait_seconds():
    from lib.project_mod.write_intents import _limit
    return _limit('lockWaitSeconds')


def _admit(descriptor, directory, started, deadline):
    """Take the exclusive flock in this process's arrival order, or raise busy.

    A release or an earlier waiter's departure wakes the next waiter at once;
    only a holder in another process is polled, by the oldest waiter alone.
    """
    import fcntl
    turn = threading.Event()
    with _descriptor_guard:
        _waiting.setdefault(directory, []).append(turn)
    try:
        while True:
            with _descriptor_guard:
                first = _waiting[directory][0] is turn
                local_holder = directory in _holders
            if first:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with _descriptor_guard:
                    ahead = _waiting[directory].index(turn)
                raise SessionLockBusy(time.monotonic() - started, directory,
                                      _holder_descriptions(directory), ahead)
            turn.wait(min(0.01, remaining) if first and not local_holder else remaining)
            turn.clear()
    finally:
        with _descriptor_guard:
            line = _waiting[directory]
            line.remove(turn)
            if line:
                line[0].set()
            else:
                del _waiting[directory]


@contextmanager
def locked_session(session_dir):
    """Hold the project journal admission; waiters are admitted first come, first served.

    Waiting is bounded by the write-intent contract's ``lockWaitSeconds``. A
    later arrival in this process never overtakes an earlier waiter, so the
    wait covers only operations already ahead in line, and a refusal names the
    holder's tool call, how long it has held the admission and the line ahead.
    """
    directory = os.path.abspath(session_dir)
    held = getattr(_held, 'sessions', None)
    if held is None:
        held = _held.sessions = {}
    if directory in held:
        yield held[directory]
        return
    owner_pid = os.getpid()
    callbacks = []
    started = time.monotonic()
    with _descriptor_guard:
        descriptor = os.open(os.path.join(directory, '.journal.lock'),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600)
        _active_descriptors.add(descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SessionLockError('Journal lock is not a regular file')
        with timing_phase('undo_journal_lock_wait'):
            _admit(descriptor, directory, started, started + _wait_seconds())
        current = os.stat(os.path.join(directory, '.journal.lock'), follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SessionLockError('Project journal lock changed during admission')
        state = held[directory] = {'afterRelease': callbacks}
        _record_holder(directory, True)
        try:
            yield state
        finally:
            _record_holder(directory, False)
            del held[directory]
    finally:
        # A child's at-fork callback already closed the inherited descriptor.
        # Its number may now name an unrelated child-owned file: never close it
        # when the child unwinds a context inherited from the parent stack.
        if os.getpid() == owner_pid:
            with _descriptor_guard:
                _active_descriptors.discard(descriptor)
                os.close(descriptor)
                line = _waiting.get(directory)
                if line:
                    line[0].set()
            post_commit.publish(callbacks)
