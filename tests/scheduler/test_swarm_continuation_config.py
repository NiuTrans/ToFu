"""Swarm wakes share chat defaults without acquiring attended capabilities."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lib.conv_config import resolve_conv_config
from lib.scheduler import conversation_dispatch
from lib.scheduler._shared import build_task_config
from lib.tasks_pkg.model_replay import policy_witness, portable_policy_witness

pytestmark = pytest.mark.unit


@pytest.fixture
def dispatch(monkeypatch):
    settings = {}
    calls = []
    conv_id = 'swarm-config-' + uuid4().hex

    def query(operation, payload):
        assert operation == 'conversation.get'
        assert payload == {'conv_id': conv_id, 'user_id': 7,
                           'derive_messages': False}
        return {'metadata': {'settings': deepcopy(settings)}}

    def create_turn(conversation_id, owner_id, payload, **kwargs):
        assert conversation_id == conv_id and owner_id == 7
        calls.append(payload)
        return SimpleNamespace(value={'attempt': {'taskId': 'continuation'}})

    monkeypatch.setattr(conversation_dispatch, 'get_storage_client',
                        lambda: SimpleNamespace(query=query))
    monkeypatch.setattr(conversation_dispatch, 'conversation_turn_commands',
                        SimpleNamespace(create_turn=create_turn))

    def run(*, tools=None, overrides=None, **kwargs):
        return conversation_dispatch.dispatch_scheduled_turn(
            conv_id, {'content': 'Integrate settled results.'},
            {} if tools is None else tools, user_id=7,
            command_id='swarm-autocontinue:wave', input_actor='virtual_user',
            input_kind='swarm_autocontinue_input',
            turn_kind='swarm_autocontinue_reply', parent_turn_id='parent',
            require_parent_is_lane_tail=True, require_parent_not_stopped=True,
            config_overrides=({'_swarmAutoContinue': True, 'schedulerEnabled': False}
                              if overrides is None else overrides), **kwargs)

    return SimpleNamespace(settings=settings, calls=calls, run=run, conv_id=conv_id)


@pytest.mark.parametrize('settings', [
    {},
    {'model': 'glm-5', 'thinkingDepth': 'high', 'schedulerEnabled': False,
     'searchMode': 'off', 'fetchEnabled': True, 'memoryEnabled': True},
    {'modelRef': {'creator_id': 'zhipu', 'model_id': 'glm-5'},
     'preferredProviderId': 'provider-a', 'thinkingDepth': 'off',
     'thinkingEnabled': False},
    {'modelRef': {'provider_id': 'provider-a', 'offering_id': 'offering-a'},
     'thinkingDepth': 'default'},
    {'chatMode': 'chat', 'searchMode': 'multi', 'fetchEnabled': True,
     'codeExecEnabled': True, 'memoryEnabled': True},
])
def test_alternating_real_builders_have_identical_replay_policy(dispatch, settings):
    dispatch.settings.update(settings)
    signatures = []
    for _ in range(3):
        human = resolve_conv_config(conv_settings=dispatch.settings, is_active=False)
        assert dispatch.run().disposition == 'started'
        swarm = dispatch.calls[-1]['config']
        signatures.extend((policy_witness(config), portable_policy_witness(config))
                          for config in (human, swarm))
    assert len(set(signatures)) == 1
    assert swarm['systemPrompt'] == ''
    assert swarm['systemPromptMode'] == 'append'
    assert swarm['systemPromptBlocks'] == {}
    assert swarm['autopilot'] is False
    assert swarm['desktopEnabled'] is False
    assert swarm['humanGuidanceEnabled'] is False
    assert swarm['_swarmAutoContinue'] is True
    assert swarm['schedulerEnabled'] is False


@pytest.mark.parametrize('field', [
    'schedulerEnabled', 'desktopEnabled', 'humanGuidanceEnabled', 'autopilotEnabled',
])
def test_attended_capabilities_remain_a_real_policy_difference(dispatch, field):
    dispatch.settings[field] = True
    human = resolve_conv_config(conv_settings=dispatch.settings, is_active=False)
    assert dispatch.run().disposition == 'started'
    swarm = dispatch.calls[-1]['config']
    runtime_field = 'autopilot' if field == 'autopilotEnabled' else field
    assert swarm[runtime_field] is False
    assert policy_witness(human) != policy_witness(swarm)
    assert portable_policy_witness(human) != portable_policy_witness(swarm)


@pytest.mark.parametrize('overrides', [
    {'systemPrompt': 'Nondefault global prompt'},
    {'systemPromptMode': 'replace'},
    {'systemPromptBlocks': {'disabled': ['base']}},
    {'tools': {'mode': 'custom'}},
])
def test_nondefault_global_policy_is_not_invented_from_settings(dispatch, overrides):
    human = resolve_conv_config(conv_settings=dispatch.settings,
                                overrides=overrides, is_active=False)
    assert dispatch.run().disposition == 'started'
    swarm = dispatch.calls[-1]['config']
    assert policy_witness(human) != policy_witness(swarm)
    assert portable_policy_witness(human) != portable_policy_witness(swarm)


def test_changed_tool_permission_still_invalidates_both_witnesses(dispatch):
    human = resolve_conv_config(conv_settings={'browserEnabled': True}, is_active=False)
    dispatch.settings['browserEnabled'] = False
    assert dispatch.run().disposition == 'started'
    swarm = dispatch.calls[-1]['config']
    assert policy_witness(human) != policy_witness(swarm)
    assert portable_policy_witness(human) != portable_policy_witness(swarm)


@pytest.mark.parametrize('active_flow', ['builtin:review', 'flow:custom'])
def test_swarm_never_inherits_a_flow(dispatch, active_flow):
    dispatch.settings.update(activeFlow=active_flow, autopilotEnabled=True)
    assert dispatch.run().disposition == 'started'
    config = dispatch.calls[-1]['config']
    assert not any(config.get(key) for key in (
        'activeFlow', 'flowBuiltin', 'flowId', 'flowDefinition', 'autopilot'))


def test_chat_mode_cannot_expand_legacy_swarm_tool_permissions(dispatch):
    from lib.tasks_pkg.chat_mode import apply_chat_mode
    dispatch.settings.update(chatMode='chat', searchMode='off', fetchEnabled=False,
                             codeExecEnabled=False, memoryEnabled=False)
    assert dispatch.run().disposition == 'started'
    config = dispatch.calls[-1]['config']
    effective = apply_chat_mode(config)
    assert effective['searchMode'] == 'off'
    assert effective['fetchEnabled'] is False
    assert effective['codeExecEnabled'] is False
    assert effective['memoryEnabled'] is False
    human = resolve_conv_config(conv_settings=dispatch.settings, is_active=False)
    assert portable_policy_witness(human) != portable_policy_witness(config)


def test_read_only_roots_and_manual_approval_survive(dispatch):
    dispatch.settings.update(projectPath='/project', projectPaths=['/project', '/reference'],
                             readOnlyPaths=['/reference'], autoApply=False)
    assert dispatch.run().disposition == 'started'
    config = dispatch.calls[-1]['config']
    assert config['projectPaths'] == ['/project', '/reference']
    assert config['readOnlyPaths'] == ['/reference']
    assert config['autoApply'] is False


def test_plan_is_not_dropped_or_given_unattended_human_guidance(dispatch):
    # Shared task-start normalization necessarily enables guidance in Plan.
    # Defer to a human rather than weaken Plan or enable that attended tool.
    dispatch.settings.update(planMode=True, humanGuidanceEnabled=False,
                             readOnlyPaths=['/project'])
    assert dispatch.run().disposition == 'start_failed'
    assert not dispatch.calls


@pytest.mark.parametrize('override', [
    {'schedulerEnabled': True}, {'_swarmAutoContinue': False},
    {'autopilot': True}, {'autopilotEnabled': True}, {'desktopEnabled': True},
    {'humanGuidanceEnabled': True}, {'activeFlow': 'builtin:review'},
    {'flowDefinition': {'nodes': []}}, {'flowId': 'custom'}, {'flowBuiltin': 'review'},
    {'planMode': False}, {'readOnlyPaths': []}, {'autoApply': True},
    {'tools': {'mode': 'custom'}}, {'_modelReplayPolicy': 'forged'},
])
def test_swarm_rejects_noncontrol_overrides(dispatch, override):
    with pytest.raises(ValueError, match='[Ss]warm'):
        dispatch.run(overrides=override)
    assert not dispatch.calls


@pytest.mark.parametrize('tools', [{'desktopEnabled': True}, '{"planMode": false}'])
def test_swarm_cannot_receive_scheduler_tool_overrides(dispatch, tools):
    with pytest.raises(ValueError, match='[Ss]warm'):
        dispatch.run(tools=tools)
    assert not dispatch.calls


@pytest.mark.parametrize('marker', ['_timer', '_proactive'])
def test_timer_and_proactive_keep_their_existing_builder_and_overrides(dispatch, marker):
    dispatch.settings.update(model='glm-5', thinkingDepth='off', desktopEnabled=True)
    tools = {'model': 'fallback', 'fetchEnabled': False, 'autoTranslate': True}
    result = conversation_dispatch.dispatch_scheduled_turn(
        dispatch.conv_id, {'content': 'Scheduled work', marker: True}, tools,
        user_id=7, command_id='timer:1', config_overrides={'schedulerEnabled': False})
    assert result.disposition == 'started'
    expected = build_task_config(tools, dispatch.settings)
    expected['schedulerEnabled'] = False
    assert dispatch.calls[-1]['config'] == expected


@pytest.mark.parametrize('plan', [False, True])
def test_real_swarm_caller_keeps_provenance_parent_fences_and_budget(dispatch, monkeypatch, plan):
    from lib import agent_inbox
    from lib.swarm import integration
    from lib.swarm import restart_recovery
    from lib.turn_initiation import INITIATOR_SWARM

    dispatch.settings.update(model='glm-5', thinkingDepth='high', planMode=plan)
    turns = [{'turnId': 'human', 'actor': 'human', 'status': 'completed', 'projection': {}},
             {'turnId': 'parent', 'actor': 'assistant', 'status': 'completed', 'projection': {}}]
    monkeypatch.setattr(restart_recovery, 'read_continuation_turns',
                        lambda conv, **kw: {'turns': turns})
    monkeypatch.setattr(integration, '_key_is_live', lambda key: False)
    monkeypatch.setattr(integration, 'get_active_session', lambda key: None)
    monkeypatch.setattr(integration, 'SWARM_AUTOCONTINUE_ENABLED', True)
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **kw: None)
    try:
        agent_inbox.enqueue(dispatch.conv_id, 'Settled evidence', priority='later',
                            mode='swarm-update', agent_id='child', extra={'status': 'completed'})
        integration._maybe_autocontinue(dispatch.conv_id, 7, source_id='wave')
        if plan:
            assert not dispatch.calls
            assert integration._autocontinue_chain.get(dispatch.conv_id, 0) == 0
        else:
            payload = dispatch.calls[-1]
            assert payload['config'] == {
                **resolve_conv_config(conv_settings=dispatch.settings, is_active=False),
                '_swarmAutoContinue': True, 'schedulerEnabled': False,
            }
            assert payload['inputActor'] == 'virtual_user'
            assert payload['inputKind'] == 'swarm_autocontinue_input'
            assert payload['kind'] == 'swarm_autocontinue_reply'
            assert payload['parentTurnId'] == 'parent'
            assert payload['requireParentIsLaneTail'] is True
            assert payload['requireParentNotStopped'] is True
            assert payload['inputTurn']['_initiator'] == INITIATOR_SWARM
            assert integration._autocontinue_chain[dispatch.conv_id] == 1
        assert agent_inbox.has_pending(dispatch.conv_id, modes=['swarm-update'])
        assert dispatch.conv_id not in integration._autocontinue_inflight
    finally:
        agent_inbox.reset_for_test(dispatch.conv_id)
        integration.reset_autocontinue_chain(dispatch.conv_id)
