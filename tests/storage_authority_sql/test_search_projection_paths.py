"""Native projection configuration never relocates authority or a live index."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import multiprocessing
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.storage_authority_sql import projection_paths as paths

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_projection_environment(monkeypatch):
    monkeypatch.delenv(paths.SEARCH_PROJECTION_ENV, raising=False)
    monkeypatch.delenv('TOFU_DB_DURABILITY', raising=False)
    monkeypatch.delenv('TOFU_DB_WAL_FLUSH_INTERVAL_MS', raising=False)


def test_default_is_legacy_and_resolution_does_not_create_files(tmp_path):
    root = tmp_path / 'data'
    result = paths.resolve_search_projection_paths(root)
    assert result.configured_directory == ''
    assert result.directory == result.default_directory == root / 'projections/tofudb-search'
    assert not root.exists()


def test_persistence_preserves_other_settings_and_env_wins_even_empty(tmp_path):
    root = tmp_path / 'data'
    config = root / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({'unrelated': {'keep': True}}))
    saved = paths.save_search_projection_directory(root, str(tmp_path / 'saved'))
    assert json.loads(config.read_text()) == {
        'unrelated': {'keep': True},
        paths.SEARCH_PROJECTION_CONFIG_KEY: str(tmp_path / 'saved'),
    }
    assert paths.resolve_search_projection_paths(root) == saved
    override = paths.resolve_search_projection_paths(
        root, environ={paths.SEARCH_PROJECTION_ENV: str(tmp_path / 'env')})
    assert override.configured_directory == str(tmp_path / 'env')
    assert override.directory != saved.directory
    automatic = paths.resolve_search_projection_paths(
        root, environ={paths.SEARCH_PROJECTION_ENV: ''})
    assert automatic.directory == automatic.default_directory
    assert automatic.configured_directory == ''
    reset = paths.save_search_projection_directory(root, '')
    assert reset.directory == automatic.directory


def test_shared_base_namespaces_canonical_authorities_and_aliases(tmp_path):
    base = tmp_path / 'candidate'
    one = tmp_path / 'one/data'
    two = tmp_path / 'two/data'
    one.mkdir(parents=True)
    alias = tmp_path / 'alias'
    alias.symlink_to(one, target_is_directory=True)
    first = paths.resolve_search_projection_paths(one, configured_directory=str(base))
    second = paths.resolve_search_projection_paths(two, configured_directory=str(base))
    same = paths.resolve_search_projection_paths(alias, configured_directory=str(base))
    assert first.directory != second.directory
    assert first.directory == same.directory
    assert first.directory.is_relative_to(base)
    assert first.configured_directory == second.configured_directory == str(base)
    assert not base.exists()


@pytest.mark.parametrize('value', [None, 7, False, {}, [], 'relative', '~/cache',
                                    '   ', '/bad\x00dir', '/' + 'x' * 4096])
def test_invalid_base_is_rejected_before_writes(tmp_path, value):
    root = tmp_path / 'data'
    with pytest.raises(ValueError):
        paths.save_search_projection_directory(root, value)
    assert not root.exists()


@pytest.mark.parametrize('suffix', ['', '/blocks', '/payload-segments'])
def test_authority_overlap_and_symlink_alias_rejected(tmp_path, suffix):
    root = tmp_path / 'data'
    authority = root / 'tofudb-authority'
    authority.mkdir(parents=True)
    marker = authority / 'KEEP'
    marker.write_bytes(b'authoritative')
    for candidate in (str(authority) + suffix, str(root), str(tmp_path)):
        with pytest.raises(ValueError, match='overlap'):
            paths.save_search_projection_directory(root, candidate)
    alias = tmp_path / 'alias'
    alias.symlink_to(authority, target_is_directory=True)
    with pytest.raises(ValueError, match='overlap'):
        paths.save_search_projection_directory(root, str(alias))
    assert marker.read_bytes() == b'authoritative'
    assert not (root / 'config').exists()


def test_namespace_symlink_cannot_join_two_indexes(tmp_path):
    root = tmp_path / 'data'
    base = tmp_path / 'cache'
    resolved = paths.resolve_search_projection_paths(root, configured_directory=str(base))
    other = tmp_path / 'unrelated'
    other.mkdir()
    resolved.directory.parent.mkdir(parents=True)
    resolved.directory.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match='symlinks'):
        paths.save_search_projection_directory(root, str(base))


def test_unwritable_effective_destination_fails_without_persisting(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    monkeypatch.setattr(paths.tempfile, 'NamedTemporaryFile', Mock(side_effect=PermissionError('denied')))
    with pytest.raises(ValueError, match='writable'):
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert not (root / 'config/server_config.json').exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX unlink semantics')
def test_writable_probe_supports_filesystems_requiring_a_link_until_close(tmp_path, monkeypatch):
    class LinkedProbe:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, data):
            return self.handle.write(data)

        def flush(self):
            if not os.fstat(self.handle.fileno()).st_nlink:
                raise FileNotFoundError('FUSE cannot flush an unlinked file')
            self.handle.flush()

    for name in ('TemporaryFile', 'NamedTemporaryFile'):
        original = getattr(paths.tempfile, name)
        monkeypatch.setattr(paths.tempfile, name,
                            lambda *a, factory=original, **k: LinkedProbe(factory(*a, **k)))
    target = tmp_path / 'projections/new-index'
    assert paths.prepare_search_projection_directory(target, tmp_path / 'authority') == target
    assert list(target.parent.iterdir()) == []


def test_file_instead_of_directory_rejected(tmp_path):
    base = tmp_path / 'not-a-directory'
    base.write_text('keep')
    with pytest.raises(ValueError, match='writable'):
        paths.save_search_projection_directory(tmp_path / 'data', str(base))
    assert base.read_text() == 'keep'


@pytest.mark.parametrize('override', ['', '/operator/cache'])
def test_env_locked_apply_has_no_filesystem_or_environment_mutation(tmp_path, monkeypatch, override):
    monkeypatch.setenv(paths.SEARCH_PROJECTION_ENV, override)
    before = dict(os.environ)
    with pytest.raises(paths.SearchProjectionEnvironmentOverrideError):
        paths.save_search_projection_directory(tmp_path / 'data', str(tmp_path / 'cache'))
    assert dict(os.environ) == before
    assert list(tmp_path.iterdir()) == []


def test_atomic_replace_failure_preserves_previous_configuration(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    paths.save_search_projection_directory(root, str(tmp_path / 'old'))
    config = root / 'config/server_config.json'
    before = config.read_bytes()
    monkeypatch.setattr(paths.os, 'replace', Mock(side_effect=OSError('disk failure')))
    with pytest.raises(OSError, match='disk failure'):
        paths.save_search_projection_directory(root, str(tmp_path / 'new'))
    assert config.read_bytes() == before


@pytest.mark.parametrize('existing', [False, True])
def test_file_fsync_eio_prevents_replace_and_cleans_staging(tmp_path, monkeypatch, existing):
    root = tmp_path / 'data'
    config = root / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    before = b'{ "unrelated": {"keep": true}, "tofudb_search_projection_dir": "" }\n'
    if existing:
        config.write_bytes(before)
    real_fsync = os.fsync

    def fail_file_fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, 'settings file fsync failed')
        real_fsync(fd)

    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(paths.os, 'replace', replace)
    monkeypatch.setattr(paths.os, 'fsync', fail_file_fsync)
    with pytest.raises(OSError, match='settings file fsync failed') as caught:
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert caught.value.errno == errno.EIO
    replace.assert_not_called()
    if existing:
        assert config.read_bytes() == before
    else:
        assert not config.exists()
    assert not list(config.parent.glob('.server-config-*.tmp'))


def test_durable_save_orders_flushed_private_file_replace_and_parent_fsync(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    config = root / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    before = b'{ "unrelated": {"keep": true} }\n'
    config.write_bytes(before)
    expected = {'unrelated': {'keep': True},
                paths.SEARCH_PROJECTION_CONFIG_KEY: str(tmp_path / 'cache')}
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def record_fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            assert info.st_size > 0, 'Buffered JSON must be flushed before fsync'
            assert stat.S_IMODE(info.st_mode) == 0o600
            assert config.read_bytes() == before
            events.append('file_fsync')
        elif os.path.samestat(info, config.parent.stat()):
            assert json.loads(config.read_text()) == expected
            events.append('directory_fsync')
        real_fsync(fd)

    def record_replace(source, destination):
        assert Path(source).parent == config.parent
        assert json.loads(Path(source).read_text()) == expected
        assert Path(destination) == config
        assert events == ['file_fsync']
        events.append('replace')
        real_replace(source, destination)

    monkeypatch.setattr(paths.os, 'fsync', record_fsync)
    monkeypatch.setattr(paths.os, 'replace', record_replace)
    paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert events == ['file_fsync', 'replace', 'directory_fsync']
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert not list(config.parent.glob('.server-config-*.tmp'))


def test_parent_fsync_eio_raises_after_replace_without_rollback(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    config = root / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"unrelated": {"keep": true}}')
    real_fsync = os.fsync
    synced_directories = []

    def fail_parent_fsync(fd):
        if os.path.samestat(os.fstat(fd), config.parent.stat()):
            synced_directories.append(fd)
            raise OSError(errno.EIO, 'settings directory fsync failed')
        real_fsync(fd)

    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(paths.os, 'replace', replace)
    monkeypatch.setattr(paths.os, 'fsync', fail_parent_fsync)
    with pytest.raises(OSError, match='settings directory fsync failed') as caught:
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert caught.value.errno == errno.EIO
    assert replace.call_count == 1, 'A post-rename failure must never roll back'
    assert json.loads(config.read_text()) == {
        'unrelated': {'keep': True},
        paths.SEARCH_PROJECTION_CONFIG_KEY: str(tmp_path / 'cache'),
    }
    assert not list(config.parent.glob('.server-config-*.tmp'))
    assert len(synced_directories) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(synced_directories[0])
    assert closed.value.errno == errno.EBADF


def test_missing_settings_ancestors_are_private_and_durably_linked(tmp_path, monkeypatch):
    root = tmp_path / 'new' / 'data'
    config = root / 'config/server_config.json'
    ancestors = [tmp_path, root.parent, root, config.parent]
    synced = []
    real_fsync = os.fsync

    def record_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            for directory in ancestors:
                if directory.exists() and os.path.samestat(os.fstat(fd), directory.stat()):
                    synced.append(directory)
        real_fsync(fd)

    monkeypatch.setattr(paths.os, 'fsync', record_fsync)
    paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert synced == ancestors
    for directory in ancestors[1:]:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert config.is_file()


def test_settings_directory_creation_fsync_failure_prevents_publication(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir()
    config = root / 'config/server_config.json'
    real_fsync = os.fsync

    def fail_creation_fsync(fd):
        if os.path.samestat(os.fstat(fd), root.stat()):
            raise OSError(errno.EIO, 'config directory creation fsync failed')
        real_fsync(fd)

    replace = Mock(wraps=os.replace)
    monkeypatch.setattr(paths.os, 'replace', replace)
    monkeypatch.setattr(paths.os, 'fsync', fail_creation_fsync)
    with pytest.raises(OSError, match='config directory creation fsync failed'):
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    replace.assert_not_called()
    assert not config.exists()
    assert list(config.parent.iterdir()) == []
    # Retry must sync the existing entry left by the failed creator, too.
    with pytest.raises(OSError, match='config directory creation fsync failed'):
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    monkeypatch.setattr(paths.os, 'fsync', real_fsync)
    paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert config.is_file()


@pytest.mark.parametrize('first_writer', ['projection', 'unrelated'])
def test_concurrent_json_updates_preserve_both_fields(tmp_path, monkeypatch, first_writer):
    from lib.json_store import update_json_atomic

    root = tmp_path / 'data'
    config = root / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"original": {"keep": true}}')
    first_read, second_read = threading.Event(), threading.Event()
    second_started, release = threading.Event(), threading.Event()
    real_read = paths.read_json

    def pause_after_read(writer):
        if writer == first_writer:
            first_read.set()
            assert release.wait(5), 'Concurrent writer was not released'
        else:
            second_read.set()

    def projection_read(*args, **kwargs):
        current = real_read(*args, **kwargs)
        pause_after_read('projection')
        return current

    def update_unrelated(current):
        pause_after_read('unrelated')
        return {**current, 'concurrent': {'keep': '并发'}}

    def save(writer):
        if writer != first_writer:
            second_started.set()
        if writer == 'projection':
            paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
        else:
            update_json_atomic(str(config), update_unrelated, strict=True)

    monkeypatch.setattr(paths, 'read_json', projection_read)
    second_writer = 'unrelated' if first_writer == 'projection' else 'projection'
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(save, first_writer)
        try:
            assert first_read.wait(5)
            second = workers.submit(save, second_writer)
            assert second_started.wait(5)
            assert not second_read.wait(0.1), 'The RMW lock must cover the read, too'
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert json.loads(config.read_text()) == {
        'original': {'keep': True}, 'concurrent': {'keep': '并发'},
        paths.SEARCH_PROJECTION_CONFIG_KEY: str(tmp_path / 'cache'),
    }


def _update_settings_in_process(config, read_started, release):
    from lib.json_store import update_json_atomic

    def update(current):
        read_started.set()
        assert release.wait(10), 'Parent did not release the settings writer'
        return {**current, 'from_process': {'keep': True}}

    update_json_atomic(config, update, default={}, strict=True)


def test_projection_save_coordinates_with_other_process_settings_writer(tmp_path, monkeypatch):
    pytest.importorskip('fcntl')
    root = tmp_path / 'data'
    config = root / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"original": {"keep": true}}')
    context = multiprocessing.get_context('spawn')
    read_started, release = context.Event(), context.Event()
    child = context.Process(target=_update_settings_in_process,
                            args=(str(config), read_started, release))
    projection_started, projection_read = threading.Event(), threading.Event()
    real_read = paths.read_json

    def observe_read(*args, **kwargs):
        projection_read.set()
        return real_read(*args, **kwargs)

    def save():
        projection_started.set()
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))

    monkeypatch.setattr(paths, 'read_json', observe_read)
    child.start()
    try:
        assert read_started.wait(10)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(save)
            try:
                assert projection_started.wait(5)
                assert not projection_read.wait(0.1), 'Must wait for the other process RMW'
            finally:
                release.set()
            future.result(timeout=10)
    finally:
        release.set()
        child.join(timeout=10)
        if child.is_alive():
            child.kill()
            child.join()
    assert child.exitcode == 0
    assert json.loads(config.read_text()) == {
        'original': {'keep': True}, 'from_process': {'keep': True},
        paths.SEARCH_PROJECTION_CONFIG_KEY: str(tmp_path / 'cache'),
    }


@pytest.mark.parametrize('corruption', ['{broken', '[]', 'null'])
def test_malformed_settings_fail_closed_without_overwrite(tmp_path, corruption):
    root = tmp_path / 'data'
    config = root / 'config/server_config.json'
    config.parent.mkdir(parents=True)
    config.write_text(corruption)
    with pytest.raises(RuntimeError):
        paths.resolve_search_projection_paths(root)
    with pytest.raises(RuntimeError):
        paths.save_search_projection_directory(root, '')
    assert config.read_text() == corruption


def _stub_native_launch(tmp_path, monkeypatch, *, supervisor_pin=None):
    from lib.storage_authority_sql import native_launch

    root = tmp_path / 'project'
    authority = root / 'data/tofudb-authority'
    authority.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('TOFU_STORAGE_PROJECT_ROOT', str(root))
    monkeypatch.setenv('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE', '1')
    monkeypatch.setenv('TOFU_STORAGE_PARENT_PID', str(os.getpid()))
    argv = ['native_launch', '/test/tofu-db', '1', '0']
    if supervisor_pin is not None:
        argv.append(str(supervisor_pin))
    monkeypatch.setattr(native_launch.sys, 'argv', argv)
    monkeypatch.setattr(native_launch, 'ProjectLease', Mock())
    exec_mock = Mock()
    monkeypatch.setattr(native_launch.os, 'execv', exec_mock)
    native_launch.main()
    command = exec_mock.call_args.args[1]
    return Path(command[command.index('--search-projection-dir') + 1])


def test_native_child_uses_pinned_path_even_if_settings_changed(tmp_path, monkeypatch):
    root = tmp_path / 'project/data'
    pinned = paths.save_search_projection_directory(root, str(tmp_path / 'old')).directory
    paths.save_search_projection_directory(root, str(tmp_path / 'new'))
    assert _stub_native_launch(tmp_path, monkeypatch, supervisor_pin=pinned) == pinned


@pytest.mark.parametrize('custom', [False, True])
def test_daemon_embedded_resolve_identically_and_active_snapshot_survives_apply(
        tmp_path, monkeypatch, custom):
    from lib.database_panel import probe
    from lib.storage.supervisor import StorageSupervisor

    root = tmp_path / 'project/data'
    if custom:
        paths.save_search_projection_directory(root, str(tmp_path / 'custom'))
    daemon_path = _stub_native_launch(tmp_path, monkeypatch)

    assert not daemon_path.exists(), 'native must initialize a missing leaf itself'
    monkeypatch.setattr('lib.storage.supervisor._resolve_tofudb_embedded_engine',
                        lambda: ('/test/tofu-db', '/test/libtofu_db.so'))
    monkeypatch.setattr('lib.storage_authority_sql.preflight.ProjectLease', Mock())
    runtime_factory = Mock()
    monkeypatch.setattr('tofu_db.embedded.EmbeddedRuntime', runtime_factory)
    client_factory = Mock()
    client_factory.return_value.health.return_value = {'ready': True, 'backend': 'tofudb'}
    monkeypatch.setattr('lib.storage.v2_embedded_client.EmbeddedStorageV2Client', client_factory)
    monkeypatch.setattr('lib.storage.supervisor.threading.Thread', Mock())
    supervisor = StorageSupervisor(project_root=tmp_path / 'project', backend='tofudb')
    supervisor._start_embedded((1, None))
    assert runtime_factory.call_args.kwargs['projection_dir'] == daemon_path

    assert not daemon_path.exists(), 'embedded must also receive an absent fresh leaf'
    assert supervisor.status()['search_projection_directory'] == str(daemon_path)
    monkeypatch.setattr(probe, 'data_root', lambda: root)
    monkeypatch.setattr('lib.storage.service.storage_status', supervisor.status)
    before = probe.search_projection_state('tofudb')
    assert not before['restartRequired']
    next_launch = paths.save_search_projection_directory(root, str(tmp_path / 'next'))
    state = probe.search_projection_state('tofudb')
    assert state['directory'] == before['directory'] == str(daemon_path)
    assert state['configuredDirectory'] == str(tmp_path / 'next')
    assert state['pendingDirectory'] == str(next_launch.directory)
    assert state['restartRequired']
    assert runtime_factory.call_count == 1
    supervisor._embedded_runtime = None
    assert supervisor.status()['search_projection_directory'] == ''


def test_daemon_supervisor_pins_spawn_argument_and_active_snapshot(tmp_path, monkeypatch):
    from lib.storage.supervisor import StorageSupervisor
    from lib.storage.startup_control import STARTUP_READY_TYPE
    from tofu_db.wire import PROTOCOL_VERSION

    monkeypatch.setenv('TOFU_STORAGE_TRANSPORT', 'daemon')
    monkeypatch.setattr('lib.storage.supervisor._resolve_tofudb_binary', lambda: '/test/tofu-db')
    monkeypatch.setattr('lib.storage.supervisor._tofudb_scope_ids', lambda: (1, None))
    child = Mock(pid=123)
    child.poll.return_value = None
    child.stdin.fileno.return_value = 10
    popen = Mock(return_value=child)
    monkeypatch.setattr('lib.storage.supervisor.subprocess.Popen', popen)
    monkeypatch.setattr('lib.storage.supervisor.os.get_inheritable', lambda fd: False)
    monkeypatch.setattr('lib.storage.supervisor.threading.Thread', Mock())
    client_factory = Mock()
    client_factory.return_value.health.return_value = {'ready': True, 'backend': 'tofudb'}
    monkeypatch.setattr('lib.storage.v2_client.StorageV2Client', client_factory)
    supervisor = StorageSupervisor(project_root=tmp_path / 'project', backend='tofudb')
    monkeypatch.setattr(supervisor, '_start_stderr_drainer', lambda process: None)
    monkeypatch.setattr(supervisor, '_read_startup_envelope', lambda process: {
        'type': STARTUP_READY_TYPE, 'protocol': PROTOCOL_VERSION,
        'backend': 'tofudb', 'port': 12345})
    root = tmp_path / 'project/data'
    expected = paths.save_search_projection_directory(root, str(tmp_path / 'old')).directory
    supervisor.start()
    assert popen.call_args.args[0][-1] == str(expected)
    paths.save_search_projection_directory(root, str(tmp_path / 'next'))
    assert supervisor.status()['search_projection_directory'] == str(expected)
    child.poll.return_value = 1
    assert supervisor.status()['search_projection_directory'] == ''


def test_non_native_status_is_null_without_resolving_paths(monkeypatch):
    from lib.database_panel import probe

    monkeypatch.setattr(paths, 'resolve_search_projection_paths', Mock(side_effect=AssertionError))
    assert probe.search_projection_state('sqlite') is None
    assert probe.search_projection_state('postgres') is None


def test_apply_and_launch_probe_leave_missing_index_leaf_absent(tmp_path):
    root = tmp_path / 'data'
    prepared = paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert prepared.directory.parent.is_dir()
    assert not prepared.directory.exists()
    paths.prepare_search_projection_directory(prepared.directory, prepared.authority_directory)
    assert not prepared.directory.exists()
    assert list(prepared.directory.parent.iterdir()) == []


def test_existing_empty_native_leaf_is_rejected_without_deleting_evidence(tmp_path):
    root = tmp_path / 'data'
    prepared = paths.resolve_search_projection_paths(root)
    prepared.directory.mkdir(parents=True)
    with pytest.raises(ValueError, match='existing empty index'):
        paths.save_search_projection_directory(root, '')
    assert prepared.directory.is_dir()
    assert list(prepared.directory.iterdir()) == []


def test_probe_existing_nonempty_projection_does_not_modify_index(tmp_path):
    root = tmp_path / 'data'
    prepared = paths.resolve_search_projection_paths(root)
    prepared.directory.mkdir(parents=True)
    marker = prepared.directory / 'index-marker'
    marker.write_bytes(b'existing index contents')
    paths.prepare_search_projection_directory(prepared.directory, prepared.authority_directory)
    assert marker.read_bytes() == b'existing index contents'
    assert list(prepared.directory.iterdir()) == [marker]


def _capacity_statvfs(*, files=1_000_000, favail=500_000, blocks=100_000_000,
                      bavail=50_000_000, frsize=4096):
    return SimpleNamespace(f_files=files, f_favail=favail, f_blocks=blocks,
                           f_bavail=bavail, f_frsize=frsize)


def test_inode_starved_volume_refused_before_persist(tmp_path, monkeypatch):
    """A nearly-full volume must fail closed instead of starving its writers.

    Incident (2026-10-12): a ~/.cache projection base exhausted /home
    inodes and took Codex's own lock/sqlite state down with it.
    """
    monkeypatch.setattr(paths.os, 'statvfs', lambda path: _capacity_statvfs(favail=10))
    root = tmp_path / 'data'
    with pytest.raises(ValueError, match='inodes'):
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert not (root / 'config/server_config.json').exists()


def test_space_starved_volume_refused_before_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.os, 'statvfs', lambda path: _capacity_statvfs(bavail=10))
    root = tmp_path / 'data'
    with pytest.raises(ValueError, match='space'):
        paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert not (root / 'config/server_config.json').exists()


def test_launch_probe_refuses_starved_existing_index(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    prepared = paths.resolve_search_projection_paths(root)
    prepared.directory.mkdir(parents=True)
    (prepared.directory / 'index-marker').write_bytes(b'existing index contents')
    monkeypatch.setattr(paths.os, 'statvfs', lambda path: _capacity_statvfs(favail=10))
    with pytest.raises(ValueError, match='inodes'):
        paths.prepare_search_projection_directory(
            prepared.directory, prepared.authority_directory)


def test_volume_without_capacity_accounting_is_accepted(tmp_path, monkeypatch):
    """FUSE mounts reporting zero f_files/f_blocks skip that dimension."""
    monkeypatch.setattr(paths.os, 'statvfs',
                        lambda path: _capacity_statvfs(files=0, blocks=0))
    root = tmp_path / 'data'
    saved = paths.save_search_projection_directory(root, str(tmp_path / 'cache'))
    assert saved.configured_directory == str(tmp_path / 'cache')


def test_daemon_launch_does_not_reject_low_search_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.os, 'statvfs', lambda path: _capacity_statvfs(favail=0))
    # Rust defers the index; Python must still exec the healthy source authority.
    assert _stub_native_launch(tmp_path, monkeypatch)


def test_small_healthy_volume_is_not_rejected_by_fixed_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.os, 'statvfs', lambda path: _capacity_statvfs(
        files=400, favail=200, blocks=25_000, bavail=12_500))
    root = tmp_path / 'data'
    assert paths.save_search_projection_directory(root, str(tmp_path / 'cache'))


def test_known_space_full_with_unknown_inode_accounting_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.os, 'statvfs', lambda path: _capacity_statvfs(files=0, bavail=0))
    with pytest.raises(ValueError, match='space'):
        paths.save_search_projection_directory(tmp_path / 'data', str(tmp_path / 'cache'))
