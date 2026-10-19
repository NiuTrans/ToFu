"""Retire an explicitly owned POSIX group before releasing inherited resources.

No daemon, pool, subprocess or persistent state is added. Callers validate the
leader/record identity and own direct-child reaping. Linux scans establish owned
writer settlement or cancellation; zombies have closed descriptors and are quiescent.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Callable


class ProcessGroupRetirementError(RuntimeError):
    """The owned group could not be proven quiescent before the cleanup bound."""


def descendant_pids(root_pid: int) -> list[int]:
    """Discover only an owned Linux subtree, including children of all threads.

    Reading kernel child lists avoids a whole-host /proc scan per timed-out
    search. Vanished processes are normal; other observation failures remain
    visible to the caller. No process is signalled by this read-only helper.
    """
    found = []
    seen = {root_pid}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        try:
            with os.scandir(f'/proc/{parent}/task') as tasks:
                thread_ids = [entry.name for entry in tasks if entry.name.isdecimal()]
        except (FileNotFoundError, ProcessLookupError):
            continue
        for thread_id in thread_ids:
            try:
                children = Path(f'/proc/{parent}/task/{thread_id}/children').read_text().split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            for value in children:
                pid = int(value)
                if pid > 1 and pid not in seen:
                    seen.add(pid)
                    found.append(pid)
                    pending.append(pid)
    return found


def read_process_fields(pid: int) -> list[bytes]:
    """Linux stat fields after the final comm delimiter (comm may contain spaces)."""
    raw = Path(f'/proc/{int(pid)}/stat').read_bytes()
    if b')' not in raw:
        raise ProcessGroupRetirementError('Cannot parse process identity')
    fields = raw.rsplit(b')', 1)[-1].split()
    if len(fields) <= 19:
        raise ProcessGroupRetirementError('Incomplete process identity')
    return fields


def group_has_live_members(pgid: int) -> bool:
    """Observe actual group members, not the lifetime of its reaped leader."""
    if sys.platform.startswith('linux'):
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():
                continue
            try:
                if os.getpgid(int(path.name)) != pgid:
                    continue
                fields = read_process_fields(int(path.name))
                if int(fields[2]) == pgid and fields[0] not in (b'Z', b'X'):
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue  # A member disappeared between discovery and observation.
            except PermissionError as error:
                # A permission-limited /proc cannot establish group quiescence.
                raise ProcessGroupRetirementError('Cannot observe process-group quiescence') from error
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def retire_process_group(pgid: int, *, wait_leader: Callable[[float], None] | None = None,
                         leader_alive: Callable[[], bool] | None = None,
                         grace_seconds: float = 3.0, kill_seconds: float = 2.0) -> None:
    """TERM, reap/wait leader, KILL remaining members, await descriptor closure.

    ``pgid`` must identify a caller-owned session/group, never a model-supplied
    arbitrary PID. A reattached caller verifies its recorded leader starttime
    before entering. A short-lived leader is not evidence its group has exited.
    """
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1 or pgid == os.getpgrp():
        raise ValueError('Refusing to retire an unowned/current process group')
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        if wait_leader is not None:
            wait_leader(kill_seconds)
        return
    if wait_leader is not None:
        try:
            wait_leader(grace_seconds)
        except subprocess.TimeoutExpired:
            pass  # The finite grace period expired; escalation is mandatory.
    else:
        deadline = time.monotonic() + grace_seconds
        while group_has_live_members(pgid) and (leader_alive is None or leader_alive()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))
    if group_has_live_members(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The last observed member exited before escalation.
    if wait_leader is not None:
        wait_leader(kill_seconds)
    deadline = time.monotonic() + kill_seconds
    while group_has_live_members(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessGroupRetirementError(f'Process group {pgid} did not retire')
        time.sleep(min(0.01, remaining))
