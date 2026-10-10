"""The same model-context operation must preserve proofs on both authorities."""
import json
import hashlib
import os
from pathlib import Path
import uuid

import pytest
import jsonschema

from lib.storage.model_continuation import settled_witness
from tests.tofudb.test_tofudb_differential import (
    tofudb_binary, tofudb_daemon, LegacyDriver,
)

pytest_plugins = ('tests._chat_sidecar',)
# serial: builds/spawns the native tofu-db engine (flock-serialized
# cargo build); queued behind the build, parallel-lane tests hit
# their 300s timeout — dedicated no-xdist phase per TESTING_STRATEGY.
pytestmark = [pytest.mark.unit, pytest.mark.serial]
@pytest.fixture(params=['sqlite', 'tofudb'] + (['postgres'] if os.getenv('TOFU_STORAGE_TEST_POSTGRES') == '1' else []))
def context_driver(request, tmp_path):
    if request.param == 'tofudb':
        yield request.getfixturevalue('tofudb_daemon')
    elif request.param == 'postgres':
        from lib.storage import StorageSupervisor, StorageError
        from tests.tofudb.test_tofudb_differential import Outcome, _normalize
        supervisor = StorageSupervisor(project_root=tmp_path, backend='postgres', startup_timeout=60)
        supervisor.start()
        class Driver:
            def execute(self, operation, payload, command_id):
                try:
                    result = (supervisor.client.query(operation, payload) if command_id is None
                              else supervisor.client.command(operation, payload, command_id))
                    return Outcome(ok=True, value=_normalize(result))
                except StorageError as exc:
                    return Outcome(ok=False, error_code=exc.code, error_message=str(exc))
        try:
            yield Driver()
        finally:
            supervisor.stop()
    else:
        request.getfixturevalue('chat_sidecar')
        yield LegacyDriver()


def invoke(driver, operation, payload, *, command=False):
    if payload.get('turn_id') in ('u1', 'a1', 'u2', 'a2'):
        payload = payload | {'turn_id': payload['conversation_id'] + '-' + payload['turn_id']}
    result = driver.execute(operation, payload, uuid.uuid4().hex if command else None)
    assert result.ok, (operation, result)
    return result.value


def seed(driver):
    conv = 'context-' + uuid.uuid4().hex
    base = {'conversation_id': conv, 'user_id': 11}
    invoke(driver, 'conversation.create', {'conv_id': conv, 'user_id': 11}, command=True)
    def append(key, actor, content):
        invoke(driver, 'turn.append_settled', base | {
            'turn_id': key, 'command_id': uuid.uuid4().hex, 'actor': actor,
            'projection': {'content': content}}, command=True)
    append('u1', 'human', 'original request')
    append('a1', 'assistant', 'original answer')
    read = invoke(driver, 'turn.context.read', base | {'turn_id': 'a1'})
    assert read['covered'] == 0
    cached = {'turns': [settled_witness(read['turns'][-1])],
              'historyProof': read['historyProof'], 'policy': 'test',
              'messages': [{'role': 'assistant', 'content': 'compacted retained history'}]}
    assert invoke(driver, 'model_continuation.put', base | {
        'lane_id': 'main', 'projection': cached}, command=True)
    append('u2', 'human', 'new request')
    append('a2', 'assistant', 'new answer')
    return base, cached


def validate_result(value):
    schema = json.loads((Path(__file__).parents[2] / 'contracts/model_context_v1.schema.json').read_text())
    jsonschema.validate(value, {'$ref': '#/$defs/result', '$defs': schema['$defs']})


def test_warm_read_only_returns_suffix_and_cold_rebuild_is_explicit(context_driver):
    base, cached = seed(context_driver)
    warm = invoke(context_driver, 'turn.context.read', base | {'turn_id': 'a2'})
    validate_result(warm)
    assert warm['covered'] == 2
    assert [row['turnId'].rsplit('-', 1)[-1] for row in warm['turns']] == ['u2', 'a2']
    assert warm['continuation'] == cached
    assert warm['historyProof']['count'] == 3
    cold = invoke(context_driver, 'turn.context.read', base | {'turn_id': 'a2', 'replay': False})
    assert len(cold['turns']) == 4 and cold['covered'] == 0
    assert cold['historyProof'] == warm['historyProof']
    digest = hashlib.sha256()
    for row in cold['turns'][:-1]:
        digest.update((json.dumps([row['turnId'], row['parentTurnId'], row['status'],
            row['currentAttemptId'], row['projectionRevision']],
            ensure_ascii=False, separators=(',', ':')) + '\n').encode())
    assert cold['historyProof']['digest'] == digest.hexdigest()
    assert invoke(context_driver, 'turn.context.read', base | {'turn_id': 'missing'}) is None


def test_large_model_context_pages_preserve_tool_history_and_owner(context_driver):
    base, _ = seed(context_driver)
    invoke(context_driver, 'model_continuation.delete', base | {'lane_id': 'main'}, command=True)
    # Tool results are model-visible; dropping rounds or truncating a Turn is
    # not an acceptable response to a cumulative 64 MiB history.
    large = 'x' * 1_000_000
    for index in range(69):
        invoke(context_driver, 'turn.append_settled', base | {
            'turn_id': f'large-{index}', 'actor': 'assistant',
            'command_id': uuid.uuid4().hex,
            'projection': {'content': f'answer-{index}',
                           'toolRounds': [{'toolName': 'read', 'toolContent': large}]},
        }, command=True)
    first = invoke(context_driver, 'turn.context.read', base | {'turn_id': 'large-68', 'replay': False})
    assert first['nextTurnId'] and len(first['turns']) < 73
    validate_result(first)
    forbidden = context_driver.execute('turn.context.read', base | {
        'user_id': 12, 'turn_id': 'large-68',
        'after_turn_id': first['nextTurnId']}, None)
    assert not forbidden.ok or forbidden.value is None
    malformed = context_driver.execute('turn.context.read', base | {
        'turn_id': 'large-68', 'after_turn_id': 'unknown'}, None)
    assert not malformed.ok
    from lib.storage.model_continuation import ModelContinuationStore
    cursors = []
    class Client:
        def query(self, operation, payload, **kwargs):
            result = invoke(context_driver, operation, payload)
            cursors.append(payload.get('after_turn_id'))
            if len(cursors) == 1:
                # Settlement/translation enrichment may race queued startup.
                # Even a genuine model-visible edit must trigger a clean read,
                # never return the prefix already read before this commit.
                original = invoke(context_driver, 'turn.get', base | {'turn_id': 'u1'})
                invoke(context_driver, 'turn.projection.update', base | {
                    'turn_id': 'u1', 'projection': {'content': 'edited while paging'},
                    'expected_projection_revision': original['projectionRevision']}, command=True)
            return result
    context = ModelContinuationStore(client=Client()).read_context(
        user_id=11, conversation_id=base['conversation_id'],
        turn_id='large-68', replay=False)
    assert len(cursors) == 4 and cursors[0] is None and cursors[2] is None
    assert context['turns'][0]['projection']['content'] == 'edited while paging'
    assert len(context['turns']) == 73
    assert context['nextTurnId'] is None
    assert [row['projection']['toolRounds'][0]['toolContent']
            for row in context['turns'] if row['turnId'].startswith('large-')] == [large] * 69


def test_model_context_page_proof_mismatch_fails_closed(monkeypatch):
    from lib.storage.errors import StorageError
    from lib.storage.model_continuation import ModelContinuationStore
    pages = [
        {'supported': True, 'covered': 0, 'turns': [{'turnId': 'a'}],
         'historyProof': {'version': 1, 'count': 2, 'digest': 'a' * 64},
         'continuation': None, 'proofEligible': True, 'nextTurnId': 'a'},
        {'supported': True, 'covered': 0, 'turns': [{'turnId': 'b'}],
         'historyProof': {'version': 1, 'count': 2, 'digest': 'b' * 64},
         'continuation': None, 'proofEligible': True, 'nextTurnId': None},
    ]
    pages *= 3  # Persistent invalidation exhausts the bounded whole-read restarts.
    class Client:
        def query(self, operation, payload, **kwargs):
            return pages.pop(0)
    with pytest.raises(StorageError, match='changed during paging') as error:
        ModelContinuationStore(client=Client()).read_context(
            user_id=11, conversation_id='conv', turn_id='b')
    assert error.value.code == 'database_conflict'


def test_edit_invalidates_prefix_before_any_cached_text_is_returned(context_driver):
    base, _ = seed(context_driver)
    original = invoke(context_driver, 'turn.get', base | {'turn_id': 'u1'})
    invoke(context_driver, 'turn.projection.update', base | {
        'turn_id': 'u1', 'projection': {'content': 'edited request'},
        'expected_projection_revision': original['projectionRevision']}, command=True)
    read = invoke(context_driver, 'turn.context.read', base | {'turn_id': 'a2'})
    assert read['covered'] == 0 and read['continuation'] is None
    assert read['turns'][0]['projection']['content'] == 'edited request'


def test_owner_and_malformed_replay_do_not_return_history(context_driver):
    base, _ = seed(context_driver)
    foreign = context_driver.execute('turn.context.read', base | {'turn_id': base['conversation_id'] + '-a2', 'user_id': 12}, None)
    assert not foreign.ok or foreign.value is None
    invalid = context_driver.execute('turn.context.read', base | {'turn_id': base['conversation_id'] + '-a2', 'replay': 'yes'}, None)
    assert not invalid.ok


def test_legacy_checkpoint_is_available_for_full_content_validation(context_driver):
    base, _ = seed(context_driver)
    invoke(context_driver, 'model_continuation.delete', base | {'lane_id': 'main'}, command=True)
    rows = invoke(context_driver, 'turn.context.read', base | {'turn_id': 'a1', 'replay': False})['turns']
    legacy = {'turns': [settled_witness(row) for row in rows],
              'messages': [{'role': 'assistant', 'content': 'legacy retained summary'}]}
    assert invoke(context_driver, 'model_continuation.put', base | {
        'lane_id': 'main', 'projection': legacy}, command=True)
    read = invoke(context_driver, 'turn.context.read', base | {'turn_id': 'a2'})
    assert read['covered'] == 0 and len(read['turns']) == 4
    assert read['continuation'] == legacy


def test_application_restores_prefix_and_reloads_full_history_on_policy_change(context_driver, monkeypatch):
    from lib import turn_lifecycle
    from lib.tasks_pkg.model_replay import policy_witness
    base, cached = seed(context_driver)
    invoke(context_driver, 'model_continuation.delete', base | {'lane_id': 'main'}, command=True)
    config = {'model': 'kimi-k3', 'excludeLast': True}
    cached['policy'] = policy_witness(config)
    assert invoke(context_driver, 'model_continuation.put', base | {
        'lane_id': 'main', 'projection': cached}, command=True)
    pair = invoke(context_driver, 'turn.create_pair', base | {
        'command_id': uuid.uuid4().hex, 'input_projection': {'content': 'next live request'},
        'config': config}, command=True)
    operations = []
    class Client:
        def query(self, operation, payload, **kwargs):
            operations.append((operation, payload.get('replay')))
            return invoke(context_driver, operation, payload)
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client())
    restored = turn_lifecycle.build_api_messages(base['conversation_id'], pair['turn']['turnId'],
                                               dict(config), user_id=11)
    assert restored[0]['content'] == 'compacted retained history'
    assert restored[-1]['content'] == 'next live request'
    assert operations == [('turn.context.read', True)]
    operations.clear()
    rebuilt = turn_lifecycle.build_api_messages(base['conversation_id'], pair['turn']['turnId'],
                                               config | {'systemPrompt': 'changed policy'}, user_id=11)
    assert any(message['content'] == 'original request' for message in rebuilt)
    assert not any(message['content'] == 'compacted retained history' for message in rebuilt)
    assert operations == [('turn.context.read', True), ('turn.context.read', False)]


@pytest.mark.parametrize('proof', [None, [], 'bad', {'version': True, 'count': 0, 'digest': '0' * 64},
    {'version': 1, 'count': True, 'digest': '0' * 64},
    {'version': 1, 'count': 10000, 'digest': '0' * 64},
    {'version': 1, 'count': 1, 'digest': 'invalid'}])
def test_malformed_proof_cannot_replace_a_valid_checkpoint(context_driver, proof):
    base, cached = seed(context_driver)
    bad = cached | {'historyProof': proof}
    assert invoke(context_driver, 'model_continuation.put', base | {
        'lane_id': 'main', 'projection': bad}, command=True) is False
    assert invoke(context_driver, 'turn.context.read', base | {'turn_id': 'a2'})['covered'] == 2
