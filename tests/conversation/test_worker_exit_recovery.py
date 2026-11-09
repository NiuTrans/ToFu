"""Physical exit survives transport completion loss without replaying effects."""
import json
import os
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager

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
    assert attempt['status'] == 'interrupted'
    turn = turns.get_turn(pair['turn']['conversationId'], pair['turn']['turnId'], user_id=owner)
    assert turn['projection']['content'] == pair['turn']['projection']['content']
    assert turn['settlement']['cause'] == 'server_restart'
    assert turn['settlement']['error']['code'] == 'worker_effect_outcome_unknown'
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


@pytest.mark.parametrize('cache_facts', [
    {'cache_prefix_hwm': 17},
    {'last_turn_cache_read': 0},
    {'cache_prefix_hwm': 17, 'last_turn_cache_read': 23},
])
def test_exit_recovery_does_not_republish_saved_cache_candidates(
        claimed, process_witness, cache_facts):
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.task_replay.task_result_checkpoint_contract import (
        TASK_RESULT_CACHE_SETTINGS_CONTRACT, TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
    )
    context, pair, factory, owner = claimed
    process, witness = process_witness
    enter(claimed, witness)
    metadata = {'attemptId': pair['attempt']['attemptId'], 'turnId': pair['turn']['turnId'],
                'usage': {'input_tokens': 31}, 'receiptOwnedByDomain': 'preserved'}
    factory(write=True).command('task_results.checkpoint', {
        'key': context.claim.task_id, 'expected_version': 0,
        'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, 'require_parent': True,
        'cache_settings_contract': TASK_RESULT_CACHE_SETTINGS_CONTRACT,
        'worker_claim': context.publication_claim(), 'value': {
            'task_id': context.claim.task_id, 'conv_id': pair['turn']['conversationId'],
            'user_id': owner, 'status': 'running', 'content': 'saved output',
            'metadata': json.dumps(metadata), **cache_facts}}, 'seed-cache-recovery')
    factory(write=True).command('conversation.settings.update', {
        'conv_id': pair['turn']['conversationId'], 'user_id': owner,
        'updates': {'cachePrefixHWM': 47, 'lastTurnCacheRead': 53}}, 'newer-cache-facts')
    process.kill()
    process.wait()
    outcome = execute_claimed_attempt(context, context.claim.payload)
    assert outcome.status == 'failed'
    assert execute_claimed_attempt(context, context.claim.payload) == outcome
    result = factory().query('task_results.replay_get', {
        'key': context.claim.task_id, 'user_id': owner, 'include_metadata': True,
        'include_terminal_payload': True})
    assert result['content'] == 'saved output'
    assert result['metadata']['usage'] == metadata['usage']
    assert result['metadata']['receiptOwnedByDomain'] == 'preserved'
    conversation = factory().query('conversation.get', {
        'conv_id': pair['turn']['conversationId'], 'user_id': owner, 'derive_messages': False})
    assert conversation['metadata']['settings']['cachePrefixHWM'] == 47
    assert conversation['metadata']['settings']['lastTurnCacheRead'] == 53


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


def _claim(factory, worker_id, *, lease_ms, shutdown_event=None):
    from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim
    from tests.support.worker_job_time import wall_ms
    document = factory(write=True).command('worker_job.claim_next', {
        'worker_id': worker_id, 'task_kinds': ['conversation-attempt'],
        'now_ms': wall_ms(), 'lease_ms': lease_ms}, uuid.uuid4().hex)
    return WorkerExecutionContext(WorkerJobClaim.from_document(
        document, expected_worker_id=worker_id), shutdown_event=shutdown_event)


def _short_lease_claim(claimed, *, shutdown_event=None):
    """Re-claim the unentered fixture job at the authority's shortest lease.

    Heartbeats never shorten an accepted deadline, so the claim that executes
    must already hold the shortest lease for a test to outlive it.
    """
    from lib.durable_worker import StorageWorkerJobStore, WorkerJobSuspension
    from tests.support.worker_job_time import wall_ms
    context, _pair, factory, _owner = claimed
    store = StorageWorkerJobStore(client_factory=factory)
    assert store.defer(context.claim, now_ms=wall_ms(),
        outcome=WorkerJobSuspension(available_at_ms=wall_ms()),
        observed_cancel_sequence=context.claim.cancel_sequence)['ok'] is True
    return store, _claim(factory, 'original', lease_ms=10_000, shutdown_event=shutdown_event)


def _forced_reclamation_keeps(task_id):
    """TTL/memory-pressure reclamation must keep unrecorded exit evidence."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    carrier = chat_task_runtime.get(task_id)
    chat_task_runtime.cleanup_stale(max_age=0)
    assert chat_task_runtime.get(task_id) is carrier
    return carrier


@contextmanager
def _renewing(store, context):
    """Renew the original claim while it executes, as the runner's monitor does.

    Its shortest lease may then end only at the explicit expiry wait, never
    because a loaded host made executor startup slow.
    """
    from tests.support.worker_job_time import wall_ms
    stop, failures = threading.Event(), []

    def renew():
        try:
            while not stop.wait(1.0):
                renewed = store.heartbeat(context.claim, now_ms=wall_ms(), lease_ms=10_000,
                                          replay_cursor=context.replay_cursor)
                assert renewed['ok'] is True, renewed
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=renew, name='test-claim-renewal', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
    if failures:
        raise failures[0]


def _superseded_carrier_retired(original):
    """The takeover fences the replaced claim and retires its carrier."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    assert original.authority_lost
    assert chat_task_runtime.get(original.claim.task_id) is None


def _take_over_after_expiry(store, factory, original):
    """The authority accepted one last renewal, then storage-owned time expired it."""
    from tests.support.worker_job_time import wall_ms, wait_until_ms
    renewed = store.heartbeat(original.claim, now_ms=wall_ms(), lease_ms=10_000,
                              replay_cursor=original.replay_cursor)
    assert renewed['ok'] is True
    wait_until_ms(renewed['job']['leaseDeadlineMs'] + 1)
    takeover = _claim(factory, 'takeover', lease_ms=300_000)
    assert takeover.claim.task_id == original.claim.task_id
    assert takeover.fencing_token > original.fencing_token
    return takeover


def _run_claimed(monkeypatch, run):
    from lib import turn_lifecycle as turns
    from lib.conversation_sync import task_start
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(turns, 'build_api_messages',
                        lambda *a, **kw: [{'role': 'user', 'content': 'accepted input'}])
    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', run)


def _recover(monkeypatch, takeover):
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *a, **kw: pytest.fail('lease takeover replayed entered effects'))
    outcome = execute_claimed_attempt(takeover, takeover.claim.payload)
    assert (outcome.status, outcome.error['code']) == ('failed', 'worker_effect_outcome_unknown')
    assert execute_claimed_attempt(takeover, takeover.claim.payload) == outcome
    return outcome


def _exit_metadata(factory, context, owner):
    return factory().query('task_results.replay_get', {
        'key': context.claim.task_id, 'user_id': owner, 'include_metadata': True})['metadata']


@pytest.mark.parametrize('stop', ['lease_lost', 'shutdown'])
def test_takeover_names_the_lost_lease_of_a_joined_worker(claimed, monkeypatch, stop):
    """A stalled authority expiring the lease is a storage fence, not a model failure.

    The worker stopped mid-tool, so effects stay unknown and nothing is replayed.
    A stopping service fails the same checkpoint closed; that is a restart
    interruption, not a lease loss. Either way the joined carrier survives
    reclamation until recovery records its exit.
    """
    from lib import turn_lifecycle as turns
    from lib.agent_core.execution_session import settle_task_execution
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.durable_worker import WorkerJobSuspension, WorkerLeaseLost
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.tasks_pkg.manager import stamp_chat_task_terminal
    from tests.support.worker_job_time import wall_ms
    _unentered, pair, factory, owner = claimed
    shutdown = threading.Event() if stop == 'shutdown' else None
    store, context = _short_lease_claim(claimed, shutdown_event=shutdown)

    def run(task):
        # The heartbeat could not renew the lease (or the service is stopping);
        # the owner control port fails closed and the post-loop delivery-error
        # branch stops without writing.
        if shutdown is None:
            context.mark_authority_lost()
        else:
            shutdown.set()
        assert invocation_abort_requested(task)
        assert isinstance(task['_invocationDeliveryError'], WorkerLeaseLost)
        stamp_chat_task_terminal(task, status='error', finish_reason='error')
        settle_task_execution(task, event={'type': 'done', 'error': task.get('error'),
                                           'finishReason': 'error'})

    _run_claimed(monkeypatch, run)
    with _renewing(store, context):
        with pytest.raises(WorkerLeaseLost):
            execute_claimed_attempt(context, context.claim.payload)
    _forced_reclamation_keeps(context.claim.task_id)
    if shutdown is None:
        takeover = _take_over_after_expiry(store, factory, context)
    else:
        # The runner marks the stopped claim and releases its job for takeover.
        context.mark_authority_lost()
        assert store.defer(context.claim, now_ms=wall_ms(),
            outcome=WorkerJobSuspension(available_at_ms=wall_ms()),
            observed_cancel_sequence=context.claim.cancel_sequence)['ok'] is True
        takeover = _claim(factory, 'takeover', lease_ms=300_000)
    _recover(monkeypatch, takeover)
    _superseded_carrier_retired(context)
    settlement = turns.get_turn(pair['turn']['conversationId'], pair['turn']['turnId'],
                                user_id=owner)['settlement']
    error = settlement['error']
    assert (error['code'], error['retryable']) == ('worker_effect_outcome_unknown', False)
    assert error['retryPolicy'] == {'mode': 'manual', 'automatic': False}
    if shutdown is None:
        assert (settlement['outcome'], settlement['cause'], settlement['evidence']) == (
            'failed', 'internal_fence_abort', 'external_authority')
        assert error['kind'] == 'storage_fence'
        assert 'lease' in error['message']
    else:
        assert (settlement['outcome'], settlement['cause'], settlement['evidence']) == (
            'interrupted', 'server_restart', 'system_recovery')
        assert error['kind'] != 'storage_fence'
        assert 'lease' not in error['message']
    metadata = _exit_metadata(factory, context, owner)
    assert metadata['usageComplete'] is False
    assert metadata['conversationWorkerExit']['settlement'] == 'unknown'
    assert metadata['conversationWorkerExit']['proof'] == 'joined'


def test_takeover_settles_the_result_a_lost_lease_left_unsaved(claimed, monkeypatch):
    """Debt retained under a replaced claim can never commit, so it cannot gate recovery.

    The worker finished, but its lease was lost before the terminal write. That
    debt used to fail every re-claim until a restart killed the carrier.
    """
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.durable_worker import WorkerLeaseLost
    from lib.tasks_pkg.manager import append_event, persist_task_result, stamp_chat_task_terminal
    _unentered, pair, factory, owner = claimed
    store, context = _short_lease_claim(claimed)

    def run(task):
        task['content'] = 'finished, never saved'
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        context.mark_authority_lost()
        # The terminal event is withheld, not raised: the carrier keeps it as debt.
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task) is False

    _run_claimed(monkeypatch, run)
    with _renewing(store, context):
        with pytest.raises(WorkerLeaseLost):
            execute_claimed_attempt(context, context.claim.payload)
    carrier = _forced_reclamation_keeps(context.claim.task_id)
    assert carrier['_terminalPersistencePending'] is True
    takeover = _take_over_after_expiry(store, factory, context)
    _recover(monkeypatch, takeover)
    _superseded_carrier_retired(context)
    settlement = turns.get_turn(pair['turn']['conversationId'], pair['turn']['turnId'],
                                user_id=owner)['settlement']
    assert (settlement['outcome'], settlement['cause'], settlement['evidence']) == (
        'failed', 'internal_fence_abort', 'external_authority')
    error = settlement['error']
    assert (error['kind'], error['code'], error['retryable']) == (
        'storage_fence', 'worker_effect_outcome_unknown', False)
    assert 'lease' in error['message']
    assert 'status=done' in error['detail']
    # The receipt settled the attempt: the retired carrier neither owes nor
    # retries debt, and the replaced claim's undeliverable events are released.
    assert not carrier.get('_terminalPersistencePending')
    assert not carrier.get('_terminalPersistenceRetryReady')
    assert carrier.get('_eventDeliveryRevoked') is True
    metadata = _exit_metadata(factory, context, owner)
    assert metadata['usageComplete'] is False
    assert metadata['conversationWorkerExit']['settlement'] == 'unknown'


def test_replaced_claim_records_unknown_exit_for_unacknowledged_terminal_debt(claimed, monkeypatch):
    """A committed terminal Turn whose required persistence never acknowledged.

    Under its own claim recovery keeps waiting for that debt. A newer claim
    cannot inherit it, so it records an unknown exit rather than confirming
    completion or waiting for a restart; the committed Turn stays as it was.
    """
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import execute_claimed_attempt
    from lib.durable_worker import WorkerDomainSettlementUnknown
    from lib.tasks_pkg.manager import append_event, stamp_chat_task_terminal
    _unentered, pair, factory, owner = claimed
    store, context = _short_lease_claim(claimed)

    def run(task):
        task['content'] = 'terminal Turn, result not acknowledged'
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        task['_terminalPersistencePending'] = True

    _run_claimed(monkeypatch, run)
    with _renewing(store, context):
        with pytest.raises(WorkerDomainSettlementUnknown, match='persistence'):
            execute_claimed_attempt(context, context.claim.payload)
    # Nothing local knows the claim will be replaced; only the takeover fences it.
    assert not context.authority_lost
    carrier = _forced_reclamation_keeps(context.claim.task_id)
    takeover = _take_over_after_expiry(store, factory, context)
    _recover(monkeypatch, takeover)
    _superseded_carrier_retired(context)
    turn = turns.get_turn(pair['turn']['conversationId'], pair['turn']['turnId'], user_id=owner)
    assert turn['status'] == 'completed'
    assert turn['projection']['content'] == 'terminal Turn, result not acknowledged'
    assert not carrier.get('_terminalPersistencePending')
    metadata = _exit_metadata(factory, context, owner)
    assert metadata['conversationWorkerExit']['settlement'] == 'unknown'
    assert metadata['conversationWorkerExit']['proof'] == 'joined'


class _RefusingExitReceipts:
    """Refuse every conversation exit receipt with the native engine's refusal."""

    def __init__(self, client, refusals):
        self._client, self._refusals = client, refusals

    def __getattr__(self, name):
        return getattr(self._client, name)

    def command(self, operation, payload, command_id=None, *args, **kwargs):
        from lib.storage.errors import StorageError
        if (operation == 'task_results.checkpoint'
                and str(command_id).startswith('conversation-worker-exit:')):
            self._refusals.append(command_id)
            raise StorageError('database_protocol_error', 'database_protocol_error')
        return self._client.command(operation, payload, command_id, *args, **kwargs)


def test_refused_receipt_settles_once_and_releases_the_superseded_carrier(claimed, monkeypatch):
    """A refusal settles the job through the failure port, which reuses this carrier.

    So the handler keeps the carrier as exit evidence; once the port settles
    the job, the superseded claim is fenced and nothing retains the carrier.
    """
    from lib import storage
    from lib.conversation_sync.worker_execution import execute_claimed_attempt, settle_claimed_failure
    from lib.conversation_sync.worker_recovery import WorkerExitReceiptRefused
    from lib.durable_worker import WorkerLeaseLost
    from lib.tasks_pkg.manager import append_event, persist_task_result, stamp_chat_task_terminal
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    _unentered, _pair, factory, _owner = claimed
    store, context = _short_lease_claim(claimed)

    def run(task):
        task['content'] = 'finished, never saved'
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        context.mark_authority_lost()
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task) is False

    _run_claimed(monkeypatch, run)
    with _renewing(store, context):
        with pytest.raises(WorkerLeaseLost):
            execute_claimed_attempt(context, context.claim.payload)
    carrier = _forced_reclamation_keeps(context.claim.task_id)
    takeover = _take_over_after_expiry(store, factory, context)
    refusals = []
    monkeypatch.setattr(storage, 'get_storage_client',
                        lambda **kw: _RefusingExitReceipts(factory(**kw), refusals))
    with pytest.raises(WorkerExitReceiptRefused) as refusal:
        execute_claimed_attempt(takeover, takeover.claim.payload)
    assert refusals
    # Still the exit evidence the failure port needs: removing it here would
    # make that port refuse for want of physical-exit evidence on every claim.
    assert _forced_reclamation_keeps(context.claim.task_id) is carrier
    outcome = settle_claimed_failure(takeover, takeover.claim.payload, refusal.value)
    assert (outcome.status, outcome.error['code']) == ('failed', 'worker_exit_receipt_refused')
    assert context.authority_lost
    assert carrier.get('_eventDeliveryRevoked') is True
    assert not any(carrier.get(key) for key in (
        '_exitReceiptPending', '_terminalTurnPending', '_terminalPersistencePending'))
    chat_task_runtime.cleanup_stale(max_age=0)
    assert chat_task_runtime.get(context.claim.task_id) is None


@pytest.mark.parametrize('stopped', [False, True], ids=['failure', 'stop'])
def test_refused_exit_receipt_settles_its_job_once_instead_of_being_reclaimed(
        claimed, process_witness, monkeypatch, stopped):
    """An authority refusal repeats on every claim, so it is a settled verdict.

    Reported as an unknown settlement, it left the lease to lapse and the job
    was re-claimed every lease forever. The terminal job keeps the refusal as
    its durable reason; the Turn is already terminal and nothing is replayed.
    """
    import time
    import uuid
    from lib import storage
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.worker_execution import conversation_registration
    from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner, WorkerJobSuspension
    from lib.storage.errors import StorageError
    context, pair, factory, owner = claimed
    process, witness = process_witness
    enter(claimed, witness)
    process.kill()
    process.wait()
    if stopped:
        accepted = factory(write=True).command('worker_job.request_cancel', {
            'task_id': context.claim.task_id, 'user_id': owner,
            'now_ms': time.time_ns() // 1_000_000, 'reason': 'user_stop'}, uuid.uuid4().hex)
        assert accepted['accepted'] is True
    store = StorageWorkerJobStore(client_factory=factory)
    now_ms = time.time_ns() // 1_000_000
    # Release the fixture's claim so an ordinary runner claims the job.
    assert store.defer(context.claim, now_ms=now_ms, outcome=WorkerJobSuspension(
        available_at_ms=now_ms, replay_cursor=context.replay_cursor),
        observed_cancel_sequence=0)['ok'] is True
    refusals = []

    class RefusingAuthority:
        def __init__(self, client):
            self._client = client

        def __getattr__(self, name):
            return getattr(self._client, name)

        def command(self, operation, payload, command_id=None, *args, **kwargs):
            if (operation == 'task_results.checkpoint'
                    and str(command_id).startswith('conversation-worker-exit:')):
                refusals.append(command_id)
                # The exact refusal the native engine sent for every receipt.
                raise StorageError('database_protocol_error', 'database_protocol_error')
            return self._client.command(operation, payload, command_id, *args, **kwargs)

    monkeypatch.setattr(storage, 'get_storage_client', lambda **kw: RefusingAuthority(factory(**kw)))
    runner = SynchronousWorkerRunner(worker_id='refused-receipt-runner', store=store,
                                     registrations=[conversation_registration()])
    expected = 'cancelled' if stopped else 'failed'
    assert runner.run_once().state == expected
    assert refusals
    job = factory().query('worker_job.get', {'task_id': context.claim.task_id, 'user_id': owner})
    assert job['status'] == expected
    assert job['error']['code'] == 'worker_exit_receipt_refused'
    assert job['error']['retryable'] is False
    assert 'database_protocol_error' in job['error']['detail']
    assert turns.get_attempt(pair['attempt']['attemptId'], user_id=owner)['status'] in (
        'failed', 'interrupted')
    assert runner.run_once().state == 'idle'
