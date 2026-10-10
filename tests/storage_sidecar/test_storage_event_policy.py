"""Launch-derived durable event queue and frame budgets."""

from __future__ import annotations

import pytest

from lib.storage_event_policy import resolve_storage_event_budget


pytestmark = pytest.mark.unit
_MIB = 1024 * 1024


@pytest.mark.parametrize(
    ('writer_queue', 'expected_mib'),
    [
        ('4', 64),
        ('8', 64),
        ('16', 64),
        ('128', 512),
        ('1024', 512),
    ],
)
def test_event_budget_scales_from_writer_queue(
    writer_queue,
    expected_mib,
):
    budget = resolve_storage_event_budget({
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': writer_queue,
    })

    assert budget.queue_capacity == 0
    assert budget.queue_byte_capacity == expected_mib * _MIB
    assert budget.batch_max_events == 500
    assert budget.batch_max_bytes == 60 * _MIB
    assert budget.event_max_bytes < budget.batch_max_bytes


def test_event_count_override_is_retired_and_representation_frame_bounds_remain():
    budget = resolve_storage_event_budget({
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': '16',
        'TOFU_STORAGE_EVENT_QUEUE_CAPACITY': '999999',
        'TOFU_STORAGE_EVENT_QUEUE_MAX_MIB': '999999',
        'TOFU_STORAGE_EVENT_BATCH_MAX_MIB': '999999',
    })

    assert budget.queue_capacity == 0
    assert budget.queue_byte_capacity == 1_024 * _MIB
    assert budget.batch_max_bytes == 60 * _MIB


def test_operator_can_lower_batch_frame_independently_of_pending_residency():
    budget = resolve_storage_event_budget({
        'TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY': '16',
        'TOFU_STORAGE_EVENT_BATCH_MAX_MIB': '8',
    })

    assert budget.queue_byte_capacity == 64 * _MIB
    assert budget.batch_max_bytes == 8 * _MIB
    assert budget.event_max_bytes == 8 * _MIB - 64 * 1024
