"""Performance/correctness guards for Request Inspector task discovery."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_swarm_discovery_is_parent_bounded_and_index_shaped():
    """Never regress to the 12M-row ``LIKE '%#agent:%'`` table scan."""
    import lib.tasks_pkg.request_inspector as ri

    src = inspect.getsource(ri.list_conv_tasks)
    assert "LIKE '%#agent:%'" not in src
    assert "task_id >= ? AND task_id < ?" in src
    assert "type='messages_snapshot'" in src
    assert 'with pooled_db(DOMAIN_CHAT)' in src


def test_supported_prefix_range_contains_children_not_siblings():
    parent = 'task-abc'
    lower = f'{parent}#agent:'
    upper = f'{parent}#agent;'
    children = [lower + 'research-1', lower + 'critic', lower + '\U0001f680']
    assert all(lower <= child < upper for child in children)
    assert not (lower <= 'task-abd#agent:x' < upper)
    assert not (lower <= parent < upper)
