"""Pending observations preserve admission; only completed results grant replay."""
from copy import deepcopy

import pytest

from lib.orchestration.execution_journal import (
    ExecutionJournal, RecoveryRefused, encode_agent_result, inspect_journal, read_journal,
)
from lib.orchestration.invocation_recovery import completion_resolver
from tests.orchestration.test_invocation_frontier import fixture

pytestmark = pytest.mark.unit


def history():
    storage, admission, record, response, calls, options = fixture()
    events = []; definition = {'name': 'pending evidence'}
    journal = ExecutionJournal(events.append)
    journal.begin(definition, 'context')
    journal.call('agent', 'finished', 0, ['finished'],
        lambda: encode_agent_result({'output': 'kept completed result'}))
    admission.update(type='execution_effect_started', key='root:agent:pending:0:0')
    events.append(admission)
    return storage, admission, record, events, definition


def test_inspection_retains_original_pending_identity_without_inventing_a_result():
    storage, admission, record, events, definition = history()
    before = deepcopy(record)
    resolve = completion_resolver(7, conversation_id='conversation',
        parent_task_id='flow-root', allow_missing=True, storage=storage)
    evidence = inspect_journal(events, definition=definition,
        initial_context='context', reconcile_agent=resolve)
    assert len(evidence.records) == 1 and len(evidence.pending_agents) == 1
    assert next(iter(evidence.records.values()))['result']['output'] == 'kept completed result'
    assert evidence.pending_agents[admission['key']] == admission
    assert 'result' not in evidence.pending_agents[admission['key']] and record == before
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        evidence.settled()
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        read_journal(events, definition=definition, initial_context='context', reconcile_agent=resolve)
    evidence.pending_agents[admission['key']]['input_hash'] = 'changed'
    assert admission['input_hash'] == 'a' * 64


@pytest.mark.parametrize('damage', ['null', 'empty', 'mismatch', 'birth', 'lookup', 'versionless'])
def test_missing_completion_does_not_hide_corruption_or_authority_failure(damage):
    storage, admission, record, events, definition = history()
    if damage == 'null': record['value']['flow_invocation_completion'] = None
    elif damage == 'empty': record['value']['flow_invocation_completion'] = {}
    elif damage == 'mismatch':
        record['value']['flow_invocation_completion'] = {'admission': {**admission, 'input_hash': 'other'}}
    elif damage == 'birth': record['value']['child_invocation'] = {**record['value']['child_invocation'], 'parent_task_id': 'other'}
    elif damage == 'versionless':
        # A present result without its accepted receipt is corrupt, not absent.
        record['value']['flow_invocation_completion'] = {'admission': admission}
    else:
        def failed(*args): raise OSError('authority unavailable')
        storage.query = failed
    resolve = completion_resolver(7, allow_missing=True, storage=storage)
    with pytest.raises(RecoveryRefused):
        inspect_journal(events, definition=definition, initial_context='context', reconcile_agent=resolve)


def test_unfinished_generation_cannot_disappear_under_a_later_continuation():
    _, _, _, events, definition = history()
    later = ExecutionJournal(events.append)
    later.begin(definition, 'context', continuation=True)
    with pytest.raises(RecoveryRefused, match='external effects are unknown'):
        inspect_journal(events, definition=definition, initial_context='context')


def test_legacy_unknown_admission_is_inspectable_but_not_a_registered_actor():
    _, admission, _, events, definition = history()
    admission.pop('invocation_completion'); admission.pop('invocation_id')
    evidence = inspect_journal(events, definition=definition, initial_context='context')
    assert evidence.pending_agents == {admission['key']: admission}
    with pytest.raises(RecoveryRefused): evidence.settled()
