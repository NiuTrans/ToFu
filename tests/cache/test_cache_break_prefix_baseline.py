"""Measure a re-bill against the prefix the previous round left cached.

Live evidence (2026-11-03 19:49-20:42, 578 Claude rounds): 18 rounds read back
only the shared ~28k system+tools head right after a predecessor that read 0
and wrote its whole ~135k prompt. ``partial_no_reuse`` required the read to
stay pinned at the predecessor's READ (0), so those re-written prefixes were
recorded as ``no_break`` beside genuine hits. The predecessor left its read
plus its write cached; that is what the next round should read back.

Consecutive zero-read misses were already caught (``no_reuse`` uses read +
write, pinned in ``test_cache_verdict_indeterminate_bucket.py``); the gap was
a non-zero partial read after a miss.
"""
from __future__ import annotations

import pytest

from lib.tasks_pkg.cache_tracking._detect import (
    BUCKET_NO_BREAK,
    _classify_break,
)

pytestmark = pytest.mark.unit


def _gates(prev_read, prev_write, read, write, *, compaction=False):
    return _classify_break(
        call_count=5, was_compaction=compaction,
        prev_cache_read=prev_read, cache_read=read,
        prev_cache_write=prev_write, cache_write=write,
        prev_prefix_tokens=prev_read + prev_write)


def test_head_only_read_after_a_miss_is_a_break():
    assert any(_gates(0, 135862, 28222, 107642)), (
        'reading 28k of the 136k prefix the predecessor wrote is a miss')


def test_reading_the_whole_predecessor_prefix_is_not_a_break():
    assert _gates(130000, 5864, 135864, 2100) == (False, False, False)


def test_large_writes_that_extend_a_fully_read_prefix_are_not_a_break():
    """Read covers the whole predecessor prefix; both rounds wrote 20k+ of
    new tail. A read within 5% of the previous READ is not a re-bill."""
    assert _gates(400000, 20000, 420000, 25000) == (False, False, False)


def test_a_shortfall_below_the_miss_floor_is_not_a_break():
    from lib.tasks_pkg.cache_tracking._policy import _MIN_CACHE_MISS_TOKENS
    read = 160000 - (_MIN_CACHE_MISS_TOKENS - 1)
    assert _gates(130000, 30000, read, 30000) == (False, False, False)


def test_compaction_stays_exempt():
    assert _gates(0, 135862, 28222, 60000, compaction=True) == (
        False, False, False)


def test_detector_records_a_head_only_read_after_a_miss(monkeypatch):
    """Drives the real detector: the round is a confirmed break, not no_break."""
    import lib.tasks_pkg.cache_tracking._detect as det

    seen = []
    real_emit = det._emit_round_record

    def _spy(conv_id, call_num, verdict, **kw):
        seen.append(det.classify_verdict(verdict))
        return real_emit(conv_id, call_num, verdict, **kw)

    monkeypatch.setattr(det, '_emit_round_record', _spy)
    conv = 'conv-prefix-baseline'
    returned = []
    for read, write in ((0, 120000), (120000, 15864), (0, 135864),
                        (28222, 107642)):
        usage = {'prompt_tokens': 1000, 'completion_tokens': 10,
                 'cache_read_input_tokens': read,
                 'cache_creation_input_tokens': write}
        returned.append(det.detect_cache_break(
            conv, [{'role': 'user', 'content': 'x'}],
            None, 'claude-opus-5', usage, user_id=1))
    assert seen[2] != BUCKET_NO_BREAK
    assert seen[3] != BUCKET_NO_BREAK, f'buckets={seen}'
    assert returned[3] is not None
