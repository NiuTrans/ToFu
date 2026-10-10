"""Cross-model continuation never resurrects already folded tool history."""
import copy

import pytest

from tests.model_routing.test_model_replay_stability import _turn


@pytest.fixture
def replay_store(monkeypatch, tmp_path):
    from lib.storage.model_replay_cache import ModelReplayCache
    from lib.tasks_pkg import model_replay
    store = ModelReplayCache(root=tmp_path / 'replay', max_bytes=2_000_000)
    monkeypatch.setattr(model_replay, 'ModelContinuationStore', lambda: store)
    return store

pytestmark = pytest.mark.unit


def test_text_compaction_survives_model_switch_without_old_runtime_rules(replay_store):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'objective', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {
        'model': 'kimi-k3', 'systemPrompt': 'user policy',
        'modelRef': {'creator_id': 'moonshot', 'model_id': 'kimi-k3'},
        'preferredProviderId': 'kimi',
        'routing': {'preferred_provider_id': 'kimi'},
    }
    history = {
        'role': 'user',
        'content': 'historical objective',
        '_isMeta': True,
        '_isObjectiveAnchor': True,
        '_retainedUserHistory': [
            {'kind': 'opening_request', 'content': 'objective'},
        ],
    }
    compact_tool = {
        'role': 'tool', 'name': 'context_compact',
        'content': ('## Context Compacted\n\n### Objective\nKeep going.\n'
                    '### Completed & Verified\n73 tests passed.'),
    }
    messages = [
        {'role': 'system', 'content': 'user policy'},
        history,
        {'role': 'user', '_contextComposer': True, 'content': 'old model rules'},
        {'role': 'assistant', 'content': 'Verified compact task state'},
        compact_tool,
        {'role': 'assistant', 'content': '73 tests passed. Next: Android R8.'},
    ]
    replay_store.put(user_id=1, conversation_id='c', lane_id='main', projection={
        'policy': replay.policy_witness(original),
        'portablePolicy': replay.portable_policy_witness(original),
        'turns': [replay.turn_witness(row) for row in rows], 'messages': messages,
        'toolLatch': {'oldModelSetting': True},
    })
    cfg = {
        **original, 'model': 'gpt-6-astra',
        'modelRef': {'creator_id': 'openai', 'model_id': 'gpt-6-astra'},
        'preferredProviderId': 'openai',
        'routing': {'preferred_provider_id': 'openai'},
    }
    restored, covered = replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)],
                                              cfg, conversation_id='c', user_id=1)
    assert covered == 2
    assert cfg['_modelReplayStatus'] == 'restored_portable'
    assert restored == [m for m in messages if not m.get('_contextComposer')]
    assert restored[1]['_retainedUserHistory'][0]['content'] == 'objective'
    assert '_modelReplayToolLatch' not in cfg
    assert 'old model rules' not in str(restored)
    assert '73 tests passed' in str(restored)
    changed = {**cfg, 'systemPrompt': 'withdrawn policy'}
    assert replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], changed,
                                  conversation_id='c', user_id=1) == ([], 0)
    assert changed['_modelReplayStatus'] == 'policy_changed'


@pytest.mark.parametrize('extra', [
    {'content': [{'type': 'image_url', 'image_url': {'url': 'private'}}]},
    {'reasoning_content': 'opaque thinking'},
    {'_unknownProviderState': 'opaque'},
])
def test_nonportable_evidence_is_not_silently_dropped(extra):
    from lib.tasks_pkg.model_replay import _portable_messages
    assert _portable_messages([{'role': 'assistant', 'content': 'done', **extra}]) is None


def test_history_carrier_private_fields_are_portable_text_metadata():
    from lib.tasks_pkg.model_replay import _portable_messages
    projected = _portable_messages([{
        'role': 'user', 'content': 'historical objective',
        '_isMeta': True, '_isObjectiveAnchor': True,
        '_retainedUserHistory': [{'kind': 'opening_request', 'content': 'x'}],
    }, {'role': 'assistant', 'content': 'done'}])
    assert projected is not None
    assert projected[0]['_retainedUserHistory'][0]['content'] == 'x'


@pytest.mark.parametrize('call', [{'tool_calls': [{'id': 'pending'}]},
                                  {'function_call': {'name': 'pending'}}])
def test_unfinished_tool_boundary_cannot_publish(call):
    from lib.tasks_pkg.model_replay import prepare_completed_task
    task = {'convId': 'c', '_turnId': 'a', '_userId': 1,
            'config': {'_modelReplayBase': []},
            'messages': [{'role': 'assistant', 'content': '', **call}]}
    assert prepare_completed_task(task) is None
    assert task['_modelReplayPublishStatus'] == 'unfinished_tool_batch'


@pytest.mark.parametrize('depth', ['low', 'medium', 'high', 'xhigh', 'max'])
def test_same_opus_effort_change_preserves_signed_prefix_and_tool_latch(replay_store, depth):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'claude-opus-5', 'thinkingDepth': 'medium', 'thinkingEnabled': True}
    messages = [
        {'role': 'user', 'content': 'work'},
        {'role': 'user', '_contextComposer': True, 'content': 'stable rules'},
        {'role': 'assistant', 'content': 'done', 'reasoning_content': 'summary',
         'thinking_signature': 'synthetic-signature',
         '_anthropic_content_blocks': [
             {'type': 'thinking', 'thinking': '', 'signature': 'synthetic-signature'},
             {'type': 'text', 'text': 'done'}]},
    ]
    replay_store.put(user_id=1, conversation_id='c', lane_id='main', projection={
        'policy': replay.policy_witness(original),
        'effortPolicy': replay.effort_policy_witness(original),
        'portablePolicy': replay.portable_policy_witness(original),
        'turns': [replay.turn_witness(row) for row in rows], 'messages': messages,
        'toolLatch': {'programmaticCalling': 'on'},
    })
    cfg = {**original, 'thinkingDepth': depth}
    restored, covered = replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                              conversation_id='c', user_id=1)
    assert covered == 2
    assert restored == messages
    assert cfg['_modelReplayStatus'] == ('restored' if depth == 'medium' else 'restored_effort')
    assert cfg['_modelReplayToolLatch'] == {'programmaticCalling': 'on'}
    restored[-1]['_anthropic_content_blocks'][0]['signature'] = 'mutated'
    saved = replay_store.get(user_id=1, conversation_id='c', lane_id='main')
    assert saved['messages'] == messages


@pytest.mark.parametrize('change', [
    {'model': 'claude-opus-4-7'}, {'providerId': 'another'},

    {'preferredProviderId': 'another'},
    {'routing': {'preferred_provider_id': 'another'}},
    {'modelRef': {'creator_id': 'anthropic', 'model_id': 'another'}},
    {'systemPrompt': 'new'}, {'grantedToolPermissions': ['read']},
    {'thinkingEnabled': False}, {'thinkingDepth': 'off'},
])
def test_effort_witness_does_not_relax_other_boundaries(replay_store, change):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'claude-opus-5', 'thinkingDepth': 'medium', 'thinkingEnabled': True}
    replay_store.put(user_id=1, conversation_id='c', lane_id='main', projection={
        'policy': replay.policy_witness(original),
        'effortPolicy': replay.effort_policy_witness(original),
        'portablePolicy': replay.portable_policy_witness(original),
        'turns': [replay.turn_witness(row) for row in rows],
        'messages': [{'role': 'assistant', 'content': 'done', 'reasoning_content': 'summary'}],
    })
    cfg = {**original, 'thinkingDepth': 'max', **change}
    assert replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                 conversation_id='c', user_id=1) == ([], 0)
    assert cfg['_modelReplayStatus'] in ('policy_changed', 'nonportable_checkpoint')


def test_legacy_checkpoint_without_effort_witness_still_rebuilds(replay_store):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'claude-opus-5', 'thinkingDepth': 'medium'}
    replay_store.put(user_id=1, conversation_id='c', lane_id='main', projection={
        'policy': replay.policy_witness(original),
        'portablePolicy': replay.portable_policy_witness(original),
        'turns': [replay.turn_witness(row) for row in rows],
        'messages': [{'role': 'assistant', 'content': 'done', 'reasoning_content': 'summary'}],
    })
    cfg = {**original, 'thinkingDepth': 'max'}
    assert replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                 conversation_id='c', user_id=1) == ([], 0)
    assert cfg['_modelReplayStatus'] == 'nonportable_checkpoint'


def test_completed_candidate_keeps_input_effort_witness():
    from lib.tasks_pkg import model_replay as replay
    original = {'model': 'claude-opus-5', 'thinkingDepth': 'medium'}
    witness = replay.effort_policy_witness(original)
    cfg = {**original, '_modelReplayBase': [], '_modelReplayEffortPolicy': witness,
           'thinkingEnabled': True}
    result = replay.prepare_completed_task({
        'config': cfg, '_userId': 1, 'convId': 'c', '_turnId': 'a1',
        'model': 'claude-opus-5', 'content': 'done',
        'messages': [{'role': 'assistant', 'content': 'done'}]})
    assert result['projection']['effortPolicy'] == witness
    assert len(witness) == 64
    assert replay.effort_policy_witness({'model': 'kimi-k3', 'thinkingDepth': 'max'}) == ''


def test_publication_exposes_missing_checkpoint_reason():
    from lib.tasks_pkg.model_replay import publish_completed_task
    task = {'convId': 'c', 'config': {}, '_userId': 1, '_turnId': 'a'}
    publish_completed_task(task)
    assert task['_modelReplayPublishStatus'] == 'missing_base'


def test_publication_pins_input_policy_before_runtime_config_normalization(monkeypatch, replay_store):
    from lib import storage
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', '', 2)]
    cfg = {'model': 'kimi-k3'}
    replay.restore_prefix(rows, cfg, conversation_id='c', user_id=1)
    original_policy = cfg['_modelReplayPolicy']
    # The runtime resolves defaults after input assembly; these should not
    # make the next identical user settings fail the checkpoint witness.
    cfg['thinkingEnabled'] = True
    rows[-1].update(status='completed', projection={'content': 'done'})
    class Client:
        def query(self, operation, payload, **kwargs):
            return copy.deepcopy(rows[-1])
    monkeypatch.setattr(storage, 'get_storage_client', lambda: Client())
    task = {'config': cfg, '_userId': 1, 'convId': 'c', '_turnId': 'a1',
            'model': 'kimi-k3', 'content': 'done',
            'messages': [{'role': 'user', 'content': 'work'},
                         {'role': 'assistant', 'content': 'done'}]}
    replay.publish_completed_task(task)
    assert task['_modelReplayPublishStatus'] == 'stored'
    assert replay_store.get(user_id=1, conversation_id='c', lane_id='main')['policy'] == original_policy
