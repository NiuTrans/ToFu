"""Real authorities retain restart election until a durable handoff or decision."""
import pytest
from tests.support.semantic_authorities import semantic_authority as semantic_authority
from tests.goals.test_goal_conversation_recovery import recovery as recovery
from lib.orchestration.events import CONTINUITY_RESTART_ERROR
from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs_detailed

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


def retire(client):
    return retire_interrupted_orchestration_runs_detailed(
        error={**CONTINUITY_RESTART_ERROR,
               'message': 'Run interrupted by a server restart before completion.'}, client=client)


def create(client, owner, run_id, tenant=''):
    assert client(write=True).command('orchestration.run.create', {
        'run_id': run_id, 'user_id': owner, 'tenant_id': tenant,
        'definition': {'nodes': [], 'edges': []}, 'input': 'original objective',
        'orch_id': 'chat-goal:' + run_id, 'created_by': 'chat_goal_mode',
    }, 'create-' + run_id)['created']


def append(client, candidate, event):
    payload = {key: candidate[key] for key in ('run_id', 'user_id', 'tenant_id')}
    page = client().query('orchestration.event.page', payload)
    assert page['caught_up']
    result = client(write=True).command('orchestration.event.append', {
        **payload, 'sequence': page['next_cursor'], 'event': event}, None)
    assert result['accepted']
    return {**payload, 'sequence': page['next_cursor'], 'event': event}


@pytest.mark.parametrize('closure', [
    'execution_recovery_claim', 'continuity_resumed', 'continuity_refused', 'continuity_exhausted',
])
def test_election_survives_lost_boot_slot_and_closes_atomically(semantic_authority, closure):
    client, owner, _ = semantic_authority
    create(client, owner, 'lost-handoff', 'tenant-a')
    first = retire(client)
    assert first.retired == 1
    candidate, = first.candidates
    second = retire(client)  # No in-memory candidates survive this boot generation.
    assert second.retired == 0
    assert second.candidates == first.candidates
    append(client, candidate, {'type': 'continuity_deferred', 'reason': 'boot_capacity'})
    assert retire(client).candidates == first.candidates
    receipt = append(client, candidate, {'type': closure, 'successor_run_id': 'successor'})
    assert retire(client).candidates == ()
    # Lost-ack replay of the same closure does not re-elect or mutate the old terminal run.
    assert client(write=True).command('orchestration.event.append', receipt, None)['accepted']
    assert retire(client).candidates == ()
    state = client().query('orchestration.run.get', {
        'run_id': 'lost-handoff', 'user_id': owner, 'tenant_id': 'tenant-a'})
    assert state['status'] == 'error'


def test_wrong_owner_receipt_cannot_close_another_tenant_election(semantic_authority):
    from lib.storage.errors import StorageError
    client, owner, _ = semantic_authority
    create(client, owner, 'isolated-election', 'tenant-a')
    candidate, = retire(client).candidates
    with pytest.raises(StorageError):
        client(write=True).command('orchestration.event.append', {
            'run_id': candidate['run_id'], 'user_id': owner, 'tenant_id': 'tenant-b',
            'sequence': 1, 'event': {'type': 'continuity_refused'}}, None)
    assert retire(client).candidates == (candidate,)
    # A conflicting event has no authority to close the pending election either.
    with pytest.raises(StorageError):
        client(write=True).command('orchestration.event.append', {
            **candidate, 'sequence': 0, 'event': {'type': 'continuity_refused'}}, None)
    assert retire(client).candidates == (candidate,)


def test_source_delete_reclaims_pending_election(semantic_authority):
    client, owner, _ = semantic_authority
    create(client, owner, 'deleted-election')
    candidate, = retire(client).candidates
    assert client(write=True).command('orchestration.run.delete', candidate, 'delete-election')['deleted']
    assert retire(client).candidates == ()


def test_dropped_retirement_result_still_rebinds_the_original_chat_goal(recovery):
    from lib.goal_runs.execution_journal import create_goal_execution_journal
    from lib.goal_runs.recovery import resume_chat_goal
    from lib.orchestration.continuity import run_boot_continuity_pass
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    case.retire()  # Simulate death before the candidate list reaches the post-ready pass.
    second = retire(case.client)
    candidates = second.candidates
    assert second.retired == 0
    summary = run_boot_continuity_pass(
        candidates=second.candidates, auto_created_by={'chat_goal_mode'}, runtime=object(),
        run_service_for_owner=lambda owner, tenant: case.runs,
        resume_for_created_by={'chat_goal_mode': lambda runs, source, **boundary:
            resume_chat_goal(runs, source, ports=case.ports, **boundary)})
    assert summary['resumed'] == 1
    assert len(case.started) == 1
    assert case.started[0]['config']['_goalObjective'] == 'full objective'
    assert retire(case.client).candidates == candidates


def test_candidate_survives_storage_process_death(semantic_authority):
    client, owner, engine = semantic_authority
    create(client, owner, 'process-crash')
    first = retire(client)
    if engine is not None:
        # Keep the disposable authority, kill the daemon, reopen that exact WAL.
        engine._process.kill()
        engine._process.wait(timeout=10)
        engine._close_connections()
        engine._log.close()
        engine._launch_serve()
    else:
        from lib.storage.service import storage_runtime
        runtime = storage_runtime()
        runtime.stop()
        runtime.start()
    after = retire(client)
    assert after.retired == 0
    assert after.candidates == first.candidates


def test_retryable_storage_failure_preserves_automatic_election(recovery):
    from lib.storage.errors import StorageError
    from lib.goal_runs.execution_journal import create_goal_execution_journal
    from lib.goal_runs.recovery import resume_chat_goal
    from lib.orchestration.continuity import run_boot_continuity_pass
    case = recovery
    create_goal_execution_journal(case.task, case.definition, case.launch, runs=case.runs)
    candidates = case.retire().candidates
    def unavailable(*args, **kwargs):
        try:
            raise StorageError('database_unavailable', 'temporary storage outage', retryable=True)
        except StorageError as exc:
            raise RuntimeError('repository read failed') from exc
    ports = dict(candidates=candidates, auto_created_by={'chat_goal_mode'}, runtime=object(),
                 run_service_for_owner=lambda owner, tenant: case.runs)
    summary = run_boot_continuity_pass(**ports, resume_for_created_by={'chat_goal_mode': unavailable})
    assert summary['deferred'] == 1 and summary['refused'] == 0
    assert retire(case.client).candidates == candidates
    assert not case.started
    summary = run_boot_continuity_pass(**ports, resume_for_created_by={
        'chat_goal_mode': lambda runs, source, **boundary:
            resume_chat_goal(runs, source, ports=case.ports, **boundary)})
    assert summary['resumed'] == 1
    assert retire(case.client).candidates == candidates


@pytest.mark.parametrize('old_action', ['receipt', 'delete'])
def test_old_source_audit_or_delete_cannot_release_successor_goal_claim(semantic_authority, old_action):
    from lib.goal_runs.repository import SidecarGoalRunRepository
    from lib.goal_runs.contract import goal_run_policy
    client, owner, _ = semantic_authority
    repo = SidecarGoalRunRepository(owner, client=client)
    def start(run):
        return repo.start(run, conversation_id='same-conversation', objective='same objective',
                          definition={'nodes': [], 'edges': []}, policy=goal_run_policy())
    start('source-goal')
    candidate, = retire(client).candidates
    start('successor-goal')
    if old_action == 'receipt':
        append(client, candidate, {'type': 'execution_recovery_claim', 'successor_run_id': 'successor-goal'})
    else:
        client(write=True).command('orchestration.run.delete', candidate, 'delete-old-source')
    next_goal = start('replacement-goal')
    assert next_goal['supersededRunIds'] == ['successor-goal']
    assert client().query('orchestration.run.get', {
        'run_id': 'successor-goal', 'user_id': owner})['status'] == 'aborted'
