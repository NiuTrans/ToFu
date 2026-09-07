"""Finite process budget for optional provider capability diagnostics."""

from __future__ import annotations

import pytest

from lib.provider_probe import build_probe_work
from lib.provider_probe_policy import (
    PROVIDER_PROBE_MAX_CELLS,
    resolve_provider_probe_budget,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ('tool_workers', 'expected'),
    [
        ('1', (1, 4, 1, 1)),
        ('4', (1, 8, 4, 4)),
        ('8', (2, 16, 4, 8)),
        ('999999', (2, 16, 4, 8)),
    ],
)
def test_provider_probe_budget_bounds_tasks_queue_and_total_calls(
    tool_workers,
    expected,
):
    budget = resolve_provider_probe_budget({
        'TOOL_MAX_PARALLEL_WORKERS': tool_workers,
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': '60',
    })

    assert (
        budget.task_workers,
        budget.queue_capacity,
        budget.cell_workers_per_task,
        budget.total_cell_workers,
    ) == expected
    assert (
        budget.task_workers * budget.cell_workers_per_task
        <= budget.total_cell_workers
    )
    assert budget.idle_seconds == 60


def test_provider_probe_budget_honors_explicit_resident_worker_override():
    budget = resolve_provider_probe_budget({
        'TOOL_MAX_PARALLEL_WORKERS': '4',
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS': '0',
    })

    assert budget.idle_seconds == 0


def test_full_current_scale_matrix_fits_bounded_diagnostic_envelope():
    """A normal large catalogue must not be rejected before probing starts.

    Regression: the Sankuai catalogue reached 1,182 routed cells (474 logical
    rows across two keys) and the legacy 400-cell request cap made every full
    matrix probe return HTTP 400 without spending a single probe call.
    """
    assert PROVIDER_PROBE_MAX_CELLS >= 1_182
    assert PROVIDER_PROBE_MAX_CELLS <= 4_096


def test_probe_work_cartesian_product_stops_at_rejection_sentinel():
    wire_ids = [f'model-{index}' for index in range(1_000)]
    work = build_probe_work(
        {'base_url': 'https://provider.invalid/v1', 'protocol': 'openai'},
        [{'model_id': 'logical', 'request_ids': wire_ids}],
        [f'sk-{index}' for index in range(100)],
        maximum_cells=PROVIDER_PROBE_MAX_CELLS + 1,
    )

    assert len(work) == PROVIDER_PROBE_MAX_CELLS + 1
    assert work[-1][0] == PROVIDER_PROBE_MAX_CELLS // len(wire_ids)
    assert work[-1][3] == (
        f'model-{PROVIDER_PROBE_MAX_CELLS % len(wire_ids)}')
    assert max(item[0] for item in work) < 100
