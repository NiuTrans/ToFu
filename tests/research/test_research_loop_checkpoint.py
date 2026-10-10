"""Complete-round recovery material survives restart without replay authority."""
from copy import deepcopy

import pytest

from lib.agent_loop import AbortSignal, run_agent_loop
from lib.research.loop_checkpoint import LoopCheckpoints, NAMESPACE
from lib.research.request_context import PART_NAMESPACE
from lib.storage.errors import StorageError
from tests.research.test_research_model_accounting import repository, snapshot
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def setup(client):
    calls = repository(client); before = snapshot(0); before['agent_dispatches'] = 1
    intent = calls.admit_model_call(ordinal=1, round_index=0, requested_model='controlled-model',
        request_digest='d' * 64, max_output=8000, usage_before=before, loop_checkpoint_version=1)
    calls.record_model_usage(intent, snapshot(), usage_mapping_received=False)
    captured = []
    class Stop(Exception): pass
    def checkpoint(value): captured.append(value); raise Stop()
    with pytest.raises(Stop):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[],
            dispatch=lambda *args: ({'tool_calls': [{'id': 'call-1', 'function': {
                'name': 'experiment', 'arguments': '{}'}}]}, 'tool_calls', {}),
            execute_tool=lambda *args: 'evidence', max_consecutive_no_progress_rounds=3,
            on_checkpoint=checkpoint)
    material = {'messages': [{'role': 'tool', 'tool_call_id': 'call-1', 'content': '原始工具结果🧪' * 12000}],
        'tool_rounds': [{'roundNum': 1, 'llmRound': 0, 'status': 'done', 'toolCallId': 'call-1'}],
        'usage_snapshot': snapshot(), 'chassis': captured[0]}
    checkpoints = LoopCheckpoints.open(client, user_id=1, task_id='research_action_observed')
    return calls, checkpoints, intent, material


def test_exact_material_is_immutable_detached_and_usage_linked():
    client = MemoryRecords(); calls, checkpoints, intent, material = setup(client)
    expected = deepcopy(material); checkpoints.save_checkpoint(intent, material)
    material['messages'][0]['content'] = 'changed'
    restored, meter = checkpoints.restore_checkpoint(1)
    assert restored == expected and meter.snapshot() == expected['usage_snapshot']
    with pytest.raises(StorageError): checkpoints.save_checkpoint(intent, material)
    restored['tool_rounds'].clear()
    assert checkpoints.load_checkpoint(1) == expected
    def no_write(*args): pytest.fail('read replayed or wrote state')
    client.command = no_write
    assert checkpoints.restore_checkpoint(1)[0] == expected


@pytest.mark.parametrize('mutation', ['missing_limits','missing_progress','boolean_counter',
    'extra_limit','negative_timeout','invalid_progress','retry_credit'])
@pytest.mark.parametrize('retained', [False, True])
def test_invalid_complete_chassis_cannot_be_published_or_restored(mutation, retained):
    client = MemoryRecords(); _, checkpoints, intent, material = setup(client)
    if retained: checkpoints.save_checkpoint(intent, material)
    chassis = material['chassis']
    if mutation == 'missing_limits': chassis.pop('limits')
    elif mutation == 'missing_progress': chassis.pop('progress')
    elif mutation == 'boolean_counter': chassis['retry_bonus_used'] = True
    elif mutation == 'extra_limit': chassis['limits']['unknown_credit'] = 100
    elif mutation == 'negative_timeout': chassis['consecutive_tool_timeouts'] = -1
    elif mutation == 'invalid_progress': chassis['progress'] = {}
    else: chassis['retry_bonus_used'] = chassis['limits']['max_retry_bonus'] + 1
    if retained:
        digest, chars, parts = checkpoints._write_material(material, 'loopMaterial')
        manifest = client.rows[NAMESPACE, checkpoints.prefix + '1']['value']
        manifest.update(material_digest=digest, material_chars=chars, parts=parts)
        with pytest.raises(StorageError, match='invalid chassis'): checkpoints.restore_checkpoint(1)
    else:
        with pytest.raises(StorageError, match='invalid chassis'): checkpoints.save_checkpoint(intent, material)
        assert checkpoints.load_checkpoint(1) is None


@pytest.mark.parametrize('mutation', ['usage', 'round', 'unsettled'])
def test_inconsistent_or_unsettled_material_is_not_published(mutation):
    client = MemoryRecords(); _, checkpoints, intent, material = setup(client)
    if mutation == 'usage': material['usage_snapshot']['total_tokens'] = 999
    elif mutation == 'round': material['chassis']['next_round'] = 9
    else: material['tool_rounds'][0]['status'] = 'searching'
    with pytest.raises(StorageError): checkpoints.save_checkpoint(intent, material)
    assert checkpoints.load_checkpoint(1) is None


@pytest.mark.parametrize('mutation', ['missing', 'content', 'admission', 'order'])
def test_corrupt_checkpoint_cannot_restore(mutation):
    client = MemoryRecords(); _, checkpoints, intent, material = setup(client)
    checkpoints.save_checkpoint(intent, material)
    manifest = client.rows[NAMESPACE, checkpoints.prefix + '1']['value']
    first = PART_NAMESPACE, checkpoints.prefix + manifest['parts'][0]
    if mutation == 'missing': client.rows.pop(first)
    elif mutation == 'content': client.rows[first]['value']['content'] = 'corrupted'
    elif mutation == 'admission': manifest['admission_id'] = '0' * 32
    else: manifest['parts'].reverse()
    with pytest.raises(StorageError): checkpoints.restore_checkpoint(1)


def test_lost_ack_is_idempotent_and_successor_refuses_old_restore():
    client = MemoryRecords(); calls, checkpoints, intent, material = setup(client)
    client.lose_ack = True; checkpoints.save_checkpoint(intent, material)
    before = snapshot(); before['agent_dispatches'] = 2
    calls.admit_model_call(ordinal=2, round_index=1, requested_model='controlled-model',
        request_digest='e' * 64, max_output=8000, usage_before=before)
    assert checkpoints.load_checkpoint(1) == material
    with pytest.raises(StorageError, match='successor'): checkpoints.restore_checkpoint(1)


def test_checkpoint_survives_selected_storage_restart(storage):
    supervisor, _ = storage
    _, checkpoints, intent, material = setup(supervisor.client)
    checkpoints.save_checkpoint(intent, material)
    supervisor.stop(); supervisor.start()
    reopened = LoopCheckpoints.open(supervisor.client, user_id=1, task_id='research_action_observed')
    restored, meter = reopened.restore_checkpoint(1)
    assert restored == material and meter.snapshot() == material['usage_snapshot']
    assert LoopCheckpoints.open(supervisor.client, user_id=2, task_id='research_action_observed') is None


@pytest.mark.parametrize('refuse', [False, True, 'false_ack', 'intent_false_ack', 'default_model_change'])
def test_production_loop_acknowledges_checkpoint_before_next_provider(monkeypatch, refuse):
    import threading
    from types import SimpleNamespace
    import lib.research.action as action
    from lib.research.workspace import empty_workspace
    client = MemoryRecords(); dispatched = []; effects = []
    original_command = client.command
    def command(operation, payload, key):
        from lib.research.model_accounting import INTENT_NAMESPACE
        if ((refuse == 'false_ack' and payload['namespace'] == NAMESPACE)
                or (refuse == 'intent_false_ack' and payload['namespace'] == INTENT_NAMESPACE)):
            return {'version':1}
        if refuse is True and payload['namespace'] == NAMESPACE:
            raise StorageError('database_unavailable', 'injected checkpoint refusal')
        return original_command(operation, payload, key)
    client.command = command
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: None)
    monkeypatch.setattr('lib.llm.stream_result.ensure_provider_stream_result', lambda result: result)
    def provider(messages, **kwargs):
        assert kwargs['prefer_model'] == 'controlled-model'
        assert kwargs['strict_model'] is (refuse != 'default_model_change')
        if refuse == 'default_model_change':
            monkeypatch.setattr(action._lib, 'LLM_MODEL', 'changed-default')
        if dispatched:
            saved = LoopCheckpoints.open(client, user_id=1, task_id='live-checkpoint').load_checkpoint(1)
            assert saved['messages'] == messages
            assert saved['tool_rounds'][0]['status'] == 'done'
            assert saved['chassis']['next_round'] == 1
        dispatched.append(True)
        message = {'role': 'assistant', 'tool_calls': [{'id': 'call-1',
            'function': {'name': 'controlled_tool', 'arguments': '{}'}}]} if len(dispatched) == 1 else {'content': '{}'}
        return message, 'tool_calls' if len(dispatched) == 1 else 'stop', {'prompt_tokens': 1, 'completion_tokens': 1}
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', provider)
    def execute(*args, **kwargs):
        effects.append(True); return 'controlled-result', [], None, [], []
    monkeypatch.setattr(action, 'execute_paper_tool', execute)
    class ResultBudget:
        def __init__(self, **kwargs): pass
        def append(self, messages, *, tool_call_id, content, **kwargs):
            messages.append({'role': 'tool', 'tool_call_id': tool_call_id, 'content': content})
            return content
        def finish_round(self, round_index): pass
    monkeypatch.setattr(action, 'PaperToolResultBudgetV2', ResultBudget)
    task = {'user_id': 1, 'task_id': 'live-checkpoint', 'action': 'experiment',
        'direction': 'Scope', 'model': 'controlled-model', 'abort_event': threading.Event(), 'tool_rounds': []}
    epoch = SimpleNamespace(result_envelope='v2', wire_schemas=[], contract_documents_by_name={})
    if refuse == 'default_model_change':
        task['model'] = ''
        monkeypatch.setattr(action._lib, 'LLM_MODEL', 'controlled-model')
    if refuse not in (False, 'default_model_change'):
        with pytest.raises(StorageError): action._run_agent(task, empty_workspace('Scope'), epoch, [])
        expected = [] if refuse == 'intent_false_ack' else [True]
        assert dispatched == expected and effects == expected
        assert LoopCheckpoints.open(client, user_id=1, task_id='live-checkpoint').load_checkpoint(1) is None
    else:
        action._run_agent(task, empty_workspace('Scope'), epoch, [])
        assert len(dispatched) == 2 and effects == [True]
        with pytest.raises(StorageError):
            action._run_agent(task, empty_workspace('Scope'), epoch, [])
        assert len(dispatched) == 2 and effects == [True]


def test_next_admission_requires_acknowledged_previous_checkpoint():
    client = MemoryRecords(); calls, checkpoints, intent, material = setup(client)
    before = snapshot(); before['agent_dispatches'] = 2
    with pytest.raises(StorageError, match='checkpoint is unresolved'):
        calls.admit_model_call(ordinal=2, round_index=1, requested_model='controlled-model',
            request_digest='e' * 64, max_output=8000, usage_before=before)
    assert calls.load_model_intent(2) is None
    checkpoints.save_checkpoint(intent, material)
    calls.admit_model_call(ordinal=2, round_index=1, requested_model='controlled-model',
        request_digest='e' * 64, max_output=8000, usage_before=before)


@pytest.mark.parametrize('fault', ['request', 'intent', 'persistent', 'unresolved_intent',
    'provider', 'owner', 'abort', 'default_model', 'changed_rounds', 'nonretryable',
    'timeout', 'checkpoint_after_second_effect', 'prolonged', 'read_outage', 'abort_wait', 'hint'])
def test_live_unpaid_frontier_recovers_without_replaying_effects_or_resetting_budget(monkeypatch, fault):
    import threading
    from types import SimpleNamespace
    import lib.research.action as action
    from lib.research.model_accounting import ModelCalls, INTENT_NAMESPACE
    from lib.research.request_context import NAMESPACE as REQUEST_NAMESPACE
    from lib.research.execution_owner import NAMESPACE as OWNER_NAMESPACE
    from lib.research.workspace import empty_workspace
    from lib.llm_errors import AbortedError
    client = MemoryRecords(); dispatched = []; effects = []; failures = []; events = []
    task = {'user_id':1, 'task_id':'live-frontier-retry', 'action':'experiment',
        'direction':'Scope', 'model':'controlled-model', 'abort_event':threading.Event(),
        'logical_budget':{'token_budget':1000,'dispatch_budget':3}, 'tool_rounds':[]}
    epoch = SimpleNamespace(result_envelope='v2', wire_schemas=[], contract_documents_by_name={})
    original_command, original_query = client.command, client.query
    pending_read_failure = [False]
    owner_read_failures = [0]
    waits = []
    def wait(delay):
        waits.append(delay)
        if fault == 'abort_wait': task['abort_event'].set(); return True
        return task['abort_event'].is_set()
    monkeypatch.setattr(task['abort_event'], 'wait', wait)
    def command(operation, payload, command_id):
        if fault == 'checkpoint_after_second_effect':
            if payload['namespace'] == NAMESPACE and payload['value']['ordinal'] == 2:
                raise StorageError('database_busy', 'injected paid checkpoint failure', retryable=True)
            return original_command(operation, payload, command_id)
        target = REQUEST_NAMESPACE if fault == 'request' else INTENT_NAMESPACE
        second = (len(dispatched) == 1 and payload['namespace'] == target
                  and (target == REQUEST_NAMESPACE or payload['value'].get('ordinal') == 2))
        fail_again = fault == 'persistent' or (fault == 'prolonged' and len(failures) < 5)
        if second and fault != 'provider' and (not failures or fail_again):
            failures.append(True)
            if fault == 'persistent' and len(failures) == 3: task['abort_event'].set()
            if fault == 'read_outage': owner_read_failures[0] = 3
            if fault == 'unresolved_intent':
                original_command(operation, payload, command_id)
                pending_read_failure[0] = True
            if fault == 'owner':
                row = next(row for (namespace,key),row in client.rows.items() if namespace == OWNER_NAMESPACE)
                row['value']['nonce'] = '0' * 32
            if fault == 'abort': task['abort_event'].set()
            if fault == 'changed_rounds': task['tool_rounds'][0]['results'] = 'different live state'
            raise StorageError('database_timeout' if fault == 'timeout' else 'database_busy', 'injected unpaid frontier failure',
                               retryable=fault != 'nonretryable', retry_after_ms=9000 if fault == 'hint' else None)
        return original_command(operation, payload, command_id)
    def query(operation, payload):
        if owner_read_failures[0] and payload['namespace'] == OWNER_NAMESPACE:
            owner_read_failures[0] -= 1
            raise StorageError('database_unavailable', 'injected recovery read outage', retryable=True)
        if pending_read_failure[0] and payload['namespace'] == INTENT_NAMESPACE:
            pending_read_failure[0] = False
            raise StorageError('database_unavailable', 'injected admission ACK uncertainty', retryable=True)
        return original_query(operation, payload)
    monkeypatch.setattr(client, 'command', command); monkeypatch.setattr(client, 'query', query)
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    monkeypatch.setattr('lib.research.action_runtime.append_action_event', lambda task,event: events.append(deepcopy(event)))
    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: None)
    if fault == 'default_model':
        task['model'] = ''; monkeypatch.setattr(action._lib, 'LLM_MODEL', 'controlled-model')
    def provider(messages, **kwargs):
        assert kwargs['prefer_model'] == 'controlled-model'
        assert kwargs['strict_model'] is (fault != 'default_model')
        dispatched.append(True)
        if len(dispatched) == 1:
            if fault == 'default_model': monkeypatch.setattr(action._lib, 'LLM_MODEL', 'changed-default')
            return {'role':'assistant','tool_calls':[{'id':'call-1','function':{
                'name':'controlled_tool','arguments':'{}'}}]}, 'tool_calls', {'prompt_tokens':7,'completion_tokens':3}
        if fault == 'provider': raise StorageError('database_busy', 'injected paid provider failure', retryable=True)
        if fault == 'checkpoint_after_second_effect':
            return {'role':'assistant','tool_calls':[{'id':'call-2','function':{
                'name':'controlled_tool','arguments':'{}'}}]}, 'tool_calls', {'prompt_tokens':7,'completion_tokens':3}
        checkpoint = LoopCheckpoints.open(client,user_id=1,task_id=task['task_id']).load_checkpoint(1)
        assert messages == checkpoint['messages']
        return {'content':'{}'}, 'stop', {'prompt_tokens':7,'completion_tokens':3}
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', provider)
    def execute(*args, **kwargs):
        effects.append(True); return 'controlled-result', [], None, [], []
    monkeypatch.setattr(action, 'execute_paper_tool', execute)
    class ResultBudget:
        def __init__(self, **kwargs): pass
        def append(self, messages, *, tool_call_id, content, **kwargs):
            messages.append({'role':'tool','tool_call_id':tool_call_id,'content':content}); return content
        def finish_round(self, round_index): pass
    monkeypatch.setattr(action, 'PaperToolResultBudgetV2', ResultBudget)
    succeeds = fault in {'request','intent','default_model','timeout','prolonged','read_outage','hint'}
    if succeeds:
        action._run_agent(task, empty_workspace('Scope'), epoch, [])
    else:
        with pytest.raises(AbortedError if fault in {'abort','abort_wait','persistent'} else StorageError):
            action._run_agent(task, empty_workspace('Scope'), epoch, [])
    assert effects == ([True, True] if fault == 'checkpoint_after_second_effect' else [True])
    assert len(dispatched) == (2 if succeeds or fault in {'provider','checkpoint_after_second_effect'} else 1)
    recovery_events = [event for event in events if event.get('detail', '').startswith('Continuing from the saved step')]
    assert len(recovery_events) == (1 if succeeds else 0)
    if fault == 'prolonged': assert waits == [1,2,4,4,4]
    if fault == 'read_outage': assert waits == [1,2,4,4]
    if fault == 'hint': assert waits == [9]
    if fault == 'persistent': assert len(failures) == 3 and waits == [1,2]
    calls = ModelCalls.open(client,user_id=1,task_id=task['task_id'])
    assert calls.load_model_usage(1)['usage_snapshot']['calls'] == 1
    assert bool(calls.load_model_intent(2)) == (succeeds or fault in {'provider','unresolved_intent','checkpoint_after_second_effect'})
    if succeeds:
        usage = calls.load_model_usage(2)['usage_snapshot']
        assert usage['calls'] == usage['agent_dispatches'] == 2 and usage['total_tokens'] == 20
        assert usage['agent_token_budget'] == 1000 and usage['agent_dispatch_budget'] == 3
        assert task['_researchUsageSnapshot'] == usage


@pytest.mark.parametrize('fault',['missing_claim','missing_id','duplicate_id'])
def test_completed_tools_require_retained_unique_claims(fault):
    client = MemoryRecords(); _, checkpoints, intent, material = setup(client)
    if fault == 'missing_claim':material['chassis']['claimed_tool_call_ids'] = []
    elif fault == 'missing_id':material['tool_rounds'][0].pop('toolCallId')
    else:material['tool_rounds'].append(dict(material['tool_rounds'][0],roundNum=2))
    with pytest.raises(StorageError,match='identity fence'):checkpoints.save_checkpoint(intent,material)
    assert checkpoints.load_checkpoint(1) is None


def test_claimed_but_unexecuted_batch_members_remain_fenced():
    client = MemoryRecords(); _, checkpoints, intent, material = setup(client)
    material['chassis']['claimed_tool_call_ids'].append('reserved-batch-member')
    checkpoints.save_checkpoint(intent,material)
    restored, meter = checkpoints.restore_checkpoint(1)
    assert set(restored['chassis']['claimed_tool_call_ids']) == {'call-1','reserved-batch-member'}


def test_absent_claim_ledger_is_not_a_completed_checkpoint():
    client = MemoryRecords(); _, checkpoints, intent, material = setup(client)
    material['chassis'].pop('claimed_tool_call_ids')
    with pytest.raises(ValueError,match='claimed_tool_call_ids'):checkpoints.save_checkpoint(intent,material)


def test_semantically_corrupt_retained_claim_ledger_cannot_restore():
    client = MemoryRecords(); _, checkpoints, intent, material = setup(client)
    checkpoints.save_checkpoint(intent,material)
    material['chassis']['claimed_tool_call_ids'] = []
    digest, chars, parts = checkpoints._write_material(material,'loopMaterial')
    header = client.rows[NAMESPACE,checkpoints.prefix+'1']['value']
    header.update(material_digest=digest,material_chars=chars,parts=parts)
    with pytest.raises(StorageError,match='identity fence'):checkpoints.restore_checkpoint(1)
