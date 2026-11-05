"""Physical exit survives transport completion loss without replaying effects."""
import json
import os
import subprocess
import sys

import pytest

from tests.conversation import test_durable_attempt_execution as execution

claimed = execution.claimed

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture
def process_witness():
    environment = dict(os.environ)
    environment.pop('LD_LIBRARY_PATH', None)
    process = subprocess.Popen([sys.executable, '-u', '-c',
        'import json,signal; from lib.process_owner import process_owner; '
        'print(json.dumps(process_owner()),flush=True); signal.pause()'],
        env=environment, stdout=subprocess.PIPE, text=True)
    witness = json.loads(process.stdout.readline())
    try:
        yield process, witness
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def enter(claimed, witness):
    from lib import turn_lifecycle as turns
    context, pair, _, owner = claimed
    turns.mark_task_started(pair['attempt']['attemptId'], context.claim.task_id,
        user_id=owner, worker_claim=context.publication_claim(), worker_process=witness)


def test_dead_worker_preserves_output_and_unknown_accounting_without_replay(
        claimed, process_witness, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.durable_worker import WorkerDomainSettlementUnknown
    context, pair, factory, owner = claimed
    process, witness = process_witness
    enter(claimed, witness)
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *a, **kw: pytest.fail('unknown effects were replayed'))
    with pytest.raises(WorkerDomainSettlementUnknown):
        execute_claimed_attempt(context, context.claim.payload)
    # A real process incarnation, rather than a fabricated PID, is killed.
    process.kill()
    process.wait()
    outcome = execute_claimed_attempt(context, context.claim.payload)
    assert outcome.status == 'failed'
    attempt = turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)
    assert attempt['status'] == 'failed'
    turn = turns.get_turn(pair['turn']['conversationId'], pair['turn']['turnId'], user_id=owner)
    assert turn['projection']['content'] == pair['turn']['projection']['content']
    result = factory().query('task_results.replay_get', {
        'key': context.claim.task_id, 'user_id': owner, 'include_metadata': True})
    assert result['metadata']['usageComplete'] is False
    error = result['error']
    if isinstance(error, str):
        error = json.loads(error)
    assert error['retryPolicy']['automatic'] is False
    assert execute_claimed_attempt(context, context.claim.payload) == outcome


def test_foreign_process_evidence_cannot_discharge_execution(claimed, process_witness):
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.durable_worker import WorkerDomainSettlementUnknown
    process, witness = process_witness
    enter(claimed, {**witness, 'host': witness['host'] + '-foreign'})
    process.kill()
    process.wait()
    context = claimed[0]
    with pytest.raises(WorkerDomainSettlementUnknown):
        execute_claimed_attempt(context, context.claim.payload)


@pytest.mark.parametrize('invalid_confirmation', [False, True])
def test_terminal_output_is_not_proof_of_required_completion_consumers(
        claimed, process_witness, invalid_confirmation):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    context, pair, factory, owner = claimed
    process, witness = process_witness
    enter(claimed, witness)
    carrier = {'id': context.claim.task_id, 'convId': pair['turn']['conversationId'],
        '_attemptId': pair['attempt']['attemptId'], '_turnId': pair['turn']['turnId'],
        '_userId': owner, '_conversationWorkerContext': context,
        'config': {}, 'status': 'done', 'finishReason': 'stop', 'content': 'final output'}
    turns.record_task_event(carrier, {'type': 'done', 'finishReason': 'stop'})
    metadata = {'attemptId': pair['attempt']['attemptId'], 'turnId': pair['turn']['turnId'],
                'usageComplete': True, 'usage': {'input_tokens': 17}}
    if invalid_confirmation:
        import hashlib
        from lib.storage.generated_worker_job_contract import WORKER_EXECUTION_EXIT_KEY
        from lib.storage.worker_execution import execution_witness
        encoded = json.dumps(execution_witness(witness, context.publication_claim()),
                             sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        metadata[WORKER_EXECUTION_EXIT_KEY] = {
            'format': 'tofu.conversation-worker-exit/v1',
            'entryDigest': hashlib.sha256(encoded).hexdigest(),
            'proof': 'process_dead', 'settlement': 'confirmed'}
    factory(write=True).command('task_results.checkpoint', {
        'key': context.claim.task_id, 'expected_version': 0,
        'worker_claim': context.publication_claim(), 'value': {
            'task_id': context.claim.task_id, 'conv_id': pair['turn']['conversationId'],
            'user_id': owner, 'status': 'done', 'content': 'final output',
            'metadata': json.dumps(metadata)}}, 'seed-terminal-before-exit')
    process.kill()
    process.wait()
    outcome = execute_claimed_attempt(context, context.claim.payload)
    assert outcome.status == 'failed'
    assert outcome.error['code'] == 'worker_effect_outcome_unknown'
    result = factory().query('task_results.replay_get', {
        'key': context.claim.task_id, 'user_id': owner, 'include_metadata': True,
        'include_terminal_payload': True})
    assert result['content'] == 'final output'
    assert result['metadata']['usageComplete'] is True
    assert result['metadata']['usage'] == {'input_tokens': 17}


def test_recovery_after_terminal_commit_loss_preserves_original_metadata(
        claimed, process_witness, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.storage.errors import StorageError
    context, pair, factory, owner = claimed
    process, witness = process_witness
    enter(claimed, witness)
    metadata = {'attemptId': pair['attempt']['attemptId'], 'turnId': pair['turn']['turnId'],
                'usage': {'input_tokens': 17}, 'receiptOwnedByDomain': 'retain-exactly'}
    factory(write=True).command('task_results.checkpoint', {
        'key': context.claim.task_id, 'expected_version': 0,
        'worker_claim': context.publication_claim(), 'value': {
            'task_id': context.claim.task_id, 'conv_id': pair['turn']['conversationId'],
            'user_id': owner, 'status': 'running', 'content': 'saved',
            'metadata': json.dumps(metadata)}}, 'seed-recovery-row')
    process.kill()
    process.wait()
    original = turns.record_task_event
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise StorageError('database_unavailable', 'terminal acknowledgement lost')
    with monkeypatch.context() as fault:
        fault.setattr(turns, 'record_task_event', lost_ack)
        with pytest.raises(StorageError):
            execute_claimed_attempt(context, context.claim.payload)
    assert execute_claimed_attempt(context, context.claim.payload).status == 'failed'
    result = factory().query('task_results.replay_get', {
        'key': context.claim.task_id, 'user_id': owner, 'include_metadata': True})
    assert result['metadata']['usage'] == {'input_tokens': 17}
    assert result['metadata']['receiptOwnedByDomain'] == 'retain-exactly'
    assert result['metadata']['usageComplete'] is False


def test_failure_port_confirms_unknown_domain_receipt_before_transport_completion(claimed, monkeypatch):
    import time
    from lib.conversation_sync.worker_execution import execute_claimed_attempt, settle_claimed_failure
    from lib.durable_worker import StorageWorkerJobStore
    context, _, factory, owner = claimed
    def failed_preparer(*args, **kwargs):
        raise RuntimeError('preparer returned no external receipt')
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor', failed_preparer)
    with pytest.raises(RuntimeError) as failure:
        execute_claimed_attempt(context, context.claim.payload)
    outcome = settle_claimed_failure(context, context.claim.payload, failure.value)
    assert outcome.status == 'failed'
    assert outcome.error['code'] == 'worker_effect_outcome_unknown'
    completion = StorageWorkerJobStore(client_factory=factory).complete(
        context.claim, outcome=outcome, now_ms=time.time_ns() // 1_000_000)
    assert completion['ok'] is True
    result = factory().query('task_results.replay_get', {
        'key': context.claim.task_id, 'user_id': owner, 'include_metadata': True})
    assert result['metadata']['usageComplete'] is False
