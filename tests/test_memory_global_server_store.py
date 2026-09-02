"""tests/test_memory_global_server_store.py — Server-side global memory store.

Covers the 2026-06 move of GLOBAL memories out of the per-project
``<project>/.tofu/skills/global/`` directory and into the project-independent
server store ``$TOFU_DATA_DIR/memories/global/``:

  * a global memory can be created + read with NO project attached;
  * a global memory created under project A is visible from project B
    (cross-project reach — the bug that motivated the change);
  * project-scoped memories still require a project and stay per-project;
  * pre-existing legacy ``.tofu/skills/global`` memories are migrated into
    the server store on first read (idempotent, original preserved).

Each test points ``TOFU_DATA_DIR`` at a fresh tmp dir and resets the module's
process-level migration latch so runs are isolated.
"""
import importlib
import os

import pytest

import lib.memory.storage as storage
import lib.memory.storage._dirs as storage_dirs


pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Redirect the server data dir to a tmp dir and clear migration state."""
    data_dir = tmp_path / 'data'
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    yield tmp_path
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _proj(tmp_path, name):
    p = tmp_path / name
    (p / '.tofu' / 'skills').mkdir(parents=True, exist_ok=True)
    return str(p)


def test_global_create_and_read_without_project(isolated_store):
    """A global memory works with project_path=None (the original bug)."""
    mem = storage.create_memory(
        name='No Project Global',
        description='global memory created with no project attached at all',
        body='body', scope='global', project_path=None,
    )
    # It physically lives in the server store, not under any project.
    assert os.path.join('memories', 'global') in mem['filepath']
    found = storage.get_eligible_memories(project_path=None)
    assert any(m['id'] == mem['id'] for m in found)


def test_global_is_cross_project(isolated_store, tmp_path):
    """A global memory created 'under' project A is visible from project B."""
    proj_a = _proj(tmp_path, 'projA')
    proj_b = _proj(tmp_path, 'projB')

    storage.create_memory(
        name='Shared Lesson',
        description='a cross-project global lesson that both A and B can see',
        body='shared', scope='global', project_path=proj_a,
    )

    from_b = storage.list_memories(project_path=proj_b, scope='global')
    assert any(m['name'] == 'Shared Lesson' for m in from_b), \
        'global memory must be visible from a different project'


def test_project_scope_still_requires_project(isolated_store):
    """project-scoped memories cannot be created without a project root."""
    with pytest.raises(ValueError):
        storage.resolve_target_dir('project', None)
    # global is fine without one
    assert storage.resolve_target_dir('global', None)


def test_project_memory_stays_local(isolated_store, tmp_path):
    """A project memory under A is NOT visible from B."""
    proj_a = _proj(tmp_path, 'projA')
    proj_b = _proj(tmp_path, 'projB')
    storage.create_memory(
        name='A Only', description='a project-scoped memory local to A only',
        body='x', scope='project', project_path=proj_a,
    )
    assert not any(
        m['name'] == 'A Only'
        for m in storage.list_memories(project_path=proj_b, scope='project'))
    assert any(
        m['name'] == 'A Only'
        for m in storage.list_memories(project_path=proj_a, scope='project'))


def test_legacy_global_is_migrated(isolated_store, tmp_path):
    """A pre-existing .tofu/skills/global memory migrates into the server store."""
    proj = _proj(tmp_path, 'legacyproj')
    legacy_dir = os.path.join(proj, storage.GLOBAL_MEMORY_SUBDIR)
    os.makedirs(legacy_dir, exist_ok=True)
    legacy_file = os.path.join(legacy_dir, 'old_global.md')
    with open(legacy_file, 'w', encoding='utf-8') as f:
        f.write('---\nname: Old Global\n'
                'description: a legacy global memory from before the move\n'
                'enabled: true\n---\n\nlegacy body\n')

    # First read triggers migration.
    mems = storage.list_memories(project_path=proj, scope='global')
    assert any(m['name'] == 'Old Global' for m in mems)

    # The migrated copy now lives in the server store.
    server_dir = storage._server_global_memory_dir()
    assert os.path.isfile(os.path.join(server_dir, 'old_global.md'))
    # Original legacy file is preserved (copy, not move).
    assert os.path.isfile(legacy_file)

    # Idempotent: a second read does not duplicate the id.
    storage._migrated_roots.clear()  # force re-scan
    mems2 = storage.list_memories(project_path=proj, scope='global')
    ids = [m['id'] for m in mems2 if m['name'] == 'Old Global']
    assert len(ids) == 1


def test_server_store_wins_on_id_collision(isolated_store, tmp_path):
    """When a legacy global and a server global share an id, server wins."""
    proj = _proj(tmp_path, 'collproj')
    # Server-store entry.
    storage.create_memory(
        name='Dup', description='the authoritative server-store version here',
        body='SERVER', scope='global', project_path=None,
    )
    # Legacy entry with the SAME id but different body.
    legacy_dir = os.path.join(proj, storage.GLOBAL_MEMORY_SUBDIR)
    os.makedirs(legacy_dir, exist_ok=True)
    with open(os.path.join(legacy_dir, 'dup.md'), 'w', encoding='utf-8') as f:
        f.write('---\nname: Dup\ndescription: stale legacy copy of the dup id\n'
                '---\n\nLEGACY\n')

    mems = [m for m in storage.list_memories(project_path=proj, scope='global')
            if m['id'] == 'dup']
    assert len(mems) == 1
    assert mems[0]['body'] == 'SERVER'
