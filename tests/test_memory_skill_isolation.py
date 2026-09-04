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

import inspect
import io
import os

import pytest

import lib.memory.storage as storage
import lib.memory.storage._dirs as dirs
import lib.memory.storage._files as memory_files


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


@pytest.mark.unit
def test_metadata_only_memory_read_stops_before_the_body(isolated, monkeypatch):
    proj = _proj(isolated)
    memory_dir = os.path.join(proj, '.tofu', 'memories')
    _write_flat(memory_dir, 'large', body='x' * 1_000_000)
    path = os.path.join(memory_dir, 'large.md')
    full = memory_files._memory_from_file(path)
    source = open(path, encoding='utf-8').read()

    class TrackedText(io.StringIO):
        def close(self):
            # Keep ``tell`` observable after the production context manager.
            pass

    tracked = TrackedText(source)

    def tracked_open(filepath, *args, **kwargs):
        assert filepath == path
        return tracked

    monkeypatch.setattr(memory_files, 'open', tracked_open, raising=False)
    summary = memory_files._memory_from_file(path, include_body=False)

    assert summary is not None and full is not None
    assert summary['body'] == ''
    assert {key: value for key, value in summary.items() if key != 'body'} == {
        key: value for key, value in full.items() if key != 'body'
    }
    assert tracked.tell() < 1_024


@pytest.mark.unit
def test_summary_route_requests_metadata_only_storage(monkeypatch):
    from quart import Quart

    from routes.api_v1.memory import list_memories_v1

    captured = {}

    def fake_list_memories(*, project_path, scope, include_body=None):
        captured.update(
            project_path=project_path,
            scope=scope,
            include_body=include_body,
        )
        return []

    monkeypatch.setattr(storage, 'list_memories', fake_list_memories)
    app = Quart('memory-summary-io')

    async def invoke():
        async with app.test_request_context('/api/v1/memory?scope=all&summary=1'):
            result = inspect.unwrap(list_memories_v1)()
            if inspect.isawaitable(result):
                await result

    import asyncio
    asyncio.run(invoke())
    assert captured['scope'] == 'all'
    assert captured['include_body'] is False


@pytest.mark.unit
def test_memory_metadata_cache_is_fresh_and_does_not_reread(isolated, monkeypatch):
    proj = _proj(isolated)
    memory_dir = os.path.join(proj, '.tofu', 'memories')
    _write_flat(memory_dir, 'cached', body='not part of metadata')
    path = os.path.join(memory_dir, 'cached.md')
    memory_files._metadata_cache.clear()
    original = memory_files._read_memory_source
    reads = []

    def counted_read(filepath, *, include_body=True):
        reads.append(filepath)
        return original(filepath, include_body=include_body)

    monkeypatch.setattr(memory_files, '_read_memory_source', counted_read)
    first = memory_files._memory_from_file(path, include_body=False)
    monkeypatch.setattr(
        memory_files._metadata_cache, '_clock_ns', lambda: 10**30)
    second = memory_files._memory_from_file(path, include_body=False)
    assert first == second
    assert reads == [path]

    with open(path, 'w', encoding='utf-8') as target:
        target.write(
            '---\nname: revised\n'
            'description: refreshed metadata after direct edit\n---\n'
            'not part of metadata\n'
        )
    third = memory_files._memory_from_file(path, include_body=False)
    assert third['name'] == 'revised'
    assert reads == [path, path]
    memory_files._metadata_cache.clear()


@pytest.mark.unit
def test_memory_metadata_cache_enforces_entry_and_byte_lru():
    from lib.memory.storage._metadata_cache import MemoryMetadataCache

    cache = MemoryMetadataCache(max_entries=2, max_bytes=4_096)
    fingerprint = (1, 2, 3, 4, 5)
    assert cache.store('/a', fingerprint, {'name': 'a'})
    assert cache.store('/b', fingerprint, {'name': 'b'})
    assert cache.lookup('/a', fingerprint) == (True, {'name': 'a'})
    assert cache.store('/c', fingerprint, {'name': 'c'})
    assert cache.lookup('/b', fingerprint) == (False, {})
    assert cache.lookup('/a', fingerprint)[0] is True
    assert cache.lookup('/c', fingerprint)[0] is True
    assert not cache.store('/oversized', fingerprint, {
        'description': 'x' * 4_096,
    })
    snapshot = cache.snapshot()
    assert snapshot['entries'] == 2
    assert snapshot['retainedBytes'] <= snapshot['maxBytes'] == 4_096
    assert snapshot['evictions'] == 1
    assert snapshot['oversized'] == 1


@pytest.mark.unit
def test_memory_metadata_cache_settles_identical_recent_fingerprint():
    from lib.memory.storage._metadata_cache import MemoryMetadataCache

    cache = MemoryMetadataCache(
        max_entries=2,
        max_bytes=4_096,
        fingerprint_settle_ns=2_100,
    )
    clock_ns = [10_000]
    cache._clock_ns = lambda: clock_ns[0]
    fingerprint = (1, 2, 3, 9_000, 9_000)
    assert cache.store('/recent', fingerprint, {'name': 'recent'})

    assert cache.lookup('/recent', fingerprint) == (False, {})
    assert cache.snapshot()['unstable'] == 1

    clock_ns[0] = 11_101
    assert cache.lookup('/recent', fingerprint) == (
        True, {'name': 'recent'})
    snapshot = cache.snapshot()
    assert snapshot['hits'] == 1
    assert snapshot['misses'] == 1


@pytest.mark.unit
def test_memory_metadata_cache_readonly_hit_is_recursively_immutable():
    from lib.memory.storage._metadata_cache import MemoryMetadataCache

    cache = MemoryMetadataCache(
        max_entries=2,
        max_bytes=4_096,
        fingerprint_settle_ns=0,
    )
    fingerprint = (1, 2, 3, 4, 5)
    metadata = {
        'name': 'immutable',
        'tags': ['one'],
        'metadata': {'openclaw': {'install': [{'kind': 'node'}]}},
    }
    assert cache.store('/immutable', fingerprint, metadata)

    cached, readonly = cache.lookup_readonly('/immutable', fingerprint)
    assert cached
    with pytest.raises(TypeError):
        readonly['name'] = 'changed'
    with pytest.raises(AttributeError):
        readonly['tags'].append('changed')
    with pytest.raises(TypeError):
        readonly['metadata']['openclaw']['install'][0]['kind'] = 'changed'

    cached, mutable = cache.lookup('/immutable', fingerprint)
    assert cached and mutable == metadata
    mutable['tags'].append('caller-owned')
    assert cache.lookup('/immutable', fingerprint)[1]['tags'] == ['one']

    cyclic = []
    cyclic.append(cyclic)
    assert not cache.store('/cyclic', fingerprint, {'cycle': cyclic})
    assert cache.snapshot()['unfreezable'] == 1


@pytest.mark.unit
def test_cached_package_metadata_does_not_alias_returned_record(
        tmp_path, monkeypatch):
    package_dir = tmp_path / 'nested-package'
    package_dir.mkdir()
    skill_path = package_dir / 'SKILL.md'
    skill_path.write_text(
        '---\n'
        'name: Nested package\n'
        'description: nested immutable metadata fixture\n'
        'tags: [one]\n'
        'metadata:\n'
        '  openclaw:\n'
        '    install:\n'
        '      - kind: node\n'
        '        package: stable-package\n'
        '---\n\nbody\n',
        encoding='utf-8',
    )
    memory_files._metadata_cache.clear()
    first = memory_files._memory_from_file(
        str(skill_path),
        package_dir=str(package_dir),
        memory_id_override='nested-package',
        include_body=False,
    )
    monkeypatch.setattr(
        memory_files._metadata_cache, '_clock_ns', lambda: 10**30)

    first['tags'].append('caller-owned')
    first['install_specs'][0]['kind'] = 'changed'
    second = memory_files._memory_from_file(
        str(skill_path),
        package_dir=str(package_dir),
        memory_id_override='nested-package',
        include_body=False,
    )

    assert second['tags'] == ['one']
    assert second['install_specs'] == [{
        'kind': 'node',
        'package': 'stable-package',
    }]
    memory_files._metadata_cache.clear()
