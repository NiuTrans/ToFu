"""Installed discovery shares one no-follow metadata observation per package.

Cached list_skills calls must still observe every admitted SKILL.md and refresh
eligibility, without rereading instructions or repeating its metadata queries.
Real temporary files pin ordering, freshness and rejection of linked/special
instruction files. Operation counts do not establish end-to-end latency.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def skill_workspace(tmp_path, monkeypatch):
    import lib.credentials_vault as vault
    import lib.memory.storage._dirs as memory_dirs
    import lib.skills.registry as registry

    monkeypatch.setattr(
        memory_dirs, '_server_data_dir', lambda: str(tmp_path / 'data'))
    monkeypatch.setattr(memory_dirs, '_migrated_roots', set())
    monkeypatch.setattr(memory_dirs, '_server_store_migrated', False)
    monkeypatch.setattr(registry, '_skills_cache', {})
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    return tmp_path


def _write_package(root: Path, name: str, body='guide', metadata='') -> Path:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    skill_md = package / 'SKILL.md'
    skill_md.write_text(
        f'---\nname: {name}\ndescription: {name} workflow\n{metadata}'
        f'---\n\n{body}\n', encoding='utf-8')
    return skill_md


def _metadata_probes(monkeypatch) -> list[tuple[str, str, bool]]:
    probes = []
    real_stat, real_lstat = os.stat, os.lstat

    def record(operation, function, path, *args, **kwargs):
        if not isinstance(path, int) and os.path.basename(path) == 'SKILL.md':
            follows = kwargs.get('follow_symlinks', operation == 'stat')
            probes.append((operation, os.fspath(path), follows))
        return function(path, *args, **kwargs)

    monkeypatch.setattr(
        os, 'stat', lambda path, *args, **kwargs:
        record('stat', real_stat, path, *args, **kwargs))
    monkeypatch.setattr(
        os, 'lstat', lambda path, *args, **kwargs:
        record('lstat', real_lstat, path, *args, **kwargs))
    return probes


def _unexpected_instruction_read(*args, **kwargs):
    raise AssertionError('An unchanged discovery hit reread its instructions')


@pytest.mark.parametrize('package_count', [1, 32, 128])
def test_cached_discovery_observes_each_instruction_file_once(
        skill_workspace, monkeypatch, package_count):
    import lib.memory.storage as storage
    from lib.skills import list_skills

    project = skill_workspace / 'project'
    root = project / '.agents' / 'skills'
    names = [f'guide-{index:03d}' for index in range(package_count)]
    paths = {_write_package(root, name) for name in reversed(names)}
    before = list_skills(str(project), owner_user_id=7)
    assert [row['id'] for row in before] == names

    monkeypatch.setattr(storage, '_memory_from_file', _unexpected_instruction_read)
    probes = _metadata_probes(monkeypatch)
    after = list_skills(str(project), owner_user_id=7)

    assert after == before
    assert {path for _operation, path, _follows in probes} == {
        str(path) for path in paths}
    assert len(probes) == package_count, (
        'Regular-file admission and fingerprint mtime must share one '
        f'metadata observation, got {len(probes)} for {package_count} packages')
    assert all(not follows for _operation, _path, follows in probes), (
        'Instruction admission must never follow a SKILL.md symlink')


def test_discovery_keeps_precedence_and_shadowed_sources(skill_workspace):
    from lib.skills import list_skills
    from lib.skills.paths import owner_global_skills_dir

    project = skill_workspace / 'project'
    extra = skill_workspace / 'extra'
    _write_package(Path(owner_global_skills_dir(7)), 'shared', 'global guide')
    _write_package(project / '.agents' / 'skills', 'shared', 'project guide')
    _write_package(extra / '.codex' / 'skills', 'shared', 'extra guide')
    _write_package(project / '.agents' / 'skills', 'alpha')
    arguments = dict(extra_paths=[str(extra)], owner_user_id=7)

    selected = list_skills(str(project), **arguments)
    assert [row['id'] for row in selected] == ['shared', 'alpha']
    assert selected[0]['scope'] == 'global'
    assert selected[0]['shadowed'] == 2
    assert selected[0]['body'] == 'global guide'
    assert list_skills(str(project), **arguments) == selected

    sources = list_skills(str(project), include_shadowed=True, **arguments)
    shared = [row for row in sources if row['id'] == 'shared']
    assert [row['body'] for row in shared] == [
        'global guide', 'project guide', 'extra guide']
    assert all(row['shadowed_by'] == shared[0]['skill_ref'] for row in shared[1:])
    assert list_skills(str(project), include_shadowed=True, **arguments) == sources


def test_discovery_rejects_linked_missing_and_special_instructions(skill_workspace):
    from lib.skills import list_skills

    project = skill_workspace / 'project'
    root = project / '.agents' / 'skills'
    outside = _write_package(skill_workspace / 'outside', 'external')
    _write_package(root, 'valid')
    _write_package(root, '.hidden')
    _write_package(root, 'global')
    (root / 'linked-package').symlink_to(outside.parent, target_is_directory=True)
    for name, target in [('linked-file', outside), ('broken', root / 'missing')]:
        (root / name).mkdir()
        (root / name / 'SKILL.md').symlink_to(target)
    (root / 'no-instructions').mkdir()
    (root / 'directory' / 'SKILL.md').mkdir(parents=True)
    if hasattr(os, 'mkfifo'):
        (root / 'fifo').mkdir()
        os.mkfifo(root / 'fifo' / 'SKILL.md')

    rows = list_skills(str(project), owner_user_id=7)
    assert [row['id'] for row in rows] == ['valid']
    assert list_skills(str(project), owner_user_id=7) == rows


def test_discovery_refreshes_edits_additions_removals_and_symlink_replacement(
        skill_workspace):
    from lib.skills import list_skills

    project = skill_workspace / 'project'
    root = project / '.agents' / 'skills'
    skill_md = _write_package(root, 'original', 'version one')
    [original] = list_skills(str(project), owner_user_id=7)
    stamp = skill_md.stat()
    _write_package(root, 'original', 'version two')
    os.utime(skill_md, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
    [updated] = list_skills(str(project), owner_user_id=7)
    assert (original['body'], updated['body']) == ('version one', 'version two')

    added = _write_package(root, 'added')
    assert [row['id'] for row in list_skills(str(project), owner_user_id=7)] == [
        'added', 'original']
    added.unlink()
    outside = _write_package(skill_workspace / 'outside', 'external')
    skill_md.unlink()
    skill_md.symlink_to(outside)
    assert list_skills(str(project), owner_user_id=7) == []


def test_cached_discovery_refreshes_eligibility_without_instruction_reads(
        skill_workspace, monkeypatch):
    import lib.memory.storage as storage
    from lib.skills import list_skills

    requirement = 'TOFU_SKILL_SNAPSHOT_TEST_REQUIREMENT'
    monkeypatch.delenv(requirement, raising=False)
    project = skill_workspace / 'project'
    _write_package(
        project / '.agents' / 'skills', 'needs-env', metadata=(
            'metadata:\n  openclaw:\n    requires:\n'
            f'      env: [{requirement}]\n'))
    [blocked] = list_skills(str(project), owner_user_id=7)
    assert blocked['eligible'] is False

    monkeypatch.setenv(requirement, 'synthetic-test-value')
    monkeypatch.setattr(storage, '_memory_from_file', _unexpected_instruction_read)
    [available] = list_skills(str(project), owner_user_id=7)
    assert available['eligible'] is True
    assert available['ineligible_reasons'] == []


def test_instruction_metadata_failure_is_file_local(skill_workspace, monkeypatch):
    from lib.skills import list_skills

    project = skill_workspace / 'project'
    root = project / '.agents' / 'skills'
    _write_package(root, 'good')
    blocked = _write_package(root, 'unreadable')
    assert len(list_skills(str(project), owner_user_id=7)) == 2
    real_stat, real_lstat = os.stat, os.lstat

    def observe(function, path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path) == str(blocked):
            raise PermissionError('synthetic unreadable instruction metadata')
        return function(path, *args, **kwargs)

    monkeypatch.setattr(
        os, 'stat', lambda path, *args, **kwargs:
        observe(real_stat, path, *args, **kwargs))
    monkeypatch.setattr(
        os, 'lstat', lambda path, *args, **kwargs:
        observe(real_lstat, path, *args, **kwargs))
    assert [row['id'] for row in list_skills(str(project), owner_user_id=7)] == ['good']
