"""Processes a test started must not outlive it.

Real lifecycle tests launch workers and resident hosts in their own sessions
(``start_new_session=True``), exactly as production does, so neither pytest's
process group nor the interpreter's exit reaps them. Ownership is the test's
directory: every such process runs with its working directory inside the
test's ``tmp_path``. Linux only; elsewhere nothing is found.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import time


def processes_under(root: Path) -> list[dict]:
    """Every live process whose working directory is ``root`` or below it."""
    base = os.path.realpath(root)
    found = []
    try:
        entries = os.listdir('/proc')
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            cwd = os.readlink(f'/proc/{entry}/cwd')
            with open(f'/proc/{entry}/cmdline', 'rb') as stream:
                cmdline = stream.read().replace(b'\0', b' ').decode(errors='replace').strip()
        except OSError:
            continue
        if cwd.endswith(' (deleted)'):
            cwd = cwd[:-len(' (deleted)')]
        if cwd == base or cwd.startswith(base + os.sep):
            found.append({'pid': int(entry), 'cwd': cwd, 'cmdline': cmdline})
    return found


def retire_processes_under(root: Path, *, timeout: float = 10.0) -> list[dict]:
    """SIGKILL every process under ``root``; return what was found alive."""
    found = processes_under(root)
    for process in found:
        try:
            os.kill(process['pid'], signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    pending = {process['pid'] for process in found}
    while pending and time.monotonic() < deadline:
        for pid in list(pending):
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            if not os.path.exists(f'/proc/{pid}') or _zombie(pid):
                pending.discard(pid)
        time.sleep(0.02)
    return found


def _zombie(pid: int) -> bool:
    try:
        with open(f'/proc/{pid}/stat', 'rb') as stream:
            raw = stream.read()
        return raw[raw.rfind(b')') + 2:].split()[0] == b'Z'
    except (OSError, IndexError):
        return True
