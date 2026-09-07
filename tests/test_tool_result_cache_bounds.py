"""Per-task tool-result reuse stays finite without weakening live semantics."""

import pytest

from lib.tasks_pkg.tool_dispatch._flags import (
    _ensure_tool_call_id_receipts,
    _ensure_tool_result_cache,
    _resolve_tool_result_cache_capacity,
    _store_tool_call_id_receipt,
    _store_tool_result_cache_entry,
)


pytestmark = pytest.mark.unit


def _task(capacity: int = 16) -> dict:
    return {
        'id': 'bounded-dedup-task',
        '_tool_result_cache': {},
        '_tool_result_cache_capacity': capacity,
    }


def test_store_evicts_oldest_receipt_at_capacity_with_negative_control():
    task = _task()
    raw_unbounded_control = {}

    for index in range(17):
        key = f'read_files::{index}'
        entry = (f'result-{index}', False)
        _store_tool_result_cache_entry(task, key, entry)
        raw_unbounded_control[key] = entry

    assert len(raw_unbounded_control) == 17, \
        'negative control must reproduce plain-dict growth'
    assert list(task['_tool_result_cache']) == [
        f'read_files::{index}' for index in range(1, 17)
    ]
    assert task['_tool_result_cache_evictions'] == 1


def test_rewrite_refreshes_fifo_age_and_preserves_hot_receipt():
    task = _task()
    for index in range(16):
        _store_tool_result_cache_entry(
            task, f'grep_search::{index}', (f'raw-{index}', False))

    _store_tool_result_cache_entry(
        task, 'grep_search::0', ('budgeted-zero', False))
    _store_tool_result_cache_entry(
        task, 'grep_search::16', ('newest', False))

    cache = task['_tool_result_cache']
    assert 'grep_search::0' in cache
    assert cache['grep_search::0'][0] == 'budgeted-zero'
    assert 'grep_search::1' not in cache
    assert list(cache)[-2:] == ['grep_search::0', 'grep_search::16']


def test_ensure_trims_legacy_oversized_cache_and_repairs_invalid_shape():
    oversized = _task()
    oversized['_tool_result_cache'] = {
        f'find_files::{index}': (str(index), False) for index in range(20)
    }

    cache = _ensure_tool_result_cache(oversized)

    assert len(cache) == 16
    assert list(cache)[0] == 'find_files::4'
    assert oversized['_tool_result_cache_evictions'] == 4

    invalid = _task()
    invalid['_tool_result_cache'] = ['not', 'a', 'mapping']
    assert _ensure_tool_result_cache(invalid) == {}
    assert invalid['_tool_result_cache'] == {}


def test_capacity_resolver_uses_shared_policy_and_hard_ceiling(monkeypatch):
    observed = {}

    def fake_resolve(name, environment, *, minimum, maximum):
        observed.update(
            name=name, environment=environment,
            minimum=minimum, maximum=maximum)
        return maximum

    monkeypatch.setattr(
        'lib.tools.resource_policy.resolve_resource_budget', fake_resolve)
    environment = {'TOFU_TOOL_RESULT_CACHE_CAPACITY': '999999'}

    assert _resolve_tool_result_cache_capacity(environment) == 1024
    assert observed == {
        'name': 'TOFU_TOOL_RESULT_CACHE_CAPACITY',
        'environment': environment,
        'minimum': 16,
        'maximum': 1024,
    }


def test_call_id_receipts_are_content_free_bounded_and_shape_repaired():
    task = _task()
    task['_tool_call_id_receipts'] = {
        f'call-{index}': {
            'signature': index,
            'name': 'read_files',
            'status': '',
            'content': 'x' * 10_000,
            'unbounded_extra': {'raw': 'y' * 10_000},
        }
        for index in range(20)
    }

    receipts = _ensure_tool_call_id_receipts(task)

    assert len(receipts) == 16
    assert list(receipts)[0] == 'call-4'
    assert all(set(receipt) == {'signature', 'name', 'status'}
               for receipt in receipts.values())
    assert receipts['call-4'] == {
        'signature': '4', 'name': 'read_files', 'status': 'done'}

    _store_tool_call_id_receipt(task, 'call-20', {
        'signature': '20', 'name': 'grep_search', 'content': 'must-drop'})
    assert len(receipts) == 16
    assert 'call-4' not in receipts
    assert receipts['call-20'] == {
        'signature': '20', 'name': 'grep_search', 'status': 'done'}


def test_call_id_receipts_repair_invalid_ledger_and_rows():
    task = _task()
    task['_tool_call_id_receipts'] = ['not', 'a', 'mapping']
    assert _ensure_tool_call_id_receipts(task) == {}

    task['_tool_call_id_receipts'] = {
        'invalid': 'raw body',
        'valid': {'name': 'list_dir', 'content': 'raw body'},
    }
    assert _ensure_tool_call_id_receipts(task) == {
        'valid': {'signature': '', 'name': 'list_dir', 'status': 'done'},
    }
