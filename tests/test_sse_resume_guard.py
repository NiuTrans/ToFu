#!/usr/bin/env python3
# Incident anchor: born in commit ab99ef8b — checkpoint: accumulated work since last commit
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Unit tests for the SSE warm-resume serviceability guard (routes/chat.py).

Fix #1 of the sync-robustness pass (2026-06-25): a Last-Event-ID resume whose
cursor is outside the retained absolute event window used to produce an empty
replay, silently stalling the warm stream and mis-indexing the live loop.
``_warm_resume_serviceable(last_event_id, base_cursor, next_cursor)`` now
checks both rolling-window edges; when it cannot service the cursor, the caller
falls back to a full state-snapshot resync (mirroring the cold path).

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
    assert _fn()(None, 4, 10) is False


def test_negative_cursor_is_not_serviceable():
    assert _fn()(-1, 4, 10) is False
    assert _fn()(-5, 4, 10) is False


def test_retained_cursor_is_serviceable():
    # Retained ids are 4..9. Client last saw id=3 → resume_from=4.
    assert _fn()(3, 4, 10) is True
    # Client last saw id=8 → resume_from=9.
    assert _fn()(8, 4, 10) is True


def test_boundary_cursor_at_buffer_end_is_serviceable():
    # Client saw the LAST retained event (id=9) → resume_from=10 == next.
    # Empty replay, then live streaming continues from absolute seq 10.
    assert _fn()(9, 4, 10) is True


def test_cursor_below_retained_window_is_not_serviceable():
    # Client needs seq=3, but the retained window starts at seq=4.
    assert _fn()(2, 4, 10) is False
    assert _fn()(0, 4, 10) is False


def test_cursor_ahead_of_buffer_is_not_serviceable():
    # Client claims id=10 but producer next seq is 10 (max id 9) →
    # resume_from=11 is in the future and must full-snapshot resync.
    assert _fn()(10, 4, 10) is False
    assert _fn()(999, 4, 10) is False


def test_empty_buffer():
    # A fresh empty task has no serviceable non-negative Last-Event-ID.
    assert _fn()(None, 0, 0) is False
    assert _fn()(0, 0, 0) is False
    assert _fn()(-1, 0, 0) is False

    # An empty retained tail can still represent a producer that has emitted
    # 40 events and evicted all of them. A client caught up through id=39 is
    # exactly at the boundary and can continue live without replay.
    assert _fn()(39, 40, 40) is True
    assert _fn()(38, 40, 40) is False
    assert _fn()(40, 40, 40) is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
