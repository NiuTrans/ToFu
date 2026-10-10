"""A mirrored native backup must leave the same durable UI witness as a file backup."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.database_panel import backup, config_store

pytestmark = pytest.mark.unit


@pytest.fixture
def native_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, '_path', lambda: str(tmp_path / 'config/panel.json'))
    source = tmp_path / 'authority-backups'
    (source / 'snapshots').mkdir(parents=True)
    (source / 'blocks').mkdir()
    (source / 'blocks/original').write_bytes(b'complete original evidence')
    manifest = source / 'snapshots/snapshot.manifest'
    manifest.write_bytes(b'published authority manifest')
    target = tmp_path / 'stable-backups'
    summary = {'ok': True, 'backend': 'tofudb', 'artifact_kind': 'directory',
               'backup': str(source), 'manifest': str(manifest), 'bytes': 54,
               'sha256': 'authority-manifest-digest'}
    requests = []

    def maintenance(operation, *, deadline):
        requests.append((operation, deadline))
        return summary

    monkeypatch.setattr('lib.storage.service.get_storage_client',
                        lambda **kwargs: SimpleNamespace(maintenance=maintenance))
    monkeypatch.setattr('lib.database_panel.switch.resolve_authority_path', Path)
    config_store.save_preferences(backup_enabled=True,
                                  backup_directory=str(target))
    return source, target, requests


def test_successful_native_mirror_persists_last_backup_without_erasing_preferences(native_snapshot):
    source, target, requests = native_snapshot
    result = backup.run_backup()
    # Read from the normal durable config owner after the request has returned.
    preferences = config_store.load_preferences()
    witness = preferences['lastBackup']
    assert witness is not None and witness['ok'] is True
    assert witness['directory'] == str(target)
    assert witness['bytes'] == result['bytes'] == 54
    assert witness['at'] > 0 and witness['error'] == ''
    assert preferences['backup'] == {'enabled': True, 'directory': str(target)}
    assert result['target'] == str(target)  # preserve the native response contract
    mirror = target / result['files'][0]
    assert (mirror / 'blocks/original').read_bytes() == (source / 'blocks/original').read_bytes()
    assert (mirror / 'snapshots/snapshot.manifest').read_bytes() == (source / 'snapshots/snapshot.manifest').read_bytes()
    assert len(requests) == 1 and requests[0][0] == 'system.backup'


@pytest.mark.parametrize('failure', ['missing_manifest', 'mirror_io'])
def test_incomplete_native_mirror_persists_failure_not_success(native_snapshot, monkeypatch, failure):
    source, target, _ = native_snapshot
    if failure == 'missing_manifest':
        (source / 'snapshots/snapshot.manifest').unlink()
    else:
        def fail_copy(*args, **kwargs):
            raise OSError('injected mirror write failure')
        monkeypatch.setattr(backup.shutil, 'copytree', fail_copy)
    with pytest.raises(backup.BackupError):
        backup.run_backup()
    preferences = config_store.load_preferences()
    assert preferences['lastBackup']['ok'] is False
    assert preferences['lastBackup']['error']
    assert preferences['backup'] == {'enabled': True, 'directory': str(target)}
