"""Kernel selection, independent weighted graph oracles and degradation policy."""

# Synthetic repository/package fixtures, never shipped-source anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = ('tests/test_fixture.py', 'tests/test_leaf.py')
from concurrent.futures import ThreadPoolExecutor
import logging
import random
import sys
from types import SimpleNamespace

import pytest

from lib.repo_map import _native_rank, _python_rank, rank
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


def weighted_focus_graphs(every=1):
    """300 (repository, focus) cases with multi-definer, dangling and test nodes.

    ``every`` keeps each n-th repository size; the random sequence is unchanged.
    """
    rng = random.Random(20261008)
    for count in range(1, 101):
        repo = RepoMap(root='fixture')
        files = [f'source_{i}.py' for i in range(count)] + ['tests/test_fixture.py']
        for ident in range(20):
            name = f'symbol_{ident}'
            repo.definitions[name] = set(rng.sample(files, rng.randrange(1, min(8, len(files)) + 1)))
            repo.references[name] = set(rng.sample(files, rng.randrange(1, len(files) + 1)))
        if count % every:
            continue
        for focus in (None, {files[0]}, set(files[:3])):
            yield repo, focus


def oracle(repo, focus=None, pagerank=None):
    """Independent multigraph assembly; NetworkX's pure-Python PageRank by default."""
    import networkx as nx
    from networkx.algorithms.link_analysis.pagerank_alg import _pagerank_python
    graph = nx.MultiDiGraph()
    for name, definers in repo.definitions.items():
        for definer in definers:
            for referencer in repo.references.get(name, set()) - definers:
                graph.add_edge('file:' + referencer, 'ident:' + name)
                graph.add_edge('ident:' + name, 'file:' + definer)
    if not graph: return {}
    personal = {'file:' + p: 50.0 for p in focus or () if 'file:' + p in graph} or None
    return {node[5:]: value * (.2 if _is_test_path(node[5:]) else 1.)
            for node, value in (pagerank or _pagerank_python)(graph, personalization=personal).items()
            if node.startswith('file:')}


@pytest.mark.parametrize('failure', [ImportError, OSError, ValueError, KeyError, AttributeError])
def test_unavailable_or_invalid_native_component_preserves_canonical_scores(monkeypatch, failure):
    def unavailable(): raise failure('fixture unavailable')
    monkeypatch.setitem(sys.modules, 'tofu_repo_kernel', SimpleNamespace(ABI_VERSION=1, load_kernel=unavailable))
    repo = repository()
    assert rank.sumbd(repo, {'a.py'}) == pytest.approx(oracle(repo, {'a.py'}), rel=1e-10, abs=1e-12)


@pytest.mark.parametrize('reference', ['networkx-python', 'networkx-scipy'])
def test_python_kernel_matches_independent_references(monkeypatch, reference):
    import networkx as nx
    pagerank = None
    if reference == 'networkx-scipy':
        # The canonical implementation both kernels follow; SciPy is optional.
        pytest.importorskip('scipy')
        pagerank = nx.pagerank
    monkeypatch.setattr(_native_rank, 'available_kernel', lambda: None)
    # Sixty cases spanning every size class keep NetworkX's slow multigraph
    # reference within a unit-test budget.
    for repo, focus in weighted_focus_graphs(every=5):
        assert rank.sumbd(repo, focus) == pytest.approx(oracle(repo, focus, pagerank), rel=1e-10, abs=1e-12)


def test_python_kernel_refuses_unconverged_scores():
    with pytest.raises(RuntimeError, match='converge'):
        _python_rank.solve([0, 1, 2, 2], [1, 2], [1, 1], [1.0, 1.0, 1.0], maximum_iterations=1)


def test_kernel_computation_failure_degrades_to_reference_counts_with_one_warning(monkeypatch, caplog):
    class FailedKernel:
        def solve(self, *args): raise RuntimeError('nonconvergence')
    monkeypatch.setattr(_native_rank, 'available_kernel', lambda: FailedKernel())
    monkeypatch.setattr(_native_rank, '_reference_count_fallback_logged', False)
    repo = repository()
    expected = rank._fallback_scores(repo, set(repo.definitions), repo.references, repo.definitions)
    with caplog.at_level(logging.DEBUG, logger=rank.logger.name):
        assert rank.sumbd(repo) == expected
        assert rank.sumbd(repo) == expected
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1 and 'nonconvergence' in warnings[0].getMessage()


def test_unexpected_kernel_errors_propagate_instead_of_degrading(monkeypatch):
    class BrokenKernel:
        def solve(self, *args): raise TypeError('caller contract violated')
    monkeypatch.setattr(_native_rank, 'available_kernel', lambda: BrokenKernel())
    with pytest.raises(TypeError, match='caller contract violated'):
        rank.sumbd(repository())


def test_installed_kernel_is_shared_and_uses_production_selection():
    package = pytest.importorskip('tofu_repo_kernel')
    with ThreadPoolExecutor(max_workers=8) as pool:
        kernels = list(pool.map(lambda _: package.load_kernel(), range(32)))
    assert all(kernel is kernels[0] for kernel in kernels)
    assert _native_rank.available_kernel() is kernels[0]
    repo = repository()
    assert rank.sumbd(repo, {'a.py'}) == pytest.approx(oracle(repo, {'a.py'}), rel=1e-10, abs=1e-12)


def test_three_hundred_weighted_focus_graphs_through_production_selection():
    pytest.importorskip('tofu_repo_kernel')
    assert _native_rank.available_kernel() is not None, 'installed native kernel did not activate'
    for repo, focus in weighted_focus_graphs():
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
