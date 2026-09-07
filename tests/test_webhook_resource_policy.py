"""Launch-derived webhook residency and outbound-attempt budgets."""

from __future__ import annotations

import pytest

from lib.webhook_policy import resolve_webhook_budget


pytestmark = pytest.mark.unit
_MIB = 1024 * 1024


def test_reference_budget_bounds_items_and_bytes_together():
    budget = resolve_webhook_budget({
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': '64',
    })

    assert budget.subscription_capacity == 64
    assert budget.owner_subscription_capacity == 64
    assert budget.queue_capacity == 128
    assert budget.retry_capacity == 64
    assert budget.queue_byte_capacity + budget.retry_byte_capacity == 16 * _MIB
    assert budget.event_max_bytes == 512 * 1_024
    assert budget.max_attempts == 5


def test_distributed_budget_remains_finite():
    budget = resolve_webhook_budget({
        'TOFU_DEPLOYMENT_MODE': 'distributed',
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': '2048',
    })

    assert budget.subscription_capacity == 2_048
    assert budget.owner_subscription_capacity == 256
    assert budget.queue_capacity == 2_048
    assert budget.retry_capacity == 1_024
    assert budget.queue_byte_capacity + budget.retry_byte_capacity == 256 * _MIB
    assert budget.event_max_bytes == _MIB


def test_operator_overrides_cannot_remove_hard_bounds():
    budget = resolve_webhook_budget({
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': '64',
        'TOFU_WEBHOOK_SUBSCRIPTION_CAPACITY': '999999',
        'TOFU_WEBHOOK_QUEUE_CAPACITY': '999999',
        'TOFU_WEBHOOK_BUFFER_MAX_MIB': '999999',
        'TOFU_WEBHOOK_EVENT_MAX_KIB': '999999',
        'TOFU_WEBHOOK_MAX_ATTEMPTS': '999999',
    })

    assert budget.subscription_capacity == 4_096
    assert budget.owner_subscription_capacity == 256
    assert budget.queue_capacity == 4_096
    assert budget.retry_capacity == 2_048
    assert budget.queue_byte_capacity + budget.retry_byte_capacity == 512 * _MIB
    assert budget.event_max_bytes == 4 * _MIB
    assert budget.max_attempts == 8


def test_event_override_is_clamped_to_half_the_total_buffer():
    budget = resolve_webhook_budget({
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': '64',
        'TOFU_WEBHOOK_BUFFER_MAX_MIB': '2',
        'TOFU_WEBHOOK_EVENT_MAX_KIB': '4096',
    })

    assert budget.event_max_bytes == _MIB
    assert budget.queue_byte_capacity == budget.retry_byte_capacity == _MIB
