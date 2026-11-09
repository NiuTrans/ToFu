"""Immutable test indexes reuse identical sources across scopes and checkouts."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.project_mod.test_landing_verification import git, write, commit

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/one/test_a.py', 'tests/two/test_b.py', 'tests/two/test_copy.py',
    'lib/alpha.py', 'lib/gamma.py',
}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Verifier')
    git(root, 'config', 'user.email', 'verifier@example.invalid')
    write(root, 'scripts/test_select.py', (ROOT / 'scripts/test_select.py').read_text())
    write(root, 'scripts/test_execution.py', (ROOT / 'scripts/test_execution.py').read_text())
    write(root, 'tests/one/test_a.py', 'from lib.alpha import value\n')
    write(root, 'tests/two/test_b.py', 'from lib.beta import value\n')
    write(root, 'tests/two/test_copy.py', 'from lib.beta import value\n')
    commit(root, 'tests')
    return root


def selector(root):
    spec = importlib.util.spec_from_file_location('index_test_selector', root / 'scripts/test_select.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(root, cache, paths=None):
    from lib.project_mod.verification_index import build_index
    return build_index(root, git(root, 'rev-parse', 'HEAD').strip(), selector(root), cache, paths)


def test_scoped_cold_index_reads_only_requested_sources(project, tmp_path):
    result = build(project, tmp_path / 'index.json', ['tests/one'])
    assert set(result.index) == {'tests/one/test_a.py'}
    assert result.parsed == 1
    assert 'lib/alpha.py' in result.index['tests/one/test_a.py']['refs']


def test_identical_blobs_are_parsed_once_and_survive_a_new_checkout(project, tmp_path):
    cache = tmp_path / 'index.json'
    first = build(project, cache)
    assert first.parsed == 2
    other = tmp_path / 'other'
    git(project, 'worktree', 'add', '-q', '--detach', str(other))
    second = build(other, cache)
    assert second.index == first.index
    assert second.parsed == 0
    assert second.reused == 3
    expected = selector(project).build_index(str(project / 'tests'), None)
    assert {p: r['refs'] for p, r in first.index.items()} == {
        p: r['refs'] for p, r in expected.items()}


def test_each_snapshot_reuses_unchanged_sources_and_forgets_deleted_paths(project, tmp_path):
    cache = tmp_path / 'index.json'
    build(project, cache)
    write(project, 'tests/one/test_a.py', 'from lib.gamma import value\n')
    (project / 'tests/two/test_b.py').unlink()
    commit(project, 'edit and remove')
    result = build(project, cache)
    assert (result.parsed, result.reused) == (1, 1)
    assert set(result.index) == {'tests/one/test_a.py', 'tests/two/test_copy.py'}
    assert 'lib/gamma.py' in result.index['tests/one/test_a.py']['refs']
    assert 'lib/alpha.py' not in result.index['tests/one/test_a.py']['refs']
    assert 'tests/two/test_b.py' not in json.loads(cache.read_text())['entries']


def test_partial_index_preserves_other_current_entries(project, tmp_path):
    cache = tmp_path / 'index.json'
    build(project, cache)
    assert build(project, cache, ['tests/one']).parsed == 0
    assert build(project, cache, ['tests/two']).parsed == 0


def test_changed_extractor_and_corrupt_cache_recompute(project, tmp_path):
    cache = tmp_path / 'index.json'
    build(project, cache)
    with (project / 'scripts/test_select.py').open('a') as stream:
        stream.write('\n# a different extractor revision\n')
    commit(project, 'selector')
    assert build(project, cache).parsed == 2


def test_valid_json_with_corrupted_references_cannot_change_selection(project, tmp_path):
    cache = tmp_path / 'index.json'
    original = build(project, cache)
    saved = json.loads(cache.read_text())
    for entry in saved['entries'].values():
        entry['refs'] = []
    cache.write_text(json.dumps(saved))
    rebuilt = build(project, cache)
    assert rebuilt.index == original.index
    assert rebuilt.parsed == 2
    cache.write_text('{')
    assert build(project, cache).parsed == 2
    cache.write_text(json.dumps({'version': 1, 'entries': []}))
    assert build(project, cache).parsed == 2


@pytest.mark.parametrize('paths', [['tests/missing'], ['../outside'], ['tests/one', 'tests/missing']])
def test_missing_or_noncanonical_scope_is_never_a_partial_success(project, tmp_path, paths):
    with pytest.raises(ValueError, match='path|match'):
        build(project, tmp_path / 'index.json', paths)


def test_cache_publication_failure_does_not_lose_the_index(project, tmp_path):
    target = tmp_path / 'directory'
    target.mkdir()
    result = build(project, target)
    assert len(result.index) == 3
    assert result.cache_error


def test_measurement_rejects_a_faster_but_incomplete_index(project, monkeypatch):
    from lib.project_mod import verification_index
    from scripts.benchmark_verification import measure
    original = verification_index.build_index

    def lose_reference(*args, **kwargs):
        result = original(*args, **kwargs)
        result.index.pop(next(iter(result.index)))
        return result

    monkeypatch.setattr(verification_index, 'build_index', lose_reference)
    with pytest.raises(AssertionError, match='reference sets differ'):
        measure(project, repetitions=1)
