"""Explicit digest-verified standalone Rust CSR bridge; no import-time load.

Entry: Kernel(path, sha256).solve(offsets, targets, weights, personalization).
Caller owns the library lifetime and pure graph data. ctypes releases the GIL;
all buffers are call-local, and Rust failure never publishes partial scores.
"""
import ctypes
from array import array
import hashlib
from pathlib import Path


class Kernel:
    def __init__(self, path, expected_sha256):
        path = Path(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError('native CSR artifact digest differs')
        self.library = ctypes.CDLL(str(path.resolve()))
        self.function = self.library.tofu_pagerank_csr_v1
        self.function.argtypes = [ctypes.c_size_t, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_double), ctypes.c_double, ctypes.c_size_t]
        self.function.restype = ctypes.c_int

    def solve(self, offsets, targets, weights, personalization, *, tolerance=1e-6, maximum_iterations=100):
        # Snapshot before numeric conversion: reentrant __float__ callbacks or
        # another caller cannot change declared buffer lengths during copying.
        offsets, targets = tuple(offsets), tuple(targets)
        weights, personalization = tuple(weights), tuple(personalization)
        count = len(personalization)
        if not count or len(offsets) != count + 1 or len(targets) != len(weights):
            raise ValueError('CSR buffer lengths differ')
        if (any(type(i) is not int or not 0 <= i <= len(weights) for i in offsets)
                or any(type(i) is not int or not 0 <= i < count for i in targets)):
            raise ValueError('CSR indices must be in buffer bounds')
        if type(maximum_iterations) is not int or not 0 < maximum_iterations < 2 ** (8 * ctypes.sizeof(ctypes.c_size_t)):
            raise ValueError('positive representable iteration limit required')
        # Contiguous array buffers avoid per-shape ctypes Array classes and
        # their self-referencing class metadata. Pointer types are fixed by
        # the immutable ABI binding; every array remains owned for this call.
        index_format = 'Q' if ctypes.sizeof(ctypes.c_size_t) == 8 else 'I'
        offset_buffer = array(index_format, offsets)
        target_buffer = array(index_format, targets)
        weight_buffer = array('d', weights)
        personal_buffer = array('d', personalization)
        output = array('d', [0.0]) * count
        def pointer(buffer, scalar):
            return ctypes.cast(buffer.buffer_info()[0], ctypes.POINTER(scalar))
        status = self.function(count, len(targets),
                               pointer(offset_buffer, ctypes.c_size_t),
                               pointer(target_buffer, ctypes.c_size_t),
                               pointer(weight_buffer, ctypes.c_double),
                               pointer(personal_buffer, ctypes.c_double),
                               pointer(output, ctypes.c_double), tolerance, maximum_iterations)
        if status == 1: raise ValueError('invalid native CSR input')
        if status == 2: raise RuntimeError('PageRank failed to converge within iteration limit')
        if status != 0: raise RuntimeError('native CSR computation failed')
        return output.tolist()
