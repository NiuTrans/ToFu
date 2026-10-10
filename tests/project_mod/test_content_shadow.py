"""Content shadow: local-disk content mirror for index-backed grep."""
import os
import time

import pytest

from lib.project_mod import content_shadow, tree_index
from lib.project_mod.read_tools import _run_index_chunks


def _fixture_project(root):
    (root / 'src' / 'pkg').mkdir(parents=True)
    (root / 'src' / 'pkg' / 'a.py').write_text(
        'def alpha():\n    return 1\n', encoding='utf-8')
    (root / 'src' / 'b.py').write_text(
        'def beta():\n    return 2\n', encoding='utf-8')
    (root / 'README.md').write_text('alpha docs\n', encoding='utf-8')


def _entry_for(root):
    paths, sizes, mtimes = [], [], []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            abs_p = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_p, root)
            st = os.stat(abs_p)
            paths.append(rel)
            sizes.append(st.st_size)
            mtimes.append(int(st.st_mtime))
    order = sorted(range(len(paths)), key=paths.__getitem__)
    paths = [paths[i] for i in order]
    sizes = [sizes[i] for i in order]
    mtimes = [mtimes[i] for i in order]
    from array import array
    return tree_index.TreeIndex(root, paths, array('q', sizes),
                                array('q', mtimes), time.time(), True)


@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    home = tmp_path / 'shadow-home'
    home.mkdir()
    monkeypatch.setenv('TOFU_CONTENT_SHADOW_DIR', str(home))
    monkeypatch.setattr(content_shadow, '_ENABLED', True)
    return home


@pytest.mark.unit
def test_sync_serves_and_rewrite_matches_live(shadow_env, tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    _fixture_project(project)
    root = str(project)
    entry = _entry_for(project)
    monkeypatch.setattr(tree_index, 'acquire', lambda r: entry if r == root else None)

    content_shadow._sync_worker(root, content_shadow._root_dir(root))

    cands = [os.path.join(root, rel) for rel in entry.paths]
    mapped = content_shadow.shadow_candidates(root, entry, cands)
    assert mapped is not None
    assert all(m.startswith(content_shadow._tree_dir(
        content_shadow._root_dir(root))) for m in mapped)

    deadline = time.monotonic() + 30
    live_raw, _, _, _live_withheld = _run_index_chunks(
        root, 'alpha', 0, 100, False, cands, deadline, 0)
    shadow_raw, _, _, _shadow_withheld = _run_index_chunks(
        root, 'alpha', 0, 100, False, mapped, deadline, 0)
    rewritten = content_shadow.rewrite_output_paths(shadow_raw, root)
    assert rewritten == live_raw


@pytest.mark.unit
def test_content_change_after_sync_falls_back(shadow_env, tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    _fixture_project(project)
    root = str(project)
    entry = _entry_for(project)
    monkeypatch.setattr(tree_index, 'acquire', lambda r: entry if r == root else None)
    content_shadow._sync_worker(root, content_shadow._root_dir(root))

    # Mutate content: the live index's columns move past the manifest.
    target = project / 'src' / 'b.py'
    target.write_text('def beta():\n    return 22\n', encoding='utf-8')
    stale = time.time() + 5
    os.utime(target, (stale, stale))
    entry2 = _entry_for(project)
    monkeypatch.setattr(tree_index, 'acquire', lambda r: entry2 if r == root else None)

    cands = [os.path.join(root, rel) for rel in entry2.paths]
    assert content_shadow.shadow_candidates(root, entry2, cands) is None

    # After a resync the shadow serves again.
    content_shadow._sync_worker(root, content_shadow._root_dir(root))
    assert content_shadow.shadow_candidates(root, entry2, cands) is not None


@pytest.mark.unit
def test_deleted_file_removed_from_shadow(shadow_env, tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    _fixture_project(project)
    root = str(project)
    entry = _entry_for(project)
    monkeypatch.setattr(tree_index, 'acquire', lambda r: entry if r == root else None)
    content_shadow._sync_worker(root, content_shadow._root_dir(root))

    (project / 'README.md').unlink()
    entry2 = _entry_for(project)
    monkeypatch.setattr(tree_index, 'acquire', lambda r: entry2 if r == root else None)
    content_shadow._sync_worker(root, content_shadow._root_dir(root))

    shadow_file = os.path.join(
        content_shadow._tree_dir(content_shadow._root_dir(root)), 'README.md')
    assert not os.path.exists(shadow_file)
    cands = [os.path.join(root, rel) for rel in entry2.paths]
    assert content_shadow.shadow_candidates(root, entry2, cands) is not None


@pytest.mark.unit
def test_oversized_file_is_not_mirrored(shadow_env, tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    _fixture_project(project)
    big = project / 'big.txt'
    big.write_text('x' * 1024, encoding='utf-8')
    root = str(project)
    entry = _entry_for(project)
    monkeypatch.setattr(tree_index, 'acquire', lambda r: entry if r == root else None)
    monkeypatch.setattr(content_shadow, '_MAX_FILE_BYTES', 100)
    content_shadow._sync_worker(root, content_shadow._root_dir(root))
    shadow_file = os.path.join(
        content_shadow._tree_dir(content_shadow._root_dir(root)), 'big.txt')
    assert not os.path.exists(shadow_file)
    # ...so the shadow refuses to serve a candidate set containing it.
    cands = [os.path.join(root, rel) for rel in entry.paths]
    monkeypatch.setattr(content_shadow, '_MAX_FILE_BYTES', 8 * 1024 * 1024)
    assert content_shadow.shadow_candidates(root, entry, cands) is None


@pytest.mark.unit
def test_invalidate_drops_shadow(shadow_env, tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    project.mkdir()
    _fixture_project(project)
    root = str(project)
    entry = _entry_for(project)
    monkeypatch.setattr(tree_index, 'acquire', lambda r: entry if r == root else None)
    content_shadow._sync_worker(root, content_shadow._root_dir(root))
    assert os.path.isdir(content_shadow._root_dir(root))
    content_shadow.invalidate(root)
    assert not os.path.exists(content_shadow._root_dir(root))
