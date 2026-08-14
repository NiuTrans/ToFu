"""Set Linux no_new_privs before replacing this process with QEMU."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import resource
import signal
import socket
import struct
import sys
from pathlib import Path


_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_PDEATHSIG = 1
_PR_CAPBSET_DROP = 24
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def enable_no_new_privileges() -> None:
    """Permanently prevent this process tree from acquiring new privilege."""

    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, "prctl(PR_SET_NO_NEW_PRIVS) failed")


def arm_parent_death_signal(expected_parent: int) -> None:
    """Make the final QEMU die when unshare's supervising process exits.

    This must run after capability/identity setup: Linux may clear PDEATHSIG
    when process credentials change. The immediate pre-exec placement is what
    prevents a namespace child from surviving as a host PID-1 orphan.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != expected_parent:
        raise RuntimeError("QEMU supervisor exited before parent-death signal was armed")


def drop_capabilities() -> None:
    """Drop namespace capabilities before any untrusted guest can execute."""

    libc = ctypes.CDLL(None, use_errno=True)
    for capability in range(64):
        result = libc.prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0)
        if result != 0 and ctypes.get_errno() not in {errno.EINVAL, errno.EPERM}:
            raise OSError(ctypes.get_errno(), "failed to drop capability bound")
    libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    header = _CapHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "capset failed")


def enter_jail(root: str) -> None:
    jail = Path(root).resolve(strict=True)
    if jail.is_symlink() or not jail.is_dir():
        raise ValueError("jail root must be a real directory")
    os.chroot(jail)
    os.chdir("/")


def enable_private_loopback() -> None:
    """Bring up only lo while CAP_NET_ADMIN still exists in the new netns."""

    request = bytearray(struct.pack("16sH22x", b"lo", 0))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        fcntl.ioctl(control.fileno(), _SIOCGIFFLAGS, request, True)
        flags = struct.unpack_from("H", request, 16)[0]
        struct.pack_into("H", request, 16, flags | _IFF_UP)
        fcntl.ioctl(control.fileno(), _SIOCSIFFLAGS, request)


def _close_extra_descriptors() -> None:
    limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    maximum = 65536 if limit == resource.RLIM_INFINITY else min(int(limit), 65536)
    os.closerange(3, maximum)


def _sanitized_environment() -> dict[str, str]:
    """Return a deterministic, credential-free environment for the jail."""

    return {
        "HOME": "/",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": "/tmp",
    }


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if len(command) == 2 and command[0] == "--probe-chroot":
        enable_private_loopback()
        enter_jail(command[1])
        enable_no_new_privileges()
        drop_capabilities()
        os.write(1, b"confined\n")
        return 0
    if command and command[0] == "--no-jail":
        command = command[1:]
    elif len(command) >= 3 and command[0] == "--chroot":
        jail, command = command[1], command[2:]
        enable_private_loopback()
        enter_jail(jail)
    else:
        raise ValueError("qemu launcher requires --chroot ROOT and a command")
    if not command or not os.path.isabs(command[0]):
        raise ValueError("qemu launcher requires an absolute executable path")
    expected_parent = os.getppid()
    enable_no_new_privileges()
    drop_capabilities()
    _close_extra_descriptors()
    arm_parent_death_signal(expected_parent)
    os.execve(command[0], command, _sanitized_environment())
    raise AssertionError("os.execve returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
