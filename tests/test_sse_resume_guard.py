#!/usr/bin/env python3
# Incident anchor: born in commit ab99ef8b — checkpoint: accumulated work since last commit
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Unit tests for the SSE warm-resume serviceability guard (routes/chat.py).

Fix #1 of the sync-robustness pass (2026-06-25): a Last-Event-ID resume whose
cursor is AHEAD of the in-memory event buffer used to slice ``events[resume_from:]``
into an empty list, silently stalling the warm stream and mis-indexing the live
loop. ``_warm_resume_serviceable(resume_cursor, n_events)`` now decides whether
the warm path can service the cursor; when it can't, the caller falls back to a
full state-snapshot resync (mirroring the cold path).

Pure-logic test — no app/server needed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _fn():
    from routes.chat import _warm_resume_serviceable
    return _warm_resume_serviceable


def test_no_cursor_is_not_serviceable():
    # None → fresh connection (caller's else-branch builds full snapshot).
    assert _fn()(None, 10) is False


def test_negative_cursor_is_not_serviceable():
    assert _fn()(-1, 10) is False
    assert _fn()(-5, 10) is False


def test_in_range_cursor_is_serviceable():
    # Buffer has 10 events (ids 0..9). Client last saw id=4 → resume_from=5 <= 10.
    assert _fn()(4, 10) is True
    # Last saw id=0 → resume_from=1.
    assert _fn()(0, 10) is True


def test_boundary_cursor_at_buffer_end_is_serviceable():
    # Client saw the LAST event (id=9) in a 10-event buffer → resume_from=10 == n.
    # Empty replay, then live streaming continues from index 10. Serviceable.
    assert _fn()(9, 10) is True


def test_cursor_ahead_of_buffer_is_not_serviceable():
    # Client claims id=10 but buffer only has 10 events (max id 9) →
    # resume_from=11 > 10 → must resync via full snapshot.
    assert _fn()(10, 10) is False
    # Far ahead (e.g. buffer was trimmed / restarted) → resync.
    assert _fn()(999, 10) is False


def test_empty_buffer():
    # Fresh task, no events yet. Only a no-cursor connection is "fresh"; any
    # non-negative cursor is ahead of an empty buffer → resync.
    assert _fn()(None, 0) is False
    assert _fn()(0, 0) is False   # resume_from=1 > 0
    assert _fn()(-1, 0) is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
