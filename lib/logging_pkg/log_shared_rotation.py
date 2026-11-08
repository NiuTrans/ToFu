"""Rotating log files that several processes of one deployment write.

Responsibility
--------------
The API host (``server.py``) and the resident execution host
(``serverctl_pkg.execution_host``) each build the application logging runtime,
so each process holds its own handler on the same ``logs/`` files.
``SharedRotationMixin`` keeps those writers consistent across rollovers: no
process writes through a descriptor whose file another process renamed away,
and every writer adopts one rollover instead of repeating it.

Every record and every rollover runs under an exclusive ``flock`` on the file
currently at the handler's path. Holding it, a writer compares its descriptor
with the path and reopens the path when a peer has rotated or removed the
file. A rollover keeps the outgoing file locked until its successor is open,
so a peer waiting on the old file then finds the replacement. On the BeeGFS
FUSE mount that holds ``logs/``, a write through a renamed descriptor shares
its O_APPEND position with the new file at the old path: both files then gain
NUL holes and lose lines (docs/LOGGING.md, "Shared rotating files").

Entry point: ``SharedRotationMixin``, listed before a ``BaseRotatingHandler``
subclass. Dependencies: standard library only. Platforms without ``fcntl``
keep the base handler behaviour.
"""

from __future__ import annotations

import logging
import os
import time

try:
    import fcntl
except ImportError:
    fcntl = None


class SharedRotationMixin:
    """Coordinate one rotating file with the other processes that write it."""

    _shared_emit_depth = 0

    def emit(self, record):
        if fcntl is None:
            super().emit(record)
            return
        if self._shared_emit_depth:
            # Logged while this handler already holds its file (a retention
            # notice raised by the rollover itself): append, never roll over.
            try:
                logging.FileHandler.emit(self, record)
            except Exception:
                self.handleError(record)
            return
        self._shared_emit_depth += 1
        try:
            self._lock_current_file()
            try:
                if self.shouldRollover(record):
                    self._roll_over_locked()
                logging.FileHandler.emit(self, record)
            finally:
                if self.stream is not None:
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        except Exception:
            self.handleError(record)
        finally:
            self._shared_emit_depth -= 1

    def _lock_current_file(self):
        while True:
            if self.stream is None:
                self.stream = self._open()
            descriptor = self.stream.fileno()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            opened = os.fstat(descriptor)
            try:
                current = os.stat(self.baseFilename)
            except FileNotFoundError:
                current = None
            if current is not None and (
                    current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                # Size decisions count every writer's bytes, not only this
                # descriptor's last write.
                self.stream.seek(0, os.SEEK_END)
                return
            self.stream.close()
            self.stream = None
            if hasattr(self, 'computeRollover'):
                # A peer rolled this period over; adopt it instead of repeating it.
                self.rolloverAt = self.computeRollover(int(time.time()))

    def _roll_over_locked(self):
        guard = os.dup(self.stream.fileno())
        try:
            self.doRollover()
        finally:
            os.close(guard)
        self._lock_current_file()


__all__ = ['SharedRotationMixin']
