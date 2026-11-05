"""Podcast admission races and failure settlement at the shared runtime."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib.production.runtime import ProductionRuntime

pytestmark = pytest.mark.unit


@pytest.fixture
def admission(monkeypatch):
    from lib.paper import podcast_runtime
    from lib.paper.podcast_engine import worker
    from lib.paper.podcast_engine.service import start_podcast

    production = ProductionRuntime('podcast-admission-spec', id_prefix='podcast',
                                   ttl=3600, push_channel=None)
    monkeypatch.setattr(podcast_runtime, '_production', production)
    monkeypatch.setattr(podcast_runtime, '_podcast_runtime', production.runtime)
    monkeypatch.setattr(worker, 'has_report', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker, 'load_cached_podcast', lambda *_args, **_kwargs: None)
    submitted = []
    monkeypatch.setattr(production.runtime, 'spawn',
                        lambda task_id, *_args: submitted.append(task_id))

    def start(**overrides):
        return start_podcast('paper', **{
            'mode': 'short', 'lang': 'zh', 'voice': 'voice', 'model': 'model',
            'force': False, 'user_id': 11, **overrides,
        })

    yield start, production, submitted
    for task_id in production.runtime.task_ids():
        production.runtime.finish(task_id, error='test cleanup')


def test_concurrent_cache_misses_submit_once(admission, monkeypatch):
    from lib.paper.podcast_engine import worker

    start, production, submitted = admission
    barrier = threading.Barrier(4)

    def miss(*_args, **_kwargs):
        barrier.wait(timeout=5)
        return None

    monkeypatch.setattr(worker, 'load_cached_podcast', miss)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: start(), range(4)))
    assert len({result['task_id'] for result in results}) == 1
    assert sum(result.get('reused', False) for result in results) == 3
    assert submitted == [results[0]['task_id']]
    assert production.runtime.get_owned(submitted[0], user_id=11) is not None
    assert production.runtime.get_owned(submitted[0], user_id=12) is None


@pytest.mark.parametrize('change', [{'user_id': 12}, {'model': 'other'}, {'voice': 'other'}])
def test_different_identity_never_joins(admission, change):
    start, _, submitted = admission
    first = start()
    second = start(**change)
    assert first['task_id'] != second['task_id']
    assert len(submitted) == 2


def test_submission_failure_is_terminal_and_can_retry(admission, monkeypatch):
    start, production, _ = admission
    failure = RuntimeError('submit failed')

    def reject(*_args):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(production.runtime, 'spawn', reject)
        with pytest.raises(RuntimeError) as caught:
            start()
    assert caught.value is failure
    failed_id, = production.runtime.task_ids()
    failed = production.runtime.get(failed_id)
    assert failed['status'] == 'error'
    assert failed['events'][-1]['type'] == 'error'
    assert start()['task_id'] != failed_id
