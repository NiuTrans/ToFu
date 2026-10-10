"""Set Linux no_new_privs before replacing this process with QEMU."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
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


def arm_parent_death_signal(expected_parent: int, *, death_signal: int = signal.SIGTERM,
                            descriptor_directory: int | None = None,
                            supervisor_identity: tuple[int, int, int] | None = None) -> None:
    """Make the final QEMU die when unshare's supervising process exits.

    This must run after capability/identity setup: Linux may clear PDEATHSIG
    when process credentials change. The immediate pre-exec placement is what
    prevents a namespace child from surviving as a host PID-1 orphan.
    """

    # getppid() is zero for namespace PID 1. The host-mounted proc inventory
    # still exposes its actual supervising parent, including adoption by init.
    host_parent = (_host_parent(descriptor_directory)
                   if descriptor_directory is not None else None)
    if expected_parent == 1 or host_parent == 1:
        raise RuntimeError("namespace supervisor exited before launch")
    if supervisor_identity is not None:
        if (host_parent != supervisor_identity[0] or _host_parent_identity(
                descriptor_directory, host_parent) != supervisor_identity[1:]):
            raise RuntimeError("namespace child was adopted by a foreign supervisor")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, death_signal, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "prctl(PR_SET_PDEATHSIG) failed")
    if (os.getppid() != expected_parent or (descriptor_directory is not None
            and _host_parent(descriptor_directory) != host_parent)):
        raise RuntimeError("QEMU supervisor exited before parent-death signal was armed")


def _host_parent(descriptor_directory: int) -> int:
    descriptor = os.open("../stat", os.O_RDONLY | os.O_CLOEXEC,
                         dir_fd=descriptor_directory)
    try:
        # comm may contain ')' or spaces; fields after its final ')' are fixed.
        fields = os.read(descriptor, 4096).decode("ascii").rsplit(") ", 1)[1].split()
        return int(fields[1])
    finally:
        os.close(descriptor)


def _host_parent_identity(descriptor_directory: int, parent: int) -> tuple[int, int]:
    metadata = os.stat(f"../../{parent}", dir_fd=descriptor_directory,
                       follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


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


def _close_extra_descriptors(descriptor_directory: int) -> None:
    """Close actual inherited handles, including those above current rlimits.

    The single-threaded launcher opens this directory before chroot, so its
    inventory still works without host /proc in the jail. listdir's temporary
    duplicate can already be closed; every other closure error refuses exec.
    The directory handle itself is included and never reaches the guest.
    """
    for name in os.listdir(descriptor_directory):
        descriptor = int(name)
        if descriptor < 3:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise


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
    # Do not infer open handles from a soft/hard limit: existing handles can
    # remain above either after prlimit lowers it. Retain only this trusted
    # inventory port across chroot, and close it before untrusted execution.
    descriptor_directory = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return _launch(list(sys.argv[1:] if argv is None else argv), descriptor_directory)
    finally:
        try:
            os.close(descriptor_directory)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise


def _launch(command: list[str], descriptor_directory: int) -> int:
    if len(command) == 2 and command[0] == "--probe-chroot":
        enable_private_loopback()
        enter_jail(command[1])
        enable_no_new_privileges()
        drop_capabilities()
        _close_extra_descriptors(descriptor_directory)
        os.write(1, b"confined\n")
        return 0
    environment = _sanitized_environment()
    death_signal = signal.SIGTERM
    supervisor_identity = None
    if command and command[0] == "--project-command":
        # Generic commands may ignore TERM (especially namespace PID 1).
        # This trusted mode always requires a jail; it cannot opt out of it.
        supervisor_port = int(os.environ['TOFU_ROOTLESS_SUPERVISOR_FD'])
        if supervisor_port < 3:
            raise ValueError("project command requires a trusted supervisor port")
        metadata = os.fstat(supervisor_port)
        supervisor_identity = (int(os.environ['TOFU_ROOTLESS_SUPERVISOR_PID']),
                               metadata.st_dev, metadata.st_ino)
        if supervisor_identity[0] <= 0:
            raise ValueError("project command requires an exact supervisor identity")
        death_signal = signal.SIGKILL
        command = ["--chroot", *command[1:]]
    if command and command[0] == "--no-jail":
        command = command[1:]
        # The host preflight supplies its owned scratch explicitly. /tmp below
        # is otherwise jail-local, not the host's shared temporary directory.
        if len(command) >= 3 and command[0] == "--host-tmpdir":
            environment["TMPDIR"] = command[1]
            command = command[2:]
        else:
            environment.pop("TMPDIR", None)
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
    arm_parent_death_signal(expected_parent, death_signal=death_signal,
                            descriptor_directory=descriptor_directory,
                            supervisor_identity=supervisor_identity)
    _close_extra_descriptors(descriptor_directory)
    os.execve(command[0], command, environment)
    raise AssertionError("os.execve returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
