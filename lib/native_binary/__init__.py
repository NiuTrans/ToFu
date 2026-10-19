"""Pinned native image admission without importing task execution machinery.

Entry _seal: verify an opened regular ELF file, copy/check its trusted digest,
then seal the exact image used by executable/library owners. Native transport
error types are shared here and re-exported by native_process for compatibility.
No native load, task execution, environment selection or process-global image.
"""
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import os
from pathlib import Path
import stat
from typing import BinaryIO


class NativeProcessError(RuntimeError):
    """The native transport/installation failed; diagnostics exclude payloads."""


class NativeConfigurationError(NativeProcessError, ValueError):
    pass


# Linux UAPI fallbacks for Python built against older libc/kernel headers.
# Authority: linux/v5.10 include/uapi/linux/fcntl.h and syscall tables.
_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_WRITE_SEAL = getattr(fcntl, "F_SEAL_WRITE", 8)
_ALL_SEALS = 1 | 2 | 4 | 8
# Declaration consumed by generated CFFI bindings; runtime never parses C.
MEMFD_CDEF = "long syscall(long number, ...);"


def _memfd(*, ffi=None) -> int:
    if hasattr(os, "memfd_create"):
        return os.memfd_create("tofu-native", 1 | 2)
    number = {"x86_64": 319, "aarch64": 279}.get(os.uname().machine)
    if number is None:
        raise NativeConfigurationError("native memfd syscall ABI is unsupported")
    if ffi is not None:
        if ffi.sizeof("long") != 8:
            raise NativeConfigurationError(
                "native memfd syscall requires a known 64-bit ABI"
            )
        libc = ffi.dlopen(None)
        descriptor = -1
        try:
            descriptor = libc.syscall(number, ffi.new("char[]", b"tofu-native"),
                                      ffi.cast("unsigned int", 1 | 2))
        except BaseException as primary:
            try:
                ffi.dlclose(libc)
            except BaseException as cleanup:
                raise BaseExceptionGroup("native memfd call and cleanup failed",
                                         [primary, cleanup]) from None
            raise
        try:
            ffi.dlclose(libc)
        except BaseException as primary:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except BaseException as cleanup:
                    raise BaseExceptionGroup("native memfd cleanup failed",
                                             [primary, cleanup]) from None
            raise
        if descriptor < 0:
            raise NativeProcessError("native sealed execution is unavailable")
        return int(descriptor)
    import ctypes

    if ctypes.sizeof(ctypes.c_long) != 8:
        raise NativeConfigurationError(
            "native memfd syscall requires a known 64-bit ABI"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    descriptor = syscall(
        ctypes.c_long(number), ctypes.c_char_p(b"tofu-native"), ctypes.c_uint(1 | 2)
    )
    if descriptor < 0:
        raise NativeProcessError("native sealed execution is unavailable")
    return int(descriptor)


def _seal(binary: Path, expected: str, limit: int, *, ffi=None) -> BinaryIO:
    """Verify then seal the exact bytes executed, preventing path replacement."""
    image = _memfd() if ffi is None else _memfd(ffi=ffi)
    try:
        hasher = hashlib.sha256()
        total = 0
        source_fd = os.open(binary, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(source_fd)
            if not stat.S_ISREG(info.st_mode):
                raise NativeConfigurationError('native executable source must be a regular file')
            if info.st_size > limit:
                raise NativeConfigurationError('native executable byte budget exceeded')
            # NONBLOCK prevents a substituted FIFO/device from trapping joined
            # acquisition. Inspect this opened descriptor, never a racy path.
            with os.fdopen(source_fd, "rb", closefd=False) as source:
                if source.read(4) != b"\x7fELF":
                    raise NativeConfigurationError(
                        "native executable must be an ELF artifact"
                    )
                source.seek(0)
                while chunk := source.read(65536):
                    total += len(chunk)
                    if total > limit:
                        raise NativeConfigurationError(
                            "native executable byte budget exceeded"
                        )
                    hasher.update(chunk)
                    offset = 0
                    while offset < len(chunk):
                        written = os.write(image, chunk[offset:])
                        if written <= 0:
                            raise NativeProcessError("native executable copy failed")
                        offset += written
        finally:
            os.close(source_fd)
        if hasher.hexdigest() != expected:
            raise NativeConfigurationError("native executable checksum differs")
        fcntl.fcntl(image, _ADD_SEALS, _ALL_SEALS)
        return os.fdopen(image, "rb", buffering=0)
    except BaseException:
        os.close(image)
        raise
