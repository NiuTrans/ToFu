"""Pytest processes yield CPU and I/O to the live server and its storage engine.

Entry point: ``lower_own_priority()``. ``tests/conftest.py`` calls it first,
before its module-level work, so every pytest controller and xdist worker that
repository tooling starts (direct pytest, Make, ``scripts/verify.py``, landing
verification) runs each thread at the lowest CPU priority (nice 19) in the idle
I/O class. Children inherit both when they fork: xdist workers and the
TofuDB/PostgreSQL engines, Node processes and browsers that tests start.
Lowering needs no privilege and addresses only this process's own threads,
never a process group or user, so a pytest launched from the live server leaves
the server, its execution host and production TofuDB unchanged.

Linux keeps both priorities per thread, and a new thread copies them from the
thread that creates it. Dependencies: the standard library and psutil (the
I/O priority system call).
Decision: docs/TESTING_STRATEGY.md, "Test processes yield to the live server".
"""
from __future__ import annotations

import os
import sys

# The POSIX nice range ends at 19, the lowest scheduling priority.
LOWEST_NICE = 19


def lower_own_priority() -> None:
    """Move every thread of this process to nice 19 and the idle I/O class."""
    if sys.platform.startswith('linux'):
        _lower_every_thread()
    elif hasattr(os, 'setpriority'):
        # Other POSIX systems keep one nice value per process.
        os.setpriority(os.PRIO_PROCESS, 0, LOWEST_NICE)


def _lower_every_thread() -> None:
    import psutil

    lowered: set[int] = set()
    # A thread created during a pass copies its creator's earlier priority, so
    # list again until a pass finds no thread that has not been lowered.
    while pending := {int(tid) for tid in os.listdir('/proc/self/task')} - lowered:
        for tid in pending:
            try:
                psutil.Process(tid).ionice(psutil.IOPRIO_CLASS_IDLE)
                os.setpriority(os.PRIO_PROCESS, tid, LOWEST_NICE)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass  # the thread exited
        lowered |= pending
