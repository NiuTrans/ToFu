"""Launch-derived Push connection and retained-byte budgets."""

from __future__ import annotations

import pytest

from lib.agent_core.push_policy import resolve_push_budget


pytestmark = pytest.mark.unit
_MIB = 1024 * 1024


def test_reference_push_budget_bounds_every_resident_multiplier():
    budget = resolve_push_budget(
        {
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "128",
            "TOFU_MAX_SSE_PER_PRINCIPAL": "12",
        }
    )

    assert budget.client_capacity == 64
    assert budget.owner_client_capacity == 12
    assert budget.event_queue_capacity == 1_000
    assert budget.event_queue_byte_capacity == 4 * _MIB
    assert budget.event_max_bytes == 2 * _MIB


def test_distributed_push_budget_is_larger_but_finite():
    budget = resolve_push_budget(
        {
            "TOFU_DEPLOYMENT_MODE": "distributed",
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "2048",
            "TOFU_MAX_SSE_PER_PRINCIPAL": "64",
        }
    )

    assert budget.client_capacity == 256
    assert budget.owner_client_capacity == 64
    assert budget.event_queue_capacity == 1_000
    assert budget.event_queue_byte_capacity == 16 * _MIB
    assert budget.event_max_bytes == 8 * _MIB


def test_push_overrides_cannot_remove_hard_bounds():
    budget = resolve_push_budget(
        {
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "64",
            "TOFU_MAX_SSE_PER_PRINCIPAL": "12",
            "TOFU_PUSH_CLIENT_CAPACITY": "999999",
            "TOFU_PUSH_OWNER_CLIENT_CAPACITY": "999999",
            "TOFU_PUSH_EVENT_QUEUE_CAPACITY": "999999",
            "TOFU_PUSH_EVENT_QUEUE_MAX_MIB": "999999",
            "TOFU_PUSH_EVENT_MAX_MIB": "999999",
        }
    )

    assert budget.client_capacity == 256
    assert budget.owner_client_capacity == 128
    assert budget.event_queue_capacity == 4_096
    assert budget.event_queue_byte_capacity == 16 * _MIB
    assert budget.event_max_bytes == 8 * _MIB


def test_single_frame_never_exceeds_its_queue():
    budget = resolve_push_budget(
        {
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "64",
            "TOFU_MAX_SSE_PER_PRINCIPAL": "12",
            "TOFU_PUSH_EVENT_QUEUE_MAX_MIB": "1",
            "TOFU_PUSH_EVENT_MAX_MIB": "8",
        }
    )

    assert budget.event_queue_byte_capacity == _MIB
    assert budget.event_max_bytes == _MIB
