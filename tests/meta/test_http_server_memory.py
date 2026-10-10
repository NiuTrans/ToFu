"""Owned memory observer joins and preserves workload failures."""
import os
import pytest
from benchmarks.http_domain_measure import ServerMemory

pytestmark=pytest.mark.unit


def test_memory_observer_failure_keeps_original_failure():
    observer=ServerMemory(os.getpid());primary=ValueError('workload error')
    observer.error=RuntimeError('observer error');observer.close(primary)
    assert 'observer error' in primary.__notes__[0]
    assert not observer.thread.is_alive()


def test_memory_observer_raises_its_own_failure_after_join():
    observer=ServerMemory(os.getpid());observer.error=RuntimeError('observer error')
    with pytest.raises(RuntimeError,match='observer error'):observer.close()
    assert not observer.thread.is_alive()
