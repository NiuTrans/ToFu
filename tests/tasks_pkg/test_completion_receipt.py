"""Continuation receipts stay consistent between the terminal event and task."""
from copy import deepcopy

import pytest

from tests.tasks_pkg.test_finalize_actual_fact_card import finalized_event as finalized_event

pytestmark = pytest.mark.unit


def test_checkpoint_and_current_usage_merge_once_without_rewriting_inputs(finalized_event):
    checkpoint = {'prompt_tokens': 100, 'completion_tokens': 20,
                  'cost_usd': 1.5, 'provider_note': 'old'}
    current = {'prompt_tokens': 10, 'completion_tokens': 4,
               'cost_usd': 0.5, 'provider_note': 'current'}
    prior_rounds = [{'round': 1, 'requestId': 'checkpoint-request'}]
    current_rounds = [{'round': 2, 'requestId': 'current-request'}]
    before = deepcopy((checkpoint, current, prior_rounds, current_rounds))
    task, event = finalized_event(
        fields={'_checkpointUsage': checkpoint, '_checkpointApiRounds': prior_rounds},
        usage=current, api_rounds=current_rounds,
    )
    assert event['usage']['prompt_tokens'] == 110
    assert event['usage']['completion_tokens'] == 24
    assert event['usage']['cost_usd'] == 2.0
    assert event['usage']['provider_note'] == 'current'
    assert task['usage'] == task['ownUsage'] == event['usage']
    assert task['_invocationCheckpointUsageMerged'] is True
    assert task['apiRounds'] == event['apiRounds'] == prior_rounds + current_rounds
    assert (checkpoint, current, prior_rounds, current_rounds) == before


def test_checkpoint_only_usage_stays_known_without_fabricated_current_round(finalized_event):
    checkpoint = {'prompt_tokens': 7, 'completion_tokens': 0, 'cost_usd': 0.0}
    task, event = finalized_event(fields={'_checkpointUsage': checkpoint})
    assert task['usage'] == task['ownUsage'] == event['usage'] == checkpoint
    assert 'apiRounds' not in event


def test_checkpoint_file_receipts_keep_root_identity_and_truncation(finalized_event):
    first = {'path': 'shared.py', 'rootPath': '/owned/first', 'action': 'added'}
    updated = {**first, 'action': 'modified'}
    other_root = {'path': 'shared.py', 'rootPath': '/owned/second', 'action': 'added'}
    inputs = [first, updated, other_root]
    before = deepcopy(inputs)
    task, event = finalized_event(fields={
        '_checkpointModifiedFileList': inputs, 'modifiedFilesTruncated': True,
    })
    assert event['modifiedFileList'] == [updated, other_root]
    assert task['modifiedFileList'] == event['modifiedFileList']
    assert task['modifiedFiles'] == event['modifiedFiles'] == 2
    assert event['modifiedFilesTruncated'] is True
    assert inputs == before
