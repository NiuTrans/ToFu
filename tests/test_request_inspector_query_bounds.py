"""Performance/correctness guards for Request Inspector task discovery."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_swarm_discovery_uses_bounded_sidecar_summaries():
    """Keep discovery on bounded, storage-owned summary operations."""
    import lib.tasks_pkg.request_inspector as ri

    src = inspect.getsource(ri.list_conv_tasks)
    assert "LIKE '%#agent:%'" not in src
    assert "'turn.timing_trace.list'" in src
    assert "'task_results.summary_list'" in src
    assert "'scan_limit': 10_000" in src
    assert "'event.inspector_summary'" in src
    assert "'task_ids': sorted(parent_ids)" in src


def test_event_inspector_summary_uses_per_task_indexed_queries():
    """The retired mega-query (roots IN (...) OR'd with dozens of prefix
    ranges) defeated the SQLite planner on a 1M-row event log: it abandoned
    ``storage_events_task_idx`` and scanned the retention index — tens of
    seconds for milliseconds of key-range work. The operation must issue
    ONE equality probe plus ONE prefix-range probe per task, never an
    OR'd mega-predicate."""
    import lib.storage_sidecar.operations_pkg._records as rec

    calls = []

    class _FakeSession:
        backend = 'sqlite'

        def fetch_all(self, sql, params=()):
            calls.append(sql)
            return []

    result = rec._event_inspector_summary(
        _FakeSession(), {'task_ids': ['t1', 't2', 't3']})
    assert result == {'records': []}
    assert len(calls) == 6  # root probe + children probe, per task id
    for sql in calls:
        assert 'task_id IN' not in sql
        root_probe = 'task_id=?' in sql
        children_probe = 'task_id >= ?' in sql
        assert root_probe != children_probe
        if children_probe:
            # SQLite plans without parameter values: the parameterized
            # prefix range needs the hint or the planner scans the
            # retention index (measured 16 s for an empty range).
            assert 'INDEXED BY storage_events_task_idx' in sql


def test_summary_list_pushes_conv_id_prefilter_into_sql():
    """conv_id-scoped summary_list must not decode every megabyte
    task_results value to filter in Python (~6 s warm): a byte-level
    instr() pre-filter rides the page SQL. Unscoped scans keep the
    backend-neutral decode path untouched."""
    import lib.storage_sidecar.operations_pkg._records as rec

    seen = []

    class _FakeSession:
        def fetch_all(self, sql, params=()):
            seen.append((sql, params))
            return []

    rec._task_results_summary_list(_FakeSession(), {'conv_id': 'conv-x'})
    assert seen
    for sql, params in seen:
        assert 'instr(value_json, ?)' in sql
        assert params[0] == 'task_results'
        assert 'conv-x' in params

    seen.clear()
    rec._task_results_summary_list(_FakeSession(), {})
    assert seen
    assert all('instr(' not in sql for sql, _ in seen)


def test_supported_prefix_range_contains_children_not_siblings():
    parent = 'task-abc'
    lower = f'{parent}#agent:'
    upper = f'{parent}#agent;'
    children = [lower + 'research-1', lower + 'critic', lower + '\U0001f680']
    assert all(lower <= child < upper for child in children)
    assert not (lower <= 'task-abd#agent:x' < upper)
    assert not (lower <= parent < upper)
