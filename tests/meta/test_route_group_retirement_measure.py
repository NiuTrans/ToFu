"""The lifecycle measurement must reject lost work before reporting clocks."""
import pytest

from benchmarks.route_group_retirement import measure
from lib.llm_dispatch.slot_pool import SlotPool

pytestmark = pytest.mark.unit


def test_lifecycle_receipt_covers_every_slot_and_releases_pool():
    result = measure(SlotPool, groups=3, candidates=4, samples=2, allocations=True)
    assert result['slots'] == 13
    assert result['retired_slots'] == 12
    assert result['remaining_slots'] == 1
    assert len(result['fixture_sha256']) == 64
    for name, values in result['raw'].items():
        assert len(values) == 2
        assert 0 <= min(values) <= result['medians'][name] <= max(values)
    assert result['released_index_bytes'] < result['live_index_bytes']


def test_idle_index_capacity_does_not_grow_with_retired_requests():
    small = measure(SlotPool, groups=2, candidates=4, samples=1, allocations=True)
    large = measure(SlotPool, groups=128, candidates=4, samples=1, allocations=True)
    # Compare the same configured survivor after quiescence, not a clock or RSS.
    assert large['idle_index_bytes'] <= small['idle_index_bytes'] * 2


@pytest.mark.parametrize('failure', ['drop', 'retain', 'reorder'])
def test_incomplete_lifecycle_cannot_report_timings(failure):
    class BrokenPool(SlotPool):
        def publish(self, slots):
            return super().publish(list(reversed(slots)) if failure == 'reorder' else slots)

        def withdraw(self, slots):
            if failure == 'retain':
                return {id(slot) for slot in slots}
            return super().withdraw(slots[:-1] if failure == 'drop' else slots)

    with pytest.raises(AssertionError):
        measure(BrokenPool, groups=3, candidates=4, samples=1)
