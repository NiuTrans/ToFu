"""Cross-seam contracts for the official GPT-5.6 integration."""

from __future__ import annotations

from pathlib import Path

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


def test_authoritative_contract_drives_models_slots_and_pricing():
    from lib.llm_dispatch.config import DEFAULT_SLOT_CONFIGS
    from lib.model_info._openai_gpt56 import (
        GPT56_ALIAS_TARGET,
        GPT56_CONTEXT_WINDOW,
        GPT56_MAX_OUTPUT_TOKENS,
        GPT56_MODEL_IDS,
        GPT56_REASONING_EFFORTS,
        OPENAI_TEMPLATE,
    )
    from lib.pricing import lookup_pricing

    expected = {
        'gpt-5.6', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'}
    assert GPT56_MODEL_IDS == expected
    assert GPT56_ALIAS_TARGET == 'gpt-5.6-sol'
    assert GPT56_REASONING_EFFORTS == (
        'none', 'low', 'medium', 'high', 'xhigh', 'max')
    assert GPT56_CONTEXT_WINDOW == 1_050_000
    assert GPT56_MAX_OUTPUT_TOKENS == 128_000
    assert OPENAI_TEMPLATE['protocol'] == 'responses'
    assert OPENAI_TEMPLATE['responses_profile'] == 'openai'
    assert expected <= set(DEFAULT_SLOT_CONFIGS)

    small = lookup_pricing('gpt-5.6-sol', prompt_tokens=272_000)
    large = lookup_pricing('gpt-5.6-sol', prompt_tokens=272_001)
    assert (small['input'], small['output']) == (5.0, 30.0)
    assert (large['input'], large['output']) == (10.0, 45.0)
    assert large['cacheWriteMul'] == 1.25
    assert large['cacheReadMul'] == 0.10
    assert large['selectedTier']['id'] == 'ctx_gt_272000'


def test_gpt56_effort_output_and_pro_mode_map_to_official_wire_values():
    from lib.llm import build_body
    from lib.llm.responses_outbound import openai_body_to_responses

    built = build_body(
        'gpt-5.6-sol', [{'role': 'user', 'content': 'work'}],
        max_tokens=200_000, thinking_enabled=True,
        thinking_depth='ultra', stream=False)
    assert built['max_tokens'] == 128_000
    assert built['reasoning_effort'] == 'max'

    wire, _ = openai_body_to_responses({
        **built,
        '_responses_feature_profile': 'openai',
        '_reasoning_mode': 'pro',
    })
    assert wire['model'] == 'gpt-5.6-sol'
    assert wire['reasoning']['effort'] == 'max'
    assert wire['reasoning']['mode'] == 'pro'

    fake, _ = openai_body_to_responses({
        'model': 'gpt-5.6-pro',
        'messages': [{'role': 'user', 'content': 'work'}],
        '_responses_feature_profile': 'openai',
        '_reasoning_mode': 'pro',
        '_multi_agent_mode': 'read_only',
    })
    assert 'multi_agent' not in fake
    assert 'reasoning' not in fake


def test_official_provider_migration_is_scoped_and_idempotent():
    from lib.conv_config._legacy import canonicalise_model_id
    from lib.llm_dispatch.openai_provider import (
        normalize_official_openai_provider,
    )

    old = {
        'id': 'openai', 'base_url': 'https://api.openai.com/v1',
        'protocol': 'openai',
        'models': [
            {'model_id': 'gpt-5.6-pro'},
            {'model_id': 'gpt-5.6'},
            {'model_id': 'gpt-5.6-sol'},
        ],
    }
    migrated = normalize_official_openai_provider(old)
    assert migrated['protocol'] == 'responses'
    assert migrated['responses_profile'] == 'openai'
    assert [row['model_id'] for row in migrated['models']] == [
        'gpt-5.6', 'gpt-5.6-sol']
    assert normalize_official_openai_provider(migrated) == migrated
    assert old['protocol'] == 'openai'
    assert canonicalise_model_id('gpt-5.6-pro') == 'gpt-5.6'

    gateway = {
        'base_url': 'https://openai-compatible.example/v1',
        'protocol': 'openai', 'models': [{'model_id': 'gpt-5.6-pro'}],
    }
    assert normalize_official_openai_provider(gateway) == gateway


def test_lean_prompt_is_default_for_gpt56_and_materially_smaller():
    from lib.tasks_pkg.system_prompt_cc import build_static_prompt

    kwargs = {
        'cwd': '/workspace', 'is_git': True, 'model': 'gpt-5.6-sol',
        'include_date': False,
        'tool_names': {'read_files', 'run_command', 'web_search'},
    }
    full = build_static_prompt(**kwargs, profile='full')
    lean = build_static_prompt(**kwargs, profile='lean')
    auto = build_static_prompt(**kwargs, profile='auto')
    assert auto == lean
    assert len(lean) < len(full) * 0.60
    assert 'You must NEVER generate or guess URLs' in lean
    assert 'never claim success without evidence' in lean
    assert 'System-added <system-reminder>' in lean


def test_ptc_and_multi_agent_auto_modes_are_task_gated(monkeypatch):
    from lib.tasks_pkg.gpt56_optimization import resolve_gpt56_optimizations
    import lib.tools.programmatic as programmatic

    monkeypatch.setattr(
        programmatic, 'eligible_programmatic_tool_names',
        lambda: {'read_files'})
    complex_task = [{
        'role': 'user',
        'content': (
            '全面并行检查多个模块，分别比较所有文件，筛选、去重并汇总结果'),
    }]
    decision = resolve_gpt56_optimizations(
        requested_programmatic='auto', requested_multi_agent='auto',
        messages=complex_task, tools=[_tool('read_files')], round_num=1)
    assert decision['programmaticCalling'] == 'auto'
    assert decision['programmaticEligibleTools'] == ['read_files']
    assert decision['multiAgent'] == 'off'
    assert decision['multiAgentReason'] == 'bounded_reduction_prefers_ptc'
    assert decision['programmaticStage']

    delegated = resolve_gpt56_optimizations(
        requested_programmatic='off', requested_multi_agent='auto',
        messages=complex_task, tools=[_tool('read_files')], round_num=1)
    assert delegated['multiAgent'] == 'read_only'
    assert delegated['multiAgentStage']

    small = resolve_gpt56_optimizations(
        requested_programmatic='auto', requested_multi_agent='auto',
        messages=[{'role': 'user', 'content': '打开一个文件'}],
        tools=[_tool('read_files')], round_num=1)
    assert small['programmaticCalling'] == 'off'
    assert small['multiAgent'] == 'off'

    later = resolve_gpt56_optimizations(
        requested_programmatic='auto', requested_multi_agent='auto',
        messages=complex_task, tools=[_tool('read_files')], round_num=2)
    assert later['multiAgent'] == 'off'
    assert later['multiAgentReason'] == 'first_round_only'


def test_multi_agent_wire_contract_includes_beta_header():
    from lib.llm._sse_core import prepare_request

    plan = prepare_request({
        'model': 'gpt-5.6-sol',
        'messages': [{'role': 'user', 'content': 'compare modules'}],
        '_responses_feature_profile': 'openai',
        '_multi_agent_mode': 'read_only',
        '_multi_agent_stage': 'compare independent modules',
        '_multi_agent_max_concurrent_subagents': 3,
    }, api_key='secret', base_url='https://api.openai.com/v1',
       api_protocol='responses')
    assert plan.body['multi_agent'] == {
        'enabled': True, 'max_concurrent_subagents': 3}
    assert 'responses_multi_agent=v1' in plan.hdrs['OpenAI-Beta']
    assert 'compare independent modules' in str(plan.body['input'])
    assert 'context_management' not in plan.body


def test_multi_agent_attribution_round_trips_and_blocks_subagent_writes():
    from lib.llm.responses_outbound import (
        openai_body_to_responses,
        responses_response_to_openai,
    )
    from lib.tasks_pkg.tool_dispatch._pipeline import (
        _blocked_multi_agent_write,
    )

    converted = responses_response_to_openai({
        'status': 'completed',
        'output': [{
            'type': 'function_call', 'call_id': 'call-1',
            'name': 'write_file', 'arguments': '{}',
            'agent': {'agent_name': '/root/reviewer'},
        }],
    })
    tool_call = converted['choices'][0]['message']['tool_calls'][0]
    assert tool_call['caller'] == {
        'type': 'multi_agent', 'agent_name': '/root/reviewer'}
    assert _blocked_multi_agent_write(
        tool_call, 'write_file', frozenset({'write_file'})) \
        == '/root/reviewer'
    assert not _blocked_multi_agent_write(
        tool_call, 'read_files', frozenset({'write_file'}))

    root_call = {**tool_call, 'caller': {
        'type': 'multi_agent', 'agent_name': '/root'}}
    assert not _blocked_multi_agent_write(
        root_call, 'write_file', frozenset({'write_file'}))

    replay, _ = openai_body_to_responses({
        'model': 'gpt-5.6-sol',
        '_responses_feature_profile': 'openai',
        'messages': [{
            'role': 'assistant', 'content': '', 'tool_calls': [tool_call],
        }],
    })
    assert replay['input'][0]['agent'] == {
        'agent_name': '/root/reviewer'}
    assert 'caller' not in replay['input'][0]


def test_multi_agent_only_exposes_root_final_answer_in_stream_and_json():
    import json

    from lib.llm.responses_outbound import responses_response_to_openai
    from lib.llm.responses_outbound._sse import ResponsesSSETranslator

    messages = [
        {
            'type': 'message', 'phase': 'commentary',
            'agent': {'agent_name': '/root'},
            'content': [{'type': 'output_text', 'text': 'root working'}],
        },
        {
            'type': 'message', 'phase': 'final_answer',
            'agent': {'agent_name': '/root/reviewer'},
            'content': [{'type': 'output_text', 'text': 'subagent result'}],
        },
        {
            'type': 'message', 'phase': 'final_answer',
            'agent': {'agent_name': '/root'},
            'content': [{'type': 'output_text', 'text': 'public result'}],
        },
    ]
    converted = responses_response_to_openai({
        'status': 'completed', 'output': messages})
    assert converted['choices'][0]['message']['content'] == 'public result'

    translator = ResponsesSSETranslator('gpt-5.6-sol')
    chunks = []
    for output_index, item in enumerate(messages):
        translator.translate(json.dumps({
            'type': 'response.output_item.added',
            'output_index': output_index, 'item': item,
        }))
        chunks.extend(translator.translate(json.dumps({
            'type': 'response.output_text.delta',
            'output_index': output_index,
            'agent': item['agent'],
            'delta': item['content'][0]['text'],
        })))
    visible = ''.join(
        chunk['choices'][0]['delta'].get('content', '')
        for chunk in chunks)
    assert visible == 'public result'


def test_frontend_defaults_expose_auto_gates_and_no_fake_pro_model():
    root = Path(__file__).resolve().parents[1]
    panel = (root / 'static/settings_panels/advanced.html').read_text()
    runtime = (root / 'frontend/src/runtime/app-runtime.js').read_text()
    assert 'id="settingResponsesPromptProfile"' in panel
    assert '<option value="auto">auto</option>' in panel
    assert "responsesCfg.promptProfile, 'auto'" in runtime
    assert "responsesCfg.multiAgent, 'auto'" in runtime
    assert "toolsCfg.programmaticCalling, 'auto'" in runtime
    assert "model_id: 'gpt-5.6-pro'" not in runtime
