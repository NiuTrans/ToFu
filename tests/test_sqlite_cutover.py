"""Cutover is a separate, fail-closed boundary from online snapshot copy."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from lib.storage_sidecar.cutover import (
    AUTHORITY_FILE,
    SQLiteCutoverError,
    activate_candidate,
    pg_history_exists,
    validate_authority_marker,
    validate_candidate,
)


pytestmark = pytest.mark.unit


def _candidate_and_report(data: Path, *, cutover_ready=True):
    candidate = data / 'tofu.db.pg-migration-test'
    db = sqlite3.connect(candidate)
    db.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)')
    db.execute('INSERT INTO items VALUES (1, ?)', ('kept',))
    db.commit()
    db.close()
    stat = candidate.stat()
    signature = {
        'rows': 1,
        'xor_sha256': hashlib.sha256(b'row').hexdigest(),
        'sum_sha256': hashlib.sha256(b'row').hexdigest(),
        'canonical_bytes': 3,
    }
    report = data / 'migration.report.json'
    report.write_text(json.dumps({
        'version': 1,
        'status': 'verified' if cutover_ready else 'snapshot_verified',
        'cutover_ready': cutover_ready,
        'cutover_reason': ('source_quiesced_and_server_default_read_only'
                           if cutover_ready else
                           'source_writes_were_not_declared_quiesced'),
        'selected_tables': 'all',
        'target': str(candidate.resolve()),
        'target_size_bytes': stat.st_size,
        'target_mtime_ns': stat.st_mtime_ns,
        'source_snapshot': '1:1:',
        'integrity_check': 'ok',
        'foreign_key_check': 'ok',
        'cross_reopen_check': 'ok',
        'tables': {
            'items': {
                'status': 'verified',
                'source': signature,
                'target': dict(signature),
            },
        },
    }), encoding='utf-8')
    return candidate, report


def test_online_writable_source_snapshot_can_never_activate(tmp_path):
    candidate, report = _candidate_and_report(
        tmp_path, cutover_ready=False)
    with pytest.raises(SQLiteCutoverError, match='not cutover-ready'):
        validate_candidate(candidate, report, tmp_path)


def test_candidate_mutation_after_report_is_rejected(tmp_path):
    candidate, report = _candidate_and_report(tmp_path)
    with candidate.open('ab') as handle:
        handle.write(b'x')
    with pytest.raises(SQLiteCutoverError, match='size changed'):
        validate_candidate(candidate, report, tmp_path)


def test_activation_archives_stale_fallback_and_attests_authority(tmp_path):
    candidate, report = _candidate_and_report(tmp_path)
    canonical = tmp_path / 'tofu.db'
    canonical.write_bytes(b'stale pre-PG fallback')

    marker = activate_candidate(
        candidate=candidate,
        report_path=report,
        canonical_path=canonical,
        data_dir=tmp_path,
        owner_approved=True,
        source_still_read_only=True,
    )

    assert not candidate.exists()
    assert sqlite3.connect(canonical).execute(
        'SELECT value FROM items').fetchone()[0] == 'kept'
    archive = Path(marker['previous_sqlite_archive'])
    assert archive.read_bytes() == b'stale pre-PG fallback'
    assert (tmp_path / AUTHORITY_FILE).is_file()
    assert validate_authority_marker(canonical, tmp_path)['status'] == 'active'


def test_activation_requires_both_human_approval_and_live_read_only(tmp_path):
    candidate, report = _candidate_and_report(tmp_path)
    canonical = tmp_path / 'tofu.db'
    with pytest.raises(SQLiteCutoverError, match='owner approval'):
        activate_candidate(
            candidate=candidate, report_path=report,
            canonical_path=canonical, data_dir=tmp_path,
            owner_approved=False, source_still_read_only=True)
    with pytest.raises(SQLiteCutoverError, match='not still quiesced'):
        activate_candidate(
            candidate=candidate, report_path=report,
            canonical_path=canonical, data_dir=tmp_path,
            owner_approved=True, source_still_read_only=False)


def test_activation_failure_restores_candidate_and_old_fallback(
        tmp_path, monkeypatch):
    import lib.storage_sidecar.cutover as cutover

    candidate, report = _candidate_and_report(tmp_path)
    canonical = tmp_path / 'tofu.db'
    canonical.write_bytes(b'old fallback')

    def _fail_marker(*_args, **_kwargs):
        raise OSError('injected marker fsync failure')

    monkeypatch.setattr(cutover, '_atomic_json', _fail_marker)
    with pytest.raises(OSError, match='injected marker'):
        cutover.activate_candidate(
            candidate=candidate, report_path=report,
            canonical_path=canonical, data_dir=tmp_path,
            owner_approved=True, source_still_read_only=True)

    assert candidate.is_file()
    assert canonical.read_bytes() == b'old fallback'
    assert not (tmp_path / AUTHORITY_FILE).exists()
    assert not list(tmp_path.glob('tofu.db.pre-pg-archive-*'))


def test_marker_cleanup_failure_never_re_attests_stale_fallback(
        tmp_path, monkeypatch):
    import lib.storage_sidecar.cutover as cutover

    candidate, report = _candidate_and_report(tmp_path)
    canonical = tmp_path / 'tofu.db'
    canonical.write_bytes(b'old fallback')
    marker_path = tmp_path / AUTHORITY_FILE

    def _half_write_marker(path, _value):
        path.write_text('{"incomplete": true}', encoding='utf-8')
        raise OSError('injected post-rename marker failure')

    real_unlink = Path.unlink

    def _refuse_marker_unlink(path, *args, **kwargs):
        if path == marker_path:
            raise OSError('injected marker cleanup failure')
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(cutover, '_atomic_json', _half_write_marker)
    monkeypatch.setattr(Path, 'unlink', _refuse_marker_unlink)
    with pytest.raises(SQLiteCutoverError, match='rollback was incomplete'):
        cutover.activate_candidate(
            candidate=candidate, report_path=report,
            canonical_path=canonical, data_dir=tmp_path,
            owner_approved=True, source_still_read_only=True)

    # The new verified DB remains canonical; the stale fallback remains a
    # separate archive. They are never swapped beneath an undeletable marker.
    assert not candidate.exists()
    assert sqlite3.connect(canonical).execute(
        'SELECT value FROM items').fetchone()[0] == 'kept'
    archives = list(tmp_path.glob('tofu.db.pre-pg-archive-*'))
    assert len(archives) == 1
    assert archives[0].read_bytes() == b'old fallback'


def test_pg_history_probe_is_bounded_and_detects_cluster(tmp_path):
    pgdata = tmp_path / 'pgdata'
    pgdata.mkdir()
    (tmp_path / 'pg_backups').mkdir()
    # Empty directories may be created by packaging/schedulers on a fresh
    # SQLite install and are not themselves PostgreSQL authority history.
    assert pg_history_exists(pgdata, tmp_path) is False
    (pgdata / 'PG_VERSION').write_text('18', encoding='ascii')
    assert pg_history_exists(pgdata, tmp_path) is True


def test_activation_command_requires_verified_dsn_secret(tmp_path):
    from scripts import activate_sqlite_cutover as command

    insecure = tmp_path / 'postgres-dsn'
    insecure.write_text(
        'postgresql://db.example/tofu?sslmode=require', encoding='utf-8')
    with pytest.raises(
            command.SQLiteCutoverError, match='sslmode=verify-full'):
        command._read_dsn_secret(insecure)
    secure = tmp_path / 'postgres-dsn-verified'
    secure.write_text(
        'postgresql://db.example/tofu?sslmode=verify-full', encoding='utf-8')
    assert command._read_dsn_secret(secure).endswith('sslmode=verify-full')


def test_activation_next_step_uses_supported_deployment_configuration(
        tmp_path, monkeypatch, capsys):
    from scripts import activate_sqlite_cutover as command

    dsn_file = tmp_path / 'postgres-dsn'
    dsn_file.write_text(
        'postgresql://db.example/tofu?sslmode=verify-full', encoding='utf-8')
    monkeypatch.setattr(command, 'validate_candidate', lambda *_args: {})
    monkeypatch.setattr(command, '_pg_quiescence', lambda _dsn: {
        'default_transaction_read_only': True,
        'other_client_sessions': 0,
    })
    monkeypatch.setattr(command, 'activate_candidate', lambda **_kwargs: {})

    assert command.main([
        '--candidate', str(tmp_path / 'candidate.sqlite3'),
        '--report', str(tmp_path / 'report.json'),
        '--canonical', str(tmp_path / 'tofu.db'),
        '--postgres-dsn-file', str(dsn_file),
        '--apply',
        '--owner-approved',
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    next_step = result['next_step']
    assert 'TOFU_DEPLOYMENT_MODE=personal' in next_step
    assert 'TOFU_PROCESS_ROLE=all' in next_step
    assert 'TOFU_DB_BACKEND=sqlite' not in next_step
    assert 'remove TOFU_POSTGRES_DSN_FILE' in next_step
