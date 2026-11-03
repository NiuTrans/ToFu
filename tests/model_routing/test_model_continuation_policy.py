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
    {'_modelImageSources': ['tofu-media:private']},
    {'_unknownProviderState': 'opaque'},
])
def test_nonportable_evidence_is_not_silently_dropped(extra):
    from lib.tasks_pkg.model_replay import _portable_messages
    assert _portable_messages([{'role': 'assistant', 'content': 'done', **extra}]) is None


def test_portable_projection_strips_only_route_bound_provider_state():
    from lib.tasks_pkg.model_replay import _portable_messages
    call = {'id': 'call-1', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{"path": "a.py"}'}}
    steer = {'role': 'user', 'content': 'use staging', '_contextInjectionId': 'inbox_delivery',
             '_isInboxInject': True, '_containsHumanSteer': True,
             '_contextInjection': {'id': 'inbox_delivery', 'eventId': 'e1'},
             '_projectMailbox': 'mailbox', '_projectMessageIds': ['m1']}
    messages = [
        {'role': 'user', 'content': 'work', '_turnId': 'u1'},
        {'role': 'assistant', 'content': '', 'tool_calls': [call],
         'reasoning_content': 'route-private thinking', 'thinking_signature': 'sig'},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'exact tool bytes'},
        steer,
        # Provider-only row: nothing portable remains.
        {'role': 'assistant', 'content': '', '_responses_items': [{'type': 'reasoning'}]},
        {'role': 'assistant', 'content': 'done',
         '_anthropic_content_blocks': [{'type': 'thinking', 'signature': 'sig'}],
         '_reasoning_continuity': 'completed_turn', 'reasoning_details': [{'x': 1}]},
    ]
    projected = _portable_messages(messages)
    assert projected == [
        messages[0],
        {'role': 'assistant', 'content': '', 'tool_calls': [call]},
        messages[2],
        steer,
        {'role': 'assistant', 'content': 'done'},
    ]
    assert projected[3] is not steer  # Detached from the stored checkpoint.
    # A final answer that was only provider state is not a portable view.
    assert _portable_messages(messages[:5]) is None


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
    assert restored == messages[:-1] + [
        {**messages[-1], '_reasoning_continuity': 'completed_turn'}]
    assert cfg['_modelReplayStatus'] == ('restored' if depth == 'medium' else 'restored_effort')
    assert cfg['_modelReplayToolLatch'] == {'programmaticCalling': 'on'}
    restored[-1]['_anthropic_content_blocks'][0]['signature'] = 'mutated'
    saved = replay_store.get(user_id=1, conversation_id='c', lane_id='main')
    assert saved['messages'] == messages


def _put_policy_checkpoint(replay_store, replay, rows, original, messages, **extra):
    replay_store.put(user_id=1, conversation_id='c', lane_id='main', projection={
        'policy': replay.policy_witness(original),
        'policyFields': replay._policy_fields(original),
        'portablePolicy': replay.portable_policy_witness(original),
        'turns': [replay.turn_witness(row) for row in rows], 'messages': messages,
        **extra})


@pytest.mark.parametrize('change', [
    {'model': 'claude-opus-4-7'}, {'providerId': 'another'},
    {'preferredProviderId': 'another'},
    {'routing': {'preferred_provider_id': 'another'}},
    {'modelRef': {'creator_id': 'anthropic', 'model_id': 'another'}},
    {'thinkingEnabled': False}, {'thinkingDepth': 'off'},
])
def test_effort_witness_does_not_relax_route_or_off_changes(replay_store, change):
    """Only portable text survives: signed reasoning never crosses these boundaries."""
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'claude-opus-5', 'thinkingDepth': 'medium', 'thinkingEnabled': True}
    _put_policy_checkpoint(
        replay_store, replay, rows, original,
        [{'role': 'assistant', 'content': 'done', 'reasoning_content': 'summary'}],
        effortPolicy=replay.effort_policy_witness(original),
        toolLatch={'programmaticCalling': 'on'})
    cfg = {**original, 'thinkingDepth': 'max', **change}
    restored, covered = replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                              conversation_id='c', user_id=1)
    assert cfg['_modelReplayStatus'] == 'restored_portable'
    assert (restored, covered) == ([{'role': 'assistant', 'content': 'done'}], 2)
    assert '_modelReplayToolLatch' not in cfg


@pytest.mark.parametrize('change', [
    {'systemPrompt': 'new'}, {'grantedToolPermissions': ['read']},
])
def test_effort_witness_does_not_relax_authority_changes(replay_store, change):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'claude-opus-5', 'thinkingDepth': 'medium', 'thinkingEnabled': True}
    _put_policy_checkpoint(
        replay_store, replay, rows, original,
        [{'role': 'assistant', 'content': 'done', 'reasoning_content': 'summary'}],
        effortPolicy=replay.effort_policy_witness(original))
    cfg = {**original, 'thinkingDepth': 'max', **change}
    assert replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                 conversation_id='c', user_id=1) == ([], 0)
    assert cfg['_modelReplayStatus'] == 'policy_changed'


def test_legacy_checkpoint_without_effort_witness_restores_only_portable_text(replay_store):
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
                                 conversation_id='c', user_id=1) == (
        [{'role': 'assistant', 'content': 'done'}], 2)
    assert cfg['_modelReplayStatus'] == 'restored_portable'


_ROUTER_VIEW = [
    {'role': 'user', 'content': 'route this', '_turnId': 'u1'},
    {'role': 'user', '_contextComposer': True, 'content': 'old model rules'},
    {'role': 'assistant', 'content': 'Dispatching.', 'reasoning_content': 'private',
     '_responses_items': [{'type': 'reasoning', 'encrypted_content': 'opaque'}]},
    {'role': 'user', 'content': 'steer: prefer staging', '_contextInjectionId': 'inbox_delivery',
     '_isInboxInject': True, '_containsHumanSteer': True, '_contextInjection': {'eventId': 'e'}},
    {'role': 'assistant', 'content': 'Worker delegated on staging.'},
]


@pytest.mark.parametrize('change', [
    {'model': 'gpt-6-astra', 'modelRef': {'creator_id': 'openai', 'model_id': 'gpt-6-astra'}},
    {'preferredProviderId': 'other', 'routing': {'preferred_provider_id': 'other'}},
    {'thinkingDepth': 'high'}, {'thinkingEnabled': False}, {'defaultThinkingDepth': 'low'},
    {'preset': 'fast'},
])
def test_route_and_effort_drift_reuse_portable_text(replay_store, change):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'route this', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'kimi-k3', 'thinkingEnabled': True, 'thinkingDepth': 'medium',
                'projectPath': '/router-home', 'grantedToolPermissions': ['delegate']}
    _put_policy_checkpoint(replay_store, replay, rows, original, copy.deepcopy(_ROUTER_VIEW),
                           toolLatch={'latched': True}, goalState={'kept': False})
    cfg = {**original, **change}
    restored, covered = replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                              conversation_id='c', user_id=1)
    assert cfg['_modelReplayStatus'] == 'restored_portable'
    assert covered == 2  # Only the uncovered suffix is rebuilt fresh.
    assert set(cfg['_modelReplayPolicyDrift']) == set(change)
    assert [message['content'] for message in restored] == [
        'route this', 'Dispatching.', 'steer: prefer staging', 'Worker delegated on staging.']
    assert not any(key in message for message in restored for key in (
        'reasoning_content', '_responses_items', '_contextComposer'))
    assert '_modelReplayToolLatch' not in cfg and '_goalWorkerState' not in cfg


@pytest.mark.parametrize('change', [
    {'grantedToolPermissions': ['delegate', 'write']},            # permissions
    {'projectPath': '/another-project'}, {'projectEnabled': False},  # project scope
    {'tools': {'programmaticCalling': 'on'}}, {'enabledTools': ['run_command']},
    {'disabledTools': ['web_search']}, {'mcpServers': ['github']},
    {'codeExecEnabled': True}, {'browserEnabled': True},           # tool authority
    {'planMode': True}, {'delegateMode': True}, {'autopilot': True},  # interaction mode
    {'systemPrompt': 'new operator rules'},                        # instructions
    {'_principalContext': {'kind': 'system'}},
    {'model': 'gpt-6-astra', 'grantedToolPermissions': ['delegate', 'write']},
])
def test_authority_drift_still_refuses(replay_store, change):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'route this', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'kimi-k3', 'thinkingEnabled': True, 'thinkingDepth': 'medium',
                'projectPath': '/router-home', 'projectEnabled': True,
                'grantedToolPermissions': ['delegate']}
    _put_policy_checkpoint(replay_store, replay, rows, original, copy.deepcopy(_ROUTER_VIEW))
    cfg = {**original, **change}
    assert replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], cfg,
                                 conversation_id='c', user_id=1) == ([], 0)
    assert cfg['_modelReplayStatus'] == 'policy_changed'
    assert cfg['_modelReplayPolicyDrift']


def test_interaction_mode_witness_keeps_standard_digests():
    from lib.tasks_pkg import model_replay as replay
    standard = {'model': 'kimi-k3'}
    assert replay._policy_fields(standard)[2]['interactionMode'] is None
    assert replay.policy_witness({**standard, 'planMode': False}) == replay.policy_witness(standard)
    assert replay.policy_witness({**standard, 'planMode': True}) != replay.policy_witness(standard)
    assert replay.policy_witness({**standard, 'delegateMode': True}) != replay.policy_witness(standard)


def test_media_checkpoint_still_rebuilds_on_route_drift(replay_store):
    from lib.tasks_pkg import model_replay as replay
    rows = [_turn('u1', 'human', 'look', 1), _turn('a1', 'assistant', 'done', 2)]
    original = {'model': 'kimi-k3'}
    _put_policy_checkpoint(replay_store, replay, rows, original, [
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'x'}}]},
        {'role': 'assistant', 'content': 'done'}])
    cfg = {'model': 'gpt-6-astra'}
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


@pytest.mark.parametrize('model', ['kimi-k3', 'claude-opus-5'])
@pytest.mark.parametrize('resume_config,target_projection,initial_status', [
    ({'contentPrefix': 'partial'}, {}, 'unsupported_mode'),
    ({'checkpointToolRounds': [{'toolName': 'read_files'}]}, {}, 'unsupported_mode'),
    ({'_resumeSource': 'checkpoint'}, {}, 'unsupported_mode'),
    ({}, {'content': 'partial'}, 'resume_target'),
    ({}, {'toolRounds': [{'toolName': 'read_files'}]}, 'resume_target'),
])
def test_completed_resume_pins_ingress_policy_and_reuses_compaction_twice(
        monkeypatch, replay_store, model, resume_config, target_projection, initial_status):
    """Runtime defaults/worktree rebinding must not resurrect folded history."""
    from lib import storage
    from lib.tasks_pkg import model_replay as replay
    original = {'model': model, 'projectPath': '/project'}
    cfg = {**original, **resume_config}
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', '', 2)]
    rows[-1]['projection'] = copy.deepcopy(target_projection)
    assert replay.restore_prefix(rows, cfg, conversation_id='c', user_id=1) == ([], 0)
    assert cfg['_modelReplayStatus'] == initial_status
    expected = (replay.policy_witness(original), replay.portable_policy_witness(original),
                replay.effort_policy_witness(original))
    # These are runtime resolutions, not a new request or new authorization.
    cfg.update(thinkingEnabled=True, projectPath='/isolated/task-1')
    rows[-1].update(status='completed', projection={'content': 'done'})
    class Client:
        def query(self, operation, payload, **kwargs):
            assert operation == 'turn.get'
            return copy.deepcopy(rows[-1])
    monkeypatch.setattr(storage, 'get_storage_client', lambda: Client())
    compacted = [
        {'role': 'user', 'content': 'work'},
        {'role': 'assistant', 'content': 'Retained compact state: all old tool facts summarized.'},
    ]
    rows[-1]['projection']['content'] = compacted[-1]['content']
    task = {'config': cfg, '_userId': 1, 'convId': 'c', '_turnId': 'a1',
            'model': model, 'content': compacted[-1]['content'],
            '_resumeContextHydrated': True, 'messages': copy.deepcopy(compacted)}
    replay.publish_completed_task(task)
    assert task['_modelReplayPublishStatus'] == 'stored'
    checkpoint = replay_store.get(user_id=1, conversation_id='c', lane_id='main')
    assert (checkpoint['policy'], checkpoint['portablePolicy'], checkpoint['effortPolicy']) == expected
    for index in (2, 3):
        rows.extend([_turn(f'u{index}', 'human', 'continue', index * 2 - 1),
                     _turn(f'a{index}', 'assistant', '', index * 2)])
        next_config = dict(original)
        prefix, covered = replay.restore_prefix(rows, next_config, conversation_id='c', user_id=1)
        assert next_config['_modelReplayStatus'] == 'restored'
        assert covered == len(rows) - 2
        assert prefix == compacted
        compacted += [{'role': 'user', 'content': 'continue'},
                      {'role': 'assistant', 'content': f'progress {index}'}]
        rows[-1].update(status='completed', projection={'content': f'progress {index}'})
        replay.publish_completed_task({
            'config': next_config, '_userId': 1, 'convId': 'c', '_turnId': f'a{index}',
            'model': model, 'content': f'progress {index}', 'messages': copy.deepcopy(compacted),
        })


@pytest.mark.parametrize('change', [
    {'projectPath': '/different-project'},
    {'systemPrompt': 'new operator rules'},
    {'grantedToolPermissions': ['read_files']},
])
def test_resumed_checkpoint_still_refuses_real_policy_changes(monkeypatch, replay_store, change):
    from lib import storage
    from lib.tasks_pkg import model_replay as replay
    original = {'model': 'kimi-k3', 'projectPath': '/project'}
    cfg = {**original, 'contentPrefix': 'partial'}
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', '', 2)]
    replay.restore_prefix(rows, cfg, conversation_id='c', user_id=1)
    cfg.update(thinkingEnabled=True, projectPath='/isolated/task-1')
    rows[-1].update(status='completed', projection={'content': 'done'})
    class Client:
        def query(self, operation, payload, **kwargs):
            return copy.deepcopy(rows[-1])
    monkeypatch.setattr(storage, 'get_storage_client', lambda: Client())
    replay.publish_completed_task({
        'config': cfg, '_userId': 1, 'convId': 'c', '_turnId': 'a1',
        'model': 'kimi-k3', 'content': 'done', '_resumeContextHydrated': True,
        'messages': [{'role': 'assistant', 'content': 'done'}],
    })
    next_cfg = {**original, **change}
    assert replay.restore_prefix(rows + [_turn('a2', 'assistant', '', 3)], next_cfg,
                                 conversation_id='c', user_id=1) == ([], 0)
    assert next_cfg['_modelReplayStatus'] == 'policy_changed'


def test_completed_resume_keeps_ingress_policy_after_builtin_profile_resolution(
        monkeypatch, replay_store, tmp_path):
    from lib import storage
    from lib.agent_core import profiles
    from lib.tasks_pkg import model_replay as replay

    monkeypatch.setattr(profiles, '_PROFILES_DIR', str(tmp_path / 'profiles'))
    original = {'model': 'kimi-k3', 'profile': 'minimal'}
    cfg = {**original, 'contentPrefix': 'partial'}
    rows = [_turn('u1', 'human', 'work', 1), _turn('a1', 'assistant', '', 2)]
    assert replay.restore_prefix(rows, cfg, conversation_id='c', user_id=1) == ([], 0)
    cfg = profiles.apply_profile(cfg)
    assert cfg['memoryEnabled'] is False
    assert replay.portable_policy_witness(cfg) != replay.portable_policy_witness(original)
    rows[-1].update(status='completed', projection={'content': 'done'})

    class Client:
        def query(self, operation, payload, **kwargs):
            assert operation == 'turn.get'
            return copy.deepcopy(rows[-1])

    monkeypatch.setattr(storage, 'get_storage_client', lambda: Client())
    messages = [{'role': 'user', 'content': 'work'},
                {'role': 'assistant', 'content': 'done'}]
    task = {'config': cfg, '_userId': 1, 'convId': 'c', '_turnId': 'a1',
            'model': 'kimi-k3', 'content': 'done', '_resumeContextHydrated': True,
            'messages': messages}
    replay.publish_completed_task(task)
    assert task['_modelReplayPublishStatus'] == 'stored'
    next_cfg = dict(original)
    prefix, covered = replay.restore_prefix(
        rows + [_turn('a2', 'assistant', '', 3)], next_cfg, conversation_id='c', user_id=1)
    assert next_cfg['_modelReplayStatus'] == 'restored'
    assert prefix == messages
    assert covered == 2
