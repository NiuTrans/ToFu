"""tests/test_skill_storage_split.py — Memory/skill physical storage split.

Covers the 2026-07 split (board epic pt_229606ca): flat memories and skill
packages stop sharing one tree —

  * project flat memories  ``.tofu/skills/*.md``      → MOVED to ``.tofu/memories/``
  * project skill packages ``.tofu/skills/<id>/``     → stay put (permanent home)
  * global skill packages  ``<data>/memories/global/<id>/`` → MOVED to
    ``<data>/skills/global/<id>/``
  * legacy per-project global packages ``.tofu/skills/global/<id>/`` → COPIED
    to the global skills store (original preserved, shadowed by id)

plus the union-preservation invariant that keeps the transitional
``list_all_memories`` return shape byte-compatible until the channels fully
decouple, and the installer's retargeting onto the split trees.

Every test redirects the server data dir to a tmp dir (the real store must
never be touched) and resets both migration latches.
"""

import io
import os
import zipfile

import pytest

import lib.memory.storage as storage
import lib.memory.storage._dirs as dirs


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Redirect the server data dir to tmp + clear migration latches."""
    data_dir = tmp_path / 'data'
    monkeypatch.setattr(dirs, '_server_data_dir', lambda: str(data_dir))
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False
    yield tmp_path
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False


# ── seeding helpers ──────────────────────────────────────────────────

def _write_flat(dirpath, fname, body='body'):
    os.makedirs(dirpath, exist_ok=True)
    name = fname[:-3] if fname.endswith('.md') else fname
    with open(os.path.join(dirpath, fname), 'w', encoding='utf-8') as f:
        f.write(f'---\nname: {name}\n'
                f'description: test memory {name} description text\n'
                f'---\n\n{body}\n')


def _write_pkg(dirpath, pkg_id):
    pkg_dir = os.path.join(dirpath, pkg_id)
    os.makedirs(os.path.join(pkg_dir, 'references'), exist_ok=True)
    with open(os.path.join(pkg_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write(f'---\nname: {pkg_id}\n'
                f'description: test skill package {pkg_id} description\n'
                f'---\n\npkg body\n')
    with open(os.path.join(pkg_dir, 'references', 'r.md'), 'w',
              encoding='utf-8') as f:
        f.write('reference\n')


def _skill_zip_bytes(pkg_folder='zipskill', name='Zip Skill'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr(f'{pkg_folder}/SKILL.md',
                    f'---\nname: {name}\n'
                    f'description: a zip-installed test skill package\n'
                    f'---\n\nbody\n')
    return buf.getvalue()


def _proj(tmp_path, name='proj'):
    p = tmp_path / name
    (p / '.tofu' / 'skills').mkdir(parents=True, exist_ok=True)
    return str(p)


def _by_id(mems):
    return {m['id']: m for m in mems}


# ── migration: project flat memories ────────────────────────────────

@pytest.mark.unit
def test_project_flat_memories_migrate_to_memories_dir(isolated):
    proj = _proj(isolated)
    legacy = os.path.join(proj, '.tofu', 'skills')
    _write_flat(legacy, 'a.md', body='AAA')
    _write_flat(legacy, 'b.md', body='BBB')
    _write_pkg(legacy, 'mypkg')
    _write_flat(os.path.join(legacy, 'global'), 'g.md')  # legacy global dir

    mems = storage.list_all_memories(project_path=proj)
    by_id = _by_id(mems)

    # Flat memories MOVED to .tofu/memories/ and still listed (project scope).
    assert os.path.isfile(os.path.join(proj, '.tofu', 'memories', 'a.md'))
    assert os.path.isfile(os.path.join(proj, '.tofu', 'memories', 'b.md'))
    assert not os.path.exists(os.path.join(legacy, 'a.md'))
    assert by_id['a']['scope'] == 'project' and not by_id['a']['is_package']
    assert by_id['b']['scope'] == 'project' and not by_id['b']['is_package']

    # Skill package + legacy global dir are NOT touched by the flat migration.
    assert os.path.isfile(os.path.join(legacy, 'mypkg', 'SKILL.md'))
    assert os.path.isfile(os.path.join(legacy, 'global', 'g.md'))
    assert by_id['mypkg']['is_package'] and by_id['mypkg']['scope'] == 'project'
    assert by_id['g']['scope'] == 'global'


@pytest.mark.unit
def test_migration_is_idempotent(isolated):
    proj = _proj(isolated)
    legacy = os.path.join(proj, '.tofu', 'skills')
    _write_flat(legacy, 'a.md')

    first = _by_id(storage.list_all_memories(project_path=proj))
    assert 'a' in first

    # Force a full re-run of every migration: state must not change.
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False
    second = _by_id(storage.list_all_memories(project_path=proj))
    assert list(second) == list(first)
    assert os.path.isfile(os.path.join(proj, '.tofu', 'memories', 'a.md'))
    # No duplicate copy reappeared in the legacy dir.
    assert not os.path.exists(os.path.join(legacy, 'a.md'))


@pytest.mark.unit
def test_migration_collision_keeps_both_copies(isolated):
    proj = _proj(isolated)
    legacy = os.path.join(proj, '.tofu', 'skills')
    _write_flat(legacy, 'a.md', body='LEGACY')
    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'a.md', body='NEW')

    mems = _by_id(storage.list_all_memories(project_path=proj))
    # Both files survive; the post-split location wins by scan order.
    assert os.path.isfile(os.path.join(legacy, 'a.md'))
    assert os.path.isfile(os.path.join(proj, '.tofu', 'memories', 'a.md'))
    assert mems['a']['body'] == 'NEW'


# ── migration: global packages ───────────────────────────────────────

@pytest.mark.unit
def test_server_store_packages_migrate_to_skills_store(isolated):
    data_dir = isolated / 'data'
    _write_pkg(str(data_dir / 'memories' / 'global'), 'gpkg')
    _write_flat(str(data_dir / 'memories' / 'global'), 'gmem.md')

    mems = _by_id(storage.list_all_memories(project_path=None))

    # Package MOVED to the skills store; flat memory stays put.
    assert os.path.isfile(
        str(data_dir / 'skills' / 'global' / 'gpkg' / 'SKILL.md'))
    assert not os.path.exists(
        str(data_dir / 'memories' / 'global' / 'gpkg'))
    assert os.path.isfile(
        str(data_dir / 'memories' / 'global' / 'gmem.md'))
    assert mems['gpkg']['is_package'] and mems['gpkg']['scope'] == 'global'
    assert not mems['gmem']['is_package']


@pytest.mark.unit
def test_legacy_project_global_package_copied_to_skills_store(isolated):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills', 'global'), 'oldpkg')

    mems = _by_id(storage.list_all_memories(project_path=proj))

    # Copied into the server SKILLS store (not the memory store); original kept.
    assert os.path.isfile(
        str(isolated / 'data' / 'skills' / 'global' / 'oldpkg' / 'SKILL.md'))
    assert not os.path.exists(
        str(isolated / 'data' / 'memories' / 'global' / 'oldpkg'))
    assert os.path.isfile(
        os.path.join(proj, '.tofu', 'skills', 'global', 'oldpkg', 'SKILL.md'))
    assert mems['oldpkg']['is_package'] and mems['oldpkg']['scope'] == 'global'


# ── installer retargeting ────────────────────────────────────────────

@pytest.mark.unit
def test_installer_targets_split_trees(isolated):
    from lib.skills import install_skill_package
    proj = _proj(isolated)

    # NB: the package id derives from the SKILL.md ``name``, not the zip
    # folder name ('Proj Skill' → ``proj_skill``).
    res_p = install_skill_package(
        _skill_zip_bytes('pskill', 'Proj Skill'), scope='project',
        project_path=proj)
    assert os.path.isfile(
        os.path.join(proj, '.tofu', 'skills', 'proj_skill', 'SKILL.md'))
    assert res_p['memory']['scope'] == 'project'

    res_g = install_skill_package(
        _skill_zip_bytes('gskill', 'Global Skill'), scope='global',
        project_path=proj)
    assert os.path.isfile(
        str(isolated / 'data' / 'skills' / 'global' / 'global_skill' / 'SKILL.md'))
    assert res_g['memory']['scope'] == 'global'
    # Global packages must NOT land in the memory store anymore.
    assert not os.path.exists(
        str(isolated / 'data' / 'memories' / 'global' / 'global_skill'))


# ── skills registry + union preservation ─────────────────────────────

@pytest.mark.unit
def test_list_skills_registry_returns_only_packages(isolated):
    from lib.skills import get_skill, list_skills
    proj = _proj(isolated)
    _write_flat(os.path.join(proj, '.tofu', 'skills'), 'mem1.md')
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    _write_flat(str(isolated / 'data' / 'memories' / 'global'), 'gmem.md')
    _write_pkg(str(isolated / 'data' / 'skills' / 'global'), 'gpkg')

    skills = _by_id(list_skills(project_path=proj))
    assert set(skills) == {'mypkg', 'gpkg'}           # no flat memories
    assert skills['mypkg']['scope'] == 'project'
    assert skills['gpkg']['scope'] == 'global'
    assert all(s['is_package'] for s in skills.values())

    assert get_skill('mypkg', project_path=proj)['id'] == 'mypkg'
    assert get_skill('nonexistent', project_path=proj) is None


@pytest.mark.unit
def test_union_preserved_full_set(isolated):
    """Byte-compat pin for the transitional list_all_memories shape: every
    noun lands in the union exactly once with the right flags."""
    proj = _proj(isolated)
    _write_flat(os.path.join(proj, '.tofu', 'skills'), 'a.md')
    _write_flat(os.path.join(proj, '.tofu', 'skills'), 'b.md')
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    _write_flat(str(isolated / 'data' / 'memories' / 'global'), 'gmem.md')
    _write_pkg(str(isolated / 'data' / 'skills' / 'global'), 'gpkg')

    mems = _by_id(storage.list_all_memories(project_path=proj))
    assert {'a', 'b', 'mypkg', 'gmem', 'gpkg'} <= set(mems)
    assert not mems['a']['is_package'] and mems['a']['scope'] == 'project'
    assert not mems['b']['is_package'] and mems['b']['scope'] == 'project'
    assert mems['mypkg']['is_package'] and mems['mypkg']['scope'] == 'project'
    assert not mems['gmem']['is_package'] and mems['gmem']['scope'] == 'global'
    assert mems['gpkg']['is_package'] and mems['gpkg']['scope'] == 'global'


@pytest.mark.unit
def test_memories_dir_scan_pin(isolated):
    """Guards the scan-set: a migrated flat memory must be listed FROM the
    new .tofu/memories/ location (a NEUTER that drops the new scan root
    flips this red)."""
    proj = _proj(isolated)
    _write_flat(os.path.join(proj, '.tofu', 'skills'), 'a.md')

    mems = _by_id(storage.list_all_memories(project_path=proj))
    assert 'a' in mems, 'migrated flat memory vanished from the listing'
    assert os.path.join('.tofu', 'memories') in mems['a']['filepath']
