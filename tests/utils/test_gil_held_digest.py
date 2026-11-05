"""SHA-256 helpers that keep the GIL produce exactly hashlib's digests."""
from __future__ import annotations

import hashlib

import pytest

from lib.utils import (
    HASHLIB_GIL_RELEASE_BYTES,
    sha256_holding_gil,
    update_holding_gil,
)

pytestmark = pytest.mark.unit

SIZES = (0, 1, HASHLIB_GIL_RELEASE_BYTES - 2, HASHLIB_GIL_RELEASE_BYTES - 1,
         HASHLIB_GIL_RELEASE_BYTES, HASHLIB_GIL_RELEASE_BYTES + 1,
         3 * HASHLIB_GIL_RELEASE_BYTES, 1_048_583)


@pytest.mark.parametrize('size', SIZES)
def test_digest_is_byte_identical_to_one_hashlib_call(size):
    data = (('é上下文-' * (size // 8 + 1)).encode('utf-8'))[:size]
    assert sha256_holding_gil(data).digest() == hashlib.sha256(data).digest()

    prefix = hashlib.sha256(b'[')
    expected = prefix.copy()
    expected.update(data)
    update_holding_gil(prefix, data)
    assert prefix.hexdigest() == expected.hexdigest()


class _RecordingHasher:
    def __init__(self):
        self.sizes = []

    def update(self, data):
        self.sizes.append(len(data))


@pytest.mark.parametrize('size', SIZES)
def test_no_update_reaches_the_gil_release_threshold(size):
    """CPython hashlib releases the GIL only for updates of 2048+ bytes."""
    recorder = _RecordingHasher()
    update_holding_gil(recorder, b'x' * size)
    assert sum(recorder.sizes) == size
    assert all(chunk < HASHLIB_GIL_RELEASE_BYTES for chunk in recorder.sizes)
