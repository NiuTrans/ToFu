"""Contract tests for opt-in context/cache efficiency experiments."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/a.py', 'lib/b.py', 'lib/first.py', 'lib/parser.py',
    'tests/test_a.py', 'tests/test_second.py',
}


def _tool(name: str) -> dict:
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': name,
            'parameters': {'type': 'object', 'properties': {}},
        },
    }


def test_context_flags_ship_resident_ptc_default_and_remain_strict():
    from lib.context_experiment_flags import (
        context_experiment_arm, normalize_context_experiment_flags)

    assert normalize_context_experiment_flags({}) == {
        'cache': {'gpt56BreakpointMode': 'explicit'},
        'tools': {
            'nativeExposure': 'routed', 'programmaticCalling': 'on',
            'programmaticExposure': 'additive',
            'toolSearch': 'auto', 'executionScope': 'available',
            'schemaBudgetTokens': 0, 'resultEnvelope': 'v2',
        },
        'responses': {
            'transport': 'sse', 'reasoningMode': 'standard',
            'verbosity': 'medium', 'imageDetail': 'auto',
            'promptProfile': 'auto',
        },
        'orchestration': {
            'multiAgent': 'auto', 'maxConcurrentAgents': 3, 'policy': 'v1',
        },
        'context': {'globalBudgetTokens': 0},
        'compaction': {'evidenceLedger': False, 'strategy': 'fixed'},
    }
    # The provider-neutral owner wins, while the old Responses fields remain
    # accepted as migration aliases.
    assert normalize_context_experiment_flags({
        'responses': {'multiAgent': 'read_only',
                      'maxConcurrentSubagents': 7},
    })['orchestration'] == {
        'multiAgent': 'read_only', 'maxConcurrentAgents': 7, 'policy': 'v1'}
    assert normalize_context_experiment_flags({
        'orchestration': {'multiAgent': 'off', 'maxConcurrentAgents': 2},
        'responses': {'multiAgent': 'read_only',
                      'maxConcurrentSubagents': 7},
    })['orchestration'] == {
        'multiAgent': 'off', 'maxConcurrentAgents': 2, 'policy': 'v1'}
    arm = context_experiment_arm({})
    assert arm['maxConcurrentAgents'] == 3
    assert arm['resultEnvelope'] == 'v2'
    assert 'maxConcurrentSubagents' not in arm

    # Legacy remains an explicit, fingerprinted rollback/control policy.
    assert normalize_context_experiment_flags({
        'tools': {'resultEnvelope': 'legacy'},
    })['tools']['resultEnvelope'] == 'legacy'
    with pytest.raises(ValueError, match='nativeExposure'):
        normalize_context_experiment_flags(
            {'tools': {'nativeExposure': 'guess'}}, strict=True)
    with pytest.raises(ValueError, match='executionScope'):
        normalize_context_experiment_flags(
            {'tools': {'executionScope': 'guess'}}, strict=True)
    with pytest.raises(ValueError, match='promptProfile'):
        normalize_context_experiment_flags(
            {'responses': {'promptProfile': 'guess'}}, strict=True)
    assert normalize_context_experiment_flags({
        'tools': [_tool('custom')],
        'tools.nativeExposure': 'routed',
    })['tools']['nativeExposure'] == 'routed'


def test_agent_run_preserves_context_experiment_tools_mapping():
    from routes.api_v1.agent_run import _build_cfg

    cfg = _build_cfg('gpt-5.6-sol', {
        'tools': {
            'nativeExposure': 'routed',
            'programmaticCalling': 'auto',
        },
    }, None)

    assert cfg['tools'] == {
        'nativeExposure': 'routed',
        'programmaticCalling': 'auto',
    }


def test_gpt56_explicit_only_cache_wire_field_and_implicit_compatibility():
    from lib.llm.responses_outbound import openai_body_to_responses

    base = {
        'model': 'gpt-5.6-sol',
        '_responses_feature_profile': 'openai',
        'messages': [
            {'role': 'system', 'content': 'stable'},
            {'role': 'user', 'content': 'work'},
        ],
        '_conv_id': 'conversation-a',
    }
    implicit, _ = openai_body_to_responses(base)
    assert 'prompt_cache_options' not in implicit
    assert implicit['input'][0]['content'][0]['prompt_cache_breakpoint'] == {
        'mode': 'explicit'}

    explicit, _ = openai_body_to_responses({
        **base, '_gpt56_breakpoint_mode': 'explicit'})
    assert explicit['prompt_cache_options'] == {'mode': 'explicit'}
    assert explicit['input'][0]['content'][0]['prompt_cache_breakpoint'] == {
        'mode': 'explicit'}


def test_context_experiment_internal_fields_never_leak_to_chat_completions():
    from lib.llm._sse_core import prepare_request
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.llm.responses_outbound import openai_body_to_responses
    from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY

    canonical = {
        'model': 'gpt-4o',
        'messages': [{'role': 'user', 'content': 'hello'}],
        '_gpt56_breakpoint_mode': 'explicit',
        '_programmatic_tool_calling': 'auto',
        ADMITTED_INPUT_TOKENS_KEY: 12_345,
    }
    plan = prepare_request(
        canonical, api_key='k', base_url='https://example.test/v1',
        api_protocol='openai')
    assert '_gpt56_breakpoint_mode' not in plan.body
    assert '_programmatic_tool_calling' not in plan.body
    assert ADMITTED_INPUT_TOKENS_KEY not in plan.body
    assert canonical[ADMITTED_INPUT_TOKENS_KEY] == 12_345
    responses, _ = openai_body_to_responses(canonical)
    anthropic = openai_body_to_anthropic(canonical)
    assert ADMITTED_INPUT_TOKENS_KEY not in responses
    assert ADMITTED_INPUT_TOKENS_KEY not in anthropic


def test_programmatic_calling_only_enables_native_read_tools_and_replays_caller():
    from lib.llm.responses_outbound import (
        openai_body_to_responses, responses_response_to_openai)

    body, _ = openai_body_to_responses({
        'model': 'gpt-5.6-sol',
        '_responses_feature_profile': 'openai',
        'messages': [{'role': 'user', 'content': 'read and filter'}],
        'tools': [_tool('read_files'), _tool('run_command')],
        '_programmatic_tool_calling': 'auto',
    })
    tools = {tool.get('name'): tool for tool in body['tools']
             if tool.get('type') == 'function'}
    assert tools['read_files']['allowed_callers'] == [
        'direct', 'programmatic']
    assert tools['read_files']['output_schema'] == {
        'type': 'object',
        'properties': {
            'content': {'type': 'string'},
            'truncated': {'type': 'boolean'},
        },
        'required': ['content', 'truncated'],
        'additionalProperties': False,
    }
    assert 'allowed_callers' not in tools['run_command']
    assert {'type': 'programmatic_tool_calling'} in body['tools']

    translated = responses_response_to_openai({
        'id': 'resp', 'status': 'completed', 'model': 'gpt-5.6-sol',
        'output': [{
            'type': 'function_call', 'call_id': 'call-1',
            'name': 'read_files', 'arguments': '{}',
            'caller': {'type': 'program', 'caller_id': 'call-prog-1'},
        }],
    })
    caller = translated['choices'][0]['message']['tool_calls'][0]['caller']
    assert caller['caller_id'] == 'call-prog-1'


def test_program_output_without_final_message_requests_one_more_response():
    from lib.llm.responses_outbound import ResponsesSSETranslator

    translator = ResponsesSSETranslator('gpt-5.6-sol')
    chunks = translator.translate(json.dumps({
        'type': 'response.completed',
        'response': {
            'usage': {'input_tokens': 10, 'output_tokens': 2},
            'output': [{
                'type': 'program_output', 'id': 'po-1',
                'call_id': 'call-prog-1', 'result': '{}',
                'status': 'completed',
            }],
        },
    }))
    assert chunks[0]['usage']['_program_pending'] is True
    assert translator.response_items[0]['type'] == 'program_output'

    from lib.tasks_pkg.stream_handler.api import analyse_stream_result
    messages = [{'role': 'user', 'content': 'work'}]
    assistant = {
        'role': 'assistant', 'content': '',
        '_responses_items': translator.response_items,
    }
    decision = analyse_stream_result(
        assistant, 'stop', {'aborted': False}, 'task', 'gpt-5.6-sol',
        1, 0, messages, usage={'_program_pending': True})
    assert decision['action'] == 'program_continue'
    assert messages[-1]['_responses_items'][0]['type'] == 'program_output'


def test_routed_native_exposure_reduces_catalog_but_keeps_discovery_floor():
    from lib.tools.registry import ToolContext, assemble_tool_list

    kwargs = dict(
        task_id='route-test', project_path='', project_enabled=False,
        search_mode='multi', search_enabled=True, fetch_enabled=True,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False,
        messages=[{'role': 'user', 'content': 'summarize current public facts'}],
    )
    full, _ = assemble_tool_list(ToolContext(
        cfg={'tools': {'nativeExposure': 'full'}}, **kwargs))
    routed_ctx = ToolContext(
        cfg={'tools': {'nativeExposure': 'routed'}}, **kwargs)
    routed, _ = assemble_tool_list(routed_ctx)
    names = {tool['function']['name'] for tool in routed}
    assert len(routed) < len(full)
    assert {
        'web_search', 'fetch_url', 'browser_download_url_to_server',
        'read_files', 'inspect_image',
    } <= names
    assert routed_ctx.omitted_spec_keys


def test_run_command_schema_stays_within_coding_round_budget():
    """The shared shell contract is paid on every coding round; guidance must
    stay complete without re-expanding its former repeated usage matrix."""
    from lib.tools.code_exec import CODE_EXEC_TOOL
    from lib.tools.gateway import tool_schema_tokens
    from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

    for tool in (PROJECT_TOOL_RUN_COMMAND, CODE_EXEC_TOOL):
        wire = json.dumps(tool, ensure_ascii=False, sort_keys=True)
        for guidance in (
            'Never use a shell no-op as a placeholder', 'NO default timeout',
            'no persistent shell', 'read_files', 'grep_search', 'find_files',
            'edit_file', 'write_file', 'browser_download_url_to_server',
            'FUSE-safe', 'credentials', '`sd`', '`mlr`', '`goawk',
            'move to the background', 'arrive automatically',
        ):
            assert guidance in wire
        assert tool_schema_tokens([tool], model='kimi-k3') <= 450


def test_core_read_search_schemas_stay_semantic_and_bounded():
    from lib.tools.gateway import tool_schema_tokens
    from lib.tools.project import PROJECT_TOOL_GREP, READ_FILES_TOOL

    expectations = (
        (PROJECT_TOOL_GREP, 475, (
            'persistent file index', 'FUSE', 'Rust/ripgrep', 'max 20 entries',
            'context_lines', 'count_only mode',
        )),
        (READ_FILES_TOOL, 450, (
            'Read WIDE', '200+ lines', '512 KB', '24k tokens', 'max 20',
            'authoritative and never widened', 'Images', 'PDFs', 'Office',
        )),
    )
    for tool, budget, guidance in expectations:
        wire = json.dumps(tool, ensure_ascii=False, sort_keys=True)
        assert all(item in wire for item in guidance)
        assert tool_schema_tokens([tool], model='kimi-k3') <= budget


def test_multiroot_compat_projection_keeps_canonical_schema():
    from lib.tools.gateway import tool_schema_tokens
    from lib.tools.project import (
        READ_FILES_TOOL, project_tools_for_runtime, with_multiroot_hint,
    )

    base = [READ_FILES_TOOL, *project_tools_for_runtime()]
    projected = with_multiroot_hint(base)
    assert projected == base
    assert tool_schema_tokens(projected, model='kimi-k3') == (
        tool_schema_tokens(base, model='kimi-k3'))


def test_routed_exposure_never_retracts_frontend_enabled_families():
    from lib.tools.registry import ToolContext, all_specs
    from lib.tools.routing import routed_native_spec_keys

    ctx = ToolContext(
        cfg={'memoryEnabled': True, 'mcpEnabled': True},
        task_id='route-pin', project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=True, desktop_enabled=True,
        image_gen_enabled=True,
        human_guidance_enabled=True, scheduler_enabled=True,
        messages=[{'role': 'user', 'content': 'hello'}],
    )
    selected = routed_native_spec_keys(ctx, specs=all_specs())
    assert {
        'browser', 'desktop', 'swarm', 'image_gen', 'human_guidance',
        'scheduler', 'memory', 'mcp', 'browser_download',
    } <= selected


def test_download_intent_uses_tool_owned_router_declaration():
    from lib.tools.registry import ToolContext, all_specs
    from lib.tools.routing import routed_native_spec_keys

    ctx = ToolContext(
        cfg={'memoryEnabled': False, 'mcpEnabled': False},
        task_id='route-download', project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        messages=[{'role': 'user', 'content': '把最新版压缩包下载到服务器本地'}],
    )
    specs = all_specs()
    owner = next(spec for spec in specs if spec.key == 'browser_download')
    assert 'download' in owner.native_route_groups
    assert 'browser_download' in routed_native_spec_keys(ctx, specs=specs)


@pytest.mark.parametrize(('user_text', 'expected_spec'), (
    ('给我做一份新品发布会 PPT', 'produce'),
    ('make a launch-review presentation deck', 'produce'),
    ('生成一张新品封面图', 'image_gen'),
    ('把前端页面放到真实浏览器里渲染看看效果', 'page_preview'),
))
def test_capability_vocabulary_routes_non_obvious_builtin_families(
        user_text, expected_spec):
    from lib.tools.registry import ToolContext, all_specs
    from lib.tools.routing import routed_native_spec_keys

    ctx = ToolContext(
        cfg={'memoryEnabled': False, 'mcpEnabled': False},
        task_id='route-capability', project_path='',
        project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False,
        messages=[{'role': 'user', 'content': user_text}],
    )
    assert expected_spec in routed_native_spec_keys(ctx, specs=all_specs())


def test_l1_evidence_arm_archives_exact_cold_tool_result(monkeypatch):
    from lib.tasks_pkg.compaction._builtin_steps._toolresults import (
        compact_tool_results)
    from lib.tasks_pkg.compaction._steps import CompactionContext

    persisted = []

    def fake_persist(content, tool_name, tool_use_id='', conv_id=''):
        persisted.append((content, tool_name, tool_use_id, conv_id))
        return '[Persisted to: /tmp/evidence.txt]\nUse read_files.'

    monkeypatch.setattr(
        'lib.tasks_pkg.compaction._persist._persist_to_disk', fake_persist)
    cold = 'important evidence\n' * 100
    messages = [
        {'role': 'assistant', 'tool_calls': [{
            'id': 'cold-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'cold-call', 'name': 'read_files',
         'content': cold},
        {'role': 'assistant', 'tool_calls': [{
            'id': 'hot-call', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'hot-call', 'name': 'read_files',
         'content': 'hot'},
    ]
    task = {'config': {'compaction': {'evidenceLedger': True}}}
    constants = SimpleNamespace(
        MICRO_HOT_TAIL=1, MICRO_COMPACT_THRESHOLD=20,
        _IMAGE_TOKENS_DEFAULT=1000)
    compact_tool_results(CompactionContext(
        messages=messages, conv_id='conv-evidence', task=task,
        constants=constants))
    assert persisted == [(cold, 'read_files', 'cold-call', 'conv-evidence')]
    assert messages[1]['content'].startswith('[Persisted to:')
    assert task['_contextEvidenceArchives'][0]['toolCallId'] == 'cold-call'


def test_evidence_ledger_tracks_files_tests_errors_and_retention():
    from lib.tasks_pkg.compaction._evidence import (
        build_evidence_ledger, evidence_retention, format_evidence_ledger)

    messages = [
        {'role': 'assistant', 'tool_calls': [{
            'id': 't1', 'type': 'function',
            'function': {'name': 'run_command', 'arguments': json.dumps({
                'cmd': 'pytest -q', 'path': 'tests/test_a.py'})}}]},
        {'role': 'tool', 'tool_call_id': 't1', 'name': 'run_command',
         'content': '1 passed'},
        {'role': 'tool', 'tool_call_id': 't2', 'name': 'read_files',
         'content': 'Traceback: example failure'},
    ]
    ledger = build_evidence_ledger(
        messages, {'modifiedFiles': ['lib/a.py'], 'todos': ['finish docs']})
    kinds = {entry['type'] for entry in ledger['entries']}
    assert {'modified_file', 'file_reference', 'test_result', 'error',
            'unfinished'} <= kinds
    prompt = format_evidence_ledger(ledger)
    retained, lost = evidence_retention(prompt, ledger)
    assert set(retained) == set(ledger['evidenceIds'])
    assert lost == []


def test_evidence_ledger_deduplicates_exact_calls_not_distinct_queries():
    from lib.tasks_pkg.compaction._evidence import build_evidence_ledger

    messages = []
    for call_id, path in (
            ('a1', 'lib/a.py'), ('a2', 'lib/a.py'), ('b1', 'lib/b.py')):
        messages.extend([
            {'role': 'assistant', 'tool_calls': [{
                'id': call_id, 'function': {
                    'name': 'read_files',
                    'arguments': json.dumps({'path': path}),
                },
            }]},
            {'role': 'tool', 'tool_call_id': call_id, 'name': 'read_files',
             'content': 'same short result'},
        ])

    ledger = build_evidence_ledger(messages)
    queries = [entry for entry in ledger['entries']
               if entry['type'] == 'query_result']
    paths = [entry for entry in ledger['entries']
             if entry['type'] == 'file_reference']
    assert len(queries) == 2  # exact a.py repeat collapsed; b.py kept
    assert {entry['value'] for entry in paths} == {'lib/a.py', 'lib/b.py'}
    a_query = next(entry for entry in queries
                   if entry.get('toolCallId') == 'a2')
    assert a_query['source'].endswith(':a2')  # newest recovery handle wins


def test_evidence_ledger_pairs_recycled_ids_by_adjacent_occurrence():
    """A later positional id must not relabel an earlier result."""
    from lib.tasks_pkg.compaction._evidence import build_evidence_ledger

    messages = [
        {'role': 'assistant', 'tool_calls': [{
            'id': 'position_0', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': json.dumps({
                'path': 'lib/first.py'})},
        }]},
        {'role': 'tool', 'tool_call_id': 'position_0',
         'content': 'ordinary source bytes'},
        {'role': 'assistant', 'tool_calls': [{
            'id': 'position_0', 'type': 'function',
            'function': {'name': 'run_command', 'arguments': json.dumps({
                'cmd': 'pytest -q tests/test_second.py'})},
        }]},
        {'role': 'tool', 'tool_call_id': 'position_0',
         'content': '1 passed'},
    ]

    ledger = build_evidence_ledger(messages)
    query_results = [entry for entry in ledger['entries']
                     if entry['type'] == 'query_result']
    test_results = [entry for entry in ledger['entries']
                    if entry['type'] == 'test_result']

    assert [entry['value'] for entry in query_results] == [
        'ordinary source bytes']
    assert [entry['value'] for entry in test_results] == ['1 passed']
    assert test_results[0]['command'] == 'pytest -q tests/test_second.py'


def test_l2_summary_always_sees_bounded_tool_working_state(monkeypatch):
    """Default L2 must not summarize an agentic turn from prose alone.

    Durable raw-result persistence remains opt-in, but the summary model always
    needs a bounded view of reads, commands and test outcomes or it will force
    the main model to rediscover them after every compaction.
    """
    import lib.tasks_pkg.compaction._layer2._summary as summary
    import lib.llm_dispatch as dispatch

    captured = []

    def fake_dispatch(messages, **kwargs):
        captured.append(messages)
        return 'summary with evidence', {
            'prompt_tokens': 100, 'completion_tokens': 10}

    monkeypatch.setattr(dispatch, 'dispatch_chat', fake_dispatch)
    monkeypatch.setattr(summary, '_codex_subscription_provider',
                        lambda task: '')
    task = {'convId': 'evidence-default', '_userId': 1, 'config': {}}
    messages = [
        {'role': 'user', 'content': 'fix the parser'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'read-1', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': json.dumps({
                'reads': [{'path': 'lib/parser.py'}]})},
        }]},
        {'role': 'tool', 'tool_call_id': 'read-1', 'name': 'read_files',
         'content': 'def parse(value): return broken(value)'},
        {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'test-1', 'type': 'function',
            'function': {'name': 'run_command', 'arguments': json.dumps({
                'cmd': 'pytest tests/test_parser.py -q'})},
        }]},
        {'role': 'tool', 'tool_call_id': 'test-1', 'name': 'run_command',
         'content': '12 passed in 0.4s'},
    ]

    result = summary._generate_query_aware_summary(
        messages, 'fix the parser', task=task)

    assert result == 'summary with evidence'
    assert captured
    prompt = captured[0][-1]['content']
    assert '## Structured Evidence Ledger' in prompt
    assert 'lib/parser.py' in prompt
    assert '12 passed in 0.4s' in prompt
    assert task['_contextEvidenceLedger']['entries']


def test_extended_outcome_and_report_use_oracle_not_health_as_quality():
    from lib.cost_experiments import (
        aggregate_cost_experiment_rows, build_cost_experiment_outcome)

    assignment = {
        'experiment_id': 'audit-v1', 'status': 'assigned', 'arm': 'optimized'}
    task = {
        'id': 'swe-task-1',
        'config': {'reasoning_effort': 'high'},
        '_benchmark': {
            'dataset': 'swe-bench-multilingual', 'taskId': 'swe-task-1',
            'oraclePassed': False, 'oracleType': 'test_patch',
            'agentVersion': 'git-abc', 'experimentArm': 'routed'},
        '_publicPriceCost': {'costUsd': 0.25},
        '_contextTelemetryRounds': [{
            'round': 1, 'stablePrefixTokens': 100,
            'toolSchemaTokens': 200, 'rawToolResultTokens': 300,
            'modelToolResultTokens': 150, 'prefixFingerprint': 'fp'}],
        '_mcpSearchCount': 2, '_mcpSearchMissCount': 1,
    }
    outcome = build_cost_experiment_outcome(
        assignment,
        usage={'prompt_tokens': 1000, 'completion_tokens': 100,
               'reasoning_tokens': 40},
        cost={'costUsd': 0.20, 'costCny': 1.4,
              'pricingSource': 'model_table'},
        api_rounds=[{'round': 1, 'latencyMs': 400,
                     'usage': {'prompt_tokens': 1000,
                               'completion_tokens': 100}}],
        finish_reason='stop', error=None, elapsed_ms=500, task=task)
    assert outcome['quality']['terminalWithoutError'] is True
    assert outcome['quality']['oraclePassed'] is False
    assert outcome['dataset'] == 'swe-bench-multilingual'
    assert outcome['metrics']['reasoningTokens'] == 40
    assert outcome['metrics']['publicPriceCostUsd'] == 0.25
    assert outcome['metrics']['toolSchemaTokens'] == 200
    assert outcome['telemetry']['mcpSearchMisses'] == 1

    outcome['completedAt'] = 1_000_000
    report = aggregate_cost_experiment_rows([
        {'id': 'c1', 'updated_at': 1_000_000,
         'messages': [{'role': 'assistant', 'costExperiment': outcome}]},
    ], experiment_id='audit-v1', days=1, now_ms=1_000_000,
       min_sample_size=1)
    arm = report['arms']['optimized']
    assert arm['terminalWithoutErrorRate'] == 1.0
    assert arm['oraclePassRate'] == 0.0
    assert arm['toolSchemaTokensPerContextRound'] == 200.0
    assert report['qualityReady'] is False  # control has no oracle sample


def test_benchmark_task_persists_outcome_without_production_ab_assignment():
    from lib.cost_experiments import build_task_cost_experiment_outcome

    outcome = build_task_cost_experiment_outcome({
        'id': 'task-no-ab', 'created_at': 1,
        'model': 'gpt-5.6-sol', 'provider_id': 'openai',
        'usage': {'prompt_tokens': 10, 'completion_tokens': 1},
        'finishReason': 'stop',
        '_benchmark': {
            'runId': 'confirm-100', 'dataset': 'multilingual',
            'taskId': 'repo-1', 'experimentArm': 'tofu-routed',
            'oraclePassed': True, 'oracleType': 'tests'},
    })
    assert outcome['experiment_id'] == 'confirm-100'
    assert outcome['arm'] == 'tofu-routed'
    assert outcome['quality']['oraclePassed'] is True


def test_benchmark_jsonl_budget_public_price_and_acceptance(tmp_path):
    from lib.benchmark_contract import (
        BenchmarkJsonlWriter, acceptance_decision, budget_status,
        build_manifest, build_task_record, public_price_cost_from_usage,
        read_jsonl)

    manifest = build_manifest(
        run_id='run-1', dataset='pilot', tasks=['t1'], agent='tofu',
        agent_version='abc', model='gpt-5.6-sol', effort='high',
        experiment_arm='explicit-routed', timeout_seconds=900,
        network_policy='disabled', environment={'gitCommit': 'abc'})
    task = build_task_record(
        run_id='run-1', dataset='pilot', task_id='t1', agent='tofu',
        agent_version='abc', model='gpt-5.6-sol', effort='high',
        experiment_arm='explicit-routed', oracle_passed=True,
        oracle_type='tests', final_patch='diff --git',
        test_result={'passed': True}, round_usage=[],
        prefix_fingerprints=['fp'], cost={'actualCostUsd': 1},
        latency_ms=1000, context_telemetry={'toolSchemaTokens': 10})
    path = tmp_path / 'run.jsonl'
    writer = BenchmarkJsonlWriter(path)
    writer.append(manifest)
    writer.append(task)
    assert [row['recordType'] for row in read_jsonl(path)] == [
        'manifest', 'task']

    priced = public_price_cost_from_usage(
        {'prompt_tokens': 1000, 'completion_tokens': 100,
         'prompt_tokens_details': {'cached_tokens': 400}},
        {'inputUsdPerMillion': 2, 'cacheReadUsdPerMillion': 0.2,
         'cacheWriteUsdPerMillion': 2.5, 'outputUsdPerMillion': 10})
    assert priced['uncachedInputTokens'] == 600
    assert priced['cacheReadTokens'] == 400
    assert priced['costUsd'] == pytest.approx(0.00228)
    assert budget_status(1200)['action'] == 'pause_and_reforecast'
    assert budget_status(1500)['action'] == 'stop'

    decision = acceptance_decision(
        candidate_oracles=[True, False, True],
        baseline_oracles=[True, False, True],
        candidate_public_cost_usd=2.0,
        baseline_public_cost_usd=2.5,
        candidate_p90_latency_ms=110,
        baseline_p90_latency_ms=100)
    assert decision['quality']['nonInferiorityEstablished'] is False
    assert decision['quality']['conclusion'] == (
        'observed_tie_not_statistically_established')
    assert decision['gates']['resolvedNotLower'] is True
    assert decision['gates']['qualityNoninferiorityEstablished'] is False
    assert decision['releaseEligible'] is False

    established = acceptance_decision(
        candidate_oracles=[True] * 100,
        baseline_oracles=[True] * 100,
        candidate_public_cost_usd=50.0,
        baseline_public_cost_usd=60.0,
        candidate_p90_latency_ms=110,
        baseline_p90_latency_ms=100,
    )
    assert established['quality']['nonInferiorityEstablished'] is True
    assert established['releaseEligible'] is True
