"""Contract tests for opt-in context/cache efficiency experiments."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'lib/a.py', 'lib/b.py', 'lib/parser.py', 'tests/test_a.py',
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


def test_context_flags_ship_task_gated_gpt56_defaults_and_remain_strict():
    from lib.context_experiment_flags import normalize_context_experiment_flags

    assert normalize_context_experiment_flags({}) == {
        'cache': {'gpt56BreakpointMode': 'explicit'},
        'tools': {
            'nativeExposure': 'routed', 'programmaticCalling': 'auto',
            'toolSearch': 'auto', 'executionScope': 'available',
        },
        'responses': {
            'transport': 'sse', 'reasoningMode': 'standard',
            'verbosity': 'medium', 'imageDetail': 'auto',
            'promptProfile': 'auto', 'multiAgent': 'auto',
            'maxConcurrentSubagents': 3,
        },
        'compaction': {'evidenceLedger': False},
    }
    with pytest.raises(ValueError, match='nativeExposure'):
        normalize_context_experiment_flags(
            {'tools': {'nativeExposure': 'guess'}}, strict=True)
    with pytest.raises(ValueError, match='executionScope'):
        normalize_context_experiment_flags(
            {'tools': {'executionScope': 'guess'}}, strict=True)
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

    plan = prepare_request({
        'model': 'gpt-4o',
        'messages': [{'role': 'user', 'content': 'hello'}],
        '_gpt56_breakpoint_mode': 'explicit',
        '_programmatic_tool_calling': 'auto',
    }, api_key='k', base_url='https://example.test/v1',
       api_protocol='openai')
    assert '_gpt56_breakpoint_mode' not in plan.body
    assert '_programmatic_tool_calling' not in plan.body


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

    from lib.tasks_pkg.stream_handler import analyse_stream_result
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
    from lib.tools import ToolContext, assemble_tool_list

    kwargs = dict(
        task_id='route-test', project_path='', project_enabled=False,
        search_mode='multi', search_enabled=True, fetch_enabled=True,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        swarm_enabled=False, image_gen_enabled=False,
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
    assert {'web_search', 'fetch_url', 'read_files', 'inspect_image'} <= names
    assert routed_ctx.omitted_spec_keys


def test_routed_exposure_never_retracts_frontend_enabled_families():
    from lib.tools import ToolContext
    from lib.tools.routing import routed_native_spec_keys

    ctx = ToolContext(
        cfg={'memoryEnabled': True, 'mcpEnabled': True},
        task_id='route-pin', project_path='', project_enabled=False,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=True, desktop_enabled=True,
        swarm_enabled=True, image_gen_enabled=True,
        human_guidance_enabled=True, scheduler_enabled=True,
        messages=[{'role': 'user', 'content': 'hello'}],
    )
    selected = routed_native_spec_keys(ctx)
    assert {
        'browser', 'desktop', 'swarm', 'image_gen', 'human_guidance',
        'scheduler', 'memory', 'mcp',
    } <= selected


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
    task = {'convId': 'evidence-default', 'config': {}}
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
    assert decision['releaseEligible'] is True
