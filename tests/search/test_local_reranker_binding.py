"""Local model objects remain request-owned across the existing search bridge."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib.search.search_bridge import bind_search_reranking
from tofu_search.config import get_config
from tofu_search.providers import submit_with_provider_context

pytestmark = pytest.mark.unit


class Scorer:
    def score(self, *_args, **_kwargs):
        return []


def test_local_ranking_binding_reaches_workers_and_restores_on_failure():
    previous = get_config()
    scorer, stop = Scorer(), threading.Event()
    with ThreadPoolExecutor(1) as pool:
        with pytest.raises(RuntimeError, match='caller failed'):
            with bind_search_reranking(scorer, cancel_event=stop):
                borrowed = submit_with_provider_context(pool, get_config).result(timeout=2)
                assert borrowed.local_reranker is scorer
                assert borrowed.local_rerank_cancel_event is stop
                assert get_config().local_reranker is scorer
                raise RuntimeError('caller failed')
    assert get_config() is previous


def test_overlapping_requests_do_not_share_model_or_stop_authority():
    barrier = threading.Barrier(2)
    scorers = [Scorer(), Scorer()]
    def run(scorer):
        with bind_search_reranking(scorer):
            barrier.wait(timeout=2)
            return get_config().local_reranker
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, scorer) for scorer in scorers]
        assert [future.result(timeout=3) for future in futures] == scorers


def test_old_package_requires_explicit_upgrade_only_for_this_optional_port(monkeypatch):
    import tofu_search.config
    monkeypatch.setattr(tofu_search.config, 'get_config', lambda: object())
    with pytest.raises(RuntimeError, match='>=0.12.23'):
        with bind_search_reranking(Scorer()):
            pass
