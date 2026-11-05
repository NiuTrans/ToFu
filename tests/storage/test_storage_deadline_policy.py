"""The observed-authority-service-time budget that storage writers may ask for.

The native authority charges its FIFO queue wait against the caller's
deadline, so a writer's budget has to cover the authority's measured service
time. These tests pin that derivation: the estimate, its bounds, the pooled
fallback for a cold operation, and the decay back to a fast authority.
"""
from __future__ import annotations

import pytest

from lib.storage.deadline_policy import (
    StorageServiceObservations,
    observe_storage_service,
    reset_storage_service_observations,
    storage_write_deadline,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_observations():
    reset_storage_service_observations()
    yield
    reset_storage_service_observations()


def _observe(kind, operation, seconds, count):
    for _ in range(count):
        observe_storage_service(kind, operation, seconds)


def test_cold_operation_answers_the_floor():
    assert storage_write_deadline('task_results.checkpoint') == 1.0


def test_observed_service_time_raises_the_budget():
    _observe('command', 'task_results.checkpoint', 0.5, 16)
    assert storage_write_deadline('task_results.checkpoint') == 2.0


def test_a_fast_authority_keeps_the_budget_at_the_floor():
    _observe('command', 'task_results.checkpoint', 0.01, 16)
    assert storage_write_deadline('task_results.checkpoint') == 1.0


def test_the_ceiling_bounds_the_budget():
    _observe('command', 'task_results.checkpoint', 5.0, 16)
    assert storage_write_deadline(
        'task_results.checkpoint', ceiling_seconds=4.0) == 4.0


def test_the_window_ages_out_and_the_budget_returns_to_the_fast_authority():
    _observe('command', 'task_results.checkpoint', 2.0, 16)
    assert storage_write_deadline('task_results.checkpoint') == 8.0
    _observe('command', 'task_results.checkpoint', 0.02, 64)
    assert storage_write_deadline('task_results.checkpoint') == 1.0


def test_a_cold_operation_inherits_the_pooled_service_time_of_its_kind():
    _observe('command', 'task_results.checkpoint', 0.5, 16)
    assert storage_write_deadline('task_results.checkpoint') == 2.0
    assert storage_write_deadline('event.append_batch') == 2.0


def test_a_query_budget_does_not_answer_a_command():
    _observe('query', 'task_results.replay_get', 0.5, 16)
    assert storage_write_deadline('task_results.replay_get', kind='query') == 2.0
    assert storage_write_deadline('task_results.checkpoint') == 1.0


def test_an_operations_own_window_wins_over_the_pooled_window():
    _observe('command', 'event.append_batch', 0.5, 16)
    _observe('command', 'task_results.checkpoint', 0.01, 16)
    assert storage_write_deadline('task_results.checkpoint') == 1.0
    assert storage_write_deadline('event.append_batch') == 2.0


def test_the_operation_cardinality_is_bounded():
    observations = StorageServiceObservations(operation_limit=4)
    for _ in range(16):
        observations.observe('command', 'op-0', 10.0)
    for index in range(100):
        observations.observe('command', f'op-{index + 1}', 0.01)
    # The oldest operation aged out of the per-operation windows, so only the
    # pooled window of the same kind answers for it.
    assert observations.estimate('command', 'op-0') == 0.01


def test_refusals_are_not_service_time():
    observations = StorageServiceObservations()
    observations.observe('command', 'task_results.checkpoint', 0.0)
    observations.observe('command', 'task_results.checkpoint', -1.0)
    assert observations.estimate('command', 'task_results.checkpoint') is None
