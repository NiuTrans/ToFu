"""Artifact text chunks: byte-identical cuts at UTF-8 boundaries, work per chunk."""
import random
import sys

import pytest

import lib.tools.result_value as result_value

pytestmark = pytest.mark.unit


def _per_character_chunks(text, limit):
    """The original per-code-point cut, kept as the oracle for exact boundaries."""
    start = size = 0
    for index, char in enumerate(text):
        width = len(char.encode('utf-8'))
        if size + width > limit and index > start:
            yield text[start:index]
            start, size = index, 0
        size += width
    if start < len(text):
        yield text[start:]


def _random_text(rng, length):
    # Every UTF-8 width, never a lone surrogate (it has no UTF-8 encoding).
    pools = ((0x20, 0x7E), (0x80, 0x7FF), (0x800, 0xD7FF), (0xE000, 0xFFFD),
             (0x10000, 0x10FFFF))
    chars = []
    for _ in range(length):
        low, high = pools[rng.randrange(len(pools))]
        chars.append(chr(rng.randint(low, high)))
    return ''.join(chars)


@pytest.mark.parametrize('limit', [*range(1, 17), 31, 64, 255, 4096])
def test_chunks_are_byte_identical_to_the_per_character_cut(monkeypatch, limit):
    rng = random.Random(limit)
    monkeypatch.setattr(result_value, '_ARTIFACT_TEXT_CHUNK_BYTES', limit)
    for length in [0, 1, 2, 3, 5, 17, 64, 257, 1000, *rng.sample(range(1, 3000), 12)]:
        text = _random_text(rng, length)
        expected = list(_per_character_chunks(text, limit))
        chunks = list(result_value._text_chunks(text))
        assert [chunk.encode('utf-8') for chunk in chunks] == [
            chunk.encode('utf-8') for chunk in expected]
        assert ''.join(chunks) == text


@pytest.mark.parametrize('tail', ['', 'TAIL', '🙂', 'é' * 3])
@pytest.mark.parametrize('pad', [0, 1, 2, 3, 4])
def test_production_chunk_size_cuts_before_a_straddling_code_point(tail, pad):
    limit = result_value._ARTIFACT_TEXT_CHUNK_BYTES
    text = 'A' * (limit - pad) + '🙂' + 'é' + tail
    assert [chunk.encode('utf-8') for chunk in result_value._text_chunks(text)] == [
        chunk.encode('utf-8') for chunk in _per_character_chunks(text, limit)]


def test_chunking_work_follows_chunks_not_characters(monkeypatch):
    """Cutting 200,000 code points into a few chunks must not visit each one."""
    limit = 64 * 1024
    monkeypatch.setattr(result_value, '_ARTIFACT_TEXT_CHUNK_BYTES', limit)
    text = _random_text(random.Random(7), 200_000)
    code = result_value._text_chunks.__code__
    lines = 0

    def trace(frame, event, arg):
        nonlocal lines
        if frame.f_code is not code:
            return None
        if event == 'line':
            lines += 1
        return trace

    sys.settrace(trace)
    try:
        chunks = list(result_value._text_chunks(text))
    finally:
        sys.settrace(None)
    assert ''.join(chunks) == text
    assert len(chunks) == len(list(_per_character_chunks(text, limit))) > 4
    assert lines <= 32 * (len(chunks) + 1), lines
