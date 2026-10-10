"""Actual worker dispatch persists intent, output and recovery in real stores."""
from copy import deepcopy
import threading
import time

import pytest

from lib.automations.execution import (ActionAdapter, AutomationExecutionHandler,
    ExecutionRegistry, presentation_registry)
from lib.automations.errors import AutomationError
from lib.automations.registry import builtin_registry
from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner
from tests.automations.test_atomic_execution import setup_rule
from tests.support.worker_job_time import wait_until_ms, wall_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def fixture(authority):
    repo, definition, event, now, factory = setup_rule(authority)
    event['evidence']['startedAt'] = event['occurredAt']
    run = repo.admit(definition.id, event, now=now)
    return factory, repo, run


def worker(factory, *, registry=None, store_type=StorageWorkerJobStore):
    class Store(store_type):
        def _client(self, *, write=False):
            return factory(write=write)
    handler = AutomationExecutionHandler(client_factory=factory,
        registry=registry if registry is not None else presentation_registry(),
        now_ms=wall_ms, poll_interval_ms=1000)
    return SynchronousWorkerRunner(worker_id='execution-worker', store=Store(),
        registrations=[handler.registration()], lease_ms=10000,
        heartbeat_interval_ms=1000, now_ms=wall_ms)


def job(factory, repo, run):
    return factory().query('worker_job.get', {'task_id': run.value['jobId'], 'user_id': repo.owner_id})


def test_task_time_uses_frozen_start_and_settles_the_durable_run(semantic_authority):
    factory, repo, run = fixture(semantic_authority)
    seen = []
    real = presentation_registry().get(builtin_registry().get('task.show_time').document())
    def execute(invocation):
        durable = repo.get('run', run.id).value
        assert durable['steps']['show']['status'] == 'dispatching'
        assert durable['steps']['show']['effectKey'] == invocation.effect_key
        assert invocation.context.principal.owner_user_id == repo.owner_id
        seen.append(invocation.effect_key)
        return real.execute(invocation)
    runner = worker(factory, registry=ExecutionRegistry([ActionAdapter(real.contract, execute)]))
    assert runner.run_once().state == 'succeeded'
    saved = repo.get('run', run.id).value
    assert saved['status'] == 'succeeded'
    assert saved['steps']['show']['output']['startedAt'] == run.value['trigger']['occurredAt']
    assert saved['steps']['show']['output']['timezone'] == 'Asia/Shanghai'
    assert job(factory, repo, run)['status'] == 'succeeded'
    assert runner.run_once().state == 'idle' and len(seen) == 1
    assert not any(t.name.startswith('durable-worker-heartbeat:') for t in threading.enumerate())


def test_cancelled_pending_run_never_invokes_an_action(semantic_authority):
    factory, repo, run = fixture(semantic_authority)
    repo.cancel(run.id, request_id='cancel-before-dispatch', reason='owner', now=time.time())
    def forbidden(invocation):
        pytest.fail('Cancellation dispatched an action')
    registry = ExecutionRegistry([ActionAdapter(builtin_registry().get('task.show_time'), forbidden)])
    assert worker(factory, registry=registry).run_once().state == 'idle'
    assert repo.get('run', run.id).value['status'] == 'cancelled'
    assert job(factory, repo, run)['status'] == 'cancelled'


def test_missing_port_fails_before_any_external_dispatch(semantic_authority):
    factory, repo, run = fixture(semantic_authority)
    assert worker(factory, registry=ExecutionRegistry([])).run_once().state == 'failed'
    saved = repo.get('run', run.id).value
    assert saved['status'] == 'failed'
    assert saved['steps']['show']['error']['code'] == 'action_adapter_unavailable'
    assert job(factory, repo, run)['status'] == 'failed'


def test_crash_after_intent_replays_presentation_under_same_effect_key(semantic_authority):
    factory, repo, run = fixture(semantic_authority)
    # This scenario needs enough run budget for SQL's actual lease expiry.
    # setup_rule's ten-second deadline cannot represent that recovery window.
    from lib.automations.definitions import prepare_definition
    definition = repo.get('definition', run.value['definitionId'])
    rule = deepcopy(definition.value['definition']); rule['policy']['maxRuntimeSeconds'] = 120
    changed = repo.publish(prepare_definition(rule), expected_version=definition.version, now=time.time())
    event = deepcopy(run.value['trigger']); event['id'] += '-recovery'
    run = repo.admit(changed.id, event, now=time.time())
    # Settle the first accepted occurrence so the new one becomes claimable.
    assert worker(factory).run_once().state == 'succeeded'
    calls = []
    real = presentation_registry().get(builtin_registry().get('task.show_time').document())
    def crash(invocation):
        calls.append(invocation.effect_key)
        raise SystemExit('Injected death after durable intent')
    with pytest.raises(SystemExit):
        worker(factory, registry=ExecutionRegistry([ActionAdapter(real.contract, crash)])).run_once()
    before = repo.get('run', run.id).value
    assert before['steps']['show']['status'] == 'dispatching'
    engine = semantic_authority[2]
    if engine is not None:
        engine.crash_cycle()
    else:
        wait_until_ms(job(factory, repo, run)['leaseDeadlineMs'])
    def recover(invocation):
        calls.append(invocation.effect_key)
        return real.execute(invocation)
    assert worker(factory, registry=ExecutionRegistry([ActionAdapter(real.contract, recover)])).run_once().state == 'succeeded'
    after = repo.get('run', run.id).value
    assert after['status'] == 'succeeded'
    assert after['fence']['token'] > before['fence']['token']
    assert calls == [before['steps']['show']['effectKey']] * 2


def test_write_port_requires_an_explicit_reconciliation_owner():
    with pytest.raises(AutomationError, match='reconciliation'):
        ExecutionRegistry([ActionAdapter(builtin_registry().get('skills.activate'), lambda invocation: {})])


def test_unknown_write_and_cancellation_require_settlement_without_redispatch(semantic_authority):
    factory, repo, initial = fixture(semantic_authority)
    assert worker(factory).run_once().state == 'succeeded'
    from lib.automations.definitions import prepare_definition
    from lib.automations.execution_repository import AutomationExecutionRepository
    from lib.identity import PrincipalContext
    repo = AutomationExecutionRepository(PrincipalContext.user(subject_id='test-publisher',
        owner_user_id=repo.owner_id, tenant_id=repo.principal.tenant_id,
        scopes=set(repo.principal.scopes) | {'skills:install'}), client_factory=factory)
    contract = builtin_registry().get('skills.activate')
    definition = repo.get('definition', initial.value['definitionId'])
    rule = deepcopy(definition.value['definition'])
    rule['policy']['maxRuntimeSeconds'] = 120
    rule['steps'] = [{'id':'write','action':contract.id,'arguments':{'snapshotId':'test-snapshot'},'after':[]}]
    updated = repo.publish(prepare_definition(rule),
        expected_version=definition.version, now=time.time())
    event = deepcopy(initial.value['trigger']);event['id'] += '-unknown-write'
    run = repo.admit(updated.id, event, now=time.time())
    calls = []; reconciled = []; settled = {'ok':False}
    proof = {'activated':True,'snapshotId':'test-snapshot','revisions':[
        {'skillId':'test-skill','version':'1','sourceId':'test-owned','digest':'a'*64}]}
    def execute(invocation):
        calls.append(invocation.effect_key)
        raise RuntimeError('Injected unknown external result; no real external action')
    def reconcile(invocation):
        reconciled.append(invocation.effect_key)
        return proof if settled['ok'] else None
    runner = worker(factory, registry=ExecutionRegistry([ActionAdapter(contract, execute, reconcile)]))
    assert runner.run_once().state == 'suspended'
    assert repo.get('run', run.id).value['status'] == 'reconciliation_required'
    wait_until_ms(job(factory, repo, run)['availableAtMs'])
    assert runner.run_once().state == 'suspended'
    assert len(calls) == 1 and len(reconciled) == 1
    repo.cancel(run.id, request_id='cancel-unknown', reason='owner', now=time.time())
    assert runner.run_once().state == 'suspended'
    assert repo.get('run', run.id).value['status'] == 'cancelling'
    assert job(factory, repo, run)['status'] == 'suspended'
    assert job(factory, repo, run)['leaseDeadlineMs'] == 0
    settled['ok'] = True
    wait_until_ms(job(factory, repo, run)['availableAtMs'])
    assert runner.run_once().state == 'cancelled'
    saved = repo.get('run', run.id).value
    assert saved['status'] == 'cancelled' and saved['steps']['write']['output'] == proof
    assert len(calls) == 1 and set(calls + reconciled) == {saved['steps']['write']['effectKey']}
    assert job(factory, repo, run)['status'] == 'cancelled'


def test_confirmation_policy_suspends_before_external_intent(semantic_authority):
    factory, repo, initial = fixture(semantic_authority)
    assert worker(factory).run_once().state == 'succeeded'
    from lib.automations.definitions import prepare_definition
    from lib.automations.execution_repository import AutomationExecutionRepository
    from lib.identity import PrincipalContext
    repo = AutomationExecutionRepository(PrincipalContext.user(subject_id='test-publisher',
        owner_user_id=repo.owner_id, tenant_id=repo.principal.tenant_id,
        scopes=set(repo.principal.scopes) | {'skills:install'}), client_factory=factory)
    definition = repo.get('definition', initial.value['definitionId'])
    rule = deepcopy(definition.value['definition'])
    rule['policy'].update(permissionMode='manual', maxRuntimeSeconds=120)
    contract = builtin_registry().get('skills.activate')
    rule['steps'] = [{'id':'write','action':contract.id,'arguments':{'snapshotId':'test-snapshot'},'after':[]}]
    updated = repo.publish(prepare_definition(rule), expected_version=definition.version, now=time.time())
    event = deepcopy(initial.value['trigger']);event['id'] += '-confirmation'
    run = repo.admit(updated.id, event, now=time.time())
    def forbidden(invocation):
        pytest.fail('Confirmation policy dispatched an external action')
    runner = worker(factory, registry=ExecutionRegistry([ActionAdapter(contract, forbidden, forbidden)]))
    assert runner.run_once().state == 'suspended'
    saved = repo.get('run', run.id).value
    assert saved['status'] == 'waiting_permission'
    assert saved['steps']['write']['status'] == 'waiting_permission'
    state = saved['steps']['write']
    assert state['attempts'] == 0 and state['arguments'] == {'snapshotId':'test-snapshot'}
    assert 'dispatchedAt' not in state and 'approval' not in state
    assert job(factory, repo, run)['leaseDeadlineMs'] == 0
    assert runner.run_once().state == 'idle'
    repo.cancel(run.id, request_id='cancel-confirmation', reason='owner', now=time.time())
    assert repo.get('run', run.id).value['status'] == 'cancelled'
    assert runner.run_once().state == 'cancelled'
    assert job(factory, repo, run)['status'] == 'cancelled'


@pytest.mark.parametrize('guard',[True,False])
def test_conditional_flow_waits_without_redispatch_or_a_retained_monitor(semantic_authority,guard):
    factory, repo, initial = fixture(semantic_authority)
    assert worker(factory).run_once().state == 'succeeded'
    from lib.automations.definitions import prepare_definition
    from lib.automations.execution_repository import AutomationExecutionRepository
    from lib.identity import PrincipalContext
    repo = AutomationExecutionRepository(PrincipalContext.user(subject_id='test-publisher',
        owner_user_id=repo.owner_id, tenant_id=repo.principal.tenant_id,
        scopes=set(repo.principal.scopes) | {'conversation.execute'}), client_factory=factory)
    definition = repo.get('definition', initial.value['definitionId'])
    rule = deepcopy(definition.value['definition'])
    rule['policy'].update(maxRuntimeSeconds=120, maxModelTokens=1000)
    contract = builtin_registry().get('flow.run')
    rule['steps'] = [{'id':'flow','action':contract.id,'after':[],'when':guard,
        'arguments':{'definitionId':'owned-flow','inputs':{}}}]
    updated = repo.publish(prepare_definition(rule), expected_version=definition.version, now=time.time())
    event = deepcopy(initial.value['trigger']);event['id'] += '-flow'
    run = repo.admit(updated.id, event, now=time.time())
    calls = []; observed = []
    def execute(invocation):
        calls.append(invocation.effect_key)
        return {'runId':'owned-run','conversationId':'owned-conversation','status':'queued'}
    def reconcile(invocation):
        observed.append(invocation.effect_key)
        status = 'running' if len(observed) == 1 else 'done'
        result = {'runId':'owned-run','conversationId':'owned-conversation','status':status}
        if status == 'done':result['resultRef'] = 'owned-result'
        return result
    runner = worker(factory, registry=ExecutionRegistry([ActionAdapter(contract, execute, reconcile)]))
    if not guard:
        assert runner.run_once().state == 'succeeded'
        assert calls == observed == []
        assert repo.get('run', run.id).value['steps']['flow']['status'] == 'skipped'
        return
    assert runner.run_once().state == 'suspended'
    for expected in ['suspended','succeeded']:
        saved_job = job(factory, repo, run)
        assert saved_job['status'] == 'suspended' and saved_job['leaseDeadlineMs'] == 0
        assert not any(t.name.startswith('durable-worker-heartbeat:') for t in threading.enumerate())
        wait_until_ms(saved_job['availableAtMs'])
        assert runner.run_once().state == expected
    saved = repo.get('run', run.id).value
    assert saved['status'] == 'succeeded' and saved['steps']['flow']['output']['status'] == 'done'
    assert len(calls) == 1 and len(observed) == 2
    assert set(calls + observed) == {saved['steps']['flow']['effectKey']}
