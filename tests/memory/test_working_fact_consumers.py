"""Task dependency history, provider uncertainty and targeted correction impact."""
import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

from tests.memory.test_working_facts import (artifact, facts, storage, turn_service,
    record, owner, dependency, PRINCIPAL)
from tests.memory.test_working_facts_context import quiet_notes, task_for
from lib.memory.working_facts import consumers
from lib.memory.working_facts.contract import validate
from lib.memory.working_facts.repository import FactConflict, FactsUnavailable
from lib.tasks_pkg import context_dependencies
from lib.tasks_pkg.context_composer.working_facts import refresh_working_facts

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def consumer_storage(storage, monkeypatch):
    monkeypatch.setattr(consumers, 'get_storage_client', lambda write=False: storage.client)


def references(*facts):
    return [{'id': fact['id'], 'revision': fact['revision']} for fact in facts]


def prepare(facts, task, *bindings):
    consumers.prepare(facts['conv_id'], principal=PRINCIPAL, task_id=task, facts=references(*bindings))


def confirm(facts, task, *bindings):
    consumers.confirm(facts['conv_id'], principal=PRINCIPAL, task_id=task, facts=references(*bindings))


def query(facts, **kwargs):
    return validate(consumers.query(facts['conv_id'], principal=PRINCIPAL, **kwargs), 'WorkingFactConsumersView')


def test_correction_finds_transitive_consumers_after_restart_and_preserves_independent_tasks(facts, storage):
    source = record(facts)['facts'][0]
    derived = record(facts, evidence=[dependency(source)])['facts'][0]
    independent = record(facts)['facts'][0]
    for task, fact in [('dependent', derived), ('independent', independent)]:
        prepare(facts, task, fact); confirm(facts, task, fact)
    assert not any(row['requiresRevalidation'] for row in query(facts)['consumers'])
    owner(facts, source, 'correct', text='Use Report 2')
    storage.stop(); storage.start()
    result = {row['taskId']: row for row in query(facts)['consumers']}
    assert result['dependent']['requiresRevalidation']
    assert result['dependent']['dependencies'][0]['reasons'] == ['source_dependency_changed']
    assert not result['independent']['requiresRevalidation']
    assert not result['independent']['consumptionUncertain']


def test_uncertain_old_request_is_not_confirmed_by_a_new_success_or_invented_middle_revision(facts):
    first = record(facts)['facts'][0]
    prepare(facts, 'task', first)
    second = owner(facts, first, 'correct', text='Second')['facts'][0]
    third = owner(facts, second, 'correct', text='Third')['facts'][0]
    prepare(facts, 'task', third)
    with pytest.raises(FactConflict, match='not prepared'):
        confirm(facts, 'task', second)
    confirm(facts, 'task', third)
    result = query(facts)['consumers'][0]
    assert result['requiresRevalidation'] and result['consumptionUncertain']
    assert result['dependencies'][0]['preparedFirstRevision'] == 1
    assert result['dependencies'][0]['confirmedFirstRevision'] == 3


def test_lost_commit_ack_has_exact_readback_and_confirmation_is_idempotent(facts, storage, monkeypatch):
    from lib.storage import StorageError
    fact = record(facts)['facts'][0]
    original = storage.client.command
    commits = []
    def lost_ack(operation, payload, *args, **kwargs):
        result = original(operation, payload, *args, **kwargs)
        if operation == 'record.put' and payload.get('namespace') == consumers.NAMESPACE:
            commits.append(copy.deepcopy(payload))
            raise StorageError('database_timeout', 'Injected lost acknowledgement')
        return result
    monkeypatch.setattr(storage.client, 'command', lost_ack)
    prepare(facts, 'task', fact); confirm(facts, 'task', fact); confirm(facts, 'task', fact)
    assert len(commits) == 2
    assert query(facts)['consumers'][0]['dependencies'][0]['status'] == 'current'


def prepared_context(facts):
    task = task_for(facts) | {'abort_event': threading.Event()}
    messages = [{'role': 'user', 'content': 'Use the report'}]
    refresh_working_facts(messages, task=task, model='fixture')
    context_dependencies.prepare(task, messages)
    return task, messages


def test_only_matching_wire_and_verified_completion_confirm_consumption(facts, quiet_notes, monkeypatch):
    record(facts)
    task, messages = prepared_context(facts)
    assert query(facts)['consumers'][0]['consumptionUncertain']
    altered = copy.deepcopy(messages)
    altered[-1]['content'] = 'Compaction removed the working fact block'
    context_dependencies.observe_wire(task, altered)
    context_dependencies.confirm(task)
    assert query(facts)['consumers'][0]['consumptionUncertain']
    context_dependencies.observe_wire(task, messages)
    context_dependencies.confirm(task)
    assert not query(facts)['consumers'][0]['consumptionUncertain']
    def forbidden(*args, **kwargs):
        raise AssertionError('Unchanged, acknowledged bindings must add no storage operations')
    monkeypatch.setattr(consumers.ConsumerRepository, 'read', forbidden)
    context_dependencies.prepare(task, messages)
    assert '_workingFactDependencyPending' not in task


def test_foreign_or_changed_context_scope_is_denied_and_cancelled_consumption_stays_unknown(facts, quiet_notes):
    from lib.identity import PrincipalContext
    record(facts)
    task, messages = prepared_context(facts)
    with pytest.raises(FactsUnavailable):
        consumers.query(facts['conv_id'], principal=PrincipalContext.user(subject_id='foreign', owner_user_id=8))
    context_dependencies.observe_wire(task, messages)
    task['abort_event'].set()
    context_dependencies.confirm(task)
    assert query(facts)['consumers'][0]['consumptionUncertain']
    task['abort_event'].clear()
    task['id'] = 'replacement-task'
    with pytest.raises(PermissionError):
        context_dependencies.confirm(task)


def test_removed_or_unadmitted_block_does_not_create_a_consumer(facts, quiet_notes):
    record(facts)
    task = task_for(facts)
    messages = [{'role': 'user', 'content': 'Continue'}]
    refresh_working_facts(messages, task=task, model='fixture')
    context_dependencies.prepare(task, [{'role': 'user', 'content': 'No admitted facts'}])
    assert query(facts)['consumers'] == []


def test_consumer_pagination_is_owned_and_preserves_all_task_bindings(facts):
    fact = record(facts)['facts'][0]
    for index in range(5):
        prepare(facts, f'task-{index}', fact)
    seen, cursor = [], ''
    while True:
        page = query(facts, cursor=cursor, page_size=2)
        seen.extend(row['taskId'] for row in page['consumers'])
        cursor = page['nextCursor']
        if not cursor:
            break
    assert set(seen) == {f'task-{index}' for index in range(5)} and len(seen) == 5
    with pytest.raises(ValueError):
        query(facts, cursor='../foreign')


def test_actual_v4_consumers_endpoint_requires_owner_and_reports_correction(facts):
    from quart import Quart, g, request
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.identity import PrincipalContext
    from routes.api_v1.working_facts import api_v1_working_facts_bp
    fact = record(facts)['facts'][0]
    prepare(facts, 'task', fact); confirm(facts, 'task', fact)
    owner(facts, fact, 'correct', text='Corrected requirement')
    app = Quart(__name__)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_working_facts_bp, name='fact_consumers_v4')))
    @app.before_request
    def identity():
        owner_id = request.headers.get('X-Test-Owner')
        if owner_id:
            g.auth_ctx = SimpleNamespace(is_authenticated=True, name='test owner', owner_user_id=int(owner_id),
                key_id='test', tenant_id=None, scopes=['chat'], has_scope=lambda scope: scope == 'chat')
            g.principal_context = PrincipalContext.user(subject_id='reviewer', owner_user_id=int(owner_id))
    async def exercise():
        client = app.test_client(); url = '/api/v4/conversations/' + facts['conv_id'] + '/working-facts/consumers'
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers={'X-Test-Owner': '8'})).status_code == 404
        response = await client.get(url, headers={'X-Test-Owner': '1'})
        assert response.status_code == 200
        assert (await response.get_json())['data']['consumers'][0]['requiresRevalidation']
        assert (await client.get(url + '?limit=0', headers={'X-Test-Owner': '1'})).status_code == 400
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['complete', 'partial', 'cancelled', 'translated_away', 'metadata_only'])
def test_ordinary_physical_request_and_verified_return_own_confirmation(facts, quiet_notes, monkeypatch, outcome):
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.llm_fallback import _call as fallback
    from lib.tasks_pkg.message_input_receipts import receipt_admission
    from lib.tasks_pkg.orchestrator import _llm_round_call as call
    record(facts)
    task, messages = prepared_context(facts)
    def provider(target, body, **kwargs):
        lease = receipt_admission(target, None)(max_output_tokens=100)
        # Exercise the same sealed physical-input observer used by dispatch,
        # including a provider conversion that removes the original block.
        physical = {'input': body['messages']} if outcome != 'translated_away' else {'input': 'Changed request'}
        if outcome == 'metadata_only':
            physical = {'input': 'Changed request', 'metadata': {'messages': body['messages']}}
        lease.prepare_input(physical)
        stream = ProviderStreamResult.from_legacy({'role': 'assistant', 'content': 'answer'},
            'premature_close' if outcome == 'partial' else 'stop', None)
        lease.confirm_input(successful=stream.is_verified_complete)
        if outcome == 'cancelled':
            target['abort_event'].set()
        return stream
    monkeypatch.setattr(fallback, 'stream_llm_response', provider)
    def model(*args, **kwargs):
        stream = fallback._stream_with_message_receipts(task, {'messages': messages})
        return {'assistant_msg': {'role': 'assistant', 'content': 'answer'},
            'finish_reason': stream.finish_reason, 'stream_result': stream, 'usage': None,
            'model': 'fixture', 'preset': None, 'thinking_enabled': False, '_loop_action': 'proceed'}
    monkeypatch.setattr(call, '_llm_call_with_fallback', model)
    rs = SimpleNamespace(model='fixture', tool_call_happened=False, preset=None,
        thinking_enabled=False, accumulated_usage={}, api_rounds=0, last_usage=None)
    call.run_llm_call_with_fallback(task, rs, {}, messages, [], SimpleNamespace(on_tool_call_ready=None),
                                  round_num=0, tid='facts', max_tokens=100)
    assert query(facts)['consumers'][0]['consumptionUncertain'] == (outcome != 'complete')


@pytest.mark.parametrize('completed', [True, False])
def test_native_context_uses_real_shared_admission_and_settlement(facts, quiet_notes, completed):
    from concurrent.futures import ThreadPoolExecutor
    from lib.agent_core.native_process import RoundRequest, RoundSettlement
    from lib.tasks_pkg.native_context import NativeContextPolicy
    record(facts)
    task = task_for(facts) | {'abort_event': threading.Event(), 'status': 'running'}
    async def run(executor):
        policy = NativeContextPolicy(principal=PRINCIPAL, task=task, executor=executor)
        policy.bind_route({'owner_user_id': 1, 'logical_model': 'fixture', 'wire_model': 'fixture-wire',
            'context_window': 131072, 'provider_id': 'fixture-provider'}, {'max_tokens': 1024}, {})
        request = RoundRequest(PRINCIPAL, task['id'], 'request', 0, 1, [{'role': 'user', 'content': 'Use the facts'}])
        try:
            await policy.prepare(request)
            assert query(facts)['consumers'][0]['consumptionUncertain']
            await policy.settle(RoundSettlement(request, {'kind': 'returned',
                'provider_state': 'provider_finished' if completed else 'premature_eof'}))
            assert query(facts)['consumers'][0]['consumptionUncertain'] is not completed
        finally:
            policy.close()
    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))


@pytest.mark.parametrize('count', [1, 256])
def test_consumer_storage_growth_is_per_fact_identity_not_per_model_round(facts, storage, tmp_path, count):
    import hashlib
    import json
    import os
    from pathlib import Path
    import time
    # Synthetic trusted bindings isolate index persistence from source creation
    # and prompt selection. This is not a model/whole-task performance result.
    bindings = [{'id': f'wf-{index:04d}', 'revision': 1} for index in range(count)]
    samples, sizes = [], []
    for index in range(15):
        task_id = f'measure-{count}-{index}'
        start = time.perf_counter_ns()
        consumers.prepare(facts['conv_id'], principal=PRINCIPAL, task_id=task_id, facts=bindings)
        prepared = time.perf_counter_ns()
        consumers.confirm(facts['conv_id'], principal=PRINCIPAL, task_id=task_id, facts=bindings)
        end = time.perf_counter_ns()
        repository = consumers.ConsumerRepository(facts['conv_id'], PRINCIPAL, task_id)
        value, version = repository.read()
        assert len(value['prepared']) == len(value['confirmed']) == count and value['pending'] == {}
        # Repeat acknowledgements preserve the exact row/version.
        consumers.confirm(facts['conv_id'], principal=PRINCIPAL, task_id=task_id, facts=bindings)
        assert repository.read() == (value, version)
        samples.append({'prepareMs': (prepared - start) / 1e6, 'confirmMs': (end - prepared) / 1e6})
        sizes.append(len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()))
    assert max(sizes) <= 1024 + count * 160
    output = Path(os.environ.get('TOFU_FACT_CONSUMER_MEASURE_OUTPUT', tmp_path)); output.mkdir(parents=True, exist_ok=True)
    backend = storage._backend
    result = {'scope': '15 fresh index records, selected disposable store, synthetic trusted bindings; no model or total task cost.',
        'backend': backend, 'factCount': count, 'recordBytes': sizes, 'samples': samples,
        'sourceSha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [
            Path(consumers.__file__), Path(__file__)]}}
    (output / f'consumer-{backend}-{count}.json').write_text(json.dumps(result, indent=2) + '\n')


def test_prepare_failure_prevents_admission_and_confirmation_failure_preserves_unknown(facts, quiet_notes, storage, monkeypatch):
    from lib.storage import StorageError
    record(facts)
    task = task_for(facts)
    messages = [{'role': 'user', 'content': 'Continue'}]
    refresh_working_facts(messages, task=task, model='fixture')
    original = storage.client.command
    def fail_index(operation, payload, *args, **kwargs):
        if operation == 'record.put' and payload.get('namespace') == consumers.NAMESPACE:
            raise StorageError('database_unavailable', 'Injected before dependency commit')
        return original(operation, payload, *args, **kwargs)
    monkeypatch.setattr(storage.client, 'command', fail_index)
    with pytest.raises(StorageError):
        context_dependencies.prepare(task, messages)
    assert '_workingFactDependencyPending' not in task and query(facts)['consumers'] == []
    monkeypatch.setattr(storage.client, 'command', original)
    context_dependencies.prepare(task, messages)
    context_dependencies.observe_wire(task, messages)
    monkeypatch.setattr(storage.client, 'command', fail_index)
    context_dependencies.confirm(task)
    assert '_workingFactDependencyConfirmed' not in task
    assert query(facts)['consumers'][0]['consumptionUncertain']
