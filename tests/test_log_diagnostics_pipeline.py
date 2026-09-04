"""Executable contract for the bounded, storage-independent log pipeline."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.incident_journal import IncidentJournalHandler
from lib.log import (
    LogContextFilter,
    bind_log_context,
    clear_log_context,
    log_fields,
    req_id,
    set_log_context,
    set_principal,
    set_req_id,
)
from lib.log_diagnostics import diagnose_logs
from lib.log_policy import LOG_DIRECTORY_MODE, LOG_FILE_MODE
import lib.log_rate_limit as log_rate_limit
from lib.log_rate_limit import DuplicateCoalescingFilter
from lib.log_redaction import RedactingFormatter, redact_text, sanitize_value
import lib.log_retention as log_retention
from lib.log_retention import (
    copytruncate_if_oversize, ensure_private_log_file, maintain_logs,
)


pytestmark = pytest.mark.unit


def _record(level=logging.ERROR, message='failed id=123'):
    return logging.LogRecord(
        name='lib.pipeline', level=level, pathname='/tmp/pipeline.py',
        lineno=42, msg=message, args=(), exc_info=None)


def test_redaction_is_recursive_and_preserves_non_secret_token_counts():
    value = sanitize_value({
        'authorization': 'Bearer abcdefghijk',
        'nested': {'api_key': 'sk-secretvalue', 'token_count': 123,
                   'accessToken': 'camel-case-secret',
                   'secretPreview': 'must-not-survive'},
        'sk-secret-shaped-mapping-key': 'safe value',
        'url': 'https://example.test/x?token=abcdefgh&safe=1',
    })
    rendered = json.dumps(value)
    assert 'abcdefghijk' not in rendered
    assert 'sk-secretvalue' not in rendered
    assert 'camel-case-secret' not in rendered
    assert 'must-not-survive' not in rendered
    assert 'sk-secret-shaped-mapping-key' not in rendered
    assert 'token=abcdefgh' not in rendered
    assert value['nested']['token_count'] == 123
    assert '<redacted>' in rendered


def test_text_redaction_covers_quoted_prefixed_and_raw_provider_credentials():
    raw = '\n'.join((
        'Authorization: Basic dXNlcjpwYXNzd29yZA==',
        '"github_token": "ghp_abcdefghijklmnopqrstuvwxyz"',
        'AWS_SECRET_ACCESS_KEY=supersecretaccessvalue',
        'url=https://person:secretpass@example.test/path',
        'google=AIzaabcdefghijklmnopqrstuvwxyz1234',
        'token_count=987',
    ))
    rendered = redact_text(raw)
    for secret in (
            'dXNlcjpwYXNzd29yZA', 'ghp_abcdefghijklmnopqrstuvwxyz',
            'supersecretaccessvalue', 'secretpass',
            'AIzaabcdefghijklmnopqrstuvwxyz1234'):
        assert secret not in rendered
    assert 'token_count=987' in rendered


def test_redaction_covers_vendor_prefixed_signed_url_queries():
    raw = (
        'GET https://files.example.test/private/archive.zip'
        '?safe=visible&Key-Pair-Id=pair-value'
        '&X-Amz-Signature=signature-value'
        '&X-Amz-Security-Token=session-value'
        '&X-Amz-Credential=credential-value')
    rendered = redact_text(raw)
    for secret in (
            'credential-value', 'session-value', 'signature-value',
            'pair-value'):
        assert secret not in rendered
    assert 'safe=visible' in rendered


def test_redaction_drops_orphaned_tail_of_one_line_secret():
    secret = 'q' * 12_000
    rendered = redact_text(
        'operation failed password=' + secret, max_chars=4096)
    assert 'q' * 16 not in rendered
    assert '<redacted>' in rendered
    assert len(rendered) <= 4096


def test_recursive_sanitizer_honors_total_container_item_ceiling():
    mapping = sanitize_value(
        {'field_%d' % index: index for index in range(50)}, max_items=30)
    sequence = sanitize_value(list(range(50)), max_items=30)
    assert len(mapping) == 30
    assert mapping['<truncated>'] == '21 more field(s)'
    assert len(sequence) == 30
    assert sequence[-1] == '<21 more item(s)>'


def test_recursive_sanitizer_is_fail_closed_for_unprintable_values():
    class Unprintable:
        def __str__(self):
            raise RuntimeError('secret from broken repr')

    sanitized = sanitize_value({'payload': Unprintable()})
    assert sanitized == {'payload': '<unprintable:Unprintable>'}
    assert 'secret from broken repr' not in json.dumps(sanitized)


def test_context_filter_bounds_and_redacts_before_async_queue():
    record = _record(message='bounded context')
    record.tofu_event_name = 'event\n' + ('n' * 500)
    record.tofu_event_fields = {
        'accessToken': 'queue-secret-value',
        'payload': 'p' * 5_000,
        'not_a_number': float('nan'),
        **{'explicit_%d' % index: index for index in range(40)},
    }
    record.tofu_coalesce_note = 'note\n' + ('c' * 500)
    set_req_id('unsafe\n' + ('r' * 200))
    set_principal('key-' + ('k' * 200), 'user-' + ('u' * 200))
    try:
        with bind_log_context(
                conversation_id='conv-' + ('x' * 1_000),
                password='ambient-secret-value'):
            assert LogContextFilter().filter(record) is True
    finally:
        clear_log_context()
        set_req_id('')
        set_principal()

    assert '\n' not in record.tofu_request_id
    assert '\\x0a' in record.tofu_request_id
    assert len(record.tofu_request_id) <= 64
    assert len(record.tofu_key_id) <= 128
    assert len(record.tofu_user_id) <= 128
    assert len(record.tofu_event_name) <= 128
    assert '\n' not in record.tofu_event_name
    assert len(record.tofu_coalesce_note) <= 256
    assert len(record.tofu_event_fields) == 30
    rendered_fields = json.dumps(record.tofu_event_fields, allow_nan=False)
    assert 'queue-secret-value' not in rendered_fields
    assert 'ambient-secret-value' not in rendered_fields
    assert len(record.tofu_event_fields['payload']) <= 600
    assert record.tofu_event_fields['not_a_number'] == '<non-finite-number>'


def test_task_kick_failure_clears_pooled_worker_correlation(monkeypatch):
    import lib.tasks_pkg.orchestrator._run as _run
    set_log_context(task_id='stale-task')
    monkeypatch.setattr(
        _run, 'check_autopilot_kick',
        lambda _task: (_ for _ in ()).throw(RuntimeError('kick failed')))
    with pytest.raises(RuntimeError, match='kick failed'):
        _run.run_task({'id': 'task-correlation-test'})
    assert req_id() == ''
    assert log_fields() == {}


def test_redacting_formatter_bounds_one_physical_record():
    formatter = RedactingFormatter('%(message)s', max_chars=4096)
    text = formatter.format(_record(message='Bearer abcdefgh ' + 'x' * 20_000))
    assert len(text) <= 4096
    assert 'abcdefgh' not in text
    assert 'log policy omitted' in text


def test_duplicate_coalescer_keeps_true_delta_at_checkpoints():
    now = [0.0]
    filt = DuplicateCoalescingFilter(
        burst=2, window_seconds=300, heartbeat_seconds=60,
        clock=lambda: now[0])
    admitted = []
    for _ in range(6):
        record = _record(message='upstream timeout attempt=918273')
        if filt.filter(record):
            admitted.append((record.tofu_window_count,
                             record.tofu_occurrence_delta,
                             record.tofu_coalesce_note))
    assert admitted == [(1, 1, ''), (2, 1, ''),
                        (4, 2, '[coalesced 2 identical occurrences; '
                               'window_total=4] ')]
    now[0] = 61.0
    heartbeat = _record(message='upstream timeout attempt=777777')
    assert filt.filter(heartbeat) is True
    assert heartbeat.tofu_window_count == 7
    assert heartbeat.tofu_occurrence_delta == 3


def test_duplicate_coalescer_flushes_the_quiet_tail_exactly_once():
    now = [0.0]
    filt = DuplicateCoalescingFilter(
        burst=1, window_seconds=300, heartbeat_seconds=60,
        clock=lambda: now[0])
    admitted = []
    for _ in range(6):
        record = _record(message='quiet flood id=918273')
        if filt.filter(record):
            admitted.append(record.tofu_occurrence_delta)
    assert sum(admitted) == 4
    now[0] = 61.0
    pending = filt.drain_pending()
    assert [record.tofu_occurrence_delta for record in pending] == [2]
    assert sum(admitted) + sum(
        record.tofu_occurrence_delta for record in pending) == 6
    assert filt.drain_pending() == []


def test_duplicate_coalescer_worker_exists_only_for_a_suppressed_tail():
    delivered = []
    filt = DuplicateCoalescingFilter(
        burst=1, window_seconds=300, heartbeat_seconds=0.1)
    try:
        assert filt.start_pending_flush(
            lambda record: delivered.append(record.tofu_occurrence_delta)) is True
        assert filt._flush_thread is None

        admitted = []
        for _ in range(3):
            record = _record(message='batch-scoped quiet tail id=918273')
            if filt.filter(record):
                admitted.append(record.tofu_occurrence_delta)
        assert admitted == [1, 1]
        worker = filt._flush_thread
        assert worker is not None and worker.is_alive()

        deadline = time.time() + 2.0
        while not delivered and time.time() < deadline:
            time.sleep(0.01)
        assert delivered == [1]
        worker.join(timeout=1.0)
        assert filt._flush_thread is None
        assert all(state.latest_record is None
                   for state in filt._windows.values())
    finally:
        filt.stop_pending_flush(timeout=1.0)


def test_duplicate_coalescer_thread_start_failure_publishes_exact_delta(
        monkeypatch):
    class UnstartableThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError('thread budget exhausted')

    delivered = []
    filt = DuplicateCoalescingFilter(
        burst=1, window_seconds=300, heartbeat_seconds=60)
    filt.start_pending_flush(delivered.append)
    monkeypatch.setattr(log_rate_limit.threading, 'Thread', UnstartableThread)

    admitted = []
    for _ in range(3):
        record = _record(message='spawn failure tail id=918273')
        if filt.filter(record):
            admitted.append(record.tofu_occurrence_delta)

    assert admitted == [1, 1, 1]
    assert sum(admitted) == 3
    assert filt._flush_thread is None
    assert delivered == []


def test_duplicate_coalescer_carries_expired_tail_with_coherent_count():
    now = [0.0]
    filt = DuplicateCoalescingFilter(
        burst=1, window_seconds=10, heartbeat_seconds=60,
        clock=lambda: now[0])
    admitted = []
    for _ in range(3):
        record = _record(message='window rollover id=918273')
        if filt.filter(record):
            admitted.append(record.tofu_occurrence_delta)
    assert admitted == [1, 1]
    now[0] = 11.0
    rollover = _record(message='window rollover id=777777')
    assert filt.filter(rollover) is True
    assert rollover.tofu_occurrence_delta == 2
    assert rollover.tofu_window_count >= rollover.tofu_occurrence_delta


def test_duplicate_coalescer_cannot_be_broken_by_unprintable_message():
    class Unprintable:
        def __str__(self):
            raise RuntimeError('do not escape the logging call')

    record = _record(message='placeholder')
    record.msg = Unprintable()
    filt = DuplicateCoalescingFilter(burst=1)
    assert filt.filter(record) is True
    assert 'Unprintable' in filt._full_text(record)


def test_critical_records_are_never_coalesced():
    filt = DuplicateCoalescingFilter(burst=1, window_seconds=300,
                                     heartbeat_seconds=300)
    for _ in range(20):
        record = _record(logging.CRITICAL, 'database corruption id=123')
        assert filt.filter(record) is True
        assert record.tofu_occurrence_delta == 1


def test_incident_journal_is_redacted_structured_and_count_aware(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_INCIDENT_LOG_MAX_BYTES', str(1 << 20))
    path = tmp_path / 'incident.jsonl'
    handler = IncidentJournalHandler(str(path))
    record = _record(message=(
        'conversation_id=ms1234567890 task_id=pt_12345678 '
        'Authorization: Bearer abcdefghijk failed'))
    record.tofu_fingerprint = 'deadbeefdeadbeef'
    record.tofu_template = 'request failed'
    record.tofu_occurrence_delta = 64
    record.tofu_window_count = 128
    record.tofu_request_id = 'ghp_requestcredentialvalue'
    record.tofu_key_id = 'sk-directcredentialvalue'
    record.tofu_user_id = 'user-123456'
    record.tofu_event_name = 'request.failed'
    record.tofu_event_fields = {'trace_id': 'trace-123456',
                                'api_key': 'sk-privatevalue'}
    try:
        handler.emit(record)
        handler.flush()
    finally:
        handler.close()
    assert stat.S_IMODE(path.stat().st_mode) == LOG_FILE_MODE
    entry = json.loads(path.read_text(encoding='utf-8'))
    assert entry['schema_version'] == 1
    assert entry['occurrence_delta'] == 64
    assert entry['conversation_id'] == 'ms1234567890'
    assert entry['task_id'] == 'pt_12345678'
    assert entry['trace_id'] == 'trace-123456'
    assert entry['user_id'] == 'user-123456'
    assert 'requestcredentialvalue' not in path.read_text(encoding='utf-8')
    assert 'directcredentialvalue' not in path.read_text(encoding='utf-8')
    assert 'abcdefghijk' not in entry['sample']
    assert entry['fields']['api_key'] == '<redacted>'
    schema = json.loads((Path(__file__).parents[1] / 'contracts' /
                         'log_incident_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(entry)


def test_incident_journal_normalizes_custom_levels_and_malformed_counts(
        tmp_path):
    path = tmp_path / 'incident.jsonl'
    handler = IncidentJournalHandler(str(path))
    record = _record(level=35, message='custom warning')
    record.tofu_fingerprint = 'f' * 200
    record.tofu_template = 'custom warning'
    record.tofu_occurrence_delta = 'not-an-int'
    record.tofu_window_count = None
    try:
        handler.emit(record)
        handler.flush()
    finally:
        handler.close()
    entry = json.loads(path.read_text(encoding='utf-8'))
    assert entry['level'] == 'WARNING'
    assert entry['occurrence_delta'] == 1
    assert len(entry['fingerprint']) == 64
    schema = json.loads((Path(__file__).parents[1] / 'contracts' /
                         'log_incident_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(entry)


def test_incident_sink_failure_never_echoes_original_record(tmp_path, capsys):
    path = tmp_path / 'incident.jsonl'
    handler = IncidentJournalHandler(str(path))

    def fail(_record):
        raise OSError('disk full around secret-record-value')

    handler._sink.emit = fail
    try:
        handler.emit(_record(message='Authorization: secret-record-value'))
        first_notice = capsys.readouterr().err
        handler.emit(_record(message='password=another-secret-record-value'))
        second_notice = capsys.readouterr().err
    finally:
        handler.close()
    assert '[incident-journal] write failed' in first_notice
    assert 'OSError' in first_notice
    assert 'secret-record-value' not in first_notice
    assert second_notice == ''


def test_diagnosis_aggregates_deltas_filters_identity_and_fits_budget(tmp_path):
    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    data_dir.mkdir()
    now = time.time()
    rows = []
    for index in range(40):
        rows.append({
            'schema_version': 1,
            'timestamp': datetime_iso(now - index),
            'level': 'ERROR' if index % 2 else 'WARNING',
            'logger': 'lib.synthetic',
            'fingerprint': 'fp-%02d' % (index % 8),
            'template': 'synthetic failure %d' % (index % 8),
            'occurrence_delta': 5,
            'request_id': 'rid-target' if index < 10 else 'rid-other',
            'user_id': 'user-a' if index % 3 else 'user-b',
            'sample': 'password=verysecret ' + ('detail ' * 100),
        })
    (log_dir / 'incident.jsonl').write_text(
        ''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    report = diagnose_logs(
        log_dir, data_dir=data_dir, request_id='rid-target',
        requesting_user_id='user-a', include_all_users=False,
        max_items=10, max_output_bytes=4096, now=now + 1)
    encoded = json.dumps(report, ensure_ascii=False, separators=(',', ':')).encode()
    assert len(encoded) <= 4096
    assert report['summary']['storage_independent'] is True
    assert report['summary']['occurrences'] == 30
    assert all('verysecret' not in row.get('sample', '')
               for row in report['incidents'])
    assert all('rid-other' not in json.dumps(row)
               for row in report['incidents'])


def test_diagnosis_ignores_nonfinite_time_and_unregistered_pseudo_backup(
        tmp_path):
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    now = time.time()
    valid = {
        'timestamp': datetime_iso(now), 'level': 'ERROR',
        'logger': 'lib.valid', 'fingerprint': 'valid-fp',
        'template': 'valid failure', 'occurrence_delta': 2,
        'sample': 'valid failure',
    }
    malformed_time = dict(valid, fingerprint='nan-fp', timestamp=float('nan'),
                          occurrence_delta=999)
    injected = dict(valid, fingerprint='injected-fp', occurrence_delta=999)
    (log_dir / 'incident.jsonl').write_text(
        json.dumps(valid) + '\n' + json.dumps(malformed_time) + '\n')
    (log_dir / 'incident.jsonl.injected').write_text(json.dumps(injected) + '\n')

    report = diagnose_logs(log_dir, now=now + 1)
    assert report['summary']['occurrences'] == 2
    assert [item['fingerprint'] for item in report['incidents']] == ['valid-fp']


def datetime_iso(timestamp):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def test_selector_miss_never_falls_back_to_unscoped_legacy_log(tmp_path):
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    now = time.time()
    (log_dir / 'incident.jsonl').write_text(json.dumps({
        'timestamp': datetime_iso(now), 'level': 'ERROR', 'logger': 'lib.x',
        'fingerprint': 'fp', 'template': 'private',
        'request_id': 'different', 'sample': 'private incident',
    }) + '\n')
    (log_dir / 'error.log').write_text(
        '2026-08-24 00:00:00 [ERROR] lib.x [MainThread]: unrelated legacy\n')
    report = diagnose_logs(
        log_dir, request_id='missing', include_all_users=True, now=now,
        window_hours=24 * 365, max_output_bytes=4096)
    assert report['summary']['source'] == 'incident_journal'
    assert report['incidents'] == []


def test_user_scoped_diagnosis_fails_closed_for_unattributed_incidents(tmp_path):
    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    now = time.time()
    (log_dir / 'incident.jsonl').write_text(json.dumps({
        'timestamp': datetime_iso(now), 'level': 'ERROR', 'logger': 'lib.x',
        'fingerprint': 'unattributed', 'template': 'private',
        'sample': 'unattributed private incident',
    }) + '\n')
    report = diagnose_logs(
        log_dir, requesting_user_id='user-a', include_all_users=False,
        now=now, max_output_bytes=4096)
    assert report['incidents'] == []
    assert report['summary']['source'] == 'incident_journal'


def test_cli_pretty_output_falls_back_to_compact_to_honor_byte_budget(
        tmp_path, capsys):
    from lib.log_diagnostics import main

    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    data_dir.mkdir()
    now = time.time()
    rows = [{
        'timestamp': datetime_iso(now), 'level': 'ERROR',
        'logger': 'lib.synthetic', 'fingerprint': 'fp-%d' % index,
        'template': 'failure ' + ('detail ' * 80),
        'occurrence_delta': 10, 'sample': 'sample ' * 100,
    } for index in range(20)]
    (log_dir / 'incident.jsonl').write_text(
        ''.join(json.dumps(row) + '\n' for row in rows))
    assert main([
        '--log-dir', str(log_dir), '--data-dir', str(data_dir),
        '--max-bytes', '4096', '--pretty',
    ]) == 0
    captured = capsys.readouterr()
    assert len(captured.out.encode('utf-8')) <= 4096
    report = json.loads(captured.out)
    assert report['summary']['occurrences'] == 200
    assert report['summary']['returned_fingerprints'] == len(
        report['incidents'])


def test_copytruncate_retains_only_bounded_tail_and_descriptor_identity(tmp_path):
    path = tmp_path / 'server-console.log'
    with path.open('ab', buffering=0) as writer:
        writer.write(('old-line\n' * 300).encode())
        before_inode = os.fstat(writer.fileno()).st_ino
        result = copytruncate_if_oversize(
            path, max_bytes=1024, backup_count=2)
        assert len(result['rotated']) == 1
        assert path.stat().st_ino == before_inode
        assert path.stat().st_size == 0
        writer.write(b'new-line\n')
    assert path.read_bytes() == b'new-line\n'
    assert 0 < (tmp_path / 'server-console.log.1').stat().st_size <= 1024
    assert stat.S_IMODE(
        (tmp_path / 'server-console.log.1').stat().st_mode) == LOG_FILE_MODE


def test_copytruncate_keeps_first_line_when_tail_starts_at_boundary(tmp_path):
    path = tmp_path / 'watchdog.log'
    path.write_bytes(b'old-line\nkeep-one\nkeep-two\n')
    retained = len(b'keep-one\nkeep-two\n')
    dry = copytruncate_if_oversize(
        path, max_bytes=retained, trigger_bytes=1,
        backup_count=1, dry_run=True)
    applied = copytruncate_if_oversize(
        path, max_bytes=retained, trigger_bytes=1, backup_count=1)
    assert dry['rotated'][0]['retained_bytes'] == retained
    assert applied['rotated'][0]['retained_bytes'] == retained
    assert (tmp_path / 'watchdog.log.1').read_bytes() == (
        b'keep-one\nkeep-two\n')


def test_copytruncate_drops_an_unbounded_partial_record(tmp_path):
    path = tmp_path / 'watchdog.log'
    path.write_bytes(b'x' * ((1 << 20) + 73))

    dry = copytruncate_if_oversize(
        path, max_bytes=1 << 20, trigger_bytes=1,
        backup_count=1, dry_run=True)
    applied = copytruncate_if_oversize(
        path, max_bytes=1 << 20, trigger_bytes=1, backup_count=1)

    assert dry['rotated'][0]['retained_bytes'] == 0
    assert applied['rotated'][0]['retained_bytes'] == 0
    assert path.stat().st_size == 0
    assert (tmp_path / 'watchdog.log.1').read_bytes() == b''


def test_copytruncate_excludes_an_incomplete_trailing_record(tmp_path):
    path = tmp_path / 'watchdog.log'
    complete = b'keep-complete\n'
    path.write_bytes(b'old-record\n' + complete + b'credential-tail-fragment')

    dry = copytruncate_if_oversize(
        path, max_bytes=40, trigger_bytes=1, backup_count=1, dry_run=True)
    applied = copytruncate_if_oversize(
        path, max_bytes=40, trigger_bytes=1, backup_count=1)

    assert dry['rotated'][0]['retained_bytes'] == len(complete)
    assert applied['rotated'][0]['retained_bytes'] == len(complete)
    assert (tmp_path / 'watchdog.log.1').read_bytes() == complete


def test_external_log_preparation_is_private_and_never_follows_symlinks(
        tmp_path):
    target = tmp_path / 'external-console.log'
    assert ensure_private_log_file(target, create=True) is False
    assert stat.S_IMODE(target.stat().st_mode) == LOG_FILE_MODE
    target.chmod(0o664)
    assert ensure_private_log_file(target) is True
    assert stat.S_IMODE(target.stat().st_mode) == LOG_FILE_MODE

    outside = tmp_path / 'outside.log'
    outside.write_text('owner data')
    outside.chmod(0o644)
    link = tmp_path / 'linked-console.log'
    link.symlink_to(outside)
    with pytest.raises(OSError):
        ensure_private_log_file(link, create=True)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_external_and_core_retention_share_one_periodic_worker(
        tmp_path, monkeypatch):
    core_passes = []
    monkeypatch.setattr(log_retention, '_RUNTIME', None)
    monkeypatch.setattr(log_retention, '_EXTERNAL_LOGS', {})
    monkeypatch.setattr(
        log_retention, 'maintenance_interval_seconds', lambda: 3600.0)
    monkeypatch.setattr(
        log_retention, 'ensure_private_log_file', lambda *_a, **_kw: False)
    monkeypatch.setattr(
        log_retention, 'copytruncate_if_oversize', lambda *_a, **_kw: {})
    monkeypatch.setattr(
        log_retention, 'maintain_logs',
        lambda log_dir, *, data_dir: core_passes.append(
            (log_dir, data_dir)) or {'errors': []})

    external_path = tmp_path / 'external-console.log'
    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    runtime = None
    try:
        log_retention.register_external_log(
            external_path, 'server_console')
        runtime = log_retention._RUNTIME
        first_worker = runtime._thread
        assert first_worker is not None and first_worker.is_alive()

        assert log_retention.start_log_maintenance(
            str(log_dir), str(data_dir)) is False
        assert log_retention._RUNTIME is runtime
        assert runtime._thread is first_worker
        deadline = time.time() + 1.0
        while not core_passes and time.time() < deadline:
            time.sleep(0.01)
        assert core_passes == [(str(log_dir), str(data_dir))]
        assert not hasattr(log_retention, '_EXTERNAL_THREAD')
    finally:
        log_retention.stop_log_maintenance(timeout=1.0)
    assert runtime is not None and runtime._thread is None


def test_quiet_logging_maintenance_wakeup_budget():
    # Historical defaults: coalescer polled every 5s, aggregate every 15s,
    # and core/external retention each every 15m. The new quiet state has one
    # shared 15m retention pass plus one hourly aggregate TTL pass.
    old_wakes_per_hour = 3600 / 5 + 3600 / 15 + 2 * (3600 / 900)
    new_wakes_per_hour = 3600 / 3600 + 3600 / 900

    assert old_wakes_per_hour == 968
    assert new_wakes_per_hour <= 5
    assert 1 - (new_wakes_per_hour / old_wakes_per_hour) >= 0.994


def test_maintenance_dry_run_and_apply_have_matching_rotation_plan(
        tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv('TOFU_WATCHDOG_LOG_MAX_BYTES', str(1 << 20))
    active = log_dir / 'watchdog.log'
    active.write_bytes(b'a' * ((1 << 20) + 123))
    (log_dir / 'watchdog.log.1').write_bytes(b'b' * 101)
    (log_dir / 'watchdog.log.2').write_bytes(b'c' * 102)
    before = {path.name: path.stat().st_size for path in log_dir.iterdir()}

    dry = maintain_logs(log_dir, data_dir=data_dir, dry_run=True)
    assert {path.name: path.stat().st_size for path in log_dir.iterdir()} == before
    applied = maintain_logs(log_dir, data_dir=data_dir)

    assert [(row['path'], row['reason']) for row in dry['removed']] == [
        (row['path'], row['reason']) for row in applied['removed']]
    assert [row['path'] for row in dry['rotated']] == [
        row['path'] for row in applied['rotated']]
    actual_bytes = sum(path.stat().st_size for path in log_dir.iterdir()
                       if path.is_file())
    assert dry['after_bytes_estimate'] == actual_bytes
    assert applied['after_bytes_estimate'] == actual_bytes


def test_maintenance_compacts_only_surviving_closed_rotations(
        tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv('TOFU_APP_LOG_MAX_BYTES', str(1 << 20))
    monkeypatch.setenv('TOFU_APP_LOG_BACKUPS', '1')
    now = time.time()

    doomed = log_dir / 'app.log.old'
    doomed.write_bytes(b'doomed partial record' * 70_000)
    survivor = log_dir / 'app.log.new'
    survivor.write_bytes(b''.join(
        b'line-%06d|payload\n' % index for index in range(70_000)))
    os.utime(doomed, (now - 120, now - 120))
    os.utime(survivor, (now - 60, now - 60))
    survivor_mtime_ns = survivor.stat().st_mtime_ns
    before = {path.name: path.read_bytes() for path in log_dir.iterdir()}

    dry = maintain_logs(log_dir, data_dir=data_dir, dry_run=True, now=now)
    assert {path.name: path.read_bytes()
            for path in log_dir.iterdir()} == before
    assert [(Path(row['path']).name, row['reason'])
            for row in dry['removed']] == [('app.log.old', 'backup_count')]
    assert [Path(row['path']).name for row in dry['compacted']] == [
        'app.log.new']

    applied = maintain_logs(log_dir, data_dir=data_dir, now=now)
    assert [(row['path'], row['reason']) for row in dry['removed']] == [
        (row['path'], row['reason']) for row in applied['removed']]
    assert [(row['path'], row['before_bytes'], row['retained_bytes'])
            for row in dry['compacted']] == [
        (row['path'], row['before_bytes'], row['retained_bytes'])
        for row in applied['compacted']]
    assert not doomed.exists()
    tail = survivor.read_bytes()
    assert 0 < len(tail) <= 1 << 20
    assert tail.endswith(b'\n')
    assert all(len(line) == len(b'line-000000|payload')
               and line.startswith(b'line-') for line in tail.splitlines())
    assert survivor.stat().st_mtime_ns == survivor_mtime_ns
    assert stat.S_IMODE(survivor.stat().st_mode) == LOG_FILE_MODE
    actual_bytes = sum(path.stat().st_size for path in log_dir.iterdir()
                       if path.is_file())
    assert dry['after_bytes_estimate'] == actual_bytes
    assert applied['after_bytes_estimate'] == actual_bytes

    report = diagnose_logs(
        log_dir=log_dir, data_dir=data_dir, max_output_bytes=16_384,
        now=now)
    assert report['maintenance']['compacted_count'] == 1
    assert report['maintenance']['removed_count'] == 1


def test_maintenance_dry_run_creates_no_directories_or_lock_files(tmp_path):
    log_dir = tmp_path / 'absent-logs'
    data_dir = tmp_path / 'absent-data'
    result = maintain_logs(log_dir, data_dir=data_dir, dry_run=True)
    assert result['dry_run'] is True
    assert result['errors'] == []
    assert not log_dir.exists()
    assert not data_dir.exists()


def test_maintenance_hardens_only_declared_log_permissions(tmp_path):
    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    os.chmod(log_dir, 0o775)  # umask-independent precondition for the dry run
    data_dir.mkdir()
    managed = log_dir / 'app.log'
    managed.write_text('managed evidence')
    managed.chmod(0o664)
    rotated = log_dir / 'error.log.1'
    rotated.write_text('rotated evidence')
    rotated.chmod(0o644)
    unmanaged = log_dir / 'operator-export.log'
    unmanaged.write_text('operator owned')
    unmanaged.chmod(0o644)

    before = diagnose_logs(
        log_dir=log_dir, data_dir=data_dir, max_output_bytes=16_384)
    assert before['inventory']['insecure_managed_count'] == 2
    assert any('non-private file modes' in hint
               for hint in before['action_hints'])

    dry = maintain_logs(log_dir, data_dir=data_dir, dry_run=True)
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o775
    assert stat.S_IMODE(managed.stat().st_mode) == 0o664
    applied = maintain_logs(log_dir, data_dir=data_dir)

    dry_actions = [
        (row['kind'], row['path'], row['before_mode'], row['after_mode'])
        for row in dry['permissions_hardened']]
    applied_actions = [
        (row['kind'], row['path'], row['before_mode'], row['after_mode'])
        for row in applied['permissions_hardened']]
    assert dry_actions == applied_actions
    assert stat.S_IMODE(log_dir.stat().st_mode) == LOG_DIRECTORY_MODE
    assert stat.S_IMODE(managed.stat().st_mode) == LOG_FILE_MODE
    assert stat.S_IMODE(rotated.stat().st_mode) == LOG_FILE_MODE
    assert stat.S_IMODE(unmanaged.stat().st_mode) == 0o644

    after = diagnose_logs(
        log_dir=log_dir, data_dir=data_dir, max_output_bytes=16_384)
    assert after['inventory']['insecure_managed_count'] == 0


def test_maintenance_skips_symlinks_and_reports_recent_unmanaged(
        tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    data_dir.mkdir()
    outside = tmp_path / 'outside.log'
    outside.write_text('do not touch')
    (log_dir / 'server-console.log').symlink_to(outside)
    (log_dir / 'custom-live.log').write_bytes(b'x' * 100)
    monkeypatch.setattr('lib.log_retention.total_log_budget_bytes', lambda: 50)

    result = maintain_logs(log_dir, data_dir=data_dir)
    assert outside.read_text() == 'do not touch'
    assert (log_dir / 'custom-live.log').exists()
    assert result['over_budget_bytes'] == 50
    assert result['unmanaged'][0]['recent_protected'] is True


def test_process_faulthandler_policy_preserves_live_and_bounds_dead_files(
        tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setenv('TOFU_FAULT_DUMP_FILES', '1')
    live = log_dir / ('tofu_faulthandler_%d.log' % os.getpid())
    dead_old = log_dir / 'tofu_faulthandler_999999991.log'
    dead_new = log_dir / 'tofu_faulthandler_999999992.log'
    live.write_bytes(b'live')
    dead_old.write_bytes(b'old')
    dead_new.write_bytes(b'new')
    os.utime(dead_old, (time.time() - 100, time.time() - 100))

    dry = maintain_logs(log_dir, data_dir=data_dir, dry_run=True)
    applied = maintain_logs(log_dir, data_dir=data_dir)
    assert [(row['path'], row['reason']) for row in dry['removed']] == [
        (row['path'], row['reason']) for row in applied['removed']]
    assert live.exists()
    assert not dead_old.exists()
    assert dead_new.exists()
    assert applied['faulthandler_process']['live_files'] == 1
    assert applied['faulthandler_process']['retained_dead_files'] == 1


def test_desktop_client_diagnostics_are_redacted_rotated_and_symlink_safe(
        tmp_path, monkeypatch):
    import routes.api_v1.desktop as desktop_route

    path = tmp_path / 'desktop_client_diag.log'
    path.write_bytes(b'x\n' * (1 << 19))
    monkeypatch.setattr(desktop_route, '_DIAG_LOG', str(path))
    monkeypatch.setenv('TOFU_DESKTOP_CLIENT_DIAG_LOG_MAX_BYTES', str(1 << 20))
    desktop_route._append_client_diag_entry({
        'ts': time.time(), 'user_id': 'user-a',
        'text': 'api_key=supersecretvalue failure evidence',
    })
    active = path.read_text(encoding='utf-8')
    assert 'supersecretvalue' not in active
    assert '<redacted>' in active
    assert (tmp_path / 'desktop_client_diag.log.1').stat().st_size == 1 << 20

    outside = tmp_path / 'outside.log'
    outside.write_text('outside-safe')
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError):
        desktop_route._append_client_diag_entry({
            'ts': time.time(), 'user_id': 'user-a', 'text': 'do not write',
        })
    assert outside.read_text() == 'outside-safe'


def test_diagnostics_endpoint_is_admin_only_and_storage_independent(
        tmp_path, monkeypatch):
    import asyncio
    from quart import g

    from lib.api_keys import AuthContext, local_admin_context
    from lib.app_factory import create_base_app
    import lib.runtime_paths as runtime_paths
    import routes.api_v1.logs as logs_route

    log_dir = tmp_path / 'logs'
    data_dir = tmp_path / 'data'
    log_dir.mkdir()
    data_dir.mkdir()
    (log_dir / 'incident.jsonl').write_text(json.dumps({
        'timestamp': datetime_iso(time.time()), 'level': 'ERROR',
        'logger': 'lib.route', 'fingerprint': 'route-fp',
        'template': 'route failed', 'occurrence_delta': 9,
        'sample': 'route failed safely',
    }) + '\n')
    monkeypatch.setattr(logs_route, 'LOG_DIR', str(log_dir))
    monkeypatch.setattr(runtime_paths, 'data_root', lambda: str(data_dir))

    def make_app(context):
        app = create_base_app(__name__, {'TESTING': True})

        @app.before_request
        async def _grant():
            g.auth_ctx = context
            g.rate_decision = None

        app.register_blueprint(logs_route.api_v1_logs_bp)
        return app

    async def fetch(app):
        response = await app.test_client().get(
            '/api/v1/logs/diagnostics?max_bytes=4096')
        return response.status_code, await response.get_json()

    loop = asyncio.new_event_loop()
    try:
        status, body = loop.run_until_complete(fetch(make_app(local_admin_context())))
        assert status == 200
        assert body['ok'] is True
        assert body['summary']['storage_independent'] is True
        assert body['summary']['occurrences'] == 9

        chat_only = AuthContext(
            key_id='chat-key', scopes=frozenset({'chat'}))
        status, body = loop.run_until_complete(fetch(make_app(chat_only)))
        assert status == 403
        assert body['ok'] is False
    finally:
        loop.close()
