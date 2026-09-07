"""Adaptive storage discovery is explicit, bounded, and fail-closed."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile

import pytest

from lib.storage_sidecar import storage_capabilities as capabilities
from lib.storage_sidecar import preflight


pytestmark = pytest.mark.unit


def _report(
    path: Path,
    *,
    storage_class: capabilities.StorageClass = 'local-block',
    persistence: capabilities.Persistence = 'persistent',
    ready: bool = True,
) -> capabilities.StorageCapabilityReport:
    return capabilities.StorageCapabilityReport(
        path=str(path),
        path_exists=True,
        path_created=False,
        filesystem_type='ext4',
        mount_point='/',
        storage_class=storage_class,
        persistence=persistence,
        free_bytes=10 * 1024 ** 3,
        writable=ready,
        private_files=ready,
        file_fsync=ready,
        directory_fsync='supported' if ready else 'unsupported',
        atomic_replace=ready,
        exclusive_lock=ready,
        sqlite_wal_recovery=ready,
        probe_latency_ms=1.25,
        limitations=(),
    )


def test_describe_mount_uses_longest_decoded_mountpoint():
    mountinfo = (
        '20 1 8:1 / / rw,relatime - ext4 /dev/root rw\n'
        '21 20 0:42 / /mnt/data rw,relatime - nfs4 server:/data rw\n'
        '22 21 0:43 / /mnt/data/team\\040one rw,relatime '
        '- fuse.beegfs beegfs rw\n'
    )
    mount = capabilities.describe_mount(
        '/mnt/data/team one/project', mountinfo_text=mountinfo)
    assert mount.filesystem_type == 'fuse.beegfs'
    assert mount.mount_point == '/mnt/data/team one'
    assert mount.storage_class == 'network-filesystem'
    assert mount.persistence == 'unknown'


@pytest.mark.parametrize(
    ('filesystem', 'storage_class', 'persistence'),
    [
        ('ext4', 'local-block', 'persistent'),
        ('tmpfs', 'memory-filesystem', 'ephemeral'),
        ('overlay', 'container-overlay', 'unknown'),
        ('fuse.bgfuse', 'network-filesystem', 'unknown'),
        ('fuse.portal', 'userspace-filesystem', 'unknown'),
        ('mysteryfs', 'unknown', 'unknown'),
    ],
)
def test_describe_mount_classifies_without_guessing(
        filesystem, storage_class, persistence):
    mountinfo = f'20 1 0:1 / / rw - {filesystem} source rw\n'
    mount = capabilities.describe_mount('/', mountinfo_text=mountinfo)
    assert mount.storage_class == storage_class
    assert mount.persistence == persistence


def test_probe_storage_path_creates_private_probe_and_cleans_it(tmp_path):
    target = tmp_path / 'candidate'
    report = capabilities.probe_storage_path(
        target, create_directory=True,
        # Make topology classification deterministic; primitive calls still
        # execute against the real test filesystem.
        mountinfo_text='20 1 8:1 / / rw - ext4 /dev/root rw\n',
    )
    assert report.path_exists and report.path_created
    assert report.writable and report.private_files and report.file_fsync
    assert report.atomic_replace and report.exclusive_lock
    assert report.sqlite_wal_recovery
    if hasattr(os, 'O_DIRECTORY'):
        assert report.directory_fsync == 'supported'
    assert report.free_bytes is not None and report.free_bytes > 0
    assert list(target.iterdir()) == []


def test_temp_directory_lifecycle_is_ephemeral_even_on_local_block_storage():
    temp_child = Path(tempfile.gettempdir()) / 'tofu-capability-lifecycle'
    mount = capabilities.describe_mount(
        temp_child,
        mountinfo_text='20 1 8:1 / / rw - ext4 /dev/root rw\n',
    )
    assert mount.storage_class == 'local-block'
    assert mount.persistence == 'ephemeral'


def test_probe_missing_path_is_observational_by_default(tmp_path):
    target = tmp_path / 'not-created'
    report = capabilities.probe_storage_path(target, mountinfo_text='')
    assert not target.exists()
    assert not report.path_exists
    assert not report.writable
    assert 'directory:missing' in report.limitations


def test_probe_permission_failure_becomes_report_not_exception(
        tmp_path, monkeypatch):
    target = tmp_path / 'read-only-policy'
    target.mkdir()

    def deny_write(_directory):
        raise PermissionError(errno.EACCES, 'denied')

    monkeypatch.setattr(capabilities, '_run_write_probe', deny_write)
    report = capabilities.probe_storage_path(target, mountinfo_text='')
    assert report.path_exists
    assert not report.writable
    assert 'write_probe:eacces' in report.limitations


def test_runtime_preflight_exposes_topology_without_a_second_policy(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        preflight,
        'describe_mount',
        lambda _path: capabilities.MountDescription(
            filesystem_type='fuse.beegfs',
            mount_point='/mnt/shared',
            storage_class='network-filesystem',
            persistence='unknown',
        ),
    )
    report = preflight.run_filesystem_preflight(tmp_path / 'data').as_dict()
    assert report['filesystem_type'] == 'fuse.beegfs'
    assert report['storage_class'] == 'network-filesystem'
    assert report['persistence'] == 'unknown'


def test_network_mount_never_becomes_local_sqlite_authority():
    report = _report(
        Path('/network'), storage_class='network-filesystem',
        persistence='unknown')
    assert report.sqlite_wal_recovery  # a one-process round trip can pass
    assert not report.sqlite_local_authority_ready


def test_postgres_plan_needs_no_filesystem_probe():
    plan = capabilities.plan_storage(backend='postgres')
    assert plan.strategy == 'client-server'
    assert plan.decision == 'automatic'
    assert not plan.user_action_required


def test_local_sqlite_authority_is_selected_automatically(tmp_path):
    plan = capabilities.plan_storage(
        backend='sqlite', authority=_report(tmp_path / 'data'))
    assert plan.strategy == 'sqlite-direct'
    assert plan.reason_code == 'local_authority_ready'
    assert not plan.user_action_required


def test_faster_local_front_requires_explicit_durability_consent(tmp_path):
    authority = _report(
        tmp_path / 'network', storage_class='network-filesystem',
        persistence='unknown')
    candidate = _report(tmp_path / 'local')
    plan = capabilities.plan_storage(
        backend='sqlite', authority=authority, candidate=candidate,
        measured_speedup=12.0)
    assert plan.strategy == 'sqlite-direct'
    assert plan.recommended_strategy == 'sqlite-local-front'
    assert plan.decision == 'consent-required'
    assert plan.user_action_required


def test_consented_verified_local_front_is_selected(tmp_path):
    plan = capabilities.plan_storage(
        backend='sqlite',
        authority=_report(
            tmp_path / 'network', storage_class='network-filesystem',
            persistence='unknown'),
        candidate=_report(tmp_path / 'local'),
        measured_speedup=12.0,
        bounded_rpo_consent=True,
    )
    assert plan.strategy == 'sqlite-local-front'
    assert plan.decision == 'automatic-after-consent'
    assert plan.durability_contract == (
        'bounded-rpo-local-ack-with-durable-shadow')


def test_remote_candidate_is_blocked_even_if_its_microbenchmark_wins(tmp_path):
    plan = capabilities.plan_storage(
        backend='sqlite',
        authority=_report(
            tmp_path / 'authority', storage_class='network-filesystem',
            persistence='unknown'),
        candidate=_report(
            tmp_path / 'other-network', storage_class='network-filesystem',
            persistence='unknown'),
        measured_speedup=100.0,
        bounded_rpo_consent=True,
    )
    assert plan.strategy == 'sqlite-direct'
    assert plan.decision == 'blocked'
    assert plan.reason_code == 'candidate_capabilities_failed'


def test_report_digest_is_deterministic_for_exact_evidence(tmp_path):
    report = _report(tmp_path / 'data')
    assert capabilities.report_digest(report) == capabilities.report_digest(report)
    assert len(capabilities.report_digest(report)) == 64


@pytest.mark.parametrize('speedup', [float('nan'), float('inf'), -1.0])
def test_non_finite_or_negative_benchmark_evidence_fails_closed(
        tmp_path, speedup):
    with pytest.raises(ValueError, match='finite and non-negative'):
        capabilities.plan_storage(
            backend='sqlite',
            authority=_report(tmp_path / 'authority'),
            candidate=_report(tmp_path / 'candidate'),
            measured_speedup=speedup,
        )
