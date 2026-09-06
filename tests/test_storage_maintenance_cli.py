"""Offline restore and handoff safety for the project-local maintenance CLI."""

from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/business.py'}

import json
import os
from pathlib import Path
import socket
import sqlite3

import pytest

from lib.storage import StorageSupervisor
from lib.storage_sidecar import cli as cli_module
from lib.storage_sidecar import fastpath
from lib.storage_sidecar.cli import main
from lib.storage_sidecar.schema import SCHEMA_VERSION


pytestmark = pytest.mark.unit


def test_backup_commands_share_the_launch_probed_timeout(monkeypatch):
    monkeypatch.setattr(
        cli_module, 'storage_backup_timeout_seconds', lambda: 21_600)

    assert cli_module._maintenance_timeout_seconds('backup') == 21_600
    assert cli_module._maintenance_timeout_seconds('baseline') == 3600


def test_cli_configuration_is_scoped_to_one_invocation(
        tmp_path: Path, monkeypatch, capsys):
    scoped_names = (
        'TOFU_STORAGE_TOKEN',
        'TOFU_STORAGE_PROJECT_ROOT',
        'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE',
        'TOFU_STORAGE_TEST_BACKEND',
    )
    for name in scoped_names:
        monkeypatch.delenv(name, raising=False)

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'cutover-check',
    ]) == 0
    capsys.readouterr()

    assert all(name not in os.environ for name in scoped_names)


def test_verified_backup_restore_and_forced_handoff_audit(
        tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        supervisor.client.command(
            'record.put',
            {'namespace': 'restore', 'key': 'value', 'value': 'before'},
            'restore-before',
        )
        backup_result = supervisor.client.maintenance(
            'system.backup', deadline=30)
        backup = tmp_path / backup_result['backup']
        manifest = tmp_path / backup_result['manifest']
        assert len(backup_result['sha256']) == 64
        assert json.loads(manifest.read_text(encoding='utf-8'))['sha256'] == (
            backup_result['sha256'])
        baseline = supervisor.client.maintenance(
            'system.baseline', deadline=30)
        assert baseline['schema_version'] == SCHEMA_VERSION
        assert any(
            table['name'] == 'storage_records' and table['rows'] == 1
            for table in baseline['tables'])
        supervisor.client.command(
            'record.put',
            {'namespace': 'restore', 'key': 'value', 'value': 'after'},
            'restore-after',
        )
    finally:
        supervisor.stop()

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'restore', str(backup), '--confirm',
    ]) == 0
    restored_output = json.loads(capsys.readouterr().out)
    assert restored_output['ok'] is True
    assert restored_output['previous'].startswith('data/backups/pre-restore-')

    with StorageSupervisor(
            project_root=tmp_path, backend='sqlite', startup_timeout=60) as check:
        row = check.client.query(
            'record.get', {'namespace': 'restore', 'key': 'value'})
        assert row['value'] == 'before'

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'handoff', '--target-host', 'new-host', '--force',
    ]) == 2
    capsys.readouterr()
    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'handoff', '--target-host', 'new-host', '--force',
        '--reason', 'old host lost after lease expiry',
    ]) == 0
    handoff = json.loads(capsys.readouterr().out)['handoff']
    assert handoff['forced'] is True
    assert handoff['target_host'] == 'new-host'
    audit = tmp_path / 'data' / 'storage-handoff-audit.jsonl'
    assert json.loads(audit.read_text(encoding='utf-8').splitlines()[-1]) == handoff


def test_restore_rejects_a_backup_that_no_longer_matches_its_manifest(
        tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    with StorageSupervisor(
            project_root=tmp_path, backend='sqlite', startup_timeout=60) as owner:
        result = owner.client.maintenance('system.backup', deadline=30)
    backup = tmp_path / result['backup']
    with backup.open('ab') as stream:
        stream.write(b'tampered')

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'restore', str(backup), '--confirm',
    ]) == 2
    error = json.loads(capsys.readouterr().err)
    assert 'manifest' in error['message']


def test_retire_fastpath_materializes_shadow_and_preserves_rollback(
        tmp_path: Path, capsys):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    connection = sqlite3.connect(classic)
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO storage_meta VALUES ('authority_uuid', 'old-classic')")
    connection.execute('CREATE TABLE items(value TEXT NOT NULL)')
    connection.execute("INSERT INTO items VALUES ('old')")
    connection.commit()
    connection.close()

    shadow_dir = data_dir / fastpath.SHADOW_DIRNAME
    shadow_dir.mkdir()
    snapshot, _shadow_wal = fastpath.shadow_paths(shadow_dir)
    connection = sqlite3.connect(snapshot)
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO storage_meta VALUES ('authority_uuid', 'shadow-current')")
    connection.execute('CREATE TABLE items(value TEXT NOT NULL)')
    connection.execute("INSERT INTO items VALUES ('current')")
    connection.commit()
    connection.close()
    fastpath.write_shadow_manifest(shadow_dir, {
        'format': 'tofu.fastpath-shadow.v1',
        'generation': 9,
        'authority_uuid': 'shadow-current',
        'snapshot_bytes': snapshot.stat().st_size,
        'wal_shipped_bytes': 0,
    })
    (shadow_dir / f'{fastpath.SNAPSHOT_NAME}.rebase-tmp').write_bytes(
        b'incomplete-rebase')
    (shadow_dir / f'{fastpath.SNAPSHOT_NAME}.rebase-state.json').write_text(
        '{}', encoding='utf-8')

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'retire-fastpath', '--confirm',
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['generation'] == 9
    assert result['next_step'] == (
        'set TOFU_STORAGE_FASTPATH=off before starting Tofu')
    assert not shadow_dir.exists()

    connection = sqlite3.connect(classic)
    assert connection.execute('SELECT value FROM items').fetchone() == ('current',)
    connection.close()
    previous = tmp_path / result['previous']
    connection = sqlite3.connect(previous)
    assert connection.execute('SELECT value FROM items').fetchone() == ('old',)
    connection.close()
    retired_shadow = tmp_path / result['retired_shadow']
    assert (retired_shadow / fastpath.SNAPSHOT_NAME).is_file()
    assert result['reclaimed_private_bytes'] == len(b'incomplete-rebase') + 2
    assert not (retired_shadow /
                f'{fastpath.SNAPSHOT_NAME}.rebase-tmp').exists()
    assert not (retired_shadow /
                f'{fastpath.SNAPSHOT_NAME}.rebase-state.json').exists()


def test_retire_fastpath_prefers_verified_local_crash_tail(
        tmp_path: Path, monkeypatch, capsys):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    classic = data_dir / 'tofu.db'
    connection = sqlite3.connect(classic)
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO storage_meta VALUES ('authority_uuid', 'old-classic')")
    connection.commit()
    connection.close()

    shadow_dir = data_dir / fastpath.SHADOW_DIRNAME
    shadow_dir.mkdir()
    snapshot, _shadow_wal = fastpath.shadow_paths(shadow_dir)
    connection = sqlite3.connect(snapshot)
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO storage_meta VALUES ('authority_uuid', 'shared-lineage')")
    connection.execute('CREATE TABLE items(value TEXT NOT NULL)')
    connection.execute("INSERT INTO items VALUES ('shadow')")
    connection.commit()
    connection.close()
    fastpath.write_shadow_manifest(shadow_dir, {
        'format': 'tofu.fastpath-shadow.v1',
        'generation': 4,
        'authority_uuid': 'shared-lineage',
        'snapshot_bytes': snapshot.stat().st_size,
        'wal_shipped_bytes': 0,
    })

    local_dir = tmp_path / 'local-front'
    local_dir.mkdir()
    local = local_dir / 'tofu.db'
    connection = sqlite3.connect(local)
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, '
        'meta_value TEXT NOT NULL)')
    connection.execute(
        "INSERT INTO storage_meta VALUES ('authority_uuid', 'shared-lineage')")
    connection.execute('CREATE TABLE items(value TEXT NOT NULL)')
    connection.execute("INSERT INTO items VALUES ('local-tail')")
    connection.commit()
    connection.close()
    fastpath.write_local_manifest(local_dir, {
        'authority_uuid': 'shared-lineage',
        'shadow_dir': str(shadow_dir),
    })
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH_DIR', str(local_dir))

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'retire-fastpath', '--confirm',
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['source'] == 'verified_local_front'
    connection = sqlite3.connect(classic)
    assert connection.execute('SELECT value FROM items').fetchone() == (
        'local-tail',)
    connection.close()


def test_cutover_check_reports_static_debt_and_live_owners_as_json(
        tmp_path: Path, capsys):
    source = tmp_path / 'lib' / 'business.py'
    source.parent.mkdir(parents=True)
    source.write_text(
        "import sqlite3\nsqlite3.connect('owned.db')\n",
        encoding='utf-8')
    data = tmp_path / 'data'
    data.mkdir()
    (data / '.server.lock').write_text(
        f'{os.getpid()}@{socket.gethostname()}\n', encoding='utf-8')

    assert main([
        '--backend', 'sqlite', '--project-root', str(tmp_path),
        'cutover-check',
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['ready'] is False
    assert report['static_boundary']['files'] == ['lib/business.py']
    assert report['ownership']['quiescent'] is False
    assert report['ownership']['owners'][0]['active'] is True


def test_sqlite_cli_backup_and_baseline_are_offline_and_non_migrating(
        tmp_path: Path, capsys):
    data = tmp_path / 'data'
    data.mkdir()
    authority = data / 'tofu.db'
    connection = sqlite3.connect(authority)
    connection.execute('CREATE TABLE legacy_items(id INTEGER PRIMARY KEY, value TEXT)')
    connection.execute('INSERT INTO legacy_items(value) VALUES (?)', ('kept',))
    connection.commit()
    connection.close()

    base = ['--backend', 'sqlite', '--project-root', str(tmp_path)]
    assert main([*base, 'baseline']) == 0
    baseline = json.loads(capsys.readouterr().out)
    assert baseline['schema_versions'] == {
        'application': None, 'storage': None,
    }
    assert baseline['table_count'] == 1
    assert baseline['index_count'] == 0
    assert baseline['row_count'] == 1
    assert len(baseline['schema_sha256']) == 64
    report = json.loads(
        (tmp_path / baseline['report']).read_text(encoding='utf-8'))
    assert report['tables'] == [{'name': 'legacy_items', 'rows': 1}]
    assert report['schema_sha256'] == baseline['schema_sha256']

    assert main([*base, 'backup']) == 0
    backup = json.loads(capsys.readouterr().out)
    artifact = tmp_path / backup['backup']
    manifest = tmp_path / backup['manifest']
    assert artifact.is_file()
    assert json.loads(manifest.read_text(encoding='utf-8'))[
        'source_mode'] == 'offline-exclusive'

    check = sqlite3.connect(authority)
    try:
        tables = {
            row[0] for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        check.close()
    assert tables == {'legacy_items'}


def test_whole_document_conversation_migration_command_is_retired(
        tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as raised:
        main([
            '--backend', 'sqlite', '--project-root', str(tmp_path),
            'migrate-conversations', '--confirm', '--batch-size', '1',
        ])
    assert raised.value.code == 2
    assert "invalid choice: 'migrate-conversations'" in capsys.readouterr().err


def test_scheduler_migration_command_is_retired(
        tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as raised:
        main([
            '--backend', 'sqlite', '--project-root', str(tmp_path),
            'migrate-scheduled-tasks', '--confirm',
        ])
    assert raised.value.code == 2
    assert "invalid choice: 'migrate-scheduled-tasks'" in (
        capsys.readouterr().err)
