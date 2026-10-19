"""Production native selection, independent weighted graph oracle and fallback."""

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = ('tests/test_fixture.py', 'tests/test_leaf.py')
from concurrent.futures import ThreadPoolExecutor
import sys
from types import SimpleNamespace

import pytest

from lib.repo_map import _native_rank, rank
from lib.repo_map.outline import RepoMap, _is_test_path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def reset_availability():
    loader = _native_rank.available_kernel
    loader.cache_clear()
    yield
    loader.cache_clear()


def repository():
    repo = RepoMap(root='fixture')
    repo.definitions = {'Engine': {'a.py', 'b.py'}, 'Helper': {'b.py', 'c.py'},
                        'Leaf': {'tests/test_leaf.py'}}
    repo.references = {'Engine': {'b.py', 'c.py', 'd.py'},
                       'Helper': {'a.py', 'd.py'}, 'Leaf': {'a.py', 'c.py'}}
    return repo


def oracle(repo, focus=None):
    import networkx as nx
    graph = nx.MultiDiGraph()
    for name, definers in repo.definitions.items():
        for definer in definers:
            for referencer in repo.references.get(name, set()) - definers:
                graph.add_edge('file:' + referencer, 'ident:' + name)
                graph.add_edge('ident:' + name, 'file:' + definer)
    if not graph: return {}
    personal = {'file:' + p: 50.0 for p in focus or () if 'file:' + p in graph} or None
    return {node[5:]: value * (.2 if _is_test_path(node[5:]) else 1.)
            for node, value in nx.pagerank(graph, personalization=personal).items()
            if node.startswith('file:')}


@pytest.mark.parametrize('failure', [ImportError, OSError, ValueError, KeyError, AttributeError])
def test_unavailable_or_invalid_native_component_preserves_canonical_scores(monkeypatch, failure):
    def unavailable(): raise failure('fixture unavailable')
    monkeypatch.setitem(sys.modules, 'tofu_repo_kernel', SimpleNamespace(ABI_VERSION=1, load_kernel=unavailable))
    repo = repository()
    assert rank.sumbd(repo, {'a.py'}) == pytest.approx(oracle(repo, {'a.py'}), rel=1e-10, abs=1e-12)


def test_native_iteration_failure_keeps_reference_count_fallback(monkeypatch):
    class FailedKernel:
        def solve(self, *args): raise RuntimeError('nonconvergence')
    monkeypatch.setattr(_native_rank, 'available_kernel', lambda: FailedKernel())
    repo = repository()
    expected = rank._fallback_scores(repo, set(repo.definitions), repo.references, repo.definitions)
    assert rank.sumbd(repo) == expected


def test_installed_kernel_is_shared_and_uses_production_selection():
    package = pytest.importorskip('tofu_repo_kernel')
    with ThreadPoolExecutor(max_workers=8) as pool:
        kernels = list(pool.map(lambda _: package.load_kernel(), range(32)))
    assert all(kernel is kernels[0] for kernel in kernels)
    assert _native_rank.available_kernel() is kernels[0]
    repo = repository()
    assert rank.sumbd(repo, {'a.py'}) == pytest.approx(oracle(repo, {'a.py'}), rel=1e-10, abs=1e-12)


def test_three_hundred_weighted_focus_graphs_through_production_selection():
    import random
    pytest.importorskip('tofu_repo_kernel')
    assert _native_rank.available_kernel() is not None, 'installed native kernel did not activate'
    rng = random.Random(20261008)
    for count in range(1, 101):
        repo = RepoMap(root='fixture')
        files = [f'source_{i}.py' for i in range(count)] + ['tests/test_fixture.py']
        for ident in range(20):
            name = f'symbol_{ident}'
            repo.definitions[name] = set(rng.sample(files, rng.randrange(1, min(8, len(files)) + 1)))
            repo.references[name] = set(rng.sample(files, rng.randrange(1, len(files) + 1)))
        for focus in (None, {files[0]}, set(files[:3])):
            assert rank.sumbd(repo, focus) == pytest.approx(oracle(repo, focus), rel=1e-10, abs=1e-12)


def test_native_buffers_snapshot_before_reentrant_numeric_conversion():
    package = pytest.importorskip('tofu_repo_kernel')
    kernel = package.load_kernel()
    personal = [1.0, 1.0]
    class MutatingWeight:
        def __float__(self):
            personal.clear()
            return 1.0
    expected = kernel.solve([0, 1, 1], [1], [1.0], [1.0, 1.0])
    assert kernel.solve([0, 1, 1], [1], [MutatingWeight()], personal) == expected
    assert personal == []
