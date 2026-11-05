"""Elapsed search budgets survive wall-clock calibration in either direction."""
from types import SimpleNamespace

import pytest

from tofu_search import SearchConfig, perform_web_search
from tofu_search.search import orchestrator

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('wall_jump', [-3600, 3600])
@pytest.mark.parametrize('elapsed', [1, 6])
def test_pipeline_budget_uses_elapsed_time_after_engine_return(monkeypatch, wall_jump, elapsed):
    clock = {'elapsed': 0, 'wall_jump': 0}
    monkeypatch.setattr(orchestrator, 'time', SimpleNamespace(
        time=lambda: 1_000_000 + clock['elapsed'] + clock['wall_jump'],
        monotonic=lambda: 100 + clock['elapsed']))

    def engine(*args):
        clock.update(elapsed=elapsed, wall_jump=wall_jump)
        return [{'url': 'https://observatory.example/quasar',
                 'title': 'Quasar calibration sensor',
                 'snippet': 'Quasar calibration sensor operating instructions.',
                 'source': 'Bing'}]

    allowances = []

    def score(query, documents, **kwargs):
        allowances.append(kwargs['timeout'])
        return [0.5] * len(documents)

    monkeypatch.setattr(orchestrator, 'search_bing', engine)
    result = perform_web_search(
        'quasar calibration sensor', engines=['Bing'], fetch_pages=False,
        filter_pages=False, deepen=False,
        config=SearchConfig(search_deadline_secs=5, local_rerank_timeout=10,
                            local_reranker=SimpleNamespace(score=score)))
    assert result[0]['url'] == 'https://observatory.example/quasar'
    assert result._deadline_hit is (elapsed >= 5)
    if elapsed < 5:
        assert allowances == [pytest.approx(5 - elapsed)]
        assert result._local_rerank['status'] == 'applied'
    else:
        assert not allowances
        assert result._local_rerank['status'] == 'deadline_skipped'
