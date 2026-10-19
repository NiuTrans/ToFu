"""Scratch-only next-launch configuration and native retained-selection fences."""
from pathlib import Path
from unittest.mock import patch
import json

import pytest

from lib.storage_authority_sql import native_fastpath_settings as settings
from lib.storage_authority_sql.storage_capabilities import MountDescription
from lib.database_panel import probe

pytestmark = pytest.mark.unit


@pytest.fixture
def scratch(tmp_path):
    root = tmp_path / 'data'
    (root / 'tofudb-authority').mkdir(parents=True)
    base = tmp_path / 'local'
    base.mkdir()
    env_file = tmp_path / '.env'
    env_file.write_text('UNRELATED=preserved\n')
    return root, base, env_file


def save(scratch, env, enabled=True, directory=None, runtime=None):
    root, base, env_file = scratch
    return settings.save_native_fastpath_settings(
        root, env_file, enabled, str(base) if directory is None else directory,
        runtime or {'ready': False}, environ=env)


def test_save_is_next_launch_only_and_keeps_sqlite_independent(scratch):
    root, base, env_file = scratch
    env = {'TOFU_STORAGE_FASTPATH': 'required', 'TOFU_STORAGE_FASTPATH_DIR': '/sqlite'}
    with patch.object(settings, 'describe_mount', return_value=MountDescription(
            'ext4', '/', 'local-block', 'persistent')):
        state = save(scratch, env)
    assert state['enabled'] and state['restartRequired']
    assert state['active'] is None and state['activeDirectory'] == ''
    assert env[settings.FASTPATH_ENV] == str(base)
    assert env['TOFU_STORAGE_FASTPATH'] == 'required'
    assert 'UNRELATED=preserved' in env_file.read_text()
    assert not list((root / 'tofudb-authority').iterdir())
    assert not list(base.iterdir())
    assert not (root / 'tofudb-fastpath-shadow').exists()
    # Subsequent panel saves are not mistaken for inherited overrides.
    save(scratch, env, enabled=False)
    assert env[settings.FASTPATH_ENV] == ''


@pytest.mark.parametrize('directory', ['', 'relative', '/does-not-exist'])
def test_invalid_base_does_not_change_configuration(scratch, directory):
    original = scratch[2].read_bytes()
    with pytest.raises(ValueError):
        save(scratch, {}, directory=directory)
    assert scratch[2].read_bytes() == original


@pytest.mark.parametrize('storage_class,persistence', [
    ('memory-filesystem', 'ephemeral'), ('network-filesystem', 'persistent'),
    ('unknown', 'unknown'), ('local-block', 'ephemeral')])
def test_rejects_unverified_or_volatile_mount(scratch, storage_class, persistence):
    with patch.object(settings, 'describe_mount', return_value=MountDescription(
            'test', '/', storage_class, persistence)), pytest.raises(ValueError):
        save(scratch, {})


@pytest.mark.parametrize('env', [
    {'TOFUDB_TRANSPORT': 'embedded'}, {'TOFU_DB_DURABILITY': 'normal'},
    {'TOFU_DB_WAL_FLUSH_INTERVAL_MS': '50'}])
def test_native_launch_constraints_are_not_silently_rewritten(scratch, env):
    with pytest.raises(ValueError):
        save(scratch, env)
    assert settings.FASTPATH_ENV not in env


def test_external_environment_override_is_typed_conflict(scratch):
    with pytest.raises(settings.NativeFastpathSettingsError) as exc:
        save(scratch, {settings.FASTPATH_ENV: '/external'})
    assert exc.value.code == 'fastpath_env_override'
    assert settings.FASTPATH_ENV not in scratch[2].read_text()


@pytest.mark.parametrize('marker', ['FASTPATH', 'FASTPATH_PENDING'])
def test_retained_marker_refuses_disable_or_change_and_allows_same_path(scratch, marker):
    root, base, env_file = scratch
    env = {settings.FASTPATH_ENV: str(base)}
    authority, front, _ = settings._selection(root, str(base))
    (authority / marker).write_text(json.dumps({'local_authority': front}))
    original = env_file.read_bytes()
    for enabled, directory in [(False, ''), (True, str(base.parent / 'other'))]:
        with pytest.raises(settings.NativeFastpathSettingsError) as exc:
            save(scratch, env, enabled, directory)
        assert exc.value.code == 'fastpath_locked'
        assert 'fastpath-export' in str(exc.value)
    state = save(scratch, env)
    assert state['locked'] and state['active'] is None
    assert env_file.read_bytes() == original


def test_damaged_marker_never_allows_save(scratch):
    root, base, _ = scratch
    (root / 'tofudb-authority' / 'FASTPATH').write_bytes(b'bad')
    with pytest.raises(settings.NativeFastpathSettingsError) as exc:
        save(scratch, {settings.FASTPATH_ENV: str(base)})
    assert exc.value.code == 'fastpath_locked'


def test_readiness_witness_not_preferences_controls_active(scratch):
    root, base, _ = scratch
    _, front, shadow = settings._selection(root, str(base))
    runtime = {'ready': True, 'backend': 'tofudb', 'native_fastpath': {
        'localAuthority': front, 'shadowDirectory': shadow}}
    env = {settings.FASTPATH_ENV: str(base)}
    state = settings.native_fastpath_state(root, runtime, env)
    assert state['active'] and state['locked'] and not state['restartRequired']
    assert state['authorityPath'] == state['activeDirectory'] == front
    assert state['shadowPath'] == shadow
    with pytest.raises(settings.NativeFastpathSettingsError):
        save(scratch, env, enabled=False, runtime=runtime)
    state = settings.native_fastpath_state(root, {'ready': True, 'backend': 'tofudb'}, env)
    assert state['active'] is False and state['restartRequired']
    assert state['authorityPath'] == str(root / 'tofudb-authority')


def test_sqlite_status_is_not_configured_equals_active():
    with patch.dict('os.environ', {'TOFU_STORAGE_FASTPATH': 'auto',
                                  'TOFU_STORAGE_FASTPATH_DIR': '/next'}):
        unknown = probe.fastpath_state('sqlite', {'ready': True, 'backend': 'sqlite'})
        inactive = probe.fastpath_state('sqlite', {'ready': True, 'backend': 'sqlite',
                                                'fastpath_active': False,
                                                'authority_path': '/live/tofu.db'})
    assert unknown['enabled'] and unknown['active'] is None
    assert inactive['enabled'] and inactive['active'] is False
    assert inactive['restartRequired'] and inactive['authorityPath'] == '/live/tofu.db'


def test_failed_dotenv_publication_does_not_change_environment(scratch):
    env = {}
    with patch.object(settings, 'describe_mount', return_value=MountDescription(
            'ext4', '/', 'local-block', 'persistent')), patch.object(
                settings, 'update_dotenv_file', side_effect=OSError('injected')):
        with pytest.raises(OSError):
            save(scratch, env)
    assert settings.FASTPATH_ENV not in env


def test_readonly_base_rejected_without_creation(scratch):
    with patch.object(settings, 'describe_mount', return_value=MountDescription(
            'ext4', '/', 'local-block', 'persistent')), patch.object(
                settings.os, 'access', return_value=False), pytest.raises(ValueError):
        save(scratch, {})
    assert not list(scratch[1].iterdir())


def test_namespace_symlink_and_authority_overlap_rejected(scratch):
    root, base, _ = scratch
    with pytest.raises(ValueError):
        save(scratch, {}, directory=str(root))
    target = base.parent / 'redirect'
    target.mkdir()
    (base / 'tofudb-fastpath').symlink_to(target, target_is_directory=True)
    with patch.object(settings, 'describe_mount', return_value=MountDescription(
            'ext4', '/', 'local-block', 'persistent')), pytest.raises(ValueError):
        save(scratch, {})
    assert not list(target.iterdir())


def test_invalid_existing_sync_interval_rejected(scratch):
    with pytest.raises(ValueError):
        save(scratch, {'TOFU_DB_FASTPATH_SYNC_INTERVAL_MS': 'invalid'})


def test_same_retained_base_can_restore_missing_next_launch_configuration(scratch):
    root, base, _ = scratch
    authority, front, _ = settings._selection(root, str(base))
    (authority / 'FASTPATH').write_text(json.dumps({'local_authority': front}))
    env = {}
    state = save(scratch, env)
    assert state['locked'] and state['enabled']
    assert env[settings.FASTPATH_ENV] == str(base)


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'dangling-symlink'])
def test_unsafe_retained_marker_is_locked_without_blocking(scratch, kind):
    import os
    root, base, env_file = scratch
    marker = root / 'tofudb-authority' / 'FASTPATH'
    if kind == 'fifo':
        if not hasattr(os, 'mkfifo'):
            pytest.skip('FIFO unavailable')
        os.mkfifo(marker)
    elif kind == 'directory':
        marker.mkdir()
    else:
        marker.symlink_to(root / 'missing-marker')
    before = env_file.read_bytes()
    with pytest.raises(settings.NativeFastpathSettingsError) as exc:
        save(scratch, {})
    assert exc.value.code == 'fastpath_locked'
    assert env_file.read_bytes() == before


@pytest.mark.parametrize('invalid_env', [
    {'TOFUDB_TRANSPORT': 'embedded'},
    {'TOFU_DB_DURABILITY': 'normal'},
    {'TOFU_DB_WAL_FLUSH_INTERVAL_MS': '50'},
    {'TOFU_DB_FASTPATH_SYNC_INTERVAL_MS': '999'},
    {'TOFU_DB_FASTPATH_SYNC_INTERVAL_MS': 'invalid'},
])
def test_retained_selection_restoration_checks_launch_constraints(scratch, invalid_env):
    root, base, env_file = scratch
    authority, front, _ = settings._selection(root, str(base))
    (authority / 'FASTPATH').write_text(json.dumps({'local_authority': front}))
    before = env_file.read_bytes()
    env = dict(invalid_env)
    with pytest.raises(ValueError):
        save(scratch, env)
    assert env_file.read_bytes() == before
    assert settings.FASTPATH_ENV not in env


@pytest.mark.parametrize('nested', [False, True])
def test_derived_front_cannot_overlap_recovery_shadow(scratch, nested):
    root, base, env_file = scratch
    _, _, shadow = settings._selection(root, str(base))
    candidate = Path(shadow) / 'nested' if nested else Path(shadow)
    candidate.mkdir(parents=True)
    before = env_file.read_bytes()
    with patch.object(settings, 'describe_mount', return_value=MountDescription(
            'ext4', '/', 'local-block', 'persistent')), pytest.raises(ValueError, match='recovery shadow'):
        save(scratch, {}, directory=str(candidate))
    assert env_file.read_bytes() == before
    assert not (candidate / 'tofudb-fastpath').exists()
