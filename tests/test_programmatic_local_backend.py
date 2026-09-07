"""Local-backend (all-models) Programmatic Tool Calling contract tests.

Pins the dual-backend resolution (native_openai vs local), the model tiering
(program vs batch), the tier-shaped execute_tools wire projection, the
Responses converter's respect for the resolved backend, and the gateway
handler's task-catalog authority for local ToolScript children.
"""

import json
import logging
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.model_info._openai_gpt56 import GPT56_ALIAS_TARGET
from lib.tasks_pkg.tool_orchestration_policy import (
    latest_user_text,
    resolve_tool_orchestration,
)
from lib.tasks_pkg.handlers import tool_gateway as gw_handler
import lib.tools.gateway as gw
from lib.tools.programmatic import (
    eligible_programmatic_tool_names,
    local_ptc_guidance,
    programmatic_tier,
    resolve_programmatic_backend,
)

pytestmark = pytest.mark.unit


def test_local_program_run_never_reuses_native_parent_id():
    from lib.tasks_pkg.handlers.tool_gateway import _program_run

    native = {'callId': 'shared', 'source': 'openai_ptc',
              'status': 'running'}
    task = {'programRuns': [native]}
    local = _program_run(task, 'shared', 'execute_program')

    assert local is not native
    assert local['callId'] != native['callId']
    assert local['gatewayCallId'] == 'shared'
    assert local['source'] == 'execute_program'
    assert _program_run(task, 'shared', 'execute_program') is local


_GPT56 = GPT56_ALIAS_TARGET  # a verified GPT-5.6 family model id


# ── backend resolution ──────────────────────────────────────────────


def test_backend_off_when_not_requested():
    assert resolve_programmatic_backend('off', protocol='responses',
                                        model=_GPT56) == 'off'
    assert resolve_programmatic_backend('', protocol='responses',
                                        model=_GPT56) == 'off'


def test_backend_off_without_eligible_tools():
    assert resolve_programmatic_backend(
        'auto', protocol='responses', model=_GPT56,
        base_url='https://api.openai.com/v1', eligible_present=False) == 'off'


def test_backend_native_openai_only_on_public_responses_gpt56():
    assert resolve_programmatic_backend(
        'auto', protocol='responses', model=_GPT56,
        base_url='https://api.openai.com/v1') == 'native_openai'
    # Explicit operator-declared profile also qualifies.
    assert resolve_programmatic_backend(
        'auto', protocol='responses', model=_GPT56,
        responses_profile='openai') == 'native_openai'


def test_backend_local_for_gateways_codex_and_non_gpt56():
    # GPT-5.6 through a non-OpenAI gateway is core-compatible, not public.
    assert resolve_programmatic_backend(
        'auto', protocol='responses', model=_GPT56,
        base_url='https://gateway.example.com/v1') == 'local'
    # Codex subscription traffic never inherits public-only fields.
    assert resolve_programmatic_backend(
        'auto', protocol='responses', model=_GPT56,
        base_url='https://chatgpt.com/backend-api/codex',
        oauth='codex') == 'local'
    # Chat-Completions models (any family) get the local backend.
    assert resolve_programmatic_backend(
        'auto', protocol='openai', model='deepseek-v4-pro',
        base_url='https://api.deepseek.com/v1') == 'local'
    assert resolve_programmatic_backend(
        'auto', protocol='anthropic', model='claude-opus-4-6') == 'local'
    # Non-GPT-5.6 model on the public Responses API is still local.
    assert resolve_programmatic_backend(
        'auto', protocol='responses', model='gpt-5.5',
        base_url='https://api.openai.com/v1') == 'local'


# ── model tiering ───────────────────────────────────────────────────


def test_tier_env_override(monkeypatch):
    monkeypatch.setenv('TOFU_PTC_TIER', 'batch')
    assert programmatic_tier('gpt-5.5') == 'batch'
    monkeypatch.setenv('TOFU_PTC_TIER', 'program')
    assert programmatic_tier('unknown-tiny-model') == 'program'


def test_tier_defaults_to_program_for_every_model():
    # No model-size split: large, small, and unknown models all get the full
    # ToolScript surface; only TOFU_PTC_TIER=batch strips the program param.
    assert programmatic_tier('gpt-5.5') == 'program'
    assert programmatic_tier('unknown-tiny-model') == 'program'
    assert programmatic_tier('definitely-unknown-model-xyz') == 'program'


def test_tier_ignores_context_window(monkeypatch):
    from lib.model_info import _context as ctx_mod

    def _boom(*args, **kwargs):
        raise AssertionError('context window must not be consulted')

    monkeypatch.setattr(ctx_mod, 'context_profile', _boom)
    assert programmatic_tier('whatever') == 'program'


# ── guidance text ───────────────────────────────────────────────────


def test_local_guidance_tiers():
    prog = local_ptc_guidance('program', ['read_files', 'grep_search'])
    assert 'program' in prog and 'task-executable tool' in prog
    assert 'read_files' not in prog and 'grep_search' not in prog
    assert 'schema' in prog and 'approval' in prog
    # The unified surface still advertises the simpler parallel-calls path
    # for independent lookups (the former batch-tier guidance, merged).
    assert 'execution=parallel' in prog
    batch = local_ptc_guidance('batch', ['read_files'])
    assert 'execution=parallel' in batch
    assert 'read_files' not in batch
    # Batch override shape must not advertise free-form programs.
    assert 'ToolScript' not in batch


# ── tier-shaped wire projection ─────────────────────────────────────


def _fn(name):
    return {'type': 'function',
            'function': {'name': name,
                         'parameters': {'type': 'object', 'properties': {}}}}


def _wire_names(tools):
    return [((t.get('function') or {}).get('name') or '')
            for t in tools if isinstance(t, dict)]


def test_execute_tools_schema_default_byte_shape():
    schema = gw.execute_tools_schema()
    props = schema['function']['parameters']['properties']
    assert 'program' in props and 'calls' in props
    assert 'ToolScript supports' in schema['function']['description']
    assert 'JSON.parse/stringify' in schema['function']['description']
    assert 'not JavaScript' in schema['function']['description']


def test_programmatic_internal_result_budget_is_aggregate_and_order_fair():
    from lib.tools.programmatic import ProgrammaticResultBudget

    budget = ProgrammaticResultBudget(max_bytes=10)
    first = budget.begin_batch(['a', 'b'])
    first.capture('b', 'B')
    first.capture('a', 'A' * 20)
    first.finish()

    assert first.result('a') == {
        'content': 'A' * 5, 'rawBytes': 20, 'outputBytes': 5,
        'truncated': True,
    }
    assert first.result('b')['content'] == 'B'
    assert budget.stats()['remainingBytes'] == 4

    second = budget.begin_batch(['c'])
    second.capture('c', '甲乙丙')
    second.finish()
    assert second.result('c')['content'] == '甲'
    assert second.result('c')['truncated'] is True
    assert budget.stats()['outputBytes'] == 9
    assert budget.stats()['remainingBytes'] == 1


def test_tool_schema_fingerprint_tracks_description_and_key_order():
    base = [_fn('read_files')]
    described = [_fn('read_files')]
    described[0]['function']['description'] = 'changed description'
    reordered = [_fn('read_files')]
    reordered[0]['function']['parameters']['properties'] = {
        'b': {'type': 'string'}, 'a': {'type': 'string'},
    }
    same_keys_other_order = [_fn('read_files')]
    same_keys_other_order[0]['function']['parameters']['properties'] = {
        'a': {'type': 'string'}, 'b': {'type': 'string'},
    }

    assert len(gw.tool_schema_fingerprint(base)) == 64
    assert gw.tool_schema_fingerprint(base) \
        != gw.tool_schema_fingerprint(described)
    assert gw.tool_schema_fingerprint(reordered) \
        != gw.tool_schema_fingerprint(same_keys_other_order)


def test_ptc_projection_appends_program_tier():
    tools = [_fn('read_files'), _fn('grep_search')]
    out = gw.ptc_local_wire_tools(tools, tier='program',
                                  eligible=['read_files'])
    assert _wire_names(out)[:2] == ['read_files', 'grep_search']
    assert _wire_names(out).count('execute_tools') == 1
    execute = out[-1]
    props = execute['function']['parameters']['properties']
    assert 'program' in props
    assert 'task-executable tool' not in execute['function']['description']
    assert 'read_files' not in execute['function']['description']


def test_ptc_projection_batch_tier_strips_program():
    out = gw.ptc_local_wire_tools([_fn('read_files')], tier='batch',
                                  eligible=['read_files'])
    execute = out[-1]
    props = execute['function']['parameters']['properties']
    assert 'program' not in props
    assert 'execution=parallel' in execute['function']['description']
    assert 'read_files' not in execute['function']['description']


def test_ptc_projection_replaces_existing_gateway_schema():
    tools = [_fn('read_files'), gw.execute_tools_schema(),
             _fn('grep_search')]
    out = gw.ptc_local_wire_tools(tools, tier='batch',
                                  eligible=['read_files'])
    assert _wire_names(out).count('execute_tools') == 1
    execute = [t for t in out
               if (t.get('function') or {}).get('name')
               == 'execute_tools'][0]
    assert 'program' not in execute['function']['parameters']['properties']


# ── Responses converter respects the resolved backend ───────────────


def _responses_body(backend):
    return {
        'model': _GPT56,
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{
            'type': 'function',
            'function': {'name': 'read_files', 'description': 'read',
                         'parameters': {'type': 'object',
                                        'properties': {
                                            'paths': {'type': 'array'}}}},
        }],
        '_responses_feature_profile': 'openai',
        '_programmatic_tool_calling': 'auto',
        '_resolved_programmatic_backend': backend,
    }


def test_responses_native_backend_keeps_hosted_ptc():
    from lib.llm.responses_outbound import openai_body_to_responses
    assert 'read_files' in eligible_programmatic_tool_names()
    out, _ = openai_body_to_responses(_responses_body('native_openai'))
    assert any(t.get('type') == 'programmatic_tool_calling'
               for t in out['tools'])


def test_responses_local_backend_never_emits_hosted_ptc():
    from lib.llm.responses_outbound import openai_body_to_responses
    out, _ = openai_body_to_responses(_responses_body('local'))
    assert not any(t.get('type') == 'programmatic_tool_calling'
                   for t in out['tools'])
    fn = [t for t in out['tools'] if t.get('name') == 'read_files'][0]
    assert 'allowed_callers' not in fn
    assert 'output_schema' not in fn


# ── intent resolution carries the tier ──────────────────────────────


def test_intent_resolution_attaches_tier():
    tools = [{'type': 'function', 'function': {'name': 'read_files'}}]
    decision = resolve_tool_orchestration(
        requested_programmatic='auto', requested_multi_agent='off',
        messages=[{'role': 'user',
                   'content': '请批量对比所有文件并去重汇总'}],
        tools=tools, round_num=1, model='gpt-5.5')
    assert decision['programmaticCalling'] == 'auto'
    assert decision['programmaticTier'] == 'program'
    assert decision['programmaticEligibleTools'] == ['read_files']

    small = resolve_tool_orchestration(
        requested_programmatic='auto', requested_multi_agent='off',
        messages=[{'role': 'user',
                   'content': '请批量对比所有文件并去重汇总'}],
        tools=tools, round_num=1, model='definitely-unknown-model-xyz')
    assert small['programmaticTier'] == 'program'

    off = resolve_tool_orchestration(
        requested_programmatic='auto', requested_multi_agent='off',
        messages=[{'role': 'user', 'content': '帮我改一下这个函数'}],
        tools=tools, round_num=1, model='gpt-5.5')
    assert off['programmaticCalling'] == 'off'
    assert off['programmaticTier'] == ''


# ── handler-side executable-catalog authority ───────────────────────


def _mk_task(ptc_local=None):
    todo = _fn('todo_write')
    todo['function']['parameters'] = {
        'type': 'object',
        'properties': {'todos': {'type': 'array'}},
        'required': ['todos'],
        'additionalProperties': False,
    }
    project_post = _fn('project_board_post')
    project_post['function']['parameters'] = {
        'type': 'object',
        'properties': {'title': {'type': 'string'}},
        'required': ['title'],
        'additionalProperties': False,
    }
    task = {
        'id': 'test-ptc',
        '_userId': 1,
        'model': 'm',
        'toolRounds': [],
        'events': [],
        '_tool_stream_active': True,
        'events_lock': threading.Lock(),
        '_executable_tool_catalog': [
            _fn('read_files'), _fn('grep_search'), todo, project_post],
    }
    if ptc_local is not None:
        task['_ptc_local'] = ptc_local
    return task


def _run_program(task, source):
    tc = {'id': 'g1', 'type': 'function',
          'function': {'name': 'execute_tools', 'arguments': '{}'}}
    _, content, _ = gw_handler.handle_execute_tools(
        task, tc, 'execute_tools', 'g1', {'program': source},
        11, {'calls': []}, {}, None, True)
    return json.loads(content)


def test_program_latch_does_not_narrow_executable_catalog(monkeypatch):
    seen = []

    def fake_execute(_task, calls, execution, **_kwargs):
        seen.extend(calls)
        assert execution == 'sequential'
        call = calls[0]
        return [{
            'call_id': call['id'],
            'name': call['function']['name'],
            'status': 'done',
            'approval': {'required': False, 'status': 'not_required'},
            'duration': 0,
            'source': 'execute_program',
            'output': 'ok',
        }]

    monkeypatch.setattr(gw_handler, '_execute_normalized', fake_execute)
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})
    payload = _run_program(
        task, "return tools.call('project_board_post', {'title': 'epic'});")
    assert payload['program']['status'] == 'ok'
    assert [call['function']['name'] for call in seen] == [
        'project_board_post']


def test_program_still_rejects_arguments_outside_catalog_schema(monkeypatch):
    def must_not_execute(*_args, **_kwargs):
        raise AssertionError('schema-invalid ToolScript child reached executor')

    monkeypatch.setattr(gw_handler, '_execute_normalized', must_not_execute)
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})
    payload = _run_program(
        task, "return tools.call('project_board_post', {'wrong': []});")

    assert payload['program']['status'] == 'ok'
    assert payload['program']['result']['status'] == 'error'
    assert 'missing_required_arguments' in json.dumps(payload)


def test_program_accepts_hosted_ptc_eligible_tool():
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})
    payload = _run_program(
        task, "const r = tools.call('read_files', "
              "{'paths': ['__missing__.txt']}); return r;")
    assert payload['program']['status'] == 'ok'


def test_program_uses_catalog_without_ptc_latch_or_search_receipt():
    task = _mk_task()
    payload = _run_program(
        task, "const r = tools.call('todo_write', {'todos': []}); return r;")
    assert payload['program']['status'] == 'ok'


def test_program_allows_search_tools_discovery():
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})
    task['_executable_tool_catalog'].append(_fn('search_tools'))
    payload = _run_program(
        task, "const r = tools.call('search_tools', {'query': 'read'}); "
              "return r;")
    assert payload['program']['status'] == 'ok'


def test_repeated_zero_child_authoring_failures_stick_to_batch_fallback():
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})

    def invoke(call_id, program):
        tc = {'id': call_id, 'type': 'function',
              'function': {'name': 'execute_tools', 'arguments': '{}'}}
        _, content, _ = gw_handler.handle_execute_tools(
            task, tc, 'execute_tools', call_id, {'program': program},
            11, {'calls': [], 'llmRound': 11}, {}, None, True)
        return json.loads(content)

    first = invoke('bad-program-1', "return 'x'.repeat(2);")
    assert first['program']['error']['code'] == 'unsafe_call'
    assert task.get('_toolScriptBatchFallback') is not True

    second = invoke('bad-program-2', "return 'y'.repeat(2);")
    assert second['program']['error']['code'] == 'unsafe_call'
    assert task['_toolScriptBatchFallback'] is True
    assert task['_ptc_local']['tier'] == 'batch'

    blocked = invoke('bad-program-3', "return {shouldNotRun:true};")
    assert blocked['program']['error']['code'] == 'toolscript_batch_fallback'
    assert len(task['programRuns']) == 2


# ── observed read fan-out activation ────────────────────────────────


def _tc(call_id, name):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': '{}'}}


def test_observed_parallel_read_fanout_activates_ptc():
    tools = [_fn('read_files'), _fn('todo_write')]
    history = [
        {'role': 'user', 'content': '帮我看看这段逻辑'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            _tc('1', 'read_files'), _tc('2', 'read_files')]},
        {'role': 'tool', 'tool_call_id': '1', 'content': 'a'},
        {'role': 'tool', 'tool_call_id': '2', 'content': 'b'},
    ]
    decision = resolve_tool_orchestration(
        requested_programmatic='auto', requested_multi_agent='off',
        messages=history, tools=tools, round_num=2, model='gpt-5.5')
    assert decision['programmaticCalling'] == 'auto'
    assert decision['programmaticReason'] == 'observed_read_fanout'
    assert decision['programmaticTier'] == 'program'


def test_observed_serialized_read_fanout_activates_ptc():
    tools = [_fn('read_files')]
    history = [
        {'role': 'user', 'content': '帮我看看这段逻辑'},
        {'role': 'assistant', 'content': '', 'tool_calls': [_tc('1', 'read_files')]},
        {'role': 'tool', 'tool_call_id': '1', 'content': 'a'},
        {'role': 'assistant', 'content': '', 'tool_calls': [_tc('2', 'read_files')]},
        {'role': 'tool', 'tool_call_id': '2', 'content': 'b'},
        {'role': 'assistant', 'content': '', 'tool_calls': [_tc('3', 'read_files')]},
        {'role': 'tool', 'tool_call_id': '3', 'content': 'c'},
    ]
    decision = resolve_tool_orchestration(
        requested_programmatic='auto', requested_multi_agent='off',
        messages=history, tools=tools, round_num=4,
        model='definitely-unknown-model-xyz')
    assert decision['programmaticCalling'] == 'auto'
    assert decision['programmaticReason'] == 'observed_read_fanout'
    # Serialized fan-out from a small/unknown model activates the same full
    # ToolScript surface — no batch demotion.
    assert decision['programmaticTier'] == 'program'


def test_write_fanout_and_single_read_do_not_activate_ptc():
    tools = [_fn('read_files'), _fn('todo_write')]
    # Writes never count toward the read-only fan-out signal.
    history = [
        {'role': 'user', 'content': '帮我看看这段逻辑'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            _tc('1', 'read_files'), _tc('2', 'todo_write')]},
        {'role': 'tool', 'tool_call_id': '1', 'content': 'a'},
        {'role': 'tool', 'tool_call_id': '2', 'content': 'b'},
    ]
    decision = resolve_tool_orchestration(
        requested_programmatic='auto', requested_multi_agent='off',
        messages=history, tools=tools, round_num=2, model='gpt-5.5')
    assert decision['programmaticCalling'] == 'off'
    assert decision['programmaticReason'] == 'task_not_bounded_reduction_shape'
    # A lone read is ordinary work, not a bounded reduction.
    single = [
        {'role': 'user', 'content': '帮我看看这段逻辑'},
        {'role': 'assistant', 'content': '', 'tool_calls': [_tc('1', 'read_files')]},
        {'role': 'tool', 'tool_call_id': '1', 'content': 'a'},
    ]
    lone = resolve_tool_orchestration(
        requested_programmatic='auto', requested_multi_agent='off',
        messages=single, tools=tools, round_num=2, model='gpt-5.5')
    assert lone['programmaticCalling'] == 'off'


# ── provider wire boundary (prepare_request) ────────────────────────


def _ptc_body(model):
    return {
        'model': model,
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [_fn('read_files')],
        '_programmatic_tool_calling': 'auto',
        '_programmatic_tier': 'batch',
        '_programmatic_eligible_tools': ['read_files'],
    }


def test_wire_boundary_local_backend_chat_completions():
    from lib.llm._sse_core import prepare_request
    plan = prepare_request(
        _ptc_body('deepseek-v4-pro'), api_key='secret',
        base_url='https://api.deepseek.com/v1', api_protocol='openai')
    names = _wire_names(plan.body.get('tools'))
    assert names.count('execute_tools') == 1
    execute = [t for t in plan.body['tools']
               if (t.get('function') or {}).get('name')
               == 'execute_tools'][0]
    assert 'program' not in execute['function']['parameters']['properties']
    # Provider projection must not append body-only guidance: it would vanish
    # from task history and break the next request's cache prefix.
    assert plan.body['messages'] == [{'role': 'user', 'content': 'hi'}]
    assert 'read_files' not in execute['function']['description']
    # Internal PTC keys must never leak onto the verbatim OpenAI wire.
    for key in ('_programmatic_tool_calling', '_programmatic_stage',
                '_programmatic_tier', '_programmatic_eligible_tools',
                '_resolved_programmatic_backend'):
        assert key not in plan.body


def test_wire_boundary_local_backend_anthropic():
    from lib.llm._sse_core import prepare_request
    plan = prepare_request(
        _ptc_body('claude-opus-4-6'), api_key='secret',
        base_url='https://api.anthropic.com', api_protocol='anthropic')
    names = [str(t.get('name') or '') for t in plan.body.get('tools') or []]
    assert names.count('execute_tools') == 1
    execute = [t for t in plan.body['tools']
               if t.get('name') == 'execute_tools'][0]
    assert 'program' not in (
        execute.get('input_schema') or {}).get('properties', {})


def test_wire_boundary_native_backend_keeps_hosted_ptc():
    from lib.llm._sse_core import prepare_request
    body = _ptc_body(_GPT56)
    body['_responses_feature_profile'] = 'openai'
    body['_programmatic_tier'] = 'program'
    plan = prepare_request(
        body, api_key='secret', base_url='https://api.openai.com/v1',
        api_protocol='responses')
    kinds = [str(t.get('type') or '') for t in plan.body.get('tools') or []]
    names = [str(t.get('name') or '') for t in plan.body.get('tools') or []]
    # Hosted PTC stays with the Responses converter; the local projection
    # must NOT add a client-side execute_tools surface on this route.
    assert 'programmatic_tool_calling' in kinds
    assert 'execute_tools' not in names


def test_wire_boundary_off_mode_leaves_wire_untouched():
    from lib.llm._sse_core import prepare_request
    body = _ptc_body('deepseek-v4-pro')
    body['_programmatic_tool_calling'] = 'off'
    plan = prepare_request(
        body, api_key='secret',
        base_url='https://api.deepseek.com/v1', api_protocol='openai')
    assert _wire_names(plan.body.get('tools')) == ['read_files']


def test_wire_boundary_absent_tier_defaults_to_program_surface():
    from lib.llm._sse_core import prepare_request
    body = _ptc_body('deepseek-v4-pro')
    del body['_programmatic_tier']
    plan = prepare_request(
        body, api_key='secret',
        base_url='https://api.deepseek.com/v1', api_protocol='openai')
    execute = [t for t in plan.body['tools']
               if (t.get('function') or {}).get('name')
               == 'execute_tools'][0]
    assert 'program' in execute['function']['parameters']['properties']


# ── resident (on) mode: universal programmatic exposure ─────────────


def test_on_mode_activates_without_any_text_shape():
    tools = [{'type': 'function', 'function': {'name': 'read_files'}}]
    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=[{'role': 'user', 'content': '帮我改一下这个函数'}],
        tools=tools, round_num=1, model='gpt-5.5')
    assert decision['programmaticCalling'] == 'on'
    assert decision['programmaticReason'] == 'resident_eligible_read_tools'
    assert decision['programmaticTier'] == 'program'
    assert decision['programmaticStage']
    assert decision['programmaticEligibleTools'] == ['read_files']

    # Unknown/small models get the same resident surface — no batch demotion.
    small = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=[{'role': 'user', 'content': '你好'}],
        tools=tools, round_num=1, model='definitely-unknown-model-xyz')
    assert small['programmaticCalling'] == 'on'
    assert small['programmaticTier'] == 'program'


def test_on_mode_without_eligible_tools_is_off():
    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=[{'role': 'user', 'content': '随便聊聊'}],
        tools=[_fn('todo_write')], round_num=1, model='gpt-5.5')
    assert decision['programmaticCalling'] == 'off'
    assert decision['programmaticReason'] == 'no_eligible_read_tools'
    assert decision['programmaticTier'] == ''


def test_on_mode_composes_with_multi_agent():
    tools = [_fn('read_files')]
    complex_msgs = [{'role': 'user', 'content': (
        '全面并行检查多个模块，分别比较所有文件，筛选、去重并汇总结果')}]
    # Explicit and automatic multi-agent decisions are independent from the
    # resident PTC data plane.
    explicit = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='read_only',
        messages=complex_msgs, tools=tools, round_num=1, model='gpt-5.5')
    assert explicit['programmaticCalling'] == 'on'
    assert explicit['multiAgent'] == 'read_only'
    assert explicit['compositionMode'] == (
        'multi_agent_with_programmatic_workers')
    auto = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='auto',
        messages=complex_msgs, tools=tools, round_num=1, model='gpt-5.5')
    assert auto['programmaticCalling'] == 'on'
    assert auto['multiAgent'] == 'read_only'
    assert auto['multiAgentReason'] == 'independent_complex_workstreams'
    assert auto['compositionMode'] == (
        'multi_agent_with_programmatic_workers')


def test_flags_ship_on_default_and_validate_on():
    from lib.context_experiment_flags import (
        normalize_context_experiment_flags)
    assert normalize_context_experiment_flags(
        {})['tools']['programmaticCalling'] == 'on'
    assert normalize_context_experiment_flags(
        {'tools': {'programmaticCalling': 'on'}}
    )['tools']['programmaticCalling'] == 'on'
    assert normalize_context_experiment_flags(
        {'tools': {'programmaticCalling': 'auto'}}
    )['tools']['programmaticCalling'] == 'auto'
    # Invalid values fall back to the shipped resident default.
    assert normalize_context_experiment_flags(
        {'tools': {'programmaticCalling': 'guess'}}
    )['tools']['programmaticCalling'] == 'on'
    with pytest.raises(ValueError, match='programmaticCalling'):
        normalize_context_experiment_flags(
            {'tools': {'programmaticCalling': 'guess'}}, strict=True)


def test_backend_resolver_accepts_on():
    assert resolve_programmatic_backend(
        'on', protocol='responses', model=_GPT56,
        base_url='https://api.openai.com/v1') == 'native_openai'
    assert resolve_programmatic_backend(
        'on', protocol='openai', model='deepseek-v4-pro',
        base_url='https://api.deepseek.com/v1') == 'local'
    assert resolve_programmatic_backend(
        'on', protocol='openai', model='deepseek-v4-pro',
        base_url='https://api.deepseek.com/v1',
        eligible_present=False) == 'off'


def test_wire_boundary_on_mode_projects_execute_tools():
    from lib.llm._sse_core import prepare_request
    body = _ptc_body('deepseek-v4-pro')
    body['_programmatic_tool_calling'] = 'on'
    plan = prepare_request(
        body, api_key='secret',
        base_url='https://api.deepseek.com/v1', api_protocol='openai')
    names = _wire_names(plan.body.get('tools'))
    assert names.count('execute_tools') == 1


def test_responses_on_mode_keeps_hosted_ptc():
    from lib.llm.responses_outbound import openai_body_to_responses
    body = _responses_body('native_openai')
    body['_programmatic_tool_calling'] = 'on'
    out, _ = openai_body_to_responses(body)
    assert any(t.get('type') == 'programmatic_tool_calling'
               for t in out['tools'])


# ── serial-chain escalation (adoption lever) ────────────────────────


def test_local_guidance_dependent_chain_example():
    prog = local_ptc_guidance('program', ['read_files'])
    # The execute_tools schema already carries the concrete ToolScript calls;
    # the stable note stays compact so the fixed gateway pair remains within
    # its independent 500-token wire contract.
    assert 'dependent calls and compact JSON' in prog
    # The batch override shape stays free of program guidance.
    assert 'dependent calls and compact JSON' not in local_ptc_guidance(
        'batch', ['read_files'])


def test_local_guidance_serial_chain_rule_is_static():
    note = local_ptc_guidance('program', ['read_files'])
    assert 'Do not continue a serial chain' in note
    assert 'read_files' not in note
    # Observed call names are telemetry, not schema-description inputs.
    assert 'find_files > grep_search' not in note


def _serial_history(rounds):
    history = [{'role': 'user', 'content': '帮我排查这个问题'}]
    for index, name in enumerate(rounds):
        call_id = str(index + 1)
        history.append({'role': 'assistant', 'content': '',
                        'tool_calls': [_tc(call_id, name)]})
        history.append({'role': 'tool', 'tool_call_id': call_id,
                        'content': 'x'})
    return history


def test_general_serial_chain_respects_control_carriers_and_user_boundary():
    from lib.tasks_pkg.tool_orchestration_policy import (
        observed_single_tool_serial_chain,
    )

    history = _serial_history(['run_command'] * 6)
    history.insert(-4, {
        'role': 'user',
        'content': '<swarm-update>peer evidence</swarm-update>',
        '_isInboxInject': True,
        '_containsHumanSteer': False,
    })
    history.insert(-2, {
        'role': 'user',
        'content': '[SYSTEM: bounded control carrier]',
        '_isMeta': True,
    })
    assert observed_single_tool_serial_chain(
        history, {'run_command'}, minimum=6, maximum=6,
    ) == ['run_command'] * 6

    history.extend([
        {'role': 'user', 'content': 'Stop and inspect only x.py.'},
        {'role': 'assistant', 'content': '',
         'tool_calls': [_tc('after-steer', 'run_command')]},
        {'role': 'tool', 'tool_call_id': 'after-steer', 'content': 'x'},
    ])
    assert observed_single_tool_serial_chain(
        history, {'run_command'}, minimum=6, maximum=6,
    ) == []


def test_serial_chain_detected_in_resident_mode():
    tools = [_fn('read_files'), _fn('grep_search'), _fn('find_files')]
    history = _serial_history(['find_files', 'grep_search', 'read_files'])
    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=history, tools=tools, round_num=4, model='gpt-5.5')
    assert decision['programmaticCalling'] == 'on'
    assert decision['programmaticSerialChain'] == [
        'find_files', 'grep_search', 'read_files']

    # Two single-call rounds are normal exploration, not escalation-worthy.
    short = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=_serial_history(['find_files', 'grep_search']),
        tools=tools, round_num=3, model='gpt-5.5')
    assert short['programmaticSerialChain'] == []

    # PTC off rounds never carry the escalation payload.
    off = resolve_tool_orchestration(
        requested_programmatic='off', requested_multi_agent='off',
        messages=history, tools=tools, round_num=4, model='gpt-5.5')
    assert off['programmaticSerialChain'] == []


def test_serial_chain_ignores_synthetic_context_carriers():
    tools = [_fn('read_files'), _fn('grep_search'), _fn('find_files')]
    history = [{'role': 'user', 'content': 'inspect the repository'}]
    for index, name in enumerate(
            ('find_files', 'grep_search', 'read_files')):
        call_id = str(index + 1)
        history.extend([
            {'role': 'assistant', 'content': '',
             'tool_calls': [_tc(call_id, name)]},
            {'role': 'tool', 'tool_call_id': call_id, 'content': 'evidence'},
        ])
        if index < 2:
            history.append({
                'role': 'user',
                'content': '<system-reminder>round context</system-reminder>',
                '_contextComposer': True,
            })

    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=history, tools=tools, round_num=4, model='kimi-k3')

    assert decision['programmaticSerialChain'] == [
        'find_files', 'grep_search', 'read_files']


def test_genuine_user_steering_breaks_serial_chain():
    tools = [_fn('read_files'), _fn('grep_search'), _fn('find_files')]
    history = _serial_history(['find_files', 'grep_search'])
    history.extend([
        {'role': 'user', 'content': 'Stop exploring and inspect only x.py.'},
        {'role': 'assistant', 'content': '',
         'tool_calls': [_tc('after-steer', 'read_files')]},
        {'role': 'tool', 'tool_call_id': 'after-steer',
         'content': 'fresh evidence'},
    ])

    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=history, tools=tools, round_num=4, model='kimi-k3')

    assert decision['programmaticSerialChain'] == []


def test_pure_inbox_evidence_is_transparent_but_human_steer_is_boundary():
    tools = [_fn('read_files'), _fn('grep_search'), _fn('find_files')]
    pure_inbox = _serial_history(
        ['find_files', 'grep_search', 'read_files'])
    pure_inbox.append({
        'role': 'user',
        'content': '<swarm-update>peer evidence</swarm-update>',
        '_isInboxInject': True,
        '_containsHumanSteer': False,
    })

    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=pure_inbox, tools=tools, round_num=4, model='kimi-k3')

    assert latest_user_text(pure_inbox) == '帮我排查这个问题'
    assert decision['programmaticSerialChain'] == [
        'find_files', 'grep_search', 'read_files']

    human_steer = list(pure_inbox[:-1])
    human_steer.append({
        'role': 'user',
        'content': 'Stop and inspect only x.py.',
        '_isInboxInject': True,
        '_containsHumanSteer': True,
    })
    steered = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=human_steer, tools=tools, round_num=4, model='kimi-k3')

    assert latest_user_text(human_steer) == 'Stop and inspect only x.py.'
    assert steered['programmaticSerialChain'] == []


def test_orchestration_intent_ignores_synthetic_user_role_carriers():
    messages = [
        {'role': 'user', 'content': 'implement the requested code change'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            _tc('read-1', 'read_files'),
        ]},
        {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'evidence'},
        {'role': 'user',
         'content': '[SYSTEM: SERIAL READ CHAIN DETECTED]',
         '_isMeta': True},
        {'role': 'user',
         'content': '<system-reminder>round context</system-reminder>',
         '_contextComposer': True},
    ]

    assert latest_user_text(messages) == 'implement the requested code change'


def test_serial_chain_broken_by_parallel_or_write_calls():
    tools = [_fn('read_files'), _fn('grep_search'), _fn('todo_write')]
    # A trailing write call breaks the read-only chain.
    decision = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=_serial_history(['list_dir', 'grep_search', 'todo_write']),
        tools=tools, round_num=4, model='gpt-5.5')
    assert decision['programmaticSerialChain'] == []
    # A parallel batch in the latest round also breaks the serial signal.
    parallel = _serial_history(['list_dir', 'grep_search'])
    parallel.append({'role': 'assistant', 'content': '', 'tool_calls': [
        _tc('p1', 'read_files'), _tc('p2', 'read_files')]})
    parallel.append({'role': 'tool', 'tool_call_id': 'p1', 'content': 'a'})
    parallel.append({'role': 'tool', 'tool_call_id': 'p2', 'content': 'b'})
    batch = resolve_tool_orchestration(
        requested_programmatic='on', requested_multi_agent='off',
        messages=parallel, tools=tools, round_num=4, model='gpt-5.5')
    assert batch['programmaticSerialChain'] == []


def test_ptc_schema_does_not_carry_runtime_serial_chain_rule():
    out = gw.ptc_local_wire_tools(
        [_fn('read_files')], tier='program', eligible=['read_files'])
    execute = out[-1]
    assert 'Do not continue a serial chain' not in execute['function']['description']
    assert 'find_files > grep_search' not in execute['function']['description']


def test_ptc_projection_preserves_fixed_gateway_token_contract():
    eligible = [f'reviewed_read_only_tool_{index}' for index in range(64)]
    out = gw.ptc_local_wire_tools(
        [gw.search_tools_schema(), gw.execute_tools_schema()],
        tier='program', eligible=eligible)
    gateways = [tool for tool in out
                if (tool.get('function') or {}).get('name')
                in {gw.SEARCH_TOOLS_NAME, gw.EXECUTE_TOOLS_NAME}]
    for model in ('kimi-k3', 'gpt-5.4'):
        assert gw.tool_schema_tokens(gateways, model=model) \
            <= gw.LOCAL_GATEWAY_MAX_TOKENS


def test_ptc_projection_eligible_names_cannot_change_gateway_schema():
    oversized_name = 'read_' + ('extremely_long_segment_' * 200)
    ordinary = gw.ptc_local_wire_tools(
        [gw.search_tools_schema(), gw.execute_tools_schema()],
        tier='program', eligible=['read_files'])
    oversized = gw.ptc_local_wire_tools(
        [gw.search_tools_schema(), gw.execute_tools_schema()],
        tier='program', eligible=[oversized_name])
    ordinary_gateways = [
        tool for tool in ordinary
        if (tool.get('function') or {}).get('name')
        in {gw.SEARCH_TOOLS_NAME, gw.EXECUTE_TOOLS_NAME}]
    oversized_gateways = [
        tool for tool in oversized
        if (tool.get('function') or {}).get('name')
        in {gw.SEARCH_TOOLS_NAME, gw.EXECUTE_TOOLS_NAME}]

    assert oversized_gateways == ordinary_gateways
    assert gw.tool_schema_tokens(oversized_gateways, model='kimi-k3') \
        <= gw.LOCAL_GATEWAY_MAX_TOKENS
    execute = next(tool for tool in oversized_gateways
                   if tool['function']['name'] == gw.EXECUTE_TOOLS_NAME)
    assert oversized_name not in execute['function']['description']
    assert 'program' in execute['function']['parameters']['properties']


def test_oversized_gateway_stays_byte_stable_instead_of_compacting():
    search = gw.search_tools_schema()
    execute = gw.execute_tools_schema()
    search['function']['description'] = 'search detail ' * 150
    execute['function']['description'] = 'execute detail ' * 150
    oversized_pair = [search, execute]

    assert gw.LOCAL_GATEWAY_MAX_TOKENS < gw.tool_schema_tokens(
        oversized_pair, model='kimi-k3') < 4_000

    out = gw.fit_tool_schema_budget(
        oversized_pair, budget_tokens=4_000, model='kimi-k3')

    # The pair is never compacted at runtime: rewriting gateway bytes between
    # rounds breaks the provider prompt-cache prefix. Oversized descriptions
    # pass through unchanged and the request continues.
    assert [tool['function']['description'] for tool in out] == [
        tool['function']['description'] for tool in oversized_pair]
    assert gw.tool_schema_tokens(out, model='kimi-k3') \
        > gw.LOCAL_GATEWAY_MAX_TOKENS


def test_single_oversized_direct_schema_is_omitted_without_rewriting():
    descriptive = _fn('descriptive_tool')
    descriptive['function']['description'] = 'detail ' * 2_000
    structural = _fn('structural_tool')
    structural['function']['parameters']['properties']['choice'] = {
        'type': 'string', 'enum': [f'value_{index}_' + ('x' * 80)
                                   for index in range(200)]}

    compacted = gw.fit_tool_schema_budget(
        [descriptive], budget_tokens=100, model='kimi-k3')
    omitted = gw.fit_tool_schema_budget(
        [structural], budget_tokens=100, model='kimi-k3')

    assert compacted == []
    assert omitted == []


def test_budget_keeps_required_tool_canonical_even_above_soft_target():
    """Regression: task 6e699b88, kimi-k3 Moonshot schema HTTP 400.

    ``description`` is both a JSON-Schema annotation keyword and a legitimate
    write_file argument name. Budget compaction may remove the former but must
    retain the latter and every validation/data semantic.
    """
    from lib.tools.project import PROJECT_TOOL_WRITE_FILE

    # Exact content-vs-content_ref validation has a 120-token annotation-free
    # structural floor. The budget still forces help-text compaction while
    # retaining the complete callable contract.
    out = gw.fit_tool_schema_budget(
        [PROJECT_TOOL_WRITE_FILE], budget_tokens=120, model='kimi-k3',
        required_names={'write_file'})

    assert _wire_names(out) == ['write_file']
    function = out[0]['function']
    parameters = function['parameters']
    assert function['description'] == PROJECT_TOOL_WRITE_FILE['function']['description']
    assert 'description' in parameters['properties']
    assert parameters['properties']['description'] == (
        PROJECT_TOOL_WRITE_FILE['function']['parameters']['properties']['description'])
    assert set(parameters['required']) <= set(parameters['properties'])
    assert gw.tool_schema_tokens(out, model='kimi-k3') > 120


def _project_schema_budget_body(model='kimi-k3'):
    from lib.tools.project import READ_FILES_TOOL, project_tools_for_runtime

    catalog = [READ_FILES_TOOL, *project_tools_for_runtime()]
    policy = {
        tool['function']['name']: 'eager' for tool in catalog
    }
    return {
        'model': model,
        'messages': [{'role': 'user', 'content': 'inspect and edit the project'}],
        'tools': catalog,
        '_executable_tool_catalog': catalog,
        '_tool_wire_catalog': catalog,
        '_tool_discovery_policy_by_name': policy,
        '_tool_search_catalog_size': len(catalog),
        '_tool_searchable_count': 0,
        '_tool_search_mode': 'local',
        '_programmatic_tool_calling': 'off',
        '_multi_agent_mode': 'off',
    }


def test_kimi_has_no_implicit_budget_but_applies_mfjs_preflight():
    from lib.llm._sse_core import prepare_request

    body = _project_schema_budget_body('kimi-k3')
    diagnostics = []
    body['_request_activity_sink'] = diagnostics.append
    expected = _wire_names(body['tools'])
    canonical_schemas = json.loads(json.dumps(body['tools']))
    expected_schemas = gw.sanitize_wire_tools(
        body['tools'], model='kimi-k3')
    expected_tokens = gw.tool_schema_tokens(
        expected_schemas, model='kimi-k3')
    assert expected_tokens > 0

    plan = prepare_request(
        body, api_key='secret', base_url='https://api.moonshot.cn/v1',
        api_protocol='openai')

    assert _wire_names(plan.body['tools']) == expected
    assert plan.body['tools'] == expected_schemas
    assert body['tools'] == canonical_schemas
    assert 'list_dir' not in expected
    assert '_tool_schema_budget_tokens' not in plan.body
    projection = diagnostics[-1]
    assert projection['kind'] == 'wire_projection'
    assert projection['toolNames'] == expected
    assert projection['schemaTokens'] == expected_tokens
    assert projection['schemaBudgetTokens'] == 0


def test_kimi_expected_mfjs_projection_is_not_a_producer_warning(caplog):
    body = _project_schema_budget_body('kimi-k3')

    with caplog.at_level(logging.WARNING, logger='lib.tools.gateway'):
        projected = gw.sanitize_wire_tools(body['tools'], model='kimi-k3')

    assert projected is not body['tools']
    assert not any(
        'producer emitted non-conforming entries' in record.message
        for record in caplog.records
    )


def test_explicit_budget_retains_code_floor_and_hidden_tool_discovery():
    from lib.tools.project import READ_FILES_TOOL, project_tools_for_runtime

    catalog = [READ_FILES_TOOL, *project_tools_for_runtime()]
    names = set(_wire_names(catalog))
    required = gw.CODE_CORE_DIRECT_TOOL_NAMES & names
    wire = gw.local_wire_tools(
        catalog,
        discovery_policy_by_name={name: 'eager' for name in names},
        discovery_catalog_size=len(catalog), searchable_count=0,
        schema_budget_tokens=100, model='kimi-k3',
        required_names=required)
    wire_names = set(_wire_names(wire))

    assert required <= wire_names
    assert {'search_tools', 'execute_tools'} <= wire_names
    assert 'write_file' not in wire_names
    assert gw.tool_schema_tokens(wire, model='kimi-k3') > 100


def test_explicit_schema_budget_is_fitted_once_after_ptc_projection(monkeypatch):
    from lib.llm._sse_core import prepare_request

    calls = []
    real_fit = gw.fit_tool_schema_budget

    def _counted_fit(*args, **kwargs):
        calls.append(tuple(_wire_names(args[0] if args else [])))
        return real_fit(*args, **kwargs)

    monkeypatch.setattr(gw, 'fit_tool_schema_budget', _counted_fit)
    body = _project_schema_budget_body('kimi-k3')
    body['_tool_schema_budget_tokens'] = 4_000
    prepare_request(
        body, api_key='secret', base_url='https://api.moonshot.cn/v1',
        api_protocol='openai')

    assert len(calls) == 1


def test_explicit_schema_budget_keeps_local_swarm_lifecycle_tools():
    """A visible spawn primitive must retain its wait/result companions."""
    from lib.llm._sse_core import prepare_request
    from lib.swarm.tools import (
        AWAIT_AGENTS_TOOL,
        GET_AGENT_RESULT_TOOL,
        SPAWN_AGENTS_TOOL,
    )

    body = _project_schema_budget_body('kimi-k3')
    swarm_tools = [
        SPAWN_AGENTS_TOOL,
        AWAIT_AGENTS_TOOL,
        GET_AGENT_RESULT_TOOL,
    ]
    body['tools'] = [*body['tools'], *swarm_tools]
    body['_executable_tool_catalog'] = list(body['tools'])
    body['_tool_wire_catalog'] = list(body['tools'])
    body['_tool_search_catalog_size'] = len(body['tools'])
    body['_multi_agent_mode'] = 'read_only'
    body['_multi_agent_stage'] = 'independent verification'
    # Force the required functional floor above the target so optional
    # ordering cannot accidentally keep the lifecycle companions.
    body['_tool_schema_budget_tokens'] = 100

    plan = prepare_request(
        body, api_key='secret', base_url='https://api.moonshot.cn/v1',
        api_protocol='openai')

    names = set(_wire_names(plan.body['tools']))
    assert {'spawn_agents', 'await_agents', 'get_agent_result'} <= names
    assert gw.tool_schema_tokens(plan.body['tools'], model='kimi-k3') > 100


def test_wire_boundary_serial_chain_key_never_leaks():
    from lib.llm._sse_core import prepare_request
    baseline_body = _ptc_body('deepseek-v4-pro')
    baseline_body['_programmatic_tier'] = 'program'
    baseline = prepare_request(
        baseline_body, api_key='secret',
        base_url='https://api.deepseek.com/v1', api_protocol='openai')
    body = _ptc_body('deepseek-v4-pro')
    body['_programmatic_tier'] = 'program'
    # A stale caller may still provide the retired sidecar.  It is stripped and
    # cannot affect the provider schema.
    body['_programmatic_serial_chain'] = ['find_files', 'grep_search',
                                          'read_files']
    plan = prepare_request(
        body, api_key='secret',
        base_url='https://api.deepseek.com/v1', api_protocol='openai')
    assert '_programmatic_serial_chain' not in plan.body
    assert plan.body['tools'] == baseline.body['tools']


def test_kimi_wire_tools_are_fixpoint_across_real_serial_chain_sequence():
    """Behaviour regression for mt8pree3rh3wj0 R4-R12.

    The orchestration policy must continue observing the real serial-read
    pattern for adoption telemetry, while the final provider tools bytes and
    their persisted Request Inspector fingerprint remain identical on every
    round.  The injected retired sidecar is a negative control: if request
    preparation ever consumes it again, this test reproduces the cache churn.
    """
    from lib.llm._sse_core import prepare_request

    direct_tools = [
        _fn('find_files'), _fn('grep_search'), _fn('read_files'),
    ]
    completed_calls = [
        'grep_search', 'grep_search', 'find_files', 'read_files',
        'grep_search', 'grep_search', 'read_files', 'read_files',
        'grep_search', 'read_files', 'run_command', 'search_tools',
    ]
    history = [{'role': 'user', 'content': '排查项目并完成修改'}]
    observed_chains = []
    wire_tools = []
    schema_fingerprints = []

    for round_num in range(1, 14):
        decision = resolve_tool_orchestration(
            requested_programmatic='on', requested_multi_agent='off',
            messages=history, tools=direct_tools, round_num=round_num,
            model='kimi-k3')
        chain = list(decision['programmaticSerialChain'])
        observed_chains.append(chain)

        diagnostics = []
        body = {
            'model': 'kimi-k3',
            'messages': list(history),
            'tools': direct_tools,
            '_programmatic_tool_calling': 'on',
            '_programmatic_tier': 'program',
            '_programmatic_eligible_tools': [
                'find_files', 'grep_search', 'read_files'],
            '_programmatic_serial_chain': chain,
            '_request_activity_sink': diagnostics.append,
        }
        plan = prepare_request(
            body, api_key='secret',
            base_url='https://api.moonshot.cn/v1', api_protocol='openai')
        wire_tools.append(json.dumps(
            plan.body['tools'], ensure_ascii=False,
            separators=(',', ':'), sort_keys=False))
        projection = next(
            item for item in diagnostics if item.get('kind') == 'wire_projection')
        schema_fingerprints.append(projection['schemaFingerprint'])
        assert len(projection['schemaFingerprint']) == 64
        assert '_programmatic_serial_chain' not in plan.body

        if round_num <= len(completed_calls):
            name = completed_calls[round_num - 1]
            call_id = f'call-{round_num}'
            history.append({
                'role': 'assistant', 'content': '',
                'tool_calls': [_tc(call_id, name)],
            })
            history.append({
                'role': 'tool', 'tool_call_id': call_id, 'content': 'result',
            })

    assert observed_chains[2] == []
    assert observed_chains[3] == [
        'grep_search', 'grep_search', 'find_files']
    assert len(observed_chains[10]) == 6  # rolling bounded telemetry window
    assert observed_chains[11] == []      # run_command breaks the read chain
    assert observed_chains[12] == []      # search_tools does not restart it
    assert len(set(wire_tools)) == 1
    assert len(set(schema_fingerprints)) == 1


def test_local_program_run_logs_completion(caplog):
    import logging

    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})
    with caplog.at_level(logging.INFO, logger=gw_handler.logger.name):
        payload = _run_program(task, "return {status: 'ok'};")
    assert payload['status'] == 'ok'
    text = '\n'.join(record.getMessage() for record in caplog.records)
    assert '[PTC] local program start' in text
    assert '[PTC] local program completed' in text


def test_local_program_persists_and_audits_bounded_syntax_repair(monkeypatch):
    audit_events = []
    monkeypatch.setattr(
        gw_handler, 'audit_log',
        lambda event, **detail: audit_events.append((event, detail)))
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})

    payload = _run_program(
        task, "return {status: 'ok' findings: [],};")

    assert payload['status'] == 'ok'
    repairs = payload['program']['stats']['syntax_repairs']
    assert repairs == task['programRuns'][0]['syntaxRepairs']
    assert repairs[0]['kind'] == 'missing_object_comma'
    repaired_audit = [detail for event, detail in audit_events
                      if event == 'toolscript_syntax_repaired']
    assert repaired_audit == [{
        'task_id': 'test-ptc', 'model': 'm', 'repair_count': 1,
        'kinds': ['missing_object_comma'],
        'offsets': [21],
    }]


def test_rejected_program_retains_repairs_without_weakening_security(monkeypatch):
    audit_events = []
    monkeypatch.setattr(
        gw_handler, 'audit_log',
        lambda event, **detail: audit_events.append((event, detail)))
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})
    source = "return {status:'ok' constructor:1};"

    payload = _run_program(task, source)

    error = payload['program']['error']
    assert payload['status'] == 'error'
    assert error['code'] == 'unsafe_member'
    assert error['syntax_repairs'] == [{
        'kind': 'missing_object_comma',
        'offset': source.index('constructor'),
    }]
    assert task['programRuns'][0]['syntaxRepairs'] == error['syntax_repairs']
    assert [event for event, _detail in audit_events].count(
        'toolscript_syntax_repaired') == 1


def test_local_program_projects_shared_parent_and_lifecycle(monkeypatch):
    import lib.tasks_pkg.orchestrator._programmatic as _programmatic
    emitted = []
    monkeypatch.setattr(
        _programmatic, 'append_event',
        lambda _task, event: emitted.append(dict(event)))
    task = _mk_task({'tier': 'program', 'eligible': ['read_files']})
    payload = _run_program(task, "return {status: 'ok', findings: []};")

    assert payload['status'] == 'ok'
    parents = [row for row in task['toolRounds']
               if row.get('_programSynthetic')]
    assert len(parents) == 1
    assert parents[0]['programBackend'] == 'local_toolscript'
    assert parents[0]['programSource'] == 'execute_program'
    assert parents[0]['programStatus'] == 'completed'
    lifecycle = [event for event in emitted
                 if event.get('type') in ('program_start', 'program_output')]
    assert [event['type'] for event in lifecycle] == [
        'program_start', 'program_output']
    assert {event['backend'] for event in lifecycle} == {
        'local_toolscript'}
