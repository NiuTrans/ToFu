"""Consumption links reserved recipients without inventing execution admission."""
from copy import deepcopy
import pytest
from lib.goal_runs.prepared_recovery import join_consumed_preparations
from lib.orchestration.execution_journal import JournalRecovery, RecoveryRefused
from tests.orchestration.test_prepared_invocation_binding import preparation

pytestmark = pytest.mark.unit


def fixture():
    _, prepared, _ = preparation()
    row = dict(source_task_id='old-actor', target_task_id=prepared['invocation_id'],
        target_registration_version=1, preparation=deepcopy(prepared),
        effect_origin=dict(task_id='original-actor', parent_task_id='original-root'))
    receipt = dict(type='execution_recovery_consumed', source_run_id='goal_old-root',
        source_cursor=9, request_sha256='c'*64, actors=[row])
    journal = JournalRecovery({}, {}, prepared_agents={prepared['key']: prepared})
    return prepared, receipt, journal


@pytest.mark.parametrize('started', [False, True])
def test_join_keeps_preparation_or_real_admission_and_original_effect_origin(started):
    prepared, receipt, journal = fixture()
    events = [dict(prepared, seq=5), dict(receipt, seq=8)]
    if started:
        admission = dict(prepared, type='execution_effect_started')
        events.append(dict(admission, seq=9))
        journal = JournalRecovery({}, {prepared['key']: admission})
    observed, witnesses = join_consumed_preparations(events, journal)
    assert observed.pending_agents[prepared['key']]['type'] == ('execution_effect_started' if started else 'execution_effect_prepared')
    assert witnesses[prepared['invocation_id']]['effect_origin'] == receipt['actors'][0]['effect_origin']
    with pytest.raises(RecoveryRefused): observed.settled()
    # A preparation without the native consumption candidate stays unadmitted.
    empty, witnesses = join_consumed_preparations([prepared], JournalRecovery({}, {}, prepared_agents={prepared['key']: prepared}))
    assert not empty.pending_agents and not witnesses


@pytest.mark.parametrize('damage', ['nonce', 'hash', 'key', 'actor', 'partial', 'duplicate', 'before', 'started', 'origin', 'origin_without_preparation', 'extra'])
def test_changed_or_partial_preparation_evidence_never_becomes_pending(damage):
    prepared, receipt, journal = fixture(); row = receipt['actors'][0]
    events = [prepared, receipt]
    if damage == 'nonce': row['preparation']['invocation_completion']['nonce'] = 'f'*32
    elif damage in {'hash', 'key'}: row['preparation']['input_hash' if damage == 'hash' else 'key'] = 'd'*64
    elif damage == 'actor': row['target_task_id'] = 'another'
    elif damage == 'partial': receipt['actors'].append(dict(source_task_id='other', target_task_id='other-target', target_registration_version=1))
    elif damage == 'duplicate': receipt['actors'].append(deepcopy(row))
    elif damage == 'before': events.reverse()
    elif damage == 'started': events.insert(1, dict(prepared, type='execution_effect_started'))
    elif damage == 'origin': row['effect_origin'].pop('parent_task_id')
    elif damage == 'origin_without_preparation': row.pop('preparation')
    else: row['extra'] = True
    with pytest.raises(RecoveryRefused): join_consumed_preparations(events, journal)
