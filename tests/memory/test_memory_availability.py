"""Memory availability equals a non-empty eligible listing but stops early.

``has_eligible_memories`` answers ``bool(get_eligible_memories(...))`` with
the same precedence, shadowing and eligibility gates, reading metadata only
until the first visible eligible memory instead of materializing the corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def availability_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    extra = tmp_path / 'extra'
    for root in (project, extra):
        (root / '.tofu' / 'memories').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._migrated_roots.update({str(project), str(extra)})
    storage_dirs._server_store_migrated = True
    memory_files._metadata_cache.clear()
    yield data_dir, project, extra
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _write_memory(directory: Path, memory_id: str, *extra_frontmatter: str):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f'{memory_id}.md').write_text(
        '---\n'
        f'name: {memory_id}\n'
        f'description: availability fixture for {memory_id}\n'
        + ''.join(f'{line}\n' for line in extra_frontmatter)
        + '---\n\nbody\n',
        encoding='utf-8',
    )


def _write_package(directory: Path, package_id: str):
    package_dir = directory / package_id
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / 'SKILL.md').write_text(
        '---\n'
        f'name: {package_id}\n'
        f'description: availability package fixture for {package_id}\n'
        '---\n\nguide\n',
        encoding='utf-8',
    )


def _empty(_data, _project, _extra):
    return False


def _disabled_only(_data, project, _extra):
    _write_memory(project / '.tofu' / 'memories', 'off', 'enabled: false')
    return False


def _ineligible_only(_data, project, _extra):
    _write_memory(
        project / '.tofu' / 'memories', 'gated',
        'requires_bins: [tofu-availability-missing-binary]')
    return False


def _package_only(_data, project, _extra):
    _write_package(project / '.tofu' / 'skills', 'guide')
    return False


def _package_shadows_same_id_memory(data, project, _extra):
    _write_package(data / 'skills' / 'global', 'shared-id')
    _write_memory(project / '.tofu' / 'memories', 'shared-id')
    return False


def _disabled_global_shadows_project_memory(data, project, _extra):
    _write_memory(data / 'memories' / 'global', 'shared-id', 'enabled: false')
    _write_memory(project / '.tofu' / 'memories', 'shared-id')
    return False


def _eligible_after_ineligible(_data, project, _extra):
    _write_memory(project / '.tofu' / 'memories', 'a-off', 'enabled: false')
    _write_memory(project / '.tofu' / 'memories', 'b-on')
    return True


def _eligible_in_extra_root_only(_data, _project, extra):
    _write_memory(extra / '.tofu' / 'memories', 'extra-note')
    return True


@pytest.mark.parametrize('arrange', [
    _empty,
    _disabled_only,
    _ineligible_only,
    _package_only,
    _package_shadows_same_id_memory,
    _disabled_global_shadows_project_memory,
    _eligible_after_ineligible,
    _eligible_in_extra_root_only,
])
def test_availability_matches_the_eligible_listing(availability_store, arrange):
    from lib.memory.storage import get_eligible_memories, has_eligible_memories

    data, project, extra = availability_store
    expected = arrange(data, project, extra)
    roots = (str(project), [str(extra)])

    assert bool(get_eligible_memories(*roots)) is expected
    assert has_eligible_memories(*roots) is expected


def test_availability_reads_only_until_the_first_eligible_memory(
        availability_store, monkeypatch):
    import lib.memory.storage._files as memory_files
    from lib.memory.storage import get_eligible_memories, has_eligible_memories

    _data, project, _extra = availability_store
    for index in range(50):
        _write_memory(project / '.tofu' / 'memories', f'note-{index:02d}')
    reads: list[str] = []
    original = memory_files._memory_from_file

    def counted(filepath, *args, **kwargs):
        reads.append(str(filepath))
        return original(filepath, *args, **kwargs)

    monkeypatch.setattr(memory_files, '_memory_from_file', counted)

    assert has_eligible_memories(str(project)) is True
    assert [Path(path).name for path in reads] == ['note-00.md']
    reads.clear()
    assert len(get_eligible_memories(str(project))) == 50
    assert len(reads) == 50
