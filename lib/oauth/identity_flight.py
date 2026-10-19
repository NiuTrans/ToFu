"""Exact account-identity mutual exclusion with active-file reclamation.

identity_flight protects refresh/redemption across threads and POSIX processes.
One persistent guard protects only inode creation/reclamation; upstream work
holds an identity-specific advisory lock. Waiters never retain stale inodes.
Crash-orphan files are reclaimed under the guard after a nonblocking lock probe.
No clock lease, stripe collision or historical lock-map growth is introduced.
"""
from contextlib import contextmanager
import errno
import os
import re
import threading
import time

from lib.weak_lock_pool import WeakLockPool

try:
    import fcntl
except ImportError:  # preserve the existing platform's process-local behavior
    fcntl = None

_identity_locks = WeakLockPool(threading.Lock)
_guard_locks = WeakLockPool(threading.Lock)
_fd_guard = threading.Lock()
_fds = set()


def _open(path, *, create=False):
    flags = os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    if create:flags |= os.O_CREAT
    with _fd_guard:
        fd = os.open(path, flags, 0o600)
        _fds.add(fd)
        return fd


def _close(fd):
    with _fd_guard:
        try:os.close(fd)
        finally:_fds.discard(fd)


def _after_fork():
    global _fds, _fd_guard, _identity_locks, _guard_locks
    # Closing inherited descriptors must not LOCK_UN the parent's shared OFD.
    for fd in _fds:
        try:os.close(fd)
        except OSError:pass
    _fds = set()
    _fd_guard = threading.Lock()
    _identity_locks = WeakLockPool(threading.Lock)
    _guard_locks = WeakLockPool(threading.Lock)


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=lambda:_fd_guard.acquire(),
                       after_in_parent=lambda:_fd_guard.release(),
                       after_in_child=_after_fork)


def _try_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):return False
        raise  # unsupported/failed locking cannot authorize a spend


@contextmanager
def _registry(path):
    with _guard_locks.lock_for(path):
        fd = _open(path, create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:_close(fd)


def _reap(parent, prefix):
    # All cooperating processes create/open/unlink active files under the guard.
    # A held file cannot be reclaimed; a dead process's flock releases in-kernel.
    with os.scandir(parent) as entries:
        for entry in entries:
            if not entry.name.startswith(prefix):continue
            key = entry.name[len(prefix):]
            if re.fullmatch('[0-9a-f]{24}', key) is None:continue
            try:fd = _open(entry.path)
            except FileNotFoundError:continue
            try:
                if _try_lock(fd):os.unlink(entry.path)
            finally:_close(fd)


@contextmanager
def identity_flight(cache_path: str, cache_key: str):
    if re.fullmatch('[0-9a-f]{24}', cache_key) is None:
        raise ValueError('account identity flight key must be an opaque fingerprint')
    scope = os.path.abspath(cache_path)
    with _identity_locks.lock_for((scope, cache_key)):
        if fcntl is None:
            yield
            return
        parent = os.path.dirname(scope)
        os.makedirs(parent, exist_ok=True)
        guard = scope + '.flight.guard'
        prefix = os.path.basename(scope) + '.flight.active.'
        path = scope + '.flight.active.' + cache_key
        acquired = None
        while acquired is None:
            with _registry(guard):
                _reap(parent, prefix)
                fd = _open(path, create=True)
                try:
                    if _try_lock(fd):acquired = fd
                    else:_close(fd)
                except BaseException:
                    _close(fd)
                    raise
            if acquired is None:
                time.sleep(0.01)  # waits only for this exact identity's owner
        owner_pid = os.getpid()
        try:
            yield
        finally:
            if os.getpid() == owner_pid:
                try:
                    with _registry(guard):
                        os.unlink(path)
                finally:_close(acquired)
