"""Isolated bounded scheduler shared by paper build service and HTTP tests."""
import pytest

from lib.paper.builds.budget import BuildBudget
from lib.paper.builds.cache import LocalBuildCache
from lib.paper.builds.runtime import PreviewBuildScheduler
from lib.production.runtime import ProductionRuntime


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    from tests.support.paper_storage import isolated_paper_storage
    budget = BuildBudget(1, 2, 1024 * 1024, 16)
    production = ProductionRuntime('paper-build-test', id_prefix='paper_test', max_tasks=32)
    result = PreviewBuildScheduler(LocalBuildCache(tmp_path / 'cache', budget),
                                   budget=budget, production=production)
    with isolated_paper_storage(tmp_path, monkeypatch):
        try:
            yield result
        finally:
            result.shutdown()
