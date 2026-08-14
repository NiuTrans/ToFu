"""Offline restore and handoff safety for the project-local maintenance CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.storage import StorageSupervisor
from lib.storage_sidecar.cli import main


pytestmark = pytest.mark.unit


def test_verified_backup_restore_and_forced_handoff_audit(
        tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=15)
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
            project_root=tmp_path, backend='sqlite', startup_timeout=15) as check:
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
