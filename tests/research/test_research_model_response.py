"""Exact replies survive restart, corruption is refused, inspection is read-only."""
from copy import deepcopy
import pytest

from lib.research.model_response import ModelResponses, NAMESPACE
from lib.research.request_context import PART_NAMESPACE
from lib.research.model_accounting import ModelCalls
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_model_accounting import repository, snapshot
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def setup(client):
    calls = repository(client)
    before = snapshot(0); before['agent_dispatches'] = 1
    intent = calls.admit_model_call(ordinal=1, round_index=1, requested_model='controlled',
        request_digest='d' * 64, max_output=8000, usage_before=before, response_snapshot_version=1)
    return calls, ModelResponses.open(client, user_id=1, task_id='research_action_observed'), intent


def message():
    return {'content': '模型原始成果🧪' * 10000,
            'tool_calls': [{'id': 'proposal', 'function': {'name': 'run_command', 'arguments': '{}'}}]}


def test_response_is_readable_before_usage_ack_without_replay_and_is_immutable():
    client = MemoryRecords(); calls, replies, intent = setup(client)
    original = message(); digest = replies.save_response(intent, original, 'tool_calls')
    original['content'] = 'changed'
    restored = replies.load_response(1)
    assert restored == {'message': message(), 'finish': 'tool_calls'}
    assert calls.load_model_usage(1) is None
    calls.record_model_usage(intent, snapshot(), usage_mapping_received=False, response_digest=digest)
    with pytest.raises(StorageError): replies.save_response(intent, {'content': 'replacement'}, 'stop')
    client.command = lambda *args: pytest.fail('Inspecting a reply must not execute or write')
    assert replies.load_response(1) == restored
    assert calls.model_calls_page()['calls'][0]['replay_allowed'] is False


@pytest.mark.parametrize('fault', ['content', 'missing', 'order', 'admission'])
def test_modified_response_never_returns_partial_reply(fault):
    client = MemoryRecords(); calls, replies, intent = setup(client)
    digest = replies.save_response(intent, message(), 'tool_calls')
    calls.record_model_usage(intent, snapshot(), usage_mapping_received=False, response_digest=digest)
    manifest = client.rows[(NAMESPACE, replies.prefix + '1')]['value']
    key = (PART_NAMESPACE, replies.prefix + manifest['parts'][0])
    if fault == 'content': client.rows[key]['value']['content'] = 'z' * len(client.rows[key]['value']['content'])
    elif fault == 'missing': del client.rows[key]
    elif fault == 'order': manifest['parts'].reverse()
    else: manifest['admission_id'] = '0' * 32
    with pytest.raises(StorageError): replies.load_response(1)


def test_usage_requires_an_acknowledged_matching_reply():
    client = MemoryRecords(); calls, replies, intent = setup(client)
    with pytest.raises(ValueError): calls.record_model_usage(intent, snapshot(), usage_mapping_received=False)
    with pytest.raises(StorageError):
        calls.record_model_usage(intent, snapshot(), usage_mapping_received=False, response_digest='f' * 64)
    assert calls.load_model_usage(1) is None


def test_acknowledged_reply_missing_manifest_is_not_legacy_absence():
    client = MemoryRecords(); calls, replies, intent = setup(client)
    digest = replies.save_response(intent, message(), 'stop')
    calls.record_model_usage(intent, snapshot(), usage_mapping_received=False, response_digest=digest)
    del client.rows[(NAMESPACE, replies.prefix + '1')]
    with pytest.raises(StorageError, match='response is missing'): replies.load_response(1)


def test_response_and_usage_survive_selected_storage_restart(storage):
    supervisor, _ = storage
    calls, replies, intent = setup(supervisor.client)
    digest = replies.save_response(intent, message(), 'tool_calls')
    calls.record_model_usage(intent, snapshot(), usage_mapping_received=False, response_digest=digest)
    supervisor.stop(); supervisor.start()
    reopened = ModelResponses.open(supervisor.client, user_id=1, task_id='research_action_observed')
    assert reopened.load_response(1) == {'message': message(), 'finish': 'tool_calls'}


def test_missing_reply_for_unfinished_call_stays_unknown():
    client = MemoryRecords(); calls, replies, intent = setup(client)
    assert replies.load_response(1) is None
    assert ModelResponses.open(client, user_id=2, task_id='research_action_observed') is None
