"""A durable child result closes the interrupted child-to-Flow handoff."""
from copy import deepcopy
import json

import pytest

from lib.orchestration.engine import FlowExecutor
from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused, read_journal
from lib.orchestration.invocation_recovery import completion_resolver, read_registered_actor_fees, FIELD
from tests.orchestration.test_orchestration_execution_recovery import graph, role, Crash
from tests.turn_lifecycle.test_task_event_delivery import (
    storage as storage, turn_service as turn_service, owner as owner, _task, _discard,
)
from tests.turn_lifecycle.test_file_effect_inspection import project as project

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('fault', ['before_commit', 'lost_commit_ack', 'before_flow_event', 'transient_ack'])
@pytest.mark.parametrize('native_fees', [False, True])
def test_registered_completion_recovers_without_dispatch_or_repricing(
        storage, turn_service, owner, monkeypatch, fault, native_fees):
    import lib.tasks_pkg.child_invocation as children
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.run_recovery import load_recovery_records
    from lib.error_envelope import make_envelope
    _, parent = _task(turn_service, storage, 'flow-completion')
    definition, events, executed = graph(role('worker')), [], []
    store = SidecarOrchestrationRunStore(1, client=lambda **_: storage.client)
    run_id = store.new_run_id()
    assert store.create_run(run_id, definition=definition, input_text='input')
    def emit(event):
        if event['type'] == 'execution_effect_completed':
            raise Crash()
        assert store.append_event(run_id, len(events) + 1, event)
        events.append(deepcopy(event))
    command, candidates = storage.client.command, []
    def checkpoint(operation, payload, *args, **kwargs):
        is_completion = operation == 'task_results.checkpoint' and FIELD in payload.get('value', {})
        if is_completion:
            candidates.append(deepcopy(payload))
        if is_completion and fault == 'before_commit':
            raise Crash()
        result = command(operation, payload, *args, **kwargs)
        if is_completion and fault == 'lost_commit_ack':
            raise Crash()
        if is_completion and fault == 'transient_ack' and len(candidates) == 1:
            from lib.storage.errors import StorageError
            raise StorageError('database_unavailable', 'lost completion ACK', retryable=True)
        return result
    monkeypatch.setattr(storage.client, 'command', checkpoint)
    def execute(child, *, policy):
        executed.append(child['id'])
        child.update(status='done', content='verified child result', thinking='proof',
            finishReason='stop', streamState='provider_finished',
            usage={'prompt_tokens': 10, 'completion_tokens': 2},
            apiRounds=[{'round': 1, 'model': 'fixture',
                'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
                'cost': {'costUsd': .125, 'pricingSource': 'original_quote'}}])
        if native_fees:
            from lib.billing.cost import transfer_native_child_fees
            from tests.billing.test_native_actor_fee_evidence import paid
            child.update(status='running', apiRounds=[], _nativePricingRequired=True, _executionActualsComplete=True)
            paid(child)
            child['status'] = 'done'
            transfer_native_child_fees(parent, child)
    monkeypatch.setattr(children, 'execute_invocation', execute)
    journal = ExecutionJournal(emit)
    journal.begin(definition, 'input')
    engine = FlowExecutor(definition, parent_task=parent, model='fixture',
        model_routing_policy='selected', on_event=emit, execution_journal=journal)
    try:
        with pytest.raises(Crash):
            engine.run(initial_context='input')
        assert len(executed) == 1
        started = next(e for e in events if e['type'] == 'execution_effect_started')
        runs = OrchestrationRunService(store)
        if fault == 'transient_ack':
            assert len(candidates) == 2 and candidates[0] == candidates[1]
        storage.stop()
        storage.start()
        from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs
        assert retire_interrupted_orchestration_runs(error=make_envelope(
            'worker_lost', message='fixture process lost', source='orchestration.startup_recovery'),
            client=lambda **_: storage.client) == 1
        persisted = store.get_events(run_id)
        with pytest.raises(RecoveryRefused, match='unknown'):
            read_journal(persisted, definition=definition, initial_context='input')
        if fault == 'before_commit':
            with pytest.raises(RecoveryRefused, match='no matching completion'):
                read_journal(persisted, definition=definition, initial_context='input',
                             reconcile_agent=completion_resolver(1))
            return
        _, records = load_recovery_records(runs, run_id, owner_user_id=1)
        receipt = records[started['key']]
        result = receipt['result']
        assert result['output'] == 'verified child result' and result['status'] == 'completed'
        if not native_fees:
            assert result['output_projection']['apiRounds'][0]['cost']['costUsd'] == .125
        assert receipt['invocation_id'] == executed[0]
        recovered_events = []
        resumed = ExecutionJournal(lambda e: recovered_events.append(deepcopy(e)), records=records)
        resumed.begin(definition, 'input', parent_run_id=run_id)
        recovered = FlowExecutor(definition, parent_task=parent, model='fixture',
            model_routing_policy='selected', on_event=lambda _: None,
            execution_journal=resumed).run(initial_context='input')
        assert recovered['ok']
        resumed.verify_consumed()
        assert executed == [started['invocation_id']]
        assert read_journal(recovered_events, definition=definition,
                            initial_context='input')[0] == records
        # Exact nonce, input, child birth and authenticated owner are mandatory.
        for field in ('nonce', 'parent_task_id', 'owner', 'input_hash'):
            changed = deepcopy(persisted)
            admission = next(e for e in changed if e['type'] == 'execution_effect_started')
            binding = admission['invocation_completion']
            if field == 'nonce': binding['nonce'] = '0' * 32
            elif field == 'parent_task_id': binding['identity'][field] = 'other-parent'
            elif field == 'input_hash': admission[field] = 'other-input'
            with pytest.raises(RecoveryRefused):
                read_journal(changed, definition=definition, initial_context='input',
                    reconcile_agent=completion_resolver(2 if field == 'owner' else 1))
        persisted_record = storage.client.query('record.get', {
            'namespace': 'task_results', 'key': executed[0], 'user_id': 1})
        assert persisted_record['value'][FIELD]['receipt'] == receipt
        assert json.dumps(receipt, allow_nan=False)
        private = read_registered_actor_fees(storage.client, started, owner_user_id=1,
            conversation_id=parent['convId'], parent_task_id=parent['id'])
        if not native_fees:
            assert private is None
            return
        assert private['registration_version'] == persisted_record['version']
        proof = private['evidence']
        assert proof['tokens'] == 10200
        assert proof['rounds'][0]['quote']['credits']['total'] == 7593
        assert proof['rounds'][0]['payer_task_id'] == parent['id']
        assert proof['rounds'][0]['producer_task_id'] == executed[0]
        assert candidates[-1]['value'][FIELD]['native_fees'] == proof
        assert all(key not in json.dumps(receipt) for key in ('native_fees', 'nativeCredits', 'nativePrice'))
        for scope in ({'owner_user_id': 2}, {'conversation_id': 'foreign'}, {'parent_task_id': 'foreign'}):
            with pytest.raises(RecoveryRefused):
                read_registered_actor_fees(storage.client, started, **{
                    'owner_user_id': 1, 'conversation_id': parent['convId'],
                    'parent_task_id': parent['id'], **scope})
        # A present but invalid private proof must never degrade to absent/zero.
        query = storage.client.query
        for damage in ('credits', 'owner', 'null', 'account', 'extra'):
            changed = deepcopy(persisted_record)
            completion = changed['value'][FIELD]
            if damage == 'credits': completion['native_fees']['rounds'][0]['quote']['credits']['total'] += 1
            elif damage == 'owner': completion['native_fees']['owner_user_id'] = 2
            elif damage == 'null': completion['native_fees'] = None
            elif damage == 'account': completion['receipt']['execution_account']['tokens'] = 0
            else: completion['extra'] = True
            with monkeypatch.context() as patch:
                patch.setattr(storage.client, 'query', lambda operation, payload, *a, **kw:
                    deepcopy(changed) if operation == 'record.get' and payload.get('key') == executed[0]
                    else query(operation, payload, *a, **kw))
                with pytest.raises(RecoveryRefused):
                    read_registered_actor_fees(storage.client, started, owner_user_id=1,
                        conversation_id=parent['convId'], parent_task_id=parent['id'])
                with pytest.raises(RecoveryRefused): completion_resolver(1)(started)
    finally:
        _discard(parent)


@pytest.mark.parametrize('result_bytes', [32 * 1024, 256 * 1024])
def test_killed_flow_reuses_completed_file_child(
        storage, turn_service, owner, project, tmp_path, result_bytes):
    import os
    from pathlib import Path
    import signal
    import statistics
    import subprocess
    import sys
    import time
    from lib.project_mod import modifications
    from lib.orchestration.run_recovery import load_recovery_records
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs
    from lib.tasks_pkg.handlers.tool_effect_inspection import inspect_for_task
    from lib.error_envelope import make_envelope
    client = storage.client
    native = hasattr(client, '_secret')
    ready, logfile = tmp_path / 'flow-ready.json', tmp_path / 'flow-writer.log'
    values = dict(endpoint=client.endpoint, native=native,
        token=client._secret.decode() if native else client._token,
        owner_id=getattr(client, '_owner_id', 1), tenant_id=getattr(client, '_tenant_id', None),
        ready=str(ready), project=str(project), journal=modifications.SESSIONS_DIR,
        result_bytes=result_bytes, native_fees=True)
    with logfile.open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'tests.support.flow_completion_crash_writer'],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE, stdout=log,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            process.stdin.write(json.dumps(values)); process.stdin.close()
            deadline = time.monotonic() + 30
            while not ready.exists():
                assert process.poll() is None, logfile.read_text()[-3000:]
                assert time.monotonic() < deadline, 'Flow did not reach durable child completion'
                time.sleep(.02)
            source = json.loads(ready.read_text())
            os.killpg(process.pid, signal.SIGKILL)
            assert process.wait(timeout=10) == -signal.SIGKILL
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=10)
    storage.stop(); storage.start()
    assert retire_interrupted_orchestration_runs(error=make_envelope('worker_lost',
        message='crash probe', source='orchestration.startup_recovery'),
        client=lambda **_: storage.client) == 1
    run_store = SidecarOrchestrationRunStore(1, client=lambda **_: storage.client)
    runs = OrchestrationRunService(run_store)
    _, records = load_recovery_records(runs, source['run_id'], owner_user_id=1)
    receipt = next(iter(records.values()))
    assert receipt['invocation_id'] == source['invocation_id']
    from lib.orchestration.execution_journal import inspect_journal
    inspected = inspect_journal(run_store.get_events(source['run_id']),
        definition=source['definition'], initial_context='input')
    admission = next(iter(inspected.pending_agents.values()))
    proof = read_registered_actor_fees(storage.client, admission, owner_user_id=1,
        conversation_id=source['conv_id'], parent_task_id=source['parent_task_id'])
    assert proof['evidence'] == source['native_fees']
    assert proof['evidence']['rounds'][0]['quote']['credits']['total'] == 7593
    observed = inspect_for_task({'_userId': 1, 'convId': source['conv_id']}, {
        'invocation_id': source['invocation_id'], 'operation_id': source['operation_id']})
    assert observed['outcome'] == 'file_published'
    resumed = ExecutionJournal(lambda _: None, records=records)
    resumed.begin(source['definition'], 'input', parent_run_id=source['run_id'])
    result = FlowExecutor(source['definition'], execution_journal=resumed,
        agent_runner=lambda *a, **k: pytest.fail('completed child was replayed')).run(initial_context='input')
    assert result['ok']
    resumed.verify_consumed()
    assert (project / 'child.txt').read_text() == 'once\n'
    samples = []
    for _ in range(32):
        start = time.perf_counter()
        assert load_recovery_records(runs, source['run_id'], owner_user_id=1)[1] == records
        samples.append((time.perf_counter() - start) * 1000)
    evidence = os.environ.get('TOFU_FLOW_COMPLETION_RECEIPTS')
    if evidence:
        destination = Path(evidence); destination.mkdir(parents=True, exist_ok=True)
        (destination / f"{'native' if native else 'sqlite'}-{result_bytes}.json").write_text(json.dumps({
            **source, 'recovery_median_ms': statistics.median(samples), 'query_samples': len(samples),
            'file_append_count': 1, 'replayed_children': 0}, indent=2) + '\n')


def test_unfinished_child_with_bound_identity_stays_unknown(storage, turn_service, owner, monkeypatch):
    import lib.tasks_pkg.child_invocation as children
    _, parent = _task(turn_service, storage, 'unfinished-flow')
    definition, events = graph(role('worker')), []
    journal = ExecutionJournal(lambda e: events.append(deepcopy(e)))
    journal.begin(definition, 'input')
    def execute(*args, **kwargs):
        raise Crash()
    monkeypatch.setattr(children, 'execute_invocation', execute)
    try:
        with pytest.raises(Crash):
            FlowExecutor(definition, parent_task=parent, model='fixture',
                model_routing_policy='selected', execution_journal=journal).run(initial_context='input')
        with pytest.raises(RecoveryRefused, match='no matching completion'):
            read_journal(events, definition=definition, initial_context='input',
                         reconcile_agent=completion_resolver(1))
    finally:
        _discard(parent)


def test_context_completion_is_immutable_and_closes_child_effects(
        storage, turn_service, owner, project):
    from lib.orchestration.invocation_recovery import RegisteredInvocationCompletion
    from lib.tasks_pkg.invocation_effects import persist_child_effect
    from lib.tasks_pkg.executor._effects import ToolEffectDurabilityError
    from lib.tasks_pkg.manager import discard_task
    from tests.turn_lifecycle.test_child_file_effects import child_of, write
    _, parent = _task(turn_service, storage, 'context-completion')
    from lib.agent_core.execution_budget import _ensure_execution_budget
    _ensure_execution_budget(parent)
    child = child_of(parent, project)
    writer = RegisteredInvocationCompletion(child)
    definition, events = graph(role('worker')), []
    context = {'messages': [{'role': 'assistant', 'content': 'verified worker state'}], 'task_state': {}}
    def emit(event):
        if event['type'] == 'execution_effect_completed':
            raise Crash()
        events.append(deepcopy(event))
    journal = ExecutionJournal(emit, worker_context_policy='conversation')
    journal.begin(definition, 'input')
    rows = []
    def runner(node, context, iteration, *, before_dispatch):
        before_dispatch(invocation_id=child['id'], completion=writer)
        row, _ = write(child, project); rows.append(row)
        return {'output': 'done', 'status': 'completed'}
    try:
        with pytest.raises(Crash):
            journal.runner(runner, prepare_before_admission=True,
                context_snapshot=lambda: context, context_restore=lambda _: None)(role('worker'), 'input', 0)
        records, _ = read_journal(events, definition=definition, initial_context='input',
            reconcile_agent=completion_resolver(1, conversation_id=parent['convId'], parent_task_id=parent['id']))
        receipt = next(iter(records.values()))
        assert receipt['result']['worker_context'] == context
        def version():
            return storage.client.query('record.get', {
                'namespace': 'task_results', 'key': child['id'], 'user_id': 1})['version']
        original_version = version()
        writer.settle(receipt)
        assert version() == original_version
        changed = deepcopy(receipt); changed['result']['output'] = 'different result'
        with pytest.raises(RecoveryRefused, match='immutable'):
            writer.settle(changed)
        with pytest.raises(ToolEffectDurabilityError, match='Completed child'):
            persist_child_effect(child, rows[0])
        for scope in ({'conversation_id': 'other'}, {'parent_task_id': 'other'}):
            with pytest.raises(RecoveryRefused, match='this launch'):
                read_journal(events, definition=definition, initial_context='input',
                             reconcile_agent=completion_resolver(1, **scope))
        assert version() == original_version
    finally:
        discard_task(child['id']); _discard(parent)
