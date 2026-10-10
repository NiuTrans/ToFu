"""Arm a Linux parent-death signal, then replace this process."""

from __future__ import annotations

import ctypes
import os
import signal
import sys


_PR_SET_PDEATHSIG = 1


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    project_supervisor = bool(command and command[0] == "--project-supervisor")
    expected_parent = None
    if project_supervisor:
        if len(command) < 3:
            raise ValueError("project supervisor requires its original parent and command")
        expected_parent = int(command[1])
        command = command[2:]
    if not command or not os.path.isabs(command[0]):
        raise ValueError("parent-death wrapper requires an absolute executable")
    parent = os.getppid()
    # Fail closed if the launcher vanished before this Python process got its
    # first instruction. PID 0 is valid for the first process in the private
    # PID namespace used by SandboxSession; PID 1 here means a host-namespace
    # orphan that has already been adopted by init.
    if (expected_parent is not None and parent != expected_parent) or (
            expected_parent is None and parent == 1):
        return 143
    libc = ctypes.CDLL(None, use_errno=True)
    death_signal = signal.SIGKILL if project_supervisor else signal.SIGTERM
    if libc.prctl(_PR_SET_PDEATHSIG, death_signal, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "prctl(PR_SET_PDEATHSIG) failed")
    # Close the fork-to-prctl race: if the launcher vanished before the signal
    # was armed, terminate rather than creating an unowned sandbox process.
    if os.getppid() != parent:
        return 143
    environment = dict(os.environ)
    if project_supervisor:
        # Pin this process's proc inode across unshare exec/fork. Namespace
        # clocks can change visible start ticks, so timestamps are unsuitable
        # here. The trusted child checks this port then closes it before exec.
        supervisor_port = os.open('/proc/self', os.O_RDONLY | os.O_DIRECTORY)
        os.set_inheritable(supervisor_port, True)
        environment['TOFU_ROOTLESS_SUPERVISOR_PID'] = str(os.getpid())
        environment['TOFU_ROOTLESS_SUPERVISOR_FD'] = str(supervisor_port)
    os.execve(command[0], command, environment)
    raise AssertionError("os.execve returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
