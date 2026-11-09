"""Retire only the recorded processes and IPC of an owned PostgreSQL test root.

Entry point: retire_cluster_root. The pytest startup reclaimer imports this
stdlib-only owner without loading cluster fixtures, initdb or clock compilation.
Contract: docs/TESTING_STRATEGY.md, Private PostgreSQL cluster lifetime.
"""
from pathlib import Path
import ctypes
import errno
import os
import signal
import sys

_LINUX = sys.platform.startswith("linux")
# <sys/ipc.h>.
_IPC_RMID = 0
# Zero-based postmaster.pid lines (PostgreSQL src/include/utils/pidfile.h).
_LOCK_FILE_LINE_PID = 0
_LOCK_FILE_LINE_DATA_DIR = 1
_LOCK_FILE_LINE_SHMEM_KEY = 6


def _serves(pid: int, data: str) -> bool:
    """True while ``pid`` is a live process of the cluster in ``data``.

    PostgreSQL processes run in their data directory. A zombie or a missing
    process has no working directory; another user's process reusing the PID
    is not this user's cluster.
    """
    try:
        return os.path.samefile(f"/proc/{pid}/cwd", data)
    except (FileNotFoundError, PermissionError):
        return False


def _sysv_segment(shmid: int):
    with open("/proc/sysvipc/shm", encoding="ascii") as table:
        header = table.readline().split()
        for line in table:
            row = dict(zip(header, map(int, line.split())))
            if row["shmid"] == shmid:
                return row
    return None


def _remove_recorded_segment(key: int, shmid: int, creator: int) -> bool:
    """Remove a dead cluster's recorded interlock; True when none remains."""
    row = _sysv_segment(shmid)
    if (row is None or (row["key"] & 0xFFFFFFFF) != (key & 0xFFFFFFFF)
            or row["cpid"] != creator):
        # Already removed, or the identifier now names another segment.
        return True
    if row["uid"] != os.getuid() or row["cuid"] != os.getuid() or row["nattch"]:
        # Unproven ownership, or orphaned cluster processes still attached.
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.shmctl(shmid, _IPC_RMID, None) != 0:
        failure = ctypes.get_errno()
        if failure not in (errno.EINVAL, errno.EIDRM):
            raise OSError(failure, os.strerror(failure))
    return True


def retire_cluster_root(root: Path) -> bool:
    """Release what a cluster root still holds; True once ``root`` may be removed.

    A process still serving the recorded data directory is asked for a fast
    shutdown and the root is kept for a later reclaim. Otherwise the SysV
    segment recorded in postmaster.pid is removed only while it carries the
    recorded key and creator, belongs to this user and has nothing attached.
    A record that cannot be read or proven keeps the root.
    """
    try:
        lines = (root / "data" / "postmaster.pid").read_text().splitlines()
    except FileNotFoundError:
        # Never started, or a clean shutdown already released everything.
        return True
    if not _LINUX or len(lines) <= _LOCK_FILE_LINE_DATA_DIR:
        return False
    try:
        # A standalone (initdb bootstrap) backend records its PID negated.
        pid = abs(int(lines[_LOCK_FILE_LINE_PID]))
    except ValueError:
        return False
    if _serves(pid, lines[_LOCK_FILE_LINE_DATA_DIR]):
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass  # It exited after the check; the next reclaim proves the rest.
        return False
    segment = (lines[_LOCK_FILE_LINE_SHMEM_KEY].split()
               if len(lines) > _LOCK_FILE_LINE_SHMEM_KEY else [])
    if not segment:
        # The process exited before it created shared memory.
        return True
    try:
        key, shmid = (int(field) for field in segment)
    except ValueError:
        return False
    return _remove_recorded_segment(key, shmid, pid)
