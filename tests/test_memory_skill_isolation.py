"""tests/test_memory_skill_isolation.py — Memory/skill channel isolation (P3).

Pins the decoupling contract (board epic pt_229606ca):

  * the MEMORY corpus (prefetch eligible set / search_memories / injection
    count hint) is pure MEMORY — skill packages no longer compete with
    experience notes for injection slots;
  * model-side CRUD (update / delete / merge) REFUSES skill packages with an
    actionable error pointing at the Settings → Skills tab;
  * the paths the Settings UI depends on keep working: union listing
    (list_all_memories), get_memory, toggle_memory (enable/disable),
    create_memory (flat memories unaffected).
"""

import os

import pytest

import lib.memory.storage as storage
import lib.memory.storage._dirs as dirs


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    monkeypatch.setattr(dirs, '_server_data_dir', lambda: str(data_dir))
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False
    yield tmp_path
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False


def _write_flat(dirpath, name, body='flat body'):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, f'{name}.md'), 'w', encoding='utf-8') as f:
        f.write(f'---\nname: {name}\n'
                f'description: flat memory {name} description text\n'
                f'---\n\n{body}\n')


def _write_pkg(dirpath, pkg_id, body='pkg guide'):
    pkg_dir = os.path.join(dirpath, pkg_id)
    os.makedirs(pkg_dir, exist_ok=True)
    with open(os.path.join(pkg_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write(f'---\nname: {pkg_id}\n'
                f'description: skill package {pkg_id} description\n'
                f'---\n\n{body}\n')
    return pkg_dir


def _proj(tmp_path, name='proj'):
    p = tmp_path / name
    (p / '.tofu').mkdir(parents=True, exist_ok=True)
    return str(p)


# ── corpus purity ────────────────────────────────────────────────────

@pytest.mark.unit
def test_eligible_memories_excludes_packages(isolated):
    proj = _proj(isolated)
    skills_root = os.path.join(proj, '.tofu', 'skills')
    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'mem1')
    _write_pkg(skills_root, 'mypkg')

    eligible = storage.get_eligible_memories(project_path=proj)
    ids = {m['id'] for m in eligible}
    assert 'mem1' in ids
    assert 'mypkg' not in ids

    # Opt-in escape hatch for callers that genuinely need the union.
    union = storage.get_eligible_memories(project_path=proj,
                                          include_packages=True)
    assert {'mem1', 'mypkg'} <= {m['id'] for m in union}


@pytest.mark.unit
def test_search_memories_corpus_excludes_packages(isolated):
    from lib.memory.relevance import search_memories
    proj = _proj(isolated)
    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'mem1',
                body='uniquetokenflat in a memory')
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg',
               body='uniquetokenpkg in a skill')

    out = search_memories('uniquetokenpkg', project_path=proj)
    assert 'No memories matched' in out          # the skill is NOT findable
    out2 = search_memories('uniquetokenflat', project_path=proj)
    assert 'mem1' in out2                        # the memory still is


@pytest.mark.unit
def test_memory_count_hint_ignores_packages(isolated):
    from lib.memory.injection import build_memory_context
    proj = _proj(isolated)
    # Only a skill package installed → memory hint is absent (None), as if
    # no memories existed at all.
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    assert build_memory_context(project_path=proj) is None

    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'mem1')
    assert build_memory_context(project_path=proj) is not None


# ── model-side CRUD guards ───────────────────────────────────────────

@pytest.mark.unit
def test_update_memory_refuses_packages(isolated):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    with pytest.raises(ValueError, match='skill package'):
        storage.update_memory('mypkg', {'body': 'rewritten'},
                              project_path=proj)
    # The package is untouched.
    with open(os.path.join(proj, '.tofu', 'skills', 'mypkg', 'SKILL.md'),
              encoding='utf-8') as f:
        assert 'pkg guide' in f.read()


@pytest.mark.unit
def test_delete_memory_refuses_packages(isolated):
    proj = _proj(isolated)
    pkg_dir = _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    with pytest.raises(ValueError, match='skill package'):
        storage.delete_memory('mypkg', project_path=proj)
    assert os.path.isfile(os.path.join(pkg_dir, 'SKILL.md'))


@pytest.mark.unit
def test_merge_memories_refuses_package_sources(isolated):
    proj = _proj(isolated)
    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'mem1')
    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'mem2')
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    with pytest.raises(ValueError, match='skill package'):
        storage.merge_memories(['mem1', 'mypkg'], name='x', description='x',
                               body='x', project_path=proj)
    # Nothing was created or deleted: the guard fires before the merge.
    assert storage.get_memory('mem1', project_path=proj) is not None
    assert storage.get_memory('mypkg', project_path=proj) is not None


# ── Settings-critical paths keep working ─────────────────────────────

@pytest.mark.unit
def test_union_listing_and_get_memory_still_cover_packages(isolated):
    proj = _proj(isolated)
    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'mem1')
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')

    ids = {m['id'] for m in storage.list_all_memories(project_path=proj)}
    assert {'mem1', 'mypkg'} <= ids
    pkg = storage.get_memory('mypkg', project_path=proj)
    assert pkg is not None and pkg['is_package']


@pytest.mark.unit
def test_toggle_memory_still_works_for_packages(isolated):
    """The Settings → Skills enable toggle calls toggle_memory on packages —
    it must NOT be caught by the model-CRUD guard."""
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')

    res = storage.toggle_memory('mypkg', enabled=False, project_path=proj)
    assert res['enabled'] is False
    assert storage.get_memory('mypkg', project_path=proj)['enabled'] is False

    # And flat memories still toggle too.
    _write_flat(os.path.join(proj, '.tofu', 'memories'), 'mem1')
    res2 = storage.toggle_memory('mem1', project_path=proj)
    assert res2['enabled'] is False


@pytest.mark.unit
def test_flat_memory_crud_unaffected(isolated):
    """Control: ordinary memories keep full CRUD semantics."""
    proj = _proj(isolated)
    mem = storage.create_memory(name='lesson', description='a lesson learned '
                                'here today', body='body', scope='project',
                                project_path=proj)
    assert os.path.join('.tofu', 'memories') in mem['filepath']

    updated = storage.update_memory(mem['id'], {'body': 'v2'},
                                    project_path=proj)
    assert updated['body'] == 'v2'

    assert storage.delete_memory(mem['id'], project_path=proj) is True
    assert storage.get_memory(mem['id'], project_path=proj) is None
