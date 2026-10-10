"""Stable Linux process handles for namespace supervision on older Python builds.

Prefer Python's wrappers. Explicit 64-bit Linux ABI entries support Python
built against older headers; unknown ABIs and kernel/seccomp refusals fail.
No numeric-PID signaling fallback is provided by this port.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import signal
import sys

# Linux v5.10 arch/x86/entry/syscalls/syscall_64.tbl and
# include/uapi/asm-generic/unistd.h (also used by arm64).
_SYSCALLS = {
    'x86_64': {'pidfd_open': 434, 'pidfd_send_signal': 424},
    'aarch64': {'pidfd_open': 434, 'pidfd_send_signal': 424},
}


def _syscall(name: str, *arguments) -> int:
    machine = platform.machine()
    if (sys.platform != 'linux' or ctypes.sizeof(ctypes.c_void_p) != 8
            or machine not in _SYSCALLS):
        raise OSError(errno.ENOSYS, 'pidfd ABI is unavailable')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(_SYSCALLS[machine][name]), *arguments)
    if result < 0:
        raise OSError(ctypes.get_errno(), f'{name} failed')
    return int(result)


def open_process(pid: int) -> int:
    wrapper = getattr(os, 'pidfd_open', None)
    if wrapper is not None:
        return wrapper(pid, 0)
    return _syscall('pidfd_open', ctypes.c_int(pid), ctypes.c_uint(0))


def send_signal(descriptor: int, value: int) -> None:
    wrapper = getattr(signal, 'pidfd_send_signal', None)
    if wrapper is not None:
        wrapper(descriptor, value, None, 0)
        return
    _syscall('pidfd_send_signal', ctypes.c_int(descriptor), ctypes.c_int(value),
             ctypes.c_void_p(), ctypes.c_uint(0))


def require_process_handles() -> None:
    """Prove both operations on this process without delivering a signal."""
    descriptor = open_process(os.getpid())
    try:
        send_signal(descriptor, 0)
    finally:
        os.close(descriptor)
