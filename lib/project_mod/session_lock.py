"""Bounded reentrant per-session journal lock shared by recovery and writers.

Entry point: locked_session(session_dir). Identity is an explicit storage path.
Only currently held locks live in thread-local state; no persistent lock cache.
Acquire this lock before the modification cache lock. Nested same-thread use
shares the outer descriptor; other threads/processes wait at most five seconds.
"""
from contextlib import contextmanager
import os
import stat
import threading
import time

from lib.project_mod import post_commit
from lib.tools.execution_timing import timing_phase

_held = threading.local()
_descriptor_guard = threading.Lock()
_active_descriptors = set()


def _after_fork_child():
    """Drop inherited ownership without unlocking the parent's file description."""
    global _held
    try:
        for descriptor in _active_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _active_descriptors.clear()
        _held = threading.local()
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

    def __init__(self, waited_seconds):
        self.waited_seconds = waited_seconds
        super().__init__(
            f'Project journal lock is busy after {waited_seconds:.2f}s; '
            'no file mutation was performed. Wait for the active project operation, '
            'then re-read the file and recompute the edit before retrying.')


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


@contextmanager
def locked_session(session_dir):
    import fcntl
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
            deadline = started + _wait_seconds()
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SessionLockBusy(time.monotonic() - started)
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        current = os.stat(os.path.join(directory, '.journal.lock'), follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SessionLockError('Project journal lock changed during admission')
        state = held[directory] = {'afterRelease': callbacks}
        try:
            yield state
        finally:
            del held[directory]
    finally:
        # A child's at-fork callback already closed the inherited descriptor.
        # Its number may now name an unrelated child-owned file: never close it
        # when the child unwinds a context inherited from the parent stack.
        if os.getpid() == owner_pid:
            with _descriptor_guard:
                _active_descriptors.discard(descriptor)
                os.close(descriptor)
            post_commit.publish(callbacks)
