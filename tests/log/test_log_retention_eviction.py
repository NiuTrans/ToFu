"""Executable spec: budget eviction keeps recent evidence and says what it removed.

Regression for 2026-10-27: ``app.log.2026-10-26`` vanished mid-day under the
global ``logs/`` budget while five-day-old error/audit rotations survived, and
the only record of the deletion was overwritten by the next maintenance pass.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

import lib.logging_pkg.log_retention as log_retention
from lib.logging_pkg.log_retention import maintain_logs


pytestmark = pytest.mark.unit

_LOGGER_NAME = 'lib.logging_pkg.log_retention'


def _local(*fields: int) -> float:
    year, month, day, hour, minute = (tuple(fields) + (0, 0))[:5]
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _write(path: Path, size: int, mtime: float) -> None:
    path.write_bytes(b'x' * (size - 1) + b'\n')
    os.utime(path, (mtime, mtime))


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    logs, data = tmp_path / 'logs', tmp_path / 'data'
    logs.mkdir()
    data.mkdir()
    return logs, data


def _removed(result: dict) -> list[tuple[str, str]]:
    return [(Path(item['path']).name, item['reason'])
            for item in result['removed']]


def _removal_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records
            if getattr(record, 'tofu_event_name', '')
            == 'logging.retention_removed']


NOW = _local(2026, 10, 27, 12)
YESTERDAY_CLOSE = _local(2026, 10, 26, 23, 59)


def test_previous_day_log_outlives_older_rotations_under_global_budget(
        tmp_path, monkeypatch):
    logs, data = _dirs(tmp_path)
    _write(logs / 'app.log', 400, NOW)
    _write(logs / 'app.log.2026-10-26', 300, YESTERDAY_CLOSE)
    _write(logs / 'error.log.6', 200, _local(2026, 10, 23, 11))
    _write(logs / 'audit.log.4', 200, _local(2026, 10, 23, 15))
    monkeypatch.setattr(log_retention, 'total_log_budget_bytes', lambda: 750)

    result = maintain_logs(logs, data_dir=data, now=NOW)

    assert (logs / 'app.log.2026-10-26').exists()
    assert _removed(result) == [
        ('error.log.6', 'global_budget'), ('audit.log.4', 'global_budget')]
    assert result['over_budget_bytes'] == 0


def test_global_budget_keeps_priority_order_within_each_age_tier(
        tmp_path, monkeypatch):
    logs, data = _dirs(tmp_path)
    _write(logs / 'app.log', 100, NOW)
    # Older history: priority still beats age, so access (35) goes first.
    _write(logs / 'error.log.5', 100, _local(2026, 10, 23, 9))
    _write(logs / 'access.log.2026-10-24', 100, _local(2026, 10, 24, 23, 59))
    # Previous-day evidence: frontend (55) yields before app (90).
    _write(logs / 'frontend.log.2026-10-26', 100, YESTERDAY_CLOSE)
    _write(logs / 'app.log.2026-10-26', 100, YESTERDAY_CLOSE)
    monkeypatch.setattr(log_retention, 'total_log_budget_bytes', lambda: 250)

    result = maintain_logs(logs, data_dir=data, now=NOW)

    assert _removed(result) == [
        ('access.log.2026-10-24', 'global_budget'),
        ('error.log.5', 'global_budget'),
        ('frontend.log.2026-10-26', 'global_budget'),
    ]
    assert (logs / 'app.log.2026-10-26').exists()


def test_required_eviction_of_previous_day_log_is_logged_with_its_reason(
        tmp_path, monkeypatch, caplog):
    logs, data = _dirs(tmp_path)
    _write(logs / 'app.log', 400, NOW)
    yesterday = logs / 'app.log.2026-10-26'
    _write(yesterday, 300, YESTERDAY_CLOSE)
    _write(logs / 'error.log.1', 200, _local(2026, 10, 27, 9))
    monkeypatch.setattr(log_retention, 'total_log_budget_bytes', lambda: 700)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    result = maintain_logs(logs, data_dir=data, now=NOW)

    assert _removed(result) == [('app.log.2026-10-26', 'global_budget')]
    [record] = _removal_records(caplog)
    assert record.name == _LOGGER_NAME
    assert record.levelno == logging.WARNING
    assert record.tofu_no_coalesce is True
    assert record.tofu_event_fields == {
        'path': str(yesterday.resolve()), 'bytes': 300,
        'modified_at': time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(YESTERDAY_CLOSE)),
        'reason': 'global_budget', 'budget_bytes': 700,
        'directory_bytes': 900,
    }
    message = record.getMessage()
    assert 'app.log.2026-10-26' in message
    assert 'reason=global_budget' in message


def test_routine_expiry_is_logged_and_dry_run_logs_nothing(
        tmp_path, monkeypatch, caplog):
    logs, data = _dirs(tmp_path)
    _write(logs / 'app.log', 100, NOW)
    expired = logs / 'app.log.2026-10-01'
    _write(expired, 100, _local(2026, 10, 1, 23, 59))
    monkeypatch.setattr(
        log_retention, 'total_log_budget_bytes', lambda: 1 << 30)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    dry = maintain_logs(logs, data_dir=data, dry_run=True, now=NOW)
    assert _removed(dry) == [('app.log.2026-10-01', 'retention_days')]
    assert expired.exists()
    assert _removal_records(caplog) == []

    applied = maintain_logs(logs, data_dir=data, now=NOW)

    assert _removed(applied) == [('app.log.2026-10-01', 'retention_days')]
    [record] = _removal_records(caplog)
    assert record.levelno == logging.INFO
    assert record.tofu_event_fields['reason'] == 'retention_days'
    assert record.tofu_event_fields['path'] == str(expired.resolve())


def test_same_date_chunks_outlive_older_rotations_after_second_size_rotation(
        tmp_path, monkeypatch):
    # 2026-10-27: the budget evicted the 13:58 size chunk app.log.2026-10-27
    # along with app.log.2026-10-26 while older priority-100 rotations stayed,
    # so the 22:25 size rotation reused the freed date name.
    logs, data = _dirs(tmp_path)
    late = _local(2026, 10, 27, 22, 30)
    _write(logs / 'app.log', 100, late)
    _write(logs / 'app.log.2026-10-26', 300, YESTERDAY_CLOSE)
    _write(logs / 'app.log.2026-10-27', 300, _local(2026, 10, 27, 13, 58))
    _write(logs / 'app.log.2026-10-27.00001', 300,
           _local(2026, 10, 27, 22, 25))
    _write(logs / 'error.log.6', 200, _local(2026, 10, 23, 11))
    _write(logs / 'audit.log.4', 200, _local(2026, 10, 23, 15))
    monkeypatch.setattr(log_retention, 'total_log_budget_bytes', lambda: 1000)

    result = maintain_logs(logs, data_dir=data, now=late)

    assert _removed(result) == [
        ('error.log.6', 'global_budget'), ('audit.log.4', 'global_budget')]
    assert sorted(path.name for path in logs.glob('app.log.*')) == [
        'app.log.2026-10-26', 'app.log.2026-10-27', 'app.log.2026-10-27.00001']
