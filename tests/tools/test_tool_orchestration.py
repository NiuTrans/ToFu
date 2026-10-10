"""Provider-neutral PTC + multi-agent composition contracts."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest


pytestmark = pytest.mark.unit


def _tool(name: str) -> dict:
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': name,
            'parameters': {'type': 'object', 'properties': {}},
        },
    }


def _wire_names(tools) -> list[str]:
    return [str((tool.get('function') or tool).get('name') or '')
            for tool in (tools or ())]


def test_generic_policy_composes_programmatic_and_multi_agent_modes():
    from lib.tasks_pkg.tool_orchestration_policy import (
        resolve_tool_orchestration,
    )

    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='read_only',
        messages=[{'role': 'user', 'content': 'compare all modules'}],
        tools=[_tool('read_files')], round_num=1, model='any-model')
    assert decision['policyVersion'] == 'tool-orchestration/v1'
    assert decision['compositionMode'] == (
        'multi_agent_with_programmatic_workers')
    assert decision['programmaticCalling'] == 'on'
    assert decision['multiAgent'] == 'read_only'


def test_local_wire_composes_execute_tools_and_read_only_swarm():
    from lib.llm._sse_core import prepare_request
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    catalog = [_tool('read_files'), SPAWN_AGENTS_TOOL]
    decision_sink = {}
    plan = prepare_request({
        'model': 'deepseek-v4-pro',
        'messages': [{'role': 'user', 'content': 'compare modules'}],
        'tools': catalog,
        '_executable_tool_catalog': catalog,
        '_tool_wire_catalog': catalog,
        '_responses_feature_profile': 'compatible',
        '_programmatic_tool_calling': 'on',
        '_programmatic_tier': 'program',
        '_programmatic_eligible_tools': ['read_files'],
        '_multi_agent_mode': 'read_only',
        '_multi_agent_stage': 'compare independent modules',
        '_multi_agent_max_concurrent_agents': 2,
        '_tool_orchestration_decision_sink': decision_sink,
    }, api_key='secret', base_url='https://example.test/v1',
       api_protocol='openai')

    names = _wire_names(plan.body['tools'])
    assert names.count('execute_program') == 1
    assert names.count('spawn_agents') == 1
    spawn = next(tool for tool in plan.body['tools']
                 if (tool.get('function') or {}).get('name') == 'spawn_agents')
    # routing stamps the per-request wave ceiling (min of the decision's
    # maxConcurrentAgents, 8, and the resource probe) onto a private copy.
    expected_spawn = deepcopy(SPAWN_AGENTS_TOOL)
    expected_spawn['function']['parameters']['properties']['agents'][
        'maxItems'] = 2
    assert spawn == expected_spawn
    assert plan.body['messages'] == [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'compare modules'}]}]
    assert decision_sink == {
        'programmaticBackend': 'local',
        'multiAgentBackend': 'local_swarm',
    }
    assert not [key for key in plan.body if key.startswith('_')]


def test_local_wire_preserves_ordinary_tools_and_stability_within_policy():
    """Per-round PTC evidence must not rewrite the cached tools prefix."""
    from lib.llm._sse_core import prepare_request
    from lib.swarm.tools import SPAWN_AGENTS_TOOL
    from lib.tools.gateway import tool_schema_fingerprint

    hidden_names = [f'hidden_read_{index}' for index in range(12)]
    catalog = [
        _tool('read_files'), deepcopy(SPAWN_AGENTS_TOOL),
        *[_tool(name) for name in hidden_names],
    ]
    discovery_policy = {name: 'searchable' for name in hidden_names}
    discovery_policy.update({
        'read_files': 'eager',
        'spawn_agents': 'eager',
    })

    def _prepare(programmatic_calling: str):
        request_catalog = deepcopy(catalog)
        return prepare_request({
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'compare modules'}],
            'tools': request_catalog,
            '_executable_tool_catalog': request_catalog,
            '_tool_wire_catalog': request_catalog,
            '_tool_discovery_policy_by_name': discovery_policy,
            '_tool_search_catalog_size': len(request_catalog),
            '_tool_searchable_count': len(hidden_names),
            '_tool_search_mode': 'auto',
            '_programmatic_tool_calling': programmatic_calling,
            '_programmatic_tier': 'program',
            '_programmatic_eligible_tools': ['read_files'],
            '_multi_agent_mode': 'read_only',
            '_multi_agent_stage': 'compare independent modules',
            '_multi_agent_max_concurrent_agents': 3,
        }, api_key='secret', base_url='https://example.test/v1',
           api_protocol='openai')

    inactive = _prepare('off')
    active = _prepare('on')

    assert inactive.programmatic_backend == 'off'
    assert active.programmatic_backend == 'local'
    assert inactive.multi_agent_backend == active.multi_agent_backend \
        == 'local_swarm'
    # The policy selects a different invocation surface. Ordinary tools remain
    # unchanged, and repeated requests within one policy keep identical schemas.
    gateway_names = {'search_tools', 'execute_tools', 'execute_program'}
    inactive_names = [t['function']['name'] for t in inactive.body['tools']]
    active_names = [t['function']['name'] for t in active.body['tools']]
    assert 'search_tools' in inactive_names and 'search_tools' in active_names
    assert inactive_names.count('execute_tools') == 1
    assert active_names.count('execute_program') == 1
    inactive_rest = [t for t in inactive.body['tools']
                     if t['function']['name'] not in gateway_names]
    active_rest = [t for t in active.body['tools']
                   if t['function']['name'] not in gateway_names]
    assert inactive_rest == active_rest
    assert tool_schema_fingerprint(inactive_rest) == tool_schema_fingerprint(active_rest)
    assert tool_schema_fingerprint(active.body['tools']) == tool_schema_fingerprint(_prepare('on').body['tools'])
    active_execute = next(t for t in active.body['tools']
                          if t['function']['name'] == 'execute_program')
    execute_props = active_execute['function']['parameters']['properties']
    assert 'code' in execute_props


@pytest.mark.parametrize(
    ('protocol', 'model', 'base_url', 'profile'),
    [
        ('anthropic', 'claude-opus-4-7', 'https://api.anthropic.com', ''),
        ('responses', 'qwen3.5-plus', 'https://gateway.example/v1',
         'compatible'),
    ],
)
def test_local_composition_survives_provider_wire_conversion(
        protocol, model, base_url, profile):
    from lib.llm._sse_core import prepare_request
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    catalog = [_tool('read_files'), SPAWN_AGENTS_TOOL]
    plan = prepare_request({
        'model': model,
        'messages': [{'role': 'user', 'content': 'compare modules'}],
        'tools': catalog,
        '_executable_tool_catalog': catalog,
        '_tool_wire_catalog': catalog,
        '_responses_feature_profile': profile,
        '_programmatic_tool_calling': 'on',
        '_programmatic_tier': 'program',
        '_programmatic_eligible_tools': ['read_files'],
        '_multi_agent_mode': 'read_only',
        '_multi_agent_stage': 'compare independent modules',
    }, api_key='secret', base_url=base_url, api_protocol=protocol)

    names = _wire_names(plan.body['tools'])
    assert names.count('execute_program') == 1
    assert names.count('spawn_agents') == 1
    assert not [key for key in plan.body if key.startswith('_')]


def test_native_wire_composes_both_extensions_and_hides_local_spawn():
    from lib.llm._sse_core import prepare_request
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    catalog = [_tool('read_files'), SPAWN_AGENTS_TOOL]
    decision_sink = {}
    plan = prepare_request({
        'model': 'gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'compare modules'}],
        'tools': catalog,
        '_executable_tool_catalog': catalog,
        '_tool_wire_catalog': catalog,
        '_responses_feature_profile': 'openai',
        '_programmatic_tool_calling': 'on',
        '_programmatic_tier': 'program',
        '_programmatic_eligible_tools': ['read_files'],
        '_multi_agent_mode': 'read_only',
        '_multi_agent_stage': 'compare independent modules',
        '_multi_agent_max_concurrent_agents': 3,
        '_tool_orchestration_decision_sink': decision_sink,
    }, api_key='secret', base_url='https://api.openai.com/v1',
       api_protocol='responses')

    assert plan.body['multi_agent'] == {
        'enabled': True, 'max_concurrent_subagents': 3}
    assert any(tool.get('type') == 'programmatic_tool_calling'
               for tool in plan.body['tools'])
    assert 'spawn_agents' not in _wire_names(plan.body['tools'])
    assert 'responses_multi_agent=v1' in plan.hdrs['OpenAI-Beta']
    assert decision_sink == {
        'programmaticBackend': 'native_openai',
        'multiAgentBackend': 'native_openai',
    }


def test_explicit_native_rejection_retries_both_lanes_locally():
    from lib.llm._sse_core import (
        activate_native_orchestration_fallback, prepare_request)
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    catalog = [_tool('read_files'), SPAWN_AGENTS_TOOL]
    canonical = {
        'model': 'gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'compare modules'}],
        'tools': catalog,
        '_executable_tool_catalog': catalog,
        '_tool_wire_catalog': catalog,
        '_responses_feature_profile': 'openai',
        '_programmatic_tool_calling': 'on',
        '_programmatic_tier': 'program',
        '_programmatic_eligible_tools': ['read_files'],
        '_multi_agent_mode': 'read_only',
    }
    native = prepare_request(
        canonical, api_key='secret', base_url='https://api.openai.com/v1',
        api_protocol='responses')
    assert activate_native_orchestration_fallback(
        400,
        'unknown tool type programmatic_tool_calling; unknown multi_agent',
        plan=native, canonical_body=canonical)
    assert canonical['_force_local_programmatic'] is True
    assert canonical['_force_local_multi_agent'] is True

    local = prepare_request(
        canonical, api_key='secret', base_url='https://api.openai.com/v1',
        api_protocol='responses')
    assert local.programmatic_backend == 'local'
    assert local.multi_agent_backend == 'local_swarm'
    assert 'multi_agent' not in local.body
    assert {'execute_program', 'spawn_agents'} <= set(
        _wire_names(local.body['tools']))
    assert 'responses_multi_agent=v1' not in local.hdrs.get(
        'OpenAI-Beta', '')


def test_native_fallback_does_not_mask_unrelated_request_errors():
    from lib.llm._sse_core import activate_native_orchestration_fallback

    plan = type('Plan', (), {
        'programmatic_backend': 'native_openai',
        'multi_agent_backend': 'native_openai',
    })()
    canonical = {'_programmatic_eligible_tools': ['read_files']}
    assert not activate_native_orchestration_fallback(
        400, 'invalid max_tokens', plan=plan, canonical_body=canonical)
    assert not [key for key in canonical if key.startswith('_force_local')]


def test_result_metadata_keeps_compact_backend_decisions_only():
    from lib.tasks_pkg.manager import build_result_meta

    meta = build_result_meta({'_toolOrchestrationDecisions': [{
        'policyVersion': 'tool-orchestration/v1',
        'compositionMode': 'multi_agent_with_programmatic_workers',
        'round': 1,
        'programmaticCalling': 'on',
        'programmaticBackend': 'local',
        'programmaticEligibleTools': ['read_files'],
        'programmaticStage': 'large private prompt text',
        'multiAgent': 'read_only',
        'multiAgentBackend': 'local_swarm',
        'maxConcurrentAgents': 3,
    }]})

    assert meta['toolOrchestrationDecisions'] == [{
        'policyVersion': 'tool-orchestration/v1',
        'compositionMode': 'multi_agent_with_programmatic_workers',
        'round': 1,
        'programmaticCalling': 'on',
        'programmaticBackend': 'local',
        'multiAgent': 'read_only',
        'multiAgentBackend': 'local_swarm',
        'maxConcurrentAgents': 3,
    }]


def test_non_native_multi_agent_fails_closed_without_local_authority():
    from lib.swarm.routing import resolve_multi_agent_backend

    assert resolve_multi_agent_backend(
        'read_only', protocol='openai', model='other-model',
        local_swarm_available=False) == 'off'


def test_read_only_worker_catalog_closes_advisory_and_artifact_mutators():
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import ArtifactStore, SubTaskSpec

    decision = {
        'multiAgent': 'read_only',
        'programmaticCalling': 'on',
        'programmaticTier': 'program',
        'programmaticEligibleTools': ['read_files'],
    }
    agent = SubAgent(
        SubTaskSpec(role='general', objective='inspect',
                    model_override='deepseek-v4-pro'),
        parent_task={
            'id': 'parent', 'convId': 'conv',
            'config': {'_toolOrchestration': decision},
        },
        all_tools=[
            _tool('read_files'), _tool('write_file'),
            _tool('integration_submit'),
            _tool('ask_human'), _tool('await_agents'),
            _tool('get_agent_result'),
        ],
        artifact_store=ArtifactStore(),
    )
    names = set(_wire_names(agent.tools))
    assert {'read_files',
            'read_artifact', 'list_artifacts'} <= names
    assert not ({'write_file', 'integration_submit',
                 'store_artifact', 'ask_human', 'await_agents',
                 'get_agent_result'} & names)
    # Programmatic schemas are compiled at the same request boundary as chat.
    assert not hasattr(agent, '_ptc_local')
    # The worker policy rides a worker_policy injection carrier, not the
    # base system message.
    assert any('Read-only worker authority' in (m.get('content') or '')
               for m in agent.messages)


def test_native_and_local_workers_share_noninteractive_leaf_boundary():
    from lib.swarm.routing import read_only_agent_banned_names

    banned = read_only_agent_banned_names({'write_file'})
    assert {'write_file', 'store_artifact', 'ask_human',
            'spawn_agents', 'await_agents', 'get_agent_result',
            'await_task'} <= banned
    assert 'project_message' not in banned


def test_read_only_spawn_wave_budget_rejects_before_launch():
    from lib.swarm.integration._tools import _handle_spawn_agents

    task = {
        'id': 'task',
        '_toolOrchestration': {
            'multiAgent': 'read_only', 'maxConcurrentAgents': 2,
        },
    }
    raw = _handle_spawn_agents(
        {'agents': [{'objective': str(index)} for index in range(3)]},
        task_id='task', task=task, cfg={}, all_tools=[], model='m',
        thinking_enabled=False, project_path='', abort_check=None,
        on_event=None)
    result = json.loads(raw)
    assert result['error']['code'] == 'multi_agent_wave_limit'
    assert result['error']['retryable'] is True
    assert '2 agents' in result['error']['next_action']
    assert result['limit'] == 2


def test_local_spawn_propagates_identity_budget_and_worker_contract(
        monkeypatch, tmp_path):
    import lib.swarm.integration._tools as swarm_tools

    captured = {}

    class FakeMaster:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.task_id = kwargs.get('task_id')

        def run_in_background(self):
            captured['started'] = True

    monkeypatch.setattr(swarm_tools, 'MasterOrchestrator', FakeMaster)
    monkeypatch.setattr(swarm_tools, '_set_session', lambda *_a, **_k: None)
    monkeypatch.setattr(
        swarm_tools, '_resolve_output_dir', lambda _task_id: tmp_path)
    from lib.swarm import persistence
    monkeypatch.setattr(persistence, 'save_session', lambda *_a, **_k: None)

    decision = {
        'multiAgent': 'read_only', 'maxConcurrentAgents': 2,
        'programmaticCalling': 'on',
        'programmaticEligibleTools': ['read_files'],
        'compositionMode': 'multi_agent_with_programmatic_workers',
    }
    raw = swarm_tools._handle_spawn_agents(
        {'agents': [{'id': 'audit', 'objective': 'inspect'}]},
        task_id='task',
        task={'id': 'task', 'convId': 'conv', '_userId': 17,
              '_toolOrchestration': decision},
        cfg={'max_parallel': 8},
        all_tools=[_tool('read_files'), _tool('write_file')],
        model='any-model', thinking_enabled=False, project_path='',
        abort_check=None, on_event=None)

    handle = json.loads(raw)
    assert captured['started'] is True
    assert captured['user_id'] == 17
    assert captured['max_parallel'] == 2
    assert _wire_names(captured['all_tools']) == ['read_files']
    assert captured['parent_config']['_toolOrchestration'] == decision
    assert handle['orchestration'] == {
        'backend': 'local_swarm', 'mode': 'read_only',
        'compositionMode': 'multi_agent_with_programmatic_workers',
        'programmaticWorkers': True, 'maxConcurrentAgents': 2,
    }


def test_full_mode_is_the_shipped_default_and_accepted():
    from lib.context_experiment_flags import (
        DEFAULT_CONTEXT_EXPERIMENT_FLAGS,
        normalize_context_experiment_flags,
    )

    assert DEFAULT_CONTEXT_EXPERIMENT_FLAGS['orchestration'][
        'multiAgent'] == 'full'
    assert normalize_context_experiment_flags({})[
        'orchestration']['multiAgent'] == 'full'
    # Explicit values still win; invalid values fall back to the default.
    assert normalize_context_experiment_flags(
        {'orchestration': {'multiAgent': 'full'}})['orchestration'][
            'multiAgent'] == 'full'
    assert normalize_context_experiment_flags(
        {'orchestration': {'multiAgent': 'bogus'}})['orchestration'][
            'multiAgent'] == 'full'


def test_full_mode_policy_resolves_full_lane_on_any_round():
    from lib.tasks_pkg.tool_orchestration_policy import (
        resolve_tool_orchestration,
    )

    for round_num in (1, 3):
        decision = resolve_tool_orchestration(
            requested_programmatic='on', requested_multi_agent='full',
            messages=[{'role': 'user', 'content': '帮我写个脚本'}],
            tools=[_tool('read_files')], round_num=round_num, model='m')
        assert decision['multiAgent'] == 'full'
        assert decision['multiAgentReason'] == 'explicit_full'
        assert decision['compositionMode'] == (
            'multi_agent_with_programmatic_workers')
        assert decision['multiAgentStage']

    # V2 keeps the first-round + decomposable-shape gates for every lane.
    refused = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='full',
        messages=[{'role': 'user', 'content': '随便聊聊'}],
        tools=[_tool('read_files')], round_num=2, model='m',
        policy_version='v2')
    assert refused['multiAgent'] == 'disabled'
    assert refused['multiAgentReason'] == 'first_round_only'
    accepted = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='full',
        messages=[{'role': 'user', 'content': '并行调研多个方向,全面对比'}],
        tools=[_tool('read_files')], round_num=1, model='m',
        policy_version='v2')
    assert accepted['multiAgent'] == 'full'
    assert accepted['shape'] == 'independent_full_authority_agents'


def test_full_mode_backend_never_routes_to_analysis_only_native():
    from lib.swarm.routing import resolve_multi_agent_backend

    # Even on a verified public GPT-5.6 Responses face, a full-authority
    # lane must stay on the local Swarm runtime: the native extension is
    # analysis-only by contract.
    assert resolve_multi_agent_backend(
        'full', protocol='responses', model='gpt-5.6-sol',
        responses_profile='openai',
        base_url='https://api.openai.com/v1',
        local_swarm_available=True) == 'local_swarm'
    assert resolve_multi_agent_backend(
        'full', protocol='openai', model='other-model',
        local_swarm_available=False) == 'off'


def test_full_mode_wire_keeps_local_spawn_without_native_extension():
    from lib.llm._sse_core import prepare_request
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    catalog = [_tool('read_files'), SPAWN_AGENTS_TOOL]
    decision_sink = {}
    plan = prepare_request({
        'model': 'gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'compare modules'}],
        'tools': catalog,
        '_executable_tool_catalog': catalog,
        '_tool_wire_catalog': catalog,
        '_responses_feature_profile': 'openai',
        '_programmatic_tool_calling': 'off',
        '_multi_agent_mode': 'full',
        '_multi_agent_stage': 'delegate independent workstreams',
        '_multi_agent_max_concurrent_agents': 3,
        '_tool_orchestration_decision_sink': decision_sink,
    }, api_key='secret', base_url='https://api.openai.com/v1',
       api_protocol='responses')

    # Local spawn stays visible; the analysis-only native extension and its
    # beta header stay OFF for a full-authority lane.
    assert 'spawn_agents' in _wire_names(plan.body['tools'])
    assert 'multi_agent' not in plan.body
    assert 'responses_multi_agent' not in plan.hdrs.get('OpenAI-Beta', '')
    assert decision_sink['multiAgentBackend'] == 'local_swarm'


def test_full_mode_sub_agent_keeps_write_authority():
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec

    agent = SubAgent(
        SubTaskSpec(role='general', objective='implement',
                    model_override='deepseek-v4-pro'),
        parent_task={
            'id': 'parent', 'convId': 'conv',
            'config': {'_toolOrchestration': {'multiAgent': 'full'}},
        },
        all_tools=[
            _tool('read_files'), _tool('write_file'),
            _tool('run_command'), _tool('ask_human'),
            _tool('spawn_agents'),
        ],
    )
    names = set(_wire_names(agent.tools))
    # Full authority: writes survive; only the leaf-worker denylist
    # (no nested orchestration, no blocking on the human) is stripped.
    assert {'read_files', 'write_file', 'run_command'} <= names
    assert not ({'ask_human', 'spawn_agents'} & names)
    # No read-only authority clause is appended for full workers.
    assert not any('Read-only worker authority' in (m.get('content') or '')
                   for m in agent.messages)


def test_full_mode_spawn_wave_uses_standard_authority_handle(
        monkeypatch, tmp_path):
    import lib.swarm.integration._tools as swarm_tools

    captured = {}

    class FakeMaster:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.task_id = kwargs.get('task_id')

        def run_in_background(self):
            captured['started'] = True

    monkeypatch.setattr(swarm_tools, 'MasterOrchestrator', FakeMaster)
    monkeypatch.setattr(swarm_tools, '_set_session', lambda *_a, **_k: None)
    monkeypatch.setattr(
        swarm_tools, '_resolve_output_dir', lambda _task_id: tmp_path)
    from lib.swarm import persistence
    monkeypatch.setattr(persistence, 'save_session', lambda *_a, **_k: None)

    raw = swarm_tools._handle_spawn_agents(
        {'agents': [{'id': 'w', 'objective': 'implement'}]},
        task_id='task',
        task={'id': 'task', 'convId': 'conv', '_userId': 17,
              '_toolOrchestration': {
                  'multiAgent': 'full', 'maxConcurrentAgents': 2}},
        cfg={}, all_tools=[_tool('read_files'), _tool('write_file')],
        model='any-model', thinking_enabled=False, project_path='',
        abort_check=None, on_event=None)

    handle = json.loads(raw)
    assert captured['started'] is True
    # The full lane must NOT route through the read-only filter: the write
    # catalog reaches the workers, and the handle says standard authority.
    assert 'write_file' in _wire_names(captured['all_tools'])
    assert handle['orchestration'] == {
        'backend': 'local_swarm', 'mode': 'standard',
    }
