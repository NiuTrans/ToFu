"""Per-file in-process mutex for one write tool call's read-modify-write.

Entry point: ``file_edit_mutex(paths)``, a context manager that holds one
mutex per resolved path for a single call's preimage read, edit and
publication, so two concurrent edits of one file cannot lose an update. It
never spans a command or another tool call and is not a workspace lease:
shell commands, other processes and external editors are not coordinated
(``docs/runbooks/GIT_WORKSPACE_GUARD.md``, "Tool calls take no workspace
leases"). Write freshness still refuses a stale observed version.

Paths are acquired in sorted order, and a thread may re-enter a path it
holds. Entries exist only while a call holds or awaits them, so the table is
bounded by live calls. Dependencies: the standard library only.
"""
from __future__ import annotations

import contextlib
import threading

_table_lock = threading.Lock()
_entries: dict[str, list] = {}  # resolved path -> [RLock, holders + waiters]


def _leave(path, entry):
    with _table_lock:
        entry[1] -= 1
        if entry[1] == 0 and _entries.get(path) is entry:
            del _entries[path]


@contextlib.contextmanager
def file_edit_mutex(paths):
    """Hold every resolved path's mutex for the body; release in reverse order."""
    held = []
    try:
        for path in sorted(set(paths)):
            with _table_lock:
                entry = _entries.setdefault(path, [threading.RLock(), 0])
                entry[1] += 1
            try:
                entry[0].acquire()
            except BaseException:
                _leave(path, entry)
                raise
            held.append((path, entry))
        yield
    finally:
        for path, entry in reversed(held):
            entry[0].release()
            _leave(path, entry)
