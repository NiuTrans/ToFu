"""Bounded, privacy-preserving live-log evidence extraction."""
from scripts.audit_storage_logs import scan_log
import pytest

pytestmark = pytest.mark.unit


def test_counts_records_not_tracebacks_and_redacts_content(tmp_path):
    path = tmp_path / 'app.log'
    path.write_text('2026-09-16 17:00:00 [ERROR] lib.storage old: database_integrity\n'
        '2026-09-16 18:00:00 [ERROR] lib.agent failure event=messages_snapshot secret=PRIVATE database_resource_exhausted\n'
        'Traceback: database_resource_exhausted\n'
        '2026-09-16 18:00:01 [INFO] lib.storage database_timeout\n')
    result = scan_log(path, since='2026-09-16 17:30:00')
    assert result['matching_log_records'] == 1
    assert result['codes'] == {'database_resource_exhausted': 1}
    assert 'PRIVATE' not in str(result)
    assert result['requested_start_in_window'] is True


def test_tail_window_is_bounded_and_reports_missing_history(tmp_path):
    path = tmp_path / 'app.log'
    path.write_text('x' * 1000 + '\n2026-09-16 18:00:00 [ERROR] lib.storage database_timeout\n')
    result = scan_log(path, since='2026-09-16 17:00:00', maximum_bytes=150)
    assert result['bytes_read'] == 150
    assert result['tail_truncated'] is True
    assert result['requested_start_in_window'] is False
    assert result['matching_log_records'] == 1
