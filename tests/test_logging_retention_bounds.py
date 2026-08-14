"""Regression coverage for size + time + total-budget log retention."""

from __future__ import annotations

import logging
import os

import pytest

from server import _SizeAndTimeRotatingFileHandler

pytestmark = pytest.mark.unit


def _record(text):
    return logging.LogRecord(
        'lib.retention_test', logging.INFO, __file__, 1, text, (), None)


def test_log_family_budget_prunes_oldest_rotated_only(tmp_path):
    base = tmp_path / 'app.log'
    base.write_bytes(b'current')
    old = tmp_path / 'app.log.2026-01-01'
    newer = tmp_path / 'app.log.2026-01-02'
    old.write_bytes(b'o' * 80)
    newer.write_bytes(b'n' * 80)
    os.utime(old, ns=(1, 1))
    os.utime(newer, ns=(2, 2))

    handler = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=1024, total_budget_bytes=100)
    try:
        assert base.exists(), 'active log must never be retention-pruned'
        assert not old.exists(), 'oldest rotated chunk should be pruned first'
        assert newer.exists(), 'budget fits after deleting only the oldest'
    finally:
        handler.close()


def test_multiple_size_rollovers_get_unique_chunks(tmp_path):
    base = tmp_path / 'app.log'
    handler = _SizeAndTimeRotatingFileHandler(
        str(base), when='midnight', backupCount=30, encoding='utf-8',
        max_bytes=40, total_budget_bytes=10_000)
    handler.setFormatter(logging.Formatter('%(message)s'))
    try:
        for i in range(5):
            handler.emit(_record(f'{i}-' + ('x' * 28)))
        handler.flush()
    finally:
        handler.close()

    chunks = sorted(tmp_path.glob('app.log.*'))
    assert len(chunks) >= 3
    assert len({path.name for path in chunks}) == len(chunks)
    assert base.exists()
