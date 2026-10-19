"""Lazy, digest-verified Git discovery ABI; caller owns authority and lifecycle.

No repository, file list or thread is retained. Deadline is cooperative, not a
hard filesystem-I/O timeout. The application has not selected this API yet.
"""
import ctypes
import hashlib
import math
import os
from pathlib import Path


class DiscoveryError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'native repository discovery failed (status {status})')


class Discovery:
    def __init__(self, path, expected_sha256):
        path = Path(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError('native discovery artifact digest differs')
        self.library = ctypes.CDLL(str(path.resolve()))
        self.function = self.library.tofu_repository_files_v1
        self.function.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint64,
                                  ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_size_t)]
        self.function.restype = ctypes.c_int
        self.free = self.library.tofu_repository_files_free_v1
        self.free.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.free.restype = None

    def list_files(self, root, *, timeout=60.0):
        """Return sorted unique raw path bytes, including authoritative empty.

        Failures raise without partial output. Bytes preserve arbitrary POSIX
        names. Timeout zero is an immediate deadline, useful for cancellation.
        """
        root, timeout_ns = validate_request(root, timeout)
        output, length = ctypes.c_void_p(), ctypes.c_size_t()
        status = self.function(root, len(root), timeout_ns, ctypes.byref(output), ctypes.byref(length))
        if status:
            raise DiscoveryError(status)
        try:
            payload = ctypes.string_at(output, length.value)
            return payload[:-1].split(b'\0') if payload else []
        finally:
            self.free(output, length)


def validate_request(root, timeout):
    root = os.fsencode(root)
    if not root or b'\0' in root:
        raise ValueError('nonempty root without NUL required')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError('finite nonnegative timeout required')
    if timeout < 0 or timeout >= 2**64 / 1e9 or not math.isfinite(timeout):
        raise ValueError('finite representable nonnegative timeout required')
    return root, int(timeout * 1e9)
