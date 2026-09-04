"""Contracts for the managed server's bounded host-local bytecode cache."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

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
        'TOFU_SERVER_PYTHON_CACHE_MAX_MIB': '8',
    }
    environment.update(overrides)
    return environment


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


@pytest.mark.parametrize(
    ('configured', 'expected_mib'),
    [('1', 8), ('not-an-integer', 64), ('99999', 512)],
)
def test_size_override_always_has_a_finite_hard_bound(
    tmp_path, configured, expected_mib,
):
    project, _cache_parent, cache_root = _paths(tmp_path)
    activation = cache.prepare_server_python_cache(
        str(project), sys.executable,
        _environment(
            cache_root,
            TOFU_SERVER_PYTHON_CACHE='off',
            TOFU_SERVER_PYTHON_CACHE_MAX_MIB=configured,
        ),
    )

    assert activation.max_bytes == expected_mib * 1024 * 1024


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


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_launch_prunes_over_budget_inactive_namespace(tmp_path):
    project, cache_parent, cache_root = _paths(tmp_path)
    cache_root.mkdir(mode=0o700)
    old_namespace = cache_root / f"ns-{'a' * 16}-{'b' * 16}"
    old_prefix = old_namespace / 'pycache'
    old_prefix.mkdir(parents=True, mode=0o700)
    (old_namespace / '.active.lock').touch(mode=0o600)
    (old_namespace / '.last-used').touch(mode=0o600)
    with (old_prefix / 'large.pyc').open('wb') as handle:
        handle.truncate(7 * 1024 * 1024)

    activation = cache.prepare_server_python_cache(
        str(project), sys.executable, _environment(cache_root),
        mountinfo_text=_mountinfo(project, cache_parent),
    )

    assert activation.managed is True
    assert not old_namespace.exists()
    activation.close_parent_lock()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX inherited-flock contract')
def test_launch_never_prunes_active_namespace_to_claim_budget(tmp_path):
    project, cache_parent, cache_root = _paths(tmp_path)
    cache_root.mkdir(mode=0o700)
    old_namespace = cache_root / f"ns-{'a' * 16}-{'b' * 16}"
    old_prefix = old_namespace / 'pycache'
    old_prefix.mkdir(parents=True, mode=0o700)
    with (old_prefix / 'large.pyc').open('wb') as handle:
        handle.truncate(7 * 1024 * 1024)
    active_lock = cache._open_lock(
        old_namespace / '.active.lock', exclusive=False)
    assert active_lock is not None

    activation = cache.prepare_server_python_cache(
        str(project), sys.executable, _environment(cache_root),
        mountinfo_text=_mountinfo(project, cache_parent),
    )

    assert activation.managed is False
    assert activation.reason == 'cache-budget-unavailable'
    assert old_namespace.exists()
    cache._close_lock(active_lock)


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
        max_bytes=64 * 1024 * 1024,
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
    manager._release_worker_bytecode_cache_lease()
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
