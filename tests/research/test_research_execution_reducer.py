"""Complete receipt streams preserve active/failed jobs without history retention."""
import pytest
from lib.research.execution_observation import ExecutionStateReducer, reduce_execution_state
from lib.research.execution_observation import receipt_execution_status
from lib.research.receipt_pages import empty_reference
from tests.research.test_research_receipt_pages import repository, fact
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def observation(job, state, *, provider='cluster', status='done'):
    row = fact(1)
    row['status'] = status
    row['execution_observation'] = {'state': state, 'job_id': job, 'provider_scope': 'provider:' + provider}
    return row


def states(rows):
    return ((row, receipt_execution_status(row, remote=True)) for row in rows)


def test_complete_paged_stream_retains_early_active_job_after_many_other_completions():
    repo = repository(MemoryRecords())
    rows = (observation('old', 'running') if n == 0 else observation(f'job-{n}', 'completed')
            for n in range(2048))
    reference = repo.prepare(empty_reference(), rows)
    with repo.open_receipts(reference) as receipts:
        assert reduce_execution_state(states(receipts)) == 'running'
    with repo.open_receipts(reference, [observation('old', 'completed')]) as receipts:
        assert reduce_execution_state(states(receipts)) == 'completed'


def test_failed_job_in_an_early_page_survives_unrelated_completion():
    repo = repository(MemoryRecords())
    reference = repo.prepare(empty_reference(), [observation('failed', 'failed')] +
                            [observation(f'job-{n}', 'completed') for n in range(100)])
    with repo.open_receipts(reference) as receipts:
        assert reduce_execution_state(states(receipts)) == 'failed'
    with repo.open_receipts(reference, [observation('failed', 'completed')]) as receipts:
        assert reduce_execution_state(states(receipts)) == 'completed'


def test_completed_jobs_are_reclaimed_instead_of_accumulating_with_history():
    reducer = ExecutionStateReducer()
    reducer.observe(observation('old', 'queued'), 'queued')
    for n in range(10000):
        reducer.observe(observation(f'job-{n}', 'running'), 'running')
        reducer.observe(observation(f'job-{n}', 'completed'), 'completed')
        assert reducer.retained_jobs == 1
    assert reducer.status == 'queued'
    reducer.observe(observation('old', 'completed'), 'completed')
    assert reducer.retained_jobs == 0 and reducer.status == 'completed'


def test_provider_mismatch_and_failed_transport_cannot_clear_active_job():
    reducer = ExecutionStateReducer()
    reducer.observe(observation('job', 'running'), 'running')
    reducer.observe(observation('job', 'completed', provider='another'), 'completed')
    reducer.observe(observation('job', 'completed', status='error'), 'failed')
    assert reducer.status == 'running' and reducer.retained_jobs == 1


@pytest.mark.parametrize('active', ['planned', 'queued', 'running'])
def test_active_jobs_take_precedence_until_their_terminal_observation(active):
    reducer = ExecutionStateReducer()
    reducer.observe(observation('failed', 'failed'), 'failed')
    reducer.observe(observation('active', active), active)
    assert reducer.status == active
    reducer.observe(observation('active', 'completed'), 'completed')
    assert reducer.status == 'failed'


def test_legacy_unidentified_receipts_keep_latest_state_without_inventing_job_identity():
    reducer = ExecutionStateReducer()
    assert reducer.status == 'planned'
    reducer.observe({'status': 'done'}, 'running')
    reducer.observe({'status': 'done'}, 'completed')
    assert reducer.status == 'completed' and reducer.retained_jobs == 0


def test_inconsistent_parsed_state_or_invalid_label_is_rejected():
    reducer = ExecutionStateReducer()
    with pytest.raises(ValueError, match='conflicts'): reducer.observe(observation('job', 'running'), 'completed')
    with pytest.raises(ValueError, match='Invalid'): reducer.observe(observation('job', 'running'), 'success')
    assert reducer.status == 'planned' and reducer.retained_jobs == 0


def test_workspace_execution_state_uses_published_history_after_restart(storage):
    from copy import deepcopy
    from lib.research.workspace import save_workspace, read_run_execution_state
    from tests.research.test_research_receipt_publication import initial, prepare, DIRECTION
    supervisor, _ = storage
    draft = initial()
    draft['runs'][0]['tool_receipts'] = [observation('early', 'running')] + [
        observation(f'other-{n}', 'completed') for n in range(39)]
    saved = save_workspace(DIRECTION, 'en', draft, expected_revision=0, user_id=1)
    saved = save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1,
                           receipt_publication=prepare(supervisor.client, saved))
    supervisor.stop(); supervisor.start()
    state = read_run_execution_state(DIRECTION, user_id=1, run_id='long-experiment')
    assert state == {'revision': 2, 'run_id': 'long-experiment', 'count': 40, 'execution_status': 'running'}
    candidate = deepcopy(saved); candidate['runs'][0]['tool_receipts'].append(observation('early', 'completed'))
    save_workspace(DIRECTION, 'en', candidate, expected_revision=2, user_id=1)
    state = read_run_execution_state(DIRECTION, user_id=1, run_id='long-experiment')
    assert state['execution_status'] == 'completed' and state['count'] == 41
