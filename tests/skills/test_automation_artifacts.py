"""Complete private frozen artifacts survive staging cleanup and corruptions fail closed."""
import io
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from lib.automations.errors import AutomationError
from lib.identity import PrincipalContext
from lib.skills.automation import artifacts
from lib.skills.automation.artifacts import SkillArtifactStore
from lib.skills.installer import stage_skill_snapshot_package

pytestmark = pytest.mark.unit


@pytest.fixture
def setup(tmp_path):
    principal = PrincipalContext.user(subject_id='artifact-owner', owner_user_id=41,
                                      tenant_id='frozen', scopes={'skills:read'})
    stage = tmp_path / 'staging'
    stage.mkdir(mode=0o700)
    store = SkillArtifactStore(tmp_path / 'artifacts', maximum_package_bytes=25 * 1024 * 1024,
                              reserved_free_bytes=1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('pkg/SKILL.md', '---\nname: citadel\ndescription: Frozen package\n---\nRead every resource.\n')
        archive.writestr('pkg/references/说明.txt', '完整冻结资源\n' * 3000)
        archive.writestr('pkg/scripts/setup.py', 'raise RuntimeError("never execute")\n')
    return principal, stage, store, buffer.getvalue()


def promote(setup):
    principal, stage, store, archive = setup
    with stage_skill_snapshot_package(archive, parent_directory=str(stage), owner_user_id=41) as package:
        descriptor = store.promote(principal, package)
        assert store.promote(principal, package) == descriptor
        extracted = Path(package['root'])
    assert not extracted.exists()
    return descriptor


def test_complete_resources_survive_private_staging_cleanup_and_reopen(setup):
    principal, _, store, _ = setup
    descriptor = promote(setup)
    digest = descriptor['seal']['content_sha256']
    root = store.verified_root(principal, digest)
    assert (root / 'references/说明.txt').read_text() == '完整冻结资源\n' * 3000
    assert (root / 'scripts/setup.py').read_text().startswith('raise RuntimeError')
    assert descriptor['seal']['file_count'] == 3
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / 'SKILL.md').stat().st_mode & 0o777 == 0o600
    reopened = SkillArtifactStore(store.root, maximum_package_bytes=25 * 1024 * 1024, reserved_free_bytes=1024)
    assert reopened.verified_root(principal, digest) == root


@pytest.mark.parametrize('mutation', ['resource', 'manifest', 'symlink'])
def test_frozen_content_and_manifest_tampering_fail_closed(setup, mutation):
    principal, _, store, _ = setup
    digest = promote(setup)['seal']['content_sha256']
    root = store.verified_root(principal, digest)
    if mutation == 'resource':
        (root / 'references/说明.txt').write_text('modified')
    elif mutation == 'manifest':
        (root.parent / 'seal.json').write_text('{}')
    else:
        (root / 'SKILL.md').unlink()
        (root / 'SKILL.md').symlink_to(root / 'scripts/setup.py')
    with pytest.raises((AutomationError, ValueError)):
        store.verified_root(principal, digest)


def test_foreign_tenant_cannot_resolve_the_same_digest(setup):
    principal, _, store, _ = setup
    digest = promote(setup)['seal']['content_sha256']
    foreign = PrincipalContext.user(subject_id='foreign', owner_user_id=41,
                                    tenant_id='other', scopes={'skills:read'})
    with pytest.raises(AutomationError):
        store.verified_root(foreign, digest)
    assert store.verified_root(principal, digest).exists()


def test_volume_headroom_rejects_before_promotion(setup, monkeypatch):
    principal, stage, store, archive = setup
    monkeypatch.setattr(artifacts.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1024))
    with stage_skill_snapshot_package(archive, parent_directory=str(stage), owner_user_id=41) as package:
        with pytest.raises(AutomationError, match='headroom'):
            store.promote(principal, package)
    assert not list(store.root.glob('*/*/seal.json'))


def test_directory_sync_failure_after_rename_retains_complete_bytes(setup, monkeypatch):
    principal, stage, store, archive = setup
    original = artifacts._sync_directory

    def fail_after_publish(path):
        if list(path.glob('*/seal.json')):
            raise OSError('Injected directory fsync failure')
        original(path)

    with stage_skill_snapshot_package(archive, parent_directory=str(stage), owner_user_id=41) as package:
        digest = package['seal']['content_sha256']
        monkeypatch.setattr(artifacts, '_sync_directory', fail_after_publish)
        with pytest.raises(OSError):
            store.promote(principal, package)
    monkeypatch.setattr(artifacts, '_sync_directory', original)
    assert store.verified_root(principal, digest).exists()
    with stage_skill_snapshot_package(archive, parent_directory=str(stage), owner_user_id=41) as package:
        assert store.promote(principal, package)['seal']['content_sha256'] == digest


def test_headroom_drop_during_copy_leaves_no_published_artifact(setup, monkeypatch):
    principal, stage, store, archive = setup
    observations = iter([25 * 1024 * 1024, 0])
    monkeypatch.setattr(artifacts.shutil, 'disk_usage', lambda _: SimpleNamespace(free=next(observations)))
    with stage_skill_snapshot_package(archive, parent_directory=str(stage), owner_user_id=41) as package:
        with pytest.raises(AutomationError, match='changed during staging'):
            store.promote(principal, package)
    assert not list(store.root.glob('*/*/seal.json'))


def test_large_complete_package_deduplicates_durable_growth(setup):
    import json
    import resource
    import time
    principal, stage, store, _ = setup
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('large/SKILL.md', '---\nname: large-fixture\ndescription: Capacity probe\n---\nRead the complete binary.\n')
        archive.writestr('large/references/full.bin', bytes(range(256)) * 40960)
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.monotonic()
    with stage_skill_snapshot_package(buffer.getvalue(), parent_directory=str(stage), owner_user_id=41) as package:
        first = store.promote(principal, package)
        root = store.verified_root(principal, first['seal']['content_sha256'])
        paths = [root / name for name in first['seal']['files']] + [root.parent / 'seal.json']
        physical = sum(path.stat().st_blocks * 512 for path in paths)
        logical = sum(path.stat().st_size for path in paths)
        assert store.promote(principal, package) == first
        assert sum(path.stat().st_blocks * 512 for path in paths) == physical
        assert (root / 'references/full.bin').stat().st_size == 10 * 1024 * 1024
    print(json.dumps({'artifact_content_bytes': first['seal']['total_bytes'],
        'artifact_files_and_manifest_bytes': logical, 'allocated_file_bytes': physical,
        'duplicate_promotion_additional_bytes': 0, 'duration_seconds': time.monotonic() - started,
        'peak_rss_growth_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_rss}))


def test_symlinked_storage_root_is_rejected(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir(mode=0o700)
    alias = tmp_path / 'alias'
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError):
        SkillArtifactStore(alias, maximum_package_bytes=100, reserved_free_bytes=0)
