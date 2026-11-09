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

pytest_plugins = ('tests._chat_sidecar', 'tests.support.semantic_authorities')
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


def test_deferred_input_verdict_rebinds_the_replay_proof(context_driver, monkeypatch):
    """The worker records the input's translation verdict after the command pinned
    the proof. Publishing that pinned proof names a history that never existed, so
    the next turn rebuilds cold; the re-pinned proof lets it restore the prefix."""
    from lib.storage.model_continuation import ModelContinuationStore
    from lib.tasks_pkg import model_replay
    from lib.tasks_pkg.input_translation import SettledInput

    base, _ = seed(context_driver)
    conv = base['conversation_id']

    class Client:
        def query(self, operation, payload, **kwargs):
            return invoke(context_driver, operation, payload)

    store = ModelContinuationStore(client=Client())
    monkeypatch.setattr(model_replay, 'ModelContinuationStore', lambda: store)

    def append(key, actor, content):
        invoke(context_driver, 'turn.append_settled', base | {
            'turn_id': f'{conv}-{key}', 'command_id': uuid.uuid4().hex, 'actor': actor,
            'projection': {'content': content}}, command=True)
        return f'{conv}-{key}'

    input_id, target_id = append('u3', 'human', '原文'), append('a3', 'assistant', 'third')
    command_read = store.read_context(user_id=11, conversation_id=conv, turn_id=target_id)
    assert command_read['covered'] == 2
    config = {}
    model_replay.restore_prefix(command_read['turns'], config, conversation_id=conv,
                                user_id=11, validated_context=command_read)
    pinned = config['_modelReplayHistoryProof']

    authored = invoke(context_driver, 'turn.get', base | {'turn_id': input_id})
    invoke(context_driver, 'turn.projection.update', base | {
        'turn_id': input_id, 'expected_projection_revision': authored['projectionRevision'],
        'projection': {'content': 'Translated', 'originalContent': '原文'}}, command=True)
    settled = invoke(context_driver, 'turn.get', base | {'turn_id': input_id})['projectionRevision']
    task = {'config': config, 'convId': conv, '_userId': 11, '_turnId': target_id,
            'messages': [{'role': 'user', 'content': 'Translated'}]}

    model_replay.rebind_history_proof(task, SettledInput(input_id, settled))

    after_verdict = store.read_context(user_id=11, conversation_id=conv, turn_id=target_id)
    assert config['_modelReplayHistoryProof'] == after_verdict['historyProof'] != pinned
    terminal = after_verdict['turns'][-1]
    next_target = (append('u4', 'human', 'next'), append('a4', 'assistant', 'next answer'))[1]
    covered = {}
    for name, proof in (('pinned', pinned), ('rebound', config['_modelReplayHistoryProof'])):
        invoke(context_driver, 'model_continuation.delete', base | {'lane_id': 'main'}, command=True)
        assert invoke(context_driver, 'model_continuation.put', base | {
            'lane_id': 'main', 'projection': {
                'turns': [settled_witness(terminal)], 'historyProof': proof, 'policy': 'test',
                'messages': [{'role': 'assistant', 'content': 'retained'}]}}, command=True)
        read = invoke(context_driver, 'turn.context.read', base | {'turn_id': next_target})
        covered[name] = (read['covered'], read['continuationStatus'])
    assert covered == {'pinned': (0, 'history_changed'), 'rebound': (6, 'available')}


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


def test_application_restores_prefix_and_reloads_full_history_on_policy_change(context_driver, monkeypatch, caplog):
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
    changed_config = config | {'systemPrompt': 'changed policy'}
    rebuilt = turn_lifecycle.build_api_messages(base['conversation_id'], pair['turn']['turnId'],
                                               changed_config, user_id=11)
    assert changed_config['_modelReplayStatus'] == 'policy_changed'
    assert 'restore=policy_changed fallback=full_history' in caplog.text
    assert any(message['content'] == 'original request' for message in rebuilt)
    assert not any(message['content'] == 'compacted retained history' for message in rebuilt)
    assert operations == [('turn.context.read', True), ('turn.context.read', False)]


    class DisappearingTargetClient(Client):
        def query(self, operation, payload, **kwargs):
            # The target may disappear between warm admission and a cold reread.
            if payload.get('replay') is False:
                return None
            return super().query(operation, payload, **kwargs)

    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: DisappearingTargetClient())
    disappeared_config = config | {'systemPrompt': 'changed policy'}
    assert turn_lifecycle.build_api_messages(
        base['conversation_id'], pair['turn']['turnId'], disappeared_config, user_id=11) is None
    assert disappeared_config['_modelReplayStatus'] == 'policy_changed'


@pytest.mark.parametrize('proof', [None, [], 'bad', {'version': True, 'count': 0, 'digest': '0' * 64},
    {'version': 1, 'count': True, 'digest': '0' * 64},
    {'version': 1, 'count': 2**53, 'digest': '0' * 64},
    {'version': 1, 'count': 1, 'digest': 'invalid'}])
def test_malformed_proof_cannot_replace_a_valid_checkpoint(context_driver, proof):
    base, cached = seed(context_driver)
    bad = cached | {'historyProof': proof}
    assert invoke(context_driver, 'model_continuation.put', base | {
        'lane_id': 'main', 'projection': bad}, command=True) is False
    assert invoke(context_driver, 'turn.context.read', base | {'turn_id': 'a2'})['covered'] == 2


# Paged Turns (docs/STORAGE.md, "Paged Turn storage", Decision 6): a Turn whose
# turn.projection.page plan has more than one page travels in the context read
# as its page-0 head; the client reads its pages pinned to the head's revision.
# Native runs when TOFUDB_BIN names a source-matched engine, and skips otherwise.
_PAGED_CONFIG = {'model': 'kimi-k3', 'excludeLast': True}
_PAGED_ROUNDS = 5
_PAGED_ROUND_BYTES = 1 << 20
# Multi-byte characters, quotes, backslashes and control characters cost more
# encoded bytes than code points.
_PAGED_TEXT_UNIT = 'Résumé "quoted" \\ path\n\t 🚀 '


def _paged_projection():
    """Tool rounds whose derived segments mirror them: more than one projection page."""
    return {
        'content': 'final answer ' + _PAGED_TEXT_UNIT * 4000,
        'thinking': _PAGED_TEXT_UNIT * 2000,
        'toolRounds': [{
            'toolCallId': f'call-{index}', 'toolName': 'read_file',
            'roundNum': index, 'llmRound': index,
            'toolArgs': {'path': f'src/module_{index}.py'},
            'toolContent': f'{index}:' + 'r' * _PAGED_ROUND_BYTES, 'status': 'done',
        } for index in range(_PAGED_ROUNDS)],
        'apiRounds': [{'round': index, 'inputTokens': 1000 + index, 'outputTokens': 10}
                      for index in range(_PAGED_ROUNDS)],
    }


def _paged_history(semantic_authority, monkeypatch):
    """A request, a paged reply, then a live input/target pair after it."""
    from lib.turn_lifecycle import append_settled_turn

    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    conversation = 'paged-context-' + uuid.uuid4().hex[:12]
    append_settled_turn(
        conversation, command_id=uuid.uuid4().hex, actor='human', user_id=owner,
        projection={'content': 'Inspect every module'},
        conversation_defaults={'allowCreate': True})
    paged = append_settled_turn(
        conversation, command_id=uuid.uuid4().hex, actor='assistant', user_id=owner,
        projection=_paged_projection())['turn']
    command_id = uuid.uuid4().hex
    pair = client(write=True).command('turn.create_pair', {
        'conversation_id': conversation, 'user_id': owner, 'command_id': command_id,
        'input_projection': {'content': 'Summarize every module'},
        'config': dict(_PAGED_CONFIG)}, command_id)
    identity = {'user_id': owner, 'conversation_id': conversation}
    return client, identity, paged['turnId'], pair['turn']['turnId']


class _Reader:
    """The authority's client, recording each query after an optional hook."""

    def __init__(self, inner, before=None):
        self.inner, self.before, self.calls = inner, before, []

    def query(self, operation, payload, **kwargs):
        if self.before is not None:
            self.before(operation, payload)
        self.calls.append((operation, dict(payload)))
        return self.inner.query(operation, payload, **kwargs)

    def command(self, *args, **kwargs):
        return self.inner.command(*args, **kwargs)


class _WholeTurnReader(_Reader):
    """The whole-Turn context read: every page-0 head replaced by its turn.get."""

    def query(self, operation, payload, **kwargs):
        result = super().query(operation, payload, **kwargs)
        if operation != 'turn.context.read' or not (result or {}).get('supported'):
            return result
        identity = {key: payload[key] for key in ('user_id', 'conversation_id')}
        turns = []
        for row in result['turns']:
            head = row.get('pagedTurn')
            if head is not None:
                row = self.inner.query('turn.get', identity | {'turn_id': head['turnId']})
                assert row['projectionRevision'] == head['projectionRevision']
            turns.append(row)
        return result | {'turns': turns}


def _read_context(reader, identity, turn_id, *, replay=False):
    from lib.storage.model_continuation import ModelContinuationStore
    return ModelContinuationStore(client=reader).read_context(
        **identity, turn_id=turn_id, replay=replay)


def _restarts(reader):
    return sum(1 for operation, payload in reader.calls
               if operation == 'turn.context.read' and 'after_turn_id' not in payload)


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native', 'postgres'], indirect=True)
def test_a_turn_above_one_page_assembles_the_whole_turn_context(semantic_authority, monkeypatch):
    from lib import turn_lifecycle
    from lib.tasks_pkg.model_replay import _history_fingerprint, policy_witness

    client, identity, paged_id, target_id = _paged_history(semantic_authority, monkeypatch)
    whole_turn = client().query('turn.get', identity | {'turn_id': paged_id})
    first_page = client().query('turn.projection.page', identity | {'turn_id': paged_id, 'page': 0})
    assert first_page['pageCount'] > 1

    raw = client().query('turn.context.read', identity | {'turn_id': target_id, 'replay': False})
    validate_result(raw)
    rows = {(row.get('pagedTurn') or row)['turnId']: row for row in raw['turns']}
    assert rows.pop(paged_id) == {'pagedTurn': first_page['turn']}
    for turn_id, row in rows.items():
        assert row == client().query('turn.get', identity | {'turn_id': turn_id})

    paged_reader, whole_reader = _Reader(client()), _WholeTurnReader(client())
    paged, whole = (_read_context(reader, identity, target_id)
                    for reader in (paged_reader, whole_reader))
    assert paged == whole and paged['turns'][1] == whole_turn and paged['nextTurnId'] is None
    pages = [payload for operation, payload in paged_reader.calls
             if operation == 'turn.projection.page']
    assert [payload['page'] for payload in pages] == list(range(first_page['pageCount']))
    assert {payload['projection_revision'] for payload in pages} == {whole_turn['projectionRevision']}
    # Digests derived from Turns never depend on the reader's projection key order.
    assert ([settled_witness(row) for row in paged['turns']]
            == [settled_witness(row) for row in whole['turns']])
    input_id = paged['turns'][-1]['parentTurnId']
    assert _history_fingerprint(paged, input_id) == _history_fingerprint(whole, input_id)

    built = []
    for reader in (_Reader(client()), _WholeTurnReader(client())):
        monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda reader=reader, **kwargs: reader)
        config = dict(_PAGED_CONFIG)
        messages = turn_lifecycle.build_api_messages(
            identity['conversation_id'], target_id, config, user_id=identity['user_id'])
        built.append((json.dumps(messages, ensure_ascii=False),
                      json.dumps(config, ensure_ascii=False, sort_keys=True)))
    assert built[0] == built[1]
    assert whole_turn['projection']['toolRounds'][-1]['toolContent'] in built[0][0]

    # The authority accepts the terminal witness and proof derived from the
    # paged Turn as its own, and covers it in the next read.
    at_paged = _read_context(_Reader(client()), identity, paged_id)
    assert at_paged['turns'][-1] == whole_turn
    command_id = uuid.uuid4().hex
    assert client(write=True).command('model_continuation.put', identity | {
        'lane_id': 'main', 'projection': {
            'turns': [settled_witness(at_paged['turns'][-1])],
            'historyProof': at_paged['historyProof'],
            'policy': policy_witness(dict(_PAGED_CONFIG)),
            'messages': [{'role': 'assistant', 'content': 'retained paged history'}]}},
        command_id) is True
    warm = client().query('turn.context.read', identity | {'turn_id': target_id})
    assert (warm['covered'], warm['continuationStatus']) == (2, 'available')


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native', 'postgres'], indirect=True)
def test_a_paged_turn_that_moves_mid_read_restarts_under_a_fresh_proof(semantic_authority, monkeypatch):
    client, identity, paged_id, target_id = _paged_history(semantic_authority, monkeypatch)
    before = client().query('turn.context.read', identity | {
        'turn_id': target_id, 'replay': False})['historyProof']
    edits = []

    def edit_once(operation, payload):
        if operation == 'turn.projection.page' and payload['page'] == 1 and not edits:
            current = client().query('turn.get', identity | {'turn_id': paged_id})
            command_id = uuid.uuid4().hex
            edits.append(client(write=True).command('turn.projection.update', identity | {
                'turn_id': paged_id,
                'expected_projection_revision': current['projectionRevision'],
                'projection': current['projection'] | {'content': 'edited while paging'}},
                command_id))

    reader = _Reader(client(), before=edit_once)
    context = _read_context(reader, identity, target_id)

    edited = client().query('turn.get', identity | {'turn_id': paged_id})
    assert edits and edited['projection']['content'] == 'edited while paging'
    # The restarted read is the history after the edit, never a mix of revisions.
    assert context == _read_context(_WholeTurnReader(client()), identity, target_id)
    assert context['turns'][1] == edited and context['historyProof'] != before
    assert _restarts(reader) == 2
    assert {payload['projection_revision'] for operation, payload in reader.calls
            if operation == 'turn.projection.page'} == {
        edited['projectionRevision'] - 1, edited['projectionRevision']}


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native', 'postgres'], indirect=True)
def test_a_paged_turn_deleted_mid_read_restarts_without_it(semantic_authority, monkeypatch):
    client, identity, paged_id, target_id = _paged_history(semantic_authority, monkeypatch)
    deleted = []

    def delete_once(operation, payload):
        if operation == 'turn.projection.page' and not deleted:
            command_id = uuid.uuid4().hex
            deleted.append(client(write=True).command('turn.delete', identity | {
                'turn_ids': [paged_id]}, command_id))

    reader = _Reader(client(), before=delete_once)
    context = _read_context(reader, identity, target_id)

    assert deleted and client().query('turn.get', identity | {'turn_id': paged_id}) is None
    assert context == _read_context(_Reader(client()), identity, target_id)
    assert paged_id not in [row['turnId'] for row in context['turns']]
    assert _restarts(reader) == 2
