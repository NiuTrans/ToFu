"""Incomplete text, callbacks or terminal evidence cannot qualify the clocks."""
import pytest

from benchmarks.sse_text_accumulation import CASES, measure
from lib.llm._sse_core import SSEAccumulator

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('case', CASES)
def test_actual_payload_measurement_preserves_complete_receipts(case):
    result = measure(SSEAccumulator, case=case, count=5, samples=2)
    assert len(result['receipt_sha256']) == len(result['fixture_sha256']) == 64
    assert len(result['raw']) == 2
    assert set(result['medians']) == {'wall_seconds', 'cpu_seconds'}


def test_allocation_samples_do_not_publish_instrumented_clocks():
    result = measure(SSEAccumulator, case='mixed', count=5, samples=1, allocations=True)
    assert set(result['medians']) == {'retained_bytes', 'peak_bytes'}
    assert result['medians']['peak_bytes'] >= result['medians']['retained_bytes'] > 0


def test_missing_callback_or_text_refuses_a_performance_result():
    class IncompleteAccumulator(SSEAccumulator):
        def _handle_delta(self, delta):
            return super()._handle_delta({key: value for key, value in delta.items() if key != 'content'})

    with pytest.raises(AssertionError, match='stream evidence'):
        measure(IncompleteAccumulator, case='mixed', count=5, samples=1)
