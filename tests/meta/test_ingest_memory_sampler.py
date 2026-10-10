"""Memory measurement teardown keeps workload errors and joins its observer."""
import pytest
from benchmarks.knowledge_html.domain_measure import MemorySampler

pytestmark=pytest.mark.unit


def test_sampler_failure_preserves_workload_error():
    sampler=MemorySampler();primary=ValueError('workload failed')
    with pytest.raises(ValueError) as caught:
        with sampler:
            sampler.error=RuntimeError('observer failed')
            raise primary
    assert caught.value is primary
    assert any('observer failed' in note for note in primary.__notes__)
    assert not sampler.thread.is_alive()


def test_sampler_failure_raises_when_workload_succeeds():
    sampler=MemorySampler()
    with pytest.raises(RuntimeError,match='observer failed'):
        with sampler:sampler.error=RuntimeError('observer failed')
    assert not sampler.thread.is_alive()
