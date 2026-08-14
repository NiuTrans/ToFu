"""Arm a Linux parent-death signal, then replace this process."""

from __future__ import annotations

import ctypes
import os
import signal
import sys


_PR_SET_PDEATHSIG = 1


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command or not os.path.isabs(command[0]):
        raise ValueError("parent-death wrapper requires an absolute executable")
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "prctl(PR_SET_PDEATHSIG) failed")
    # Close the fork-to-prctl race: if the launcher vanished before the signal
    # was armed, terminate rather than creating an unowned sandbox process.
    if os.getppid() != parent:
        return 143
    os.execve(command[0], command, dict(os.environ))
    raise AssertionError("os.execve returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
