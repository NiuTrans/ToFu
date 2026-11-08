"""Contracts for choosing fast local storage for replicas and task checkouts."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.storage_authority_sql import storage_capabilities
from serverctl_pkg import local_placement as placement


pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != 'posix', reason='placement is POSIX-only'),
]

_GIB = 1024 ** 3


@pytest.fixture
def durable_tmp(monkeypatch):
    """Treat pytest's temporary tree as the durable volume it stands in for."""
    monkeypatch.setattr(placement, 'ephemeral_roots', lambda _environ: [])


def _mount(path: Path) -> str:
    return str(path.resolve()).replace(' ', r'\040')


def _mountinfo(source: Path, local: Path, *, source_fs: str = 'fuse.bgfuse',
               local_fs: str = 'ext4', local_options: str = 'rw',
               local_device: str = '8:2') -> str:
    return (
        '20 1 8:1 / / rw - ext4 /dev/root rw\n'
        f'21 20 0:60 / {_mount(source)} rw - {source_fs} remote rw\n'
        f'22 20 {local_device} / {_mount(local)} {local_options} - {local_fs} /dev/local rw\n'
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source = tmp_path / 'network-volume'
    local = tmp_path / 'local-disk'
    (source / 'env' / 'bin').mkdir(parents=True)
    (local / 'home').mkdir(parents=True)
    return source, local, {'HOME': str(local / 'home')}


def _usage(free: int = 400 * _GIB, total: int = 500 * _GIB):
    return lambda _path: SimpleNamespace(free=free, total=total)


def _latency(source: Path, slow: float = 400.0, fast: float = 5.0):
    return lambda directory: slow if str(directory).startswith(str(source.resolve())) else fast


def _choose(source: Path, local: Path, environment: dict[str, str], purpose: str, **overrides):
    options = {
        'environ': environment,
        'mountinfo_text': _mountinfo(source, local),
        'latency_probe': _latency(source),
        'disk_usage': _usage(),
    }
    options.update(overrides)
    return placement.choose_local_root(source / 'env' / 'bin', purpose, **options)


def test_volume_description_matches_the_storage_authority_probe(tmp_path):
    """The stdlib manager-side classifier and the storage probe never drift."""
    filesystem_types = sorted({
        *placement._NETWORK_FILESYSTEMS, *placement._LOCAL_BLOCK_FILESYSTEMS,
        *placement._MEMORY_FILESYSTEMS, *placement._OVERLAY_FILESYSTEMS,
        *storage_capabilities._NETWORK_FILESYSTEMS,
        *storage_capabilities._LOCAL_BLOCK_FILESYSTEMS,
        *storage_capabilities._MEMORY_FILESYSTEMS,
        *storage_capabilities._OVERLAY_FILESYSTEMS,
        'fuse', 'fuse.bgfuse', 'fuse.sshfs', 'fuse.rclone', 'fuseblk', 'proc', 'squashfs',
    })
    (tmp_path / 'network volume' / 'deep').mkdir(parents=True)
    paths = [Path('/'), Path('/proc/self'), tmp_path / 'network volume' / 'deep']
    for filesystem_type in filesystem_types:
        mountinfo = (
            '20 1 8:1 / / rw - ext4 /dev/root rw\n'
            f'21 20 0:60 / {_mount(tmp_path)}/network\\040volume rw - '
            f'{filesystem_type} remote rw\n'
        )
        for path in paths:
            mirrored = placement.describe_volume(path, mountinfo_text=mountinfo)
            authority = storage_capabilities.describe_mount(path, mountinfo_text=mountinfo)
            assert (mirrored.filesystem_type, mirrored.mount_point, mirrored.storage_class,
                    mirrored.persistence) == (
                authority.filesystem_type, authority.mount_point,
                authority.storage_class, authority.persistence), (filesystem_type, path)


@pytest.mark.parametrize('purpose, leaf', [
    (placement.PURPOSE_RUNTIME, 'runtime-replicas'),
    (placement.PURPOSE_NATIVE_BUILD, 'native-builds'),
])
def test_runtime_caches_place_on_a_decisively_faster_private_local_root(
        tmp_path, durable_tmp, purpose, leaf):
    source, local, environment = _layout(tmp_path)

    decision = _choose(source, local, environment, purpose)

    assert decision.selected and decision.reason == 'selected'
    root = Path(decision.root)
    assert root == local / 'home' / '.cache' / 'tofu' / leaf
    assert root.is_dir() and (root.stat().st_mode & 0o777) == 0o700
    status = decision.as_status()
    assert (status['sourceClass'], status['speedup']) == ('network-filesystem', 80.0)


def test_runtime_placement_declines_without_a_real_benefit(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)

    local_source = _choose(source, local, environment, placement.PURPOSE_RUNTIME,
                           mountinfo_text=_mountinfo(source, local, source_fs='xfs'))
    marginal = _choose(source, local, environment, placement.PURPOSE_RUNTIME,
                       latency_probe=_latency(source, slow=12.0, fast=5.0))
    disabled = _choose(source, local, {**environment, 'TOFU_RUNTIME_REPLICA': 'off'},
                       placement.PURPOSE_RUNTIME)

    assert (local_source.selected, local_source.reason) == (False, 'source-local')
    assert (marginal.selected, marginal.reason) == (False, 'insufficient-speedup')
    assert (disabled.selected, disabled.reason) == (False, 'disabled')
    assert not (local / 'home' / '.cache').exists()


@pytest.mark.parametrize(('mountinfo_overrides', 'usage', 'reason'), [
    ({'local_fs': 'nfs4'}, _usage(), 'candidate-not-local'),
    ({'local_fs': 'tmpfs'}, _usage(), 'candidate-not-local'),
    ({'local_options': 'rw,nosuid,noexec'}, _usage(), 'candidate-noexec'),
    ({}, _usage(free=1 * _GIB, total=10 * _GIB), 'insufficient-space'),
    ({'local_device': '0:60'}, _usage(), 'candidate-on-source-volume'),
])
def test_runtime_placement_refuses_unsafe_targets(
        tmp_path, durable_tmp, mountinfo_overrides, usage, reason):
    source, local, environment = _layout(tmp_path)

    decision = _choose(source, local, environment, placement.PURPOSE_RUNTIME,
                       mountinfo_text=_mountinfo(source, local, **mountinfo_overrides),
                       disk_usage=usage)

    assert (decision.selected, decision.reason) == (False, reason)


def test_temporary_directories_are_never_placement_roots(tmp_path):
    source, local, environment = _layout(tmp_path)

    pytest_tmp = _choose(source, local, {**environment, 'TOFU_RUNTIME_REPLICA': 'on'},
                         placement.PURPOSE_RUNTIME)
    age_cleaned = _choose(source, local, {**environment, 'TOFU_LOCAL_DIR': '/var/tmp/tofu'},
                          placement.PURPOSE_RUNTIME)

    assert (pytest_tmp.selected, pytest_tmp.reason) == (False, 'candidate-persistence-ephemeral')
    assert (age_cleaned.selected, age_cleaned.reason) == (
        False, 'candidate-persistence-ephemeral')


def test_redirected_xdg_cache_falls_back_to_the_home_cache(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    redirected = {**environment, 'XDG_CACHE_HOME': str(source / 'tool-cache')}

    roots = placement.candidate_roots(placement.PURPOSE_RUNTIME, redirected)
    decision = _choose(source, local, redirected, placement.PURPOSE_RUNTIME)

    assert roots == [source / 'tool-cache' / 'tofu' / 'runtime-replicas',
                     local / 'home' / '.cache' / 'tofu' / 'runtime-replicas']
    assert decision.selected and Path(decision.root) == roots[1]
    assert placement.candidate_roots(
        placement.PURPOSE_RUNTIME, {**redirected, 'TOFU_LOCAL_DIR': str(local / 'x')},
    ) == [local / 'x' / 'runtime-replicas']


def test_lanes_use_a_local_root_by_default_and_follow_the_operator_switch(
        tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    overlay = _mountinfo(source, local, local_fs='overlay')

    default_overlay = _choose(source, local, environment, placement.PURPOSE_LANES,
                              mountinfo_text=overlay)
    disabled = _choose(source, local, {**environment, 'TOFU_LOCAL_WORKSPACES': 'off'},
                       placement.PURPOSE_LANES, mountinfo_text=overlay)

    assert default_overlay.selected
    assert Path(default_overlay.root) == local / 'home' / '.local' / 'state' / 'tofu' / 'lanes'
    assert (disabled.selected, disabled.reason) == (False, 'disabled')
    assert placement.placement_mode(placement.PURPOSE_WORKSPACES, environment) == 'off'


def test_task_checkouts_stay_on_the_data_volume_unless_the_operator_opts_in(
        tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    overlay = _mountinfo(source, local, local_fs='overlay')
    automatic = {**environment, 'TOFU_LOCAL_WORKSPACES': 'auto'}

    default = _choose(source, local, environment, placement.PURPOSE_WORKSPACES)
    automatic_overlay = _choose(source, local, automatic, placement.PURPOSE_WORKSPACES,
                                mountinfo_text=overlay)
    automatic_disk = _choose(source, local, automatic, placement.PURPOSE_WORKSPACES)
    forced_overlay = _choose(source, local, {**environment, 'TOFU_LOCAL_WORKSPACES': 'on'},
                             placement.PURPOSE_WORKSPACES, mountinfo_text=overlay)

    assert (default.selected, default.reason) == (False, 'disabled')
    assert placement.placement_mode(
        placement.PURPOSE_WORKSPACES, {'TOFU_LOCAL_WORKSPACES': 'typo'}) == 'off'
    assert (automatic_overlay.selected, automatic_overlay.reason) == (
        False, 'candidate-persistence-unknown')
    assert automatic_disk.selected
    assert Path(automatic_disk.root) == (
        local / 'home' / '.local' / 'state' / 'tofu' / 'workspaces')
    assert forced_overlay.selected


def test_a_shared_existing_root_is_never_adopted(tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir()
    shared.chmod(0o777)

    with pytest.raises(PermissionError):
        placement.ensure_private_directory(shared)


def test_native_authority_places_only_on_proven_persistent_local_disks(tmp_path, durable_tmp):
    source, local, environment = _layout(tmp_path)
    purpose = placement.PURPOSE_NATIVE_AUTHORITY

    decision = _choose(source, local, environment, purpose)
    assert decision.selected
    assert Path(decision.root) == local / 'home' / '.local' / 'state' / 'tofu' / 'native-authority'

    crowded = _choose(source, local, environment, purpose, required_bytes=450 * _GIB)
    assert (crowded.selected, crowded.reason) == (False, 'insufficient-space')

    overlay = _mountinfo(source, local, local_fs='overlay')
    refused = _choose(source, local, environment, purpose, mountinfo_text=overlay)
    assert (refused.selected, refused.reason) == (False, 'candidate-persistence-unknown')
    consented = _choose(source, local, {**environment, 'TOFU_DB_FASTPATH': 'on'}, purpose,
                        mountinfo_text=overlay)
    assert consented.selected
    assert placement.placement_mode(purpose, {'TOFU_DB_FASTPATH': 'off'}) == 'off'
    assert placement.placement_mode(purpose, {}) == 'auto'
