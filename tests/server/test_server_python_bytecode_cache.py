"""Contracts for the managed server's bounded host-local bytecode cache."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import time

import pytest

import server_manager as sm
from serverctl_pkg import python_bytecode_cache as cache


pytestmark = pytest.mark.unit


def _mount_path(path: Path) -> str:
    return str(path.resolve()).replace(' ', r'\040')


def _mountinfo(
    project: Path,
    cache_parent: Path,
    *,
    source_filesystem: str = 'fuse.bgfuse',
    cache_filesystem: str = 'xfs',
) -> str:
    return (
        '20 1 8:1 / / rw - ext4 /dev/root rw\n'
        f'21 20 0:2 / {_mount_path(project)} rw - '
        f'{source_filesystem} source rw\n'
        f'22 20 8:2 / {_mount_path(cache_parent)} rw - '
        f'{cache_filesystem} /dev/cache rw\n'
    )


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / 'network-project'
    cache_parent = tmp_path / 'local-cache-volume'
    cache_root = cache_parent / 'tofu-server-cache'
    project.mkdir()
    cache_parent.mkdir()
    return project, cache_parent, cache_root


def _environment(cache_root: Path, **overrides: str) -> dict[str, str]:
    environment = {
        'TOFU_SERVER_PYTHON_CACHE': 'auto',
        'TOFU_SERVER_PYTHON_CACHE_DIR': str(cache_root),
    }
    environment.update(overrides)
    return environment


def _namespace(cache_root: Path, name: str, *, age_seconds: float = 0.0,
               identity: tuple[str, ...] | None = None, size: int = 0) -> Path:
    namespace = cache_root / name
    (namespace / 'pycache').mkdir(parents=True, mode=0o700)
    namespace.chmod(0o700)
    (namespace / '.active.lock').touch(mode=0o600)
    marker = namespace / '.last-used'
    marker.touch(mode=0o600)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(marker, (stamp, stamp))
    if identity is not None:
        cache.record_namespace_identity(namespace, identity)
    if size:
        with (namespace / 'pycache' / 'large.pyc').open('wb') as handle:
            handle.truncate(size)
    return namespace


_WEEK = 7 * 24 * 3600


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_auto_selects_private_local_cache_for_remote_checkout(tmp_path):
    project, cache_parent, cache_root = _paths(tmp_path)

    activation = cache.prepare_server_python_cache(
        str(project), sys.executable, _environment(cache_root),
        mountinfo_text=_mountinfo(project, cache_parent),
    )

    assert activation.selected is True
    assert activation.managed is True
    assert activation.reason == 'remote-source-local-cache'
    assert activation.source_filesystem == 'fuse.bgfuse'
    assert activation.cache_filesystem == 'xfs'
    assert Path(activation.pycache_prefix).parent.name == activation.namespace
    assert stat.S_IMODE(cache_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(activation.pycache_prefix).stat().st_mode) == 0o700
    assert activation.lock_fd is not None
    competing = cache._open_lock(
        cache_root / activation.namespace / '.active.lock', exclusive=True)
    assert competing is None
    restored = cache.reacquire_server_python_cache_lease(
        str(project), sys.executable, str(cache_root), activation.namespace,
        mountinfo_text=_mountinfo(project, cache_parent),
    )
    assert restored is not None

    activation.close_parent_lock()
    still_protected = cache._open_lock(
        cache_root / activation.namespace / '.active.lock', exclusive=True)
    assert still_protected is None
    cache._close_lock(restored)
    released = cache._open_lock(
        cache_root / activation.namespace / '.active.lock', exclusive=True)
    assert released is not None
    cache._close_lock(released)


@pytest.mark.parametrize(
    ('environment_override', 'source_filesystem', 'reason', 'selected'),
    [
        ({}, 'xfs', 'source-not-remote', False),
        ({'TOFU_SERVER_PYTHON_CACHE': 'off'}, 'fuse.bgfuse', 'disabled', False),
        ({'PYTHONDONTWRITEBYTECODE': '0'}, 'fuse.bgfuse',
         'python-bytecode-writes-disabled', False),
        ({'PYTHONPYCACHEPREFIX': '/operator/cache'}, 'fuse.bgfuse',
         'operator-python-prefix', True),
    ],
)
def test_existing_python_policy_and_auto_local_source_are_unchanged(
    tmp_path,
    environment_override,
    source_filesystem,
    reason,
    selected,
):
    project, cache_parent, cache_root = _paths(tmp_path)
    environment = _environment(cache_root, **environment_override)

    activation = cache.prepare_server_python_cache(
        str(project), sys.executable, environment,
        mountinfo_text=_mountinfo(
            project, cache_parent, source_filesystem=source_filesystem),
    )

    assert activation.managed is False
    assert activation.selected is selected
    assert activation.reason == reason
    assert not cache_root.exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_force_mode_can_accelerate_local_source_but_cache_must_stay_local(
    tmp_path,
):
    project, cache_parent, cache_root = _paths(tmp_path)
    environment = _environment(
        cache_root, TOFU_SERVER_PYTHON_CACHE='force')

    activation = cache.prepare_server_python_cache(
        str(project), sys.executable, environment,
        mountinfo_text=_mountinfo(
            project, cache_parent, source_filesystem='xfs'),
    )

    assert activation.managed is True
    assert activation.reason == 'forced'
    activation.close_parent_lock()

    rejected = cache.prepare_server_python_cache(
        str(project), sys.executable, environment,
        mountinfo_text=_mountinfo(
            project, cache_parent, source_filesystem='xfs',
            cache_filesystem='nfs'),
    )
    assert rejected.managed is False
    assert rejected.reason == 'cache-not-local'


# Measured 2026-10-25: the 64 MiB quota met a 1.6 GiB namespace at every
# launch and deleted it, so each boot recompiled the whole server from the
# network volume. Size never removes bytecode that can still be used.
@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_launch_keeps_its_own_namespace_whatever_its_size(tmp_path):
    project, cache_parent, cache_root = _paths(tmp_path)
    cache_root.mkdir(mode=0o700)
    name = cache._namespace_name(str(project), sys.executable)
    own = _namespace(cache_root, name, size=96 * 1024 * 1024)

    activation = cache.prepare_server_python_cache(
        str(project), sys.executable, _environment(cache_root),
        mountinfo_text=_mountinfo(project, cache_parent),
    )

    assert activation.managed is True and activation.namespace == name
    assert (own / 'pycache' / 'large.pyc').stat().st_size == 96 * 1024 * 1024
    activation.close_parent_lock()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_launch_reclaims_only_namespaces_nothing_can_use(tmp_path):
    project, cache_parent, cache_root = _paths(tmp_path)
    cache_root.mkdir(mode=0o700)
    removed_project = tmp_path / 'removed-project'
    expired = _namespace(cache_root, f"ns-{'1' * 16}-{'a' * 16}", age_seconds=_WEEK + 60)
    orphaned = _namespace(cache_root, f"ns-{'2' * 16}-{'a' * 16}",
                          identity=(str(removed_project), sys.executable))
    unreachable = _namespace(cache_root, f"ns-{'3' * 16}-{'a' * 16}",
                             identity=(str(tmp_path / 'offline' / 'volume' / 'project'),))
    recent = _namespace(cache_root, f"ns-{'4' * 16}-{'a' * 16}",
                        identity=(str(project), sys.executable), size=32 * 1024 * 1024)
    leased = _namespace(cache_root, f"ns-{'5' * 16}-{'a' * 16}", age_seconds=_WEEK + 60)
    lease = cache._open_lock(leased / '.active.lock', exclusive=False)
    assert lease is not None
    try:
        activation = cache.prepare_server_python_cache(
            str(project), sys.executable, _environment(cache_root),
            mountinfo_text=_mountinfo(project, cache_parent),
        )
        assert activation.managed is True
        activation.close_parent_lock()
    finally:
        cache._close_lock(lease)

    assert not expired.exists(), 'unused for a week'
    assert not orphaned.exists(), 'its project was provably removed'
    assert unreachable.exists(), 'an unreachable volume proves no removal'
    assert recent.exists(), 'recent and intact, whatever its size'
    assert leased.exists(), 'in use by a live worker'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_disk_pressure_reclaims_unused_namespaces_oldest_first(tmp_path, monkeypatch):
    project, cache_parent, cache_root = _paths(tmp_path)
    cache_root.mkdir(mode=0o700)
    older = _namespace(cache_root, f"ns-{'6' * 16}-{'a' * 16}", age_seconds=3600)
    newer = _namespace(cache_root, f"ns-{'7' * 16}-{'a' * 16}", age_seconds=60)
    monkeypatch.setattr(cache, 'volume_has_room', lambda _path: not older.exists())

    activation = cache.prepare_server_python_cache(
        str(project), sys.executable, _environment(cache_root),
        mountinfo_text=_mountinfo(project, cache_parent),
    )

    assert activation.managed is True
    assert not older.exists() and newer.exists()
    activation.close_parent_lock()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_a_full_disk_skips_the_cache_without_touching_one_in_use(tmp_path, monkeypatch):
    project, cache_parent, cache_root = _paths(tmp_path)
    cache_root.mkdir(mode=0o700)
    leased = _namespace(cache_root, f"ns-{'8' * 16}-{'a' * 16}")
    lease = cache._open_lock(leased / '.active.lock', exclusive=False)
    monkeypatch.setattr(cache, 'volume_has_room', lambda _path: False)
    environment = _environment(cache_root)
    try:
        activation = cache.prepare_server_python_cache(
            str(project), sys.executable, environment,
            mountinfo_text=_mountinfo(project, cache_parent),
        )
    finally:
        cache._close_lock(lease)

    assert (activation.managed, activation.reason) == (False, 'insufficient-cache-space')
    assert leased.exists()
    assert 'PYTHONPYCACHEPREFIX' not in environment


def _mirror(prefix: Path, source: Path) -> Path:
    mirror = prefix / str(source.resolve()).lstrip(os.sep)
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / 'module.cpython-312.pyc').write_bytes(b'bytecode')
    return mirror


def test_sweep_removes_only_bytecode_of_removed_source_directories(tmp_path, monkeypatch):
    sources = tmp_path / 'sources'
    live = sources / 'live' / 'package'
    gone = sources / 'removed-worktree' / 'package'
    replaced = sources / 'replaced'
    offline = sources / 'offline-volume' / 'project'
    erroring = sources / 'erroring'
    for directory in (live, gone, replaced, offline, erroring):
        directory.mkdir(parents=True)
    prefix = tmp_path / 'namespace' / 'pycache'
    mirrors = {name: _mirror(prefix, path) for name, path in (
        ('live', live), ('gone', gone), ('replaced', replaced), ('offline', offline),
        ('erroring', erroring))}
    gone_root = (prefix / str(sources.resolve()).lstrip(os.sep) / 'removed-worktree')
    import shutil
    shutil.rmtree(sources / 'removed-worktree')
    replaced.rmdir()
    replaced.write_text('now a file')
    # An unmounted volume leaves its mount point behind, empty.
    shutil.rmtree(sources / 'offline-volume' / 'project')
    real_stat = os.stat
    erroring_path = str(erroring.resolve())

    def flaky_stat(path, *args, **kwargs):
        if os.fspath(path) == erroring_path:
            raise OSError(5, 'Input/output error')
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(cache.os, 'stat', flaky_stat)
    result = cache.sweep_dead_bytecode(prefix)

    assert not gone_root.exists(), 'a removed worktree takes its whole subtree'
    assert not mirrors['replaced'].exists(), 'a directory replaced by a file'
    assert (mirrors['live'] / 'module.cpython-312.pyc').exists()
    assert mirrors['offline'].exists(), 'an empty mount point proves nothing'
    assert mirrors['erroring'].exists(), 'an I/O error proves nothing'
    assert result['subtreesRemoved'] == 2


@pytest.mark.skipif(os.name == 'nt', reason='POSIX detached-sweeper contract')
def test_sweep_requests_are_rate_limited_detached_and_single(tmp_path, monkeypatch):
    cache_root = tmp_path / 'cache'
    cache_root.mkdir(mode=0o700)
    name = f"ns-{'9' * 16}-{'a' * 16}"
    namespace = _namespace(cache_root, name)
    launched = []
    monkeypatch.setattr(cache.subprocess, 'Popen',
                        lambda argv, **options: launched.append((argv, options)))

    assert cache.request_bytecode_sweep(cache_root, name, python='/source/python')
    assert not cache.request_bytecode_sweep(cache_root, name), 'swept recently'
    argv, options = launched[0]
    assert argv[:4] == ['/source/python', '-s', '-m', 'serverctl_pkg.python_bytecode_cache']
    assert argv[4:] == ['sweep', '--root', str(cache_root), '--namespace', name]
    assert options['start_new_session'] is True
    assert not any(key.startswith('PYTHON') and key != 'PYTHONDONTWRITEBYTECODE'
                   for key in options['env'])

    stale = time.time() - cache.SWEEP_INTERVAL_SECONDS - 60
    os.utime(namespace / cache.SWEEP_MARKER_NAME, (stale, stale))
    held = cache._open_lock(namespace / cache.SWEEP_LOCK_NAME, exclusive=True)
    try:
        assert not cache.request_bytecode_sweep(cache_root, name), 'a sweep is running'
    finally:
        cache._close_lock(held)
    assert not cache.request_bytecode_sweep(cache_root, 'ns-not-a-namespace')
    assert len(launched) == 1


@pytest.mark.skipif(os.name == 'nt', reason='POSIX detached-sweeper contract')
def test_sweeper_entry_point_records_its_outcome(tmp_path):
    cache_root = tmp_path / 'cache'
    cache_root.mkdir(mode=0o700)
    name = f"ns-{'c' * 16}-{'a' * 16}"
    namespace = _namespace(cache_root, name)
    removed = tmp_path / 'removed'
    removed.mkdir()
    mirror = _mirror(namespace / 'pycache', removed)
    removed.rmdir()

    assert cache.main(['sweep', '--root', str(cache_root), '--namespace', name]) == 0

    assert not mirror.exists()
    outcome = json.loads((namespace / cache.SWEEP_MARKER_NAME).read_text())
    assert outcome['subtreesRemoved'] == 1 and outcome['directoriesChecked'] >= 1


def test_manager_requests_sweeps_only_for_a_managed_cache(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    requests = []
    monkeypatch.setattr(cache, 'request_bytecode_sweep',
                        lambda root, namespace, **kwargs: requests.append(
                            (root, namespace, kwargs)) or True)
    manager = sm.LifecycleManager(str(project))

    manager._state['workerBytecodeCache'] = {'managed': False}
    manager._request_bytecode_sweep()
    manager._state['workerBytecodeCache'] = {
        'managed': True, 'cacheRoot': '/cache', 'namespace': 'ns-x'}
    manager._request_bytecode_sweep()

    assert requests == [('/cache', 'ns-x', {'python': manager.python})]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pass_fds contract')
def test_manager_applies_prefix_and_owns_namespace_lease(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv('PYTHONPYCACHEPREFIX', raising=False)
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    (project / 'server.py').write_text('raise SystemExit(0)\n')
    lock_fd = os.open(tmp_path / 'cache.lock', os.O_RDWR | os.O_CREAT, 0o600)

    # The manager runs a project's serverctl.py frontend preflight before the
    # bytecode-cache step; this test owns the cache seam, not the preflight.
    monkeypatch.setattr(sm, 'run_frontend_preflight',
                        lambda *_args, **_kwargs: (True, ''))
    activation = cache.ServerPythonCacheActivation(
        selected=True,
        managed=True,
        reason='remote-source-local-cache',
        mode='auto',
        namespace=f"ns-{'a' * 16}-{'b' * 16}",
        pycache_prefix='/local/prefix',
        lock_fd=lock_fd,
    )
    monkeypatch.setattr(cache, 'prepare_server_python_cache',
                        lambda *_args, **_kwargs: activation)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _path: {
        'running': False, 'projectPath': str(project.resolve())})
    monkeypatch.setattr(sm, 'port_accepts', lambda *_args, **_kwargs: False)
    monkeypatch.setattr(sm, 'listener_pids', lambda _port: [])
    monkeypatch.setattr(sm, 'proc_start_ticks', lambda _pid: 99)
    child_options = {}

    class _FakeProcess:
        pid = 4321

    def fake_popen(*_args, **kwargs):
        child_options.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(sm.subprocess, 'Popen', fake_popen)
    manager = sm.LifecycleManager(str(project))

    result = manager._spawn('test')

    assert result['ok'] is True
    assert child_options['env']['PYTHONPYCACHEPREFIX'] == '/local/prefix'
    assert 'pass_fds' not in child_options
    assert manager.status()['workerBytecodeCache']['selected'] is True
    assert os.fstat(lock_fd)
    manager._release_worker_leases()
    with pytest.raises(OSError):
        os.fstat(lock_fd)


def test_cache_helper_failure_never_blocks_worker_spawn(tmp_path, monkeypatch):
    monkeypatch.delenv('PYTHONPYCACHEPREFIX', raising=False)
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    (project / 'server.py').write_text('raise SystemExit(0)\n')

    monkeypatch.setattr(sm, 'run_frontend_preflight',
                        lambda *_args, **_kwargs: (True, ''))

    def fail_cache_setup(*_args, **_kwargs):
        raise RuntimeError('optional cache unavailable')

    monkeypatch.setattr(cache, 'prepare_server_python_cache', fail_cache_setup)
    monkeypatch.setattr(sm, 'read_lock_status', lambda _path: {
        'running': False, 'projectPath': str(project.resolve())})
    monkeypatch.setattr(sm, 'port_accepts', lambda *_args, **_kwargs: False)
    monkeypatch.setattr(sm, 'proc_start_ticks', lambda _pid: 99)
    child_options = {}

    class _FakeProcess:
        pid = 4321

    def fake_popen(*_args, **kwargs):
        child_options.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(sm.subprocess, 'Popen', fake_popen)
    manager = sm.LifecycleManager(str(project))

    result = manager._spawn('test')

    assert result['ok'] is True
    assert 'PYTHONPYCACHEPREFIX' not in child_options['env']
    assert 'pass_fds' not in child_options
    assert manager._state['workerBytecodeCache'] == {
        'selected': False,
        'managed': False,
        'reason': 'cache-helper-unavailable',
    }
