"""Stored usage projection retains sibling evidence and component prices."""

from copy import deepcopy

import pytest

from lib.paper.report_metadata import merge_report_second_pass, recompute_report_totals


pytestmark = pytest.mark.unit


def test_report_projection_sums_each_usage_kind_and_keeps_sibling_fields():
    metadata = {
        'promptTokens': 10, 'completionTokens': 4, 'cacheReadTokens': 3,
        'cacheWriteTokens': 2, 'reasoningTokens': 1, 'costCny': 3, 'costUsd': 0.5,
        'citationAudit': {'suspicious': ['synthetic']},
        'secondPasses': {'deepen': {'calls': 2, 'usage': {'prompt_tokens': 5}, 'costCny': 2}},
    }
    sibling = deepcopy(metadata['secondPasses']['deepen'])
    result = merge_report_second_pass(metadata, 'insight', {
        'fired': False, 'costCny': 1, 'costUsd': 0.25,
        'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'cache_read_tokens': 4,
                  'cache_write_tokens': 5, 'reasoning_tokens': 6},
    })
    assert result['totalUsage'] == {
        'prompt_tokens': 17, 'completion_tokens': 7, 'cache_read_tokens': 7,
        'cache_write_tokens': 7, 'reasoning_tokens': 7}
    assert result['totalCostCny'] == 6 and result['totalCostUsd'] == 0.75
    assert result['secondPasses']['deepen'] == sibling
    assert result['citationAudit'] == {'suspicious': ['synthetic']}


@pytest.mark.parametrize('malformed', [None, True, '12', 2.5, [], {}])
def test_invalid_usage_and_cost_records_cannot_change_valid_totals(malformed):
    metadata = {
        'promptTokens': malformed, 'completionTokens': 7, 'costCny': '12',
        'secondPasses': {
            'unstructured': malformed,
            'invalid-fields': {'usage': {'prompt_tokens': malformed}, 'costCny': True},
            'valid': {'usage': {'prompt_tokens': 3}, 'costCny': 2},
        },
    }
    recompute_report_totals(metadata)
    assert metadata['totalUsage']['prompt_tokens'] == 3
    assert metadata['totalUsage']['completion_tokens'] == 7
    assert metadata['totalCostCny'] == 2
    assert 'totalCostUsd' not in metadata


@pytest.mark.parametrize('passes', [None, 'broken', [], 2])
def test_replacement_normalizes_a_non_object_pass_container(passes):
    metadata = {'secondPasses': passes, 'model': 'body-model'}
    entry = {'usage': {'prompt_tokens': 3}, 'costCny': 2}
    merge_report_second_pass(metadata, 'insight', entry)
    assert metadata['secondPasses'] == {'insight': entry}
    assert metadata['model'] == 'body-model'
    assert metadata['totalUsage']['prompt_tokens'] == 3
    assert metadata['totalCostCny'] == 2


def test_replacement_removes_only_the_previous_same_pass_contribution():
    metadata = {'promptTokens': 10, 'costCny': 2}
    merge_report_second_pass(metadata, 'insight', {
        'usage': {'prompt_tokens': 7}, 'costCny': 3})
    merge_report_second_pass(metadata, 'insight', {
        'usage': {'prompt_tokens': 1}, 'costCny': 0.5})
    assert metadata['totalUsage']['prompt_tokens'] == 11
    assert metadata['totalCostCny'] == 2.5


def test_zero_replacement_updates_prior_totals_without_inventing_an_unpriced_currency():
    metadata = {'promptTokens': 10}
    merge_report_second_pass(metadata, 'insight', {
        'usage': {'prompt_tokens': 7}, 'costCny': 3})
    merge_report_second_pass(metadata, 'insight', {
        'usage': {'prompt_tokens': 1}, 'costCny': 0})
    assert metadata['totalUsage']['prompt_tokens'] == 11
    assert metadata['totalCostCny'] == 0
    assert 'totalCostUsd' not in metadata
