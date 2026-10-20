"""Durable logical dispatch fences and cumulative usage before tool execution."""
from copy import deepcopy
import pytest

from lib.research.model_accounting import ModelCalls, INTENT_NAMESPACE, USAGE_NAMESPACE
from lib.research.action_observations import _digest
from lib.storage.errors import StorageError
from lib.paper.agent_usage import PaperAgentUsageMeter
from tests.research.test_research_action_observations import MemoryRecords, intent_repository
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def repository(client):
    intent_repository(client)
    return ModelCalls.open(client, user_id=1, task_id='research_action_observed')


def admit(repo, ordinal=1):
    before = snapshot(ordinal - 1); before['agent_dispatches'] = ordinal
    return repo.admit_model_call(ordinal=ordinal, round_index=ordinal, requested_model='controlled-model',
                                request_digest='d' * 64, max_output=8000, usage_before=before)


def snapshot(ordinal=1):
    # Synthetic accounting only: this helper fabricates an arbitrary ordinal, so
    # it must not carry an envelope that would refuse the very dispatch it is
    # describing.
    meter = PaperAgentUsageMeter('research-action')
    for _index in range(ordinal):
        meter.begin_dispatch()
        meter.observe_agent_round(None, {})
    return meter.snapshot()


def test_missing_usage_stays_unresolved_and_neither_same_nor_next_slot_can_dispatch():
    client = MemoryRecords(); repo = repository(client); intent = admit(repo)
    page = repo.model_calls_page()
    assert page['calls'][0]['state'] == 'unresolved_usage'
    assert page['calls'][0]['replay_allowed'] is False
    with pytest.raises(StorageError): admit(repo)
    with pytest.raises(StorageError, match='usage is unresolved'): admit(repo, 2)
    repo.record_model_usage(intent, snapshot(), usage_mapping_received=False)
    stored = repo.load_model_usage(1)
    assert stored['usage_snapshot']['agent_dispatches'] == 1
    assert stored['usage_mapping_received'] is False
    admit(repo, 2)


def test_usage_checkpoint_is_exact_immutable_and_matches_admitted_counter():
    client = MemoryRecords(); repo = repository(client); intent = admit(repo)
    value = snapshot()
    repo.record_model_usage(intent, value, usage_mapping_received=False)
    repo.record_model_usage(intent, deepcopy(value), usage_mapping_received=False)
    assert repo.load_model_usage(1)['usage_snapshot'] == value
    changed = deepcopy(value); changed['total_tokens'] = 999
    with pytest.raises(StorageError): repo.record_model_usage(intent, changed, usage_mapping_received=False)
    with pytest.raises(ValueError): repo.record_model_usage(intent, snapshot(0), usage_mapping_received=False)


def test_lost_ack_requires_exact_own_admission_and_usage():
    client = MemoryRecords(); repo = repository(client); client.lose_ack = True
    intent = admit(repo); repo.record_model_usage(intent, snapshot(), usage_mapping_received=False)
    assert repo.load_model_usage(1)['admission_id'] == intent['admission_id']
    with pytest.raises(StorageError): admit(repo)


def test_model_usage_survives_selected_store_restart_without_resetting_counters(storage):
    supervisor, _ = storage
    repo = repository(supervisor.client)
    first = admit(repo); repo.record_model_usage(first, snapshot(), usage_mapping_received=False)
    second = admit(repo, 2); repo.record_model_usage(second, snapshot(2), usage_mapping_received=False)
    supervisor.stop(); supervisor.start()
    reopened = ModelCalls.open(supervisor.client, user_id=1, task_id='research_action_observed')
    assert reopened.load_model_usage(2)['usage_snapshot']['agent_dispatches'] == 2
    assert reopened.load_model_usage(2)['usage_snapshot']['unmetered_calls'] == 2
    assert len(reopened.model_calls_page()['calls']) == 2
    with pytest.raises(StorageError): admit(reopened)


@pytest.mark.parametrize('refusal', ['none', 'owner', 'context', 'intent', 'response', 'usage'])
def test_live_dispatch_accounts_before_tools_and_durability_failure_blocks_progress(monkeypatch, refusal):
    import threading
    from types import SimpleNamespace
    import lib.research.action as action
    from lib.research.workspace import empty_workspace
    client = MemoryRecords(); provider_calls = []; tools = []
    original_command = client.command
    def command(operation, payload, key):
        from lib.research.execution_owner import NAMESPACE as OWNER_NAMESPACE
        from lib.research.request_context import NAMESPACE as CONTEXT_NAMESPACE
        from lib.research.model_response import NAMESPACE as RESPONSE_NAMESPACE
        failed_namespace = CONTEXT_NAMESPACE if refusal == 'context' else (INTENT_NAMESPACE if refusal == 'intent' else (RESPONSE_NAMESPACE if refusal == 'response' else USAGE_NAMESPACE))
        if (refusal == 'owner' and payload['namespace'] == OWNER_NAMESPACE) or (refusal not in {'none', 'owner'} and payload['namespace'] == failed_namespace):
            raise StorageError('database_unavailable', 'injected model checkpoint refusal')
        return original_command(operation, payload, key)
    client.command = command
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    def provider(*args, **kwargs):
        from lib.research.request_context import RequestContexts
        saved = RequestContexts.open(client, user_id=1, task_id='model-live').load_request(1)
        assert saved['messages'] == [] and saved['tools'] == []
        assert saved['max_output'] == kwargs['max_tokens']
        assert saved['action_definition']['model_override'] == kwargs['prefer_model']
        assert saved['action_definition']['logical_budget'] == {'token_budget':0,'dispatch_budget':0}
        provider_calls.append(True)
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', provider)
    monkeypatch.setattr('lib.llm.stream_result.ensure_provider_stream_result', lambda result: result)
    monkeypatch.setattr(action, '_messages', lambda *args: [])
    monkeypatch.setattr(action, 'make_paper_exec_shim', lambda **kwargs: None)
    def loop(**kwargs):
        meter = kwargs['usage_meter']; meter.begin_dispatch()
        kwargs['dispatch'](1, [])
        meter.observe_agent_round({'prompt_tokens': 100, 'completion_tokens': 5}, {})
        kwargs['on_round_result'](1, {'content': '{}'}, None,
                                  {'prompt_tokens': 100, 'completion_tokens': 5})
        restored = ModelCalls.open(client, user_id=1, task_id='model-live')
        assert restored.load_model_usage(1)['usage_snapshot']['total_tokens'] == 105
        from lib.research.model_response import ModelResponses
        reply = ModelResponses.open(client, user_id=1, task_id='model-live').load_response(1)
        assert reply['message'] == {'content': '{}'} and reply['finish'] is None
        tools.append(True)
        return SimpleNamespace(completed=True)
    monkeypatch.setattr(action, 'run_guarded_paper_agent_loop', loop)
    task = {'user_id': 1, 'model': 'controlled-model', 'task_id': 'model-live', 'action': 'experiment',
            'direction': 'Scope', 'abort_event': threading.Event(), 'tool_rounds': []}
    epoch = SimpleNamespace(result_envelope='v2', wire_schemas=[])
    if refusal == 'none':
        action._run_agent(task, empty_workspace('Scope'), epoch, [])
        assert provider_calls == [True] and tools == [True]
    else:
        with pytest.raises(StorageError): action._run_agent(task, empty_workspace('Scope'), epoch, [])
        assert bool(provider_calls) == (refusal in {'response', 'usage'}) and tools == []


def test_new_model_call_cannot_reset_previous_accounting_before_paid_dispatch():
    client = MemoryRecords(); repo = repository(client)
    first = admit(repo); repo.record_model_usage(first, snapshot(), usage_mapping_received=False)
    reset = snapshot(0); reset['agent_dispatches'] = 2; reset['calls'] = 1
    with pytest.raises(ValueError, match='cannot reset'):
        repo.admit_model_call(ordinal=2, round_index=2, requested_model='controlled-model',
            request_digest='d' * 64, max_output=8000, usage_before=reset)
    assert repo.load_model_intent(2) is None


def test_concurrent_model_admission_has_one_winner_on_selected_store(storage):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    supervisor, _ = storage; repo = repository(supervisor.client)
    barrier = threading.Barrier(2); lock = threading.Lock(); reads = [0]
    class RacingClient:
        def query(self, operation, payload):
            value = supervisor.client.query(operation, payload)
            if operation == 'record.get' and payload['namespace'] == INTENT_NAMESPACE:
                with lock:
                    reads[0] += 1; wait = reads[0] <= 2
                if wait: barrier.wait(timeout=5)
            return value
        def command(self, *args): return supervisor.client.command(*args)
    def attempt():
        raced = ModelCalls.open(RacingClient(), user_id=1, task_id='research_action_observed')
        try: admit(raced); return True
        except StorageError: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]


@pytest.mark.parametrize('field', ['priced_calls', 'prompt_tokens', 'completion_tokens',
    'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', 'cost_usd', 'cost_cny'])
@pytest.mark.parametrize('mutation', ['decrease', 'remove'])
def test_checkpoint_cannot_reduce_or_drop_previously_accounted_usage(field, mutation):
    repo = repository(MemoryRecords())
    before = snapshot(0); before['agent_dispatches'] = 1; before[field] = 5
    intent = repo.admit_model_call(ordinal=1, round_index=1, requested_model='controlled-model',
        request_digest='d' * 64, max_output=8000, usage_before=before)
    after = deepcopy(before); after['calls'] = 1
    if mutation == 'remove': after.pop(field)
    else: after[field] = 4
    with pytest.raises(ValueError, match='cannot decrease'):
        repo.record_model_usage(intent, after, usage_mapping_received=True)
    assert repo.load_model_usage(1) is None
    after[field] = 6
    repo.record_model_usage(intent, after, usage_mapping_received=True)
    assert repo.load_model_usage(1)['usage_snapshot'][field] == 6


@pytest.mark.parametrize('field,value,replacement', [
    ('cost_estimated', True, False), ('budget_ignored', True, False),
    ('models', ['previous-model'], ['current-model'])])
def test_checkpoint_preserves_uncertainty_violation_and_model_history(field, value, replacement):
    repo = repository(MemoryRecords())
    before = snapshot(0); before['agent_dispatches'] = 1; before[field] = value
    intent = repo.admit_model_call(ordinal=1, round_index=1, requested_model='controlled-model',
        request_digest='d' * 64, max_output=8000, usage_before=before)
    after = deepcopy(before); after['calls'] = 1; after[field] = replacement
    with pytest.raises(ValueError, match='cannot erase'):
        repo.record_model_usage(intent, after, usage_mapping_received=True)
    assert repo.load_model_usage(1) is None
