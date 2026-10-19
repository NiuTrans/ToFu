"""Stable streamed source identity, exact quote binding and bounded retention."""
import hashlib
import os
import tracemalloc

import pytest

from lib.artifacts.file_snapshot import read_project_snapshot

pytestmark = pytest.mark.unit


def test_exact_unicode_quotes_cross_read_boundaries_without_retaining_the_file(tmp_path):
    data = b'a' * 65534 + '证据跨边界\nsecond line'.encode() + b'\nend'
    (tmp_path / 'source.txt').write_bytes(data)
    quotes = ['a证据', '证据跨边界\nsecond line', 'Second line']
    result = read_project_snapshot('source.txt', [str(tmp_path)], quotes=quotes)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data) and result.content is None
    assert result.found_quotes == frozenset(quotes[:2])
    assert result.relative_path == 'source.txt' and result.root == str(tmp_path)


@pytest.mark.parametrize('size', [4 * 1024 * 1024, 64 * 1024 * 1024])
def test_fingerprint_retention_stays_bounded_for_large_sources(tmp_path, size):
    block = b'source evidence\n' * 4096
    source = tmp_path / 'large.txt'
    expected = hashlib.sha256()
    with source.open('wb') as target:
        remaining = size
        while remaining:
            piece = block[:remaining]; target.write(piece); expected.update(piece); remaining -= len(piece)
    tracemalloc.start()
    try:
        result = read_project_snapshot(str(source), [str(tmp_path)], quotes=['evidence\nsource'])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.content is None and result.sha256 == expected.hexdigest()
    assert result.found_quotes == frozenset(['evidence\nsource'])
    assert peak < 1024 * 1024, (size, peak)


def test_cancelled_snapshot_closes_every_descriptor_before_returning(tmp_path, monkeypatch):
    (tmp_path / 'source.txt').write_bytes(b'x' * 100000)
    opened, calls = [], []
    real_open = os.open

    def capture_open(*args, **kwargs):
        fd = real_open(*args, **kwargs); opened.append(fd); return fd

    def check():
        calls.append(True)
        if len(calls) == 3:
            raise InterruptedError('cancelled')

    monkeypatch.setattr(os, 'open', capture_open)
    with pytest.raises(InterruptedError):
        read_project_snapshot('source.txt', [str(tmp_path)], check=check)
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
