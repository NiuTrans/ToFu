"""Static-prompt profile resolution, measurement, and adoption evidence."""

from __future__ import annotations

import hashlib

import pytest

import lib.context_telemetry as context_telemetry
from lib.benchmark_contract import BenchmarkContractError, build_task_record_v2
from lib.context_telemetry import (
    PROMPT_PROFILE_EVIDENCE_VERSION,
    capture_round_context,
)
from lib.tasks_pkg.context_composer import (
    ComposeRequest,
    collect_context_blocks,
    compose_context,
)
from lib.tasks_pkg.system_prompt_cc import (
    build_static_prompt,
    resolve_static_prompt_profile,
)
from lib.tasks_pkg.context_composer import compose_task_context
from lib.token_counter import count_text


pytestmark = pytest.mark.unit


def _prompt_kwargs() -> dict:
    return {
        'cwd': '/workspace',
        'is_git': True,
        'model': 'kimi-k3',
        'tool_names': {
            'read_files', 'apply_diff', 'insert_content', 'write_file',
            'find_files', 'grep_search', 'run_command', 'web_search',
            'fetch_url',
        },
        'include_date': False,
    }


def _platform_block(task: dict, *, messages: list[dict] | None = None,
                    mode: str = 'append'):
    request = ComposeRequest(
        model='kimi-k3',
        task=task,
        system_prompt_mode=mode,
    )
    blocks = collect_context_blocks(
        messages or [{'role': 'user', 'content': 'do the task'}], request)
    return next(block for block in blocks if block.id == 'platform_static')


def _benchmark_task(*, arm: str, context_blocks: list[dict]) -> dict:
    return build_task_record_v2(
        run_id='prompt-run',
        dataset='prompt-pilot',
        family='long-agent',
        task_id='task-1',
        agent={
            'name': 'tofu', 'version': 'test', 'commitSha256': 'a' * 64,
        },
        provider_face='meituan-chat',
        provider_slot_id='kimi-slot-fixture',
        thinking='high',
        experiment_arm=arm,
        oracle={'passed': True, 'type': 'exact'},
        rounds=[{'round': 1, 'usage': {'inputTokens': 10}}],
        context_blocks=context_blocks,
        tool_schemas=[],
        tool_results=[],
        compactions=[],
        call_graph=[],
        retries=[],
        cost={'agentCostUsd': 0.01},
        latency={
            'rawWallMs': 100,
            'oracleReadyMs': 100,
            'queueMs': 0,
            'ttftMs': 10,
            'modelMs': 80,
            'toolMs': 10,
            'translationCpuMs': 10,
            'proxyCpuMs': 20,
            'codexFavoredCorrectedWallMs': 90,
        },
    )


def test_kimi_profile_resolution_is_explicit_and_gpt56_auto_stays_lean():
    assert resolve_static_prompt_profile('kimi-k3', 'auto') == 'full'
    assert resolve_static_prompt_profile('kimi-k3', 'lean') == 'lean'
    assert resolve_static_prompt_profile(
        'kimi-k3', 'lean_no_tools') == 'lean_no_tools'
    assert resolve_static_prompt_profile('kimi-k3', 'unknown') == 'full'
    assert resolve_static_prompt_profile('gpt-5.6-sol', 'auto') == 'lean'


def test_kimi_lean_candidate_has_stable_hash_and_at_least_84pct_reduction():
    kwargs = _prompt_kwargs()
    full = build_static_prompt(**kwargs, profile='full')
    lean = build_static_prompt(**kwargs, profile='lean')
    repeated = build_static_prompt(**kwargs, profile='lean')
    full_tokens = count_text(full, model='kimi-k3')
    lean_tokens = count_text(lean, model='kimi-k3')

    assert repeated == lean
    assert hashlib.sha256(repeated.encode()).hexdigest() == hashlib.sha256(
        lean.encode()).hexdigest()
    assert full_tokens >= 2_500
    assert 350 <= lean_tokens <= 450
    assert 1 - lean_tokens / full_tokens >= 0.84

    variants = {
        name: build_static_prompt(**kwargs, profile=name)
        for name in (
            'lean', 'lean_no_url', 'lean_no_safety', 'lean_no_tools',
            'lean_no_output', 'lean_no_autonomy',
        )
    }
    assert len({hashlib.sha256(value.encode()).hexdigest()
                for value in variants.values()}) == len(variants)

    discovery_lean = build_static_prompt(
        **kwargs, profile='lean', tool_search_available=True)
    assert 'Hidden built-in tools may include' in discovery_lean
    assert count_text(discovery_lean, model='kimi-k3') <= 450


def test_composer_gates_hidden_capability_guidance_on_live_search_catalog():
    def platform_content(task: dict) -> str:
        blocks = collect_context_blocks(
            [{'role': 'user', 'content': 'make something'}],
            ComposeRequest(
                model='gpt-5.6-sol', task=task, has_real_tools=True,
                tool_names=frozenset({'read_files'}),
            ),
        )
        return next(
            block.content for block in blocks if block.id == 'platform_static')

    searchable_task = {
        'config': {'responses': {'promptProfile': 'lean'}},
        '_toolSearchMode': 'local',
        '_toolSearchableCount': 8,
        '_toolSearchCatalogSize': 24,
    }
    assert 'Hidden built-in tools may include' in platform_content(
        searchable_task)

    disabled_task = dict(searchable_task, _toolSearchMode='off')
    assert 'Hidden built-in tools may include' not in platform_content(
        disabled_task)

    undersized_task = dict(searchable_task, _toolSearchCatalogSize=11)
    assert 'Hidden built-in tools may include' not in platform_content(
        undersized_task)


def test_composer_stamps_requested_effective_hash_and_round_evidence():
    task = {'config': {'responses': {'promptProfile': 'lean'}}}
    block = _platform_block(task)
    evidence = task['_promptProfileV1']

    assert block.content
    assert block.provenance == {'promptProfile': evidence}
    assert evidence['contractVersion'] == PROMPT_PROFILE_EVIDENCE_VERSION
    assert evidence['requestedProfile'] == 'lean'
    assert evidence['resolvedProfile'] == 'lean'
    assert evidence['effectiveProfile'] == 'lean'
    assert evidence['status'] == 'applied'
    assert evidence['charCount'] == len(block.content)
    assert evidence['tokenCount'] == count_text(block.content, model='kimi-k3')
    assert evidence['sha256'] == hashlib.sha256(
        block.content.encode()).hexdigest()

    rendered = compose_context(
        [{'role': 'user', 'content': 'do the task'}],
        ComposeRequest(model='kimi-k3', task=task),
    )
    platform_row = next(
        row for row in rendered.manifest if row['id'] == 'platform_static')
    assert platform_row['injected'] is True
    assert platform_row['provenance']['promptProfile'] == evidence
    assert task['_contextManifest'] == rendered.manifest

    snapshot = capture_round_context(
        task,
        [{'role': 'system', 'content': block.content},
         {'role': 'user', 'content': 'do the task'}],
        [],
        round_num=0,
        model='kimi-k3',
    )
    assert snapshot['promptProfile'] == evidence
    assert snapshot['promptProfile'] is not evidence


def test_round_telemetry_reuses_only_unchanged_string_identities(monkeypatch):
    unchanged_content = 'unchanged private tool result ' * 30
    rewritten_content = ''.join([
        'rewritten private tool result ',
        'with a distinct immutable identity',
    ])
    fallback_messages = []
    monkeypatch.setattr(
        context_telemetry,
        'tool_schema_tokens',
        lambda *args, **kwargs: pytest.fail(
            'validated admission schema count must be reused'),
    )
    monkeypatch.setattr(
        context_telemetry,
        '_content_tokens',
        lambda message, **kwargs: fallback_messages.append(message) or 17,
    )
    messages = [
        {'role': 'tool', 'content': unchanged_content},
        {'role': 'tool', 'content': rewritten_content},
    ]
    task = {}

    snapshot = context_telemetry.capture_round_context(
        task,
        messages,
        [{'type': 'function'}],
        round_num=0,
        model='gpt-5.6-sol',
        precomputed_tool_schema_tokens=321,
        reusable_text_token_counts_by_identity={
            id(unchanged_content): 123,
            id(rewritten_content): -1,
        },
    )

    assert snapshot['toolSchemaTokens'] == 321
    assert snapshot['modelToolResultTokens'] == 140
    assert fallback_messages == [messages[1]]
    assert task['_contextTelemetryRounds'] == [snapshot]
    assert id(unchanged_content) not in snapshot.values()


def test_nonempty_tool_surface_recounts_an_untrusted_zero_admission_value(
    monkeypatch,
):
    schema_calls = []
    monkeypatch.setattr(
        context_telemetry,
        'tool_schema_tokens',
        lambda tools, **kwargs: schema_calls.append(tools) or 55,
    )

    snapshot = context_telemetry.capture_round_context(
        {},
        [{'role': 'user', 'content': 'hello'}],
        [{'type': 'function'}],
        round_num=0,
        model='gpt-5.6-sol',
        precomputed_tool_schema_tokens=0,
    )

    assert snapshot['toolSchemaTokens'] == 55
    assert schema_calls == [[{'type': 'function'}]]


def test_schema_token_evidence_requires_same_ordered_objects_and_model():
    first = {'type': 'function', 'function': {'name': 'read_files'}}
    second = {'type': 'function', 'function': {'name': 'grep_search'}}
    source = [first, second]
    evidence = context_telemetry.build_tool_schema_evidence(
        source,
        1_234,
        model='gpt-5.6-sol',
        source_fingerprint='a' * 64,
    )

    assert context_telemetry.reusable_tool_schema_metrics(
        evidence, list(source), model='gpt-5.6-sol') == (
            1_234, 'a' * 64)
    assert context_telemetry.reusable_tool_schema_token_count(
        evidence, [second, first], model='gpt-5.6-sol') is None
    assert context_telemetry.reusable_tool_schema_token_count(
        evidence, [dict(first), second], model='gpt-5.6-sol') is None
    assert context_telemetry.reusable_tool_schema_token_count(
        evidence, list(source), model='claude-opus-4.8') is None
    assert context_telemetry.reusable_tool_schema_token_count(
        {'model': 'gpt-5.6-sol', 'token_count': 1},
        list(source),
        model='gpt-5.6-sol',
    ) is None
    assert context_telemetry.record_tool_schema_fingerprint(
        evidence, [dict(first), second], 'b' * 64) is False
    assert context_telemetry.record_tool_schema_fingerprint(
        evidence, list(source), 'not-a-sha256') is False
    assert context_telemetry.record_tool_schema_fingerprint(
        evidence, list(source), 'b' * 64) is True
    assert context_telemetry.tool_schema_fingerprint_from_evidence(
        evidence) == 'b' * 64


def test_replace_mode_records_that_platform_profile_was_not_applied():
    task = {'config': {'responses': {'promptProfile': 'lean'}}}
    block = _platform_block(
        task,
        messages=[
            {'role': 'system', 'content': 'operator replacement'},
            {'role': 'user', 'content': 'do the task'},
        ],
        mode='replace',
    )
    evidence = task['_promptProfileV1']

    assert block.content == ''
    assert block.suppressed_reason == 'replace_mode'
    assert evidence['requestedProfile'] == 'lean'
    assert evidence['resolvedProfile'] == 'lean'
    assert evidence['effectiveProfile'] == ''
    assert evidence['status'] == 'suppressed'
    assert evidence['reason'] == 'replace_mode'
    assert evidence['charCount'] == evidence['tokenCount'] == 0
    assert evidence['sha256'] == ''


def test_prompt_build_failure_records_error_instead_of_false_adoption(
        monkeypatch):
    def fail_build(**_kwargs):
        raise RuntimeError('fault injection')

    monkeypatch.setattr(
        'lib.tasks_pkg.context_composer._providers.system_prompt_cc.'
        'build_static_prompt',
        fail_build,
    )
    task = {'config': {'responses': {'promptProfile': 'lean'}}}
    block = _platform_block(task)
    evidence = task['_promptProfileV1']

    assert block.content == ''
    assert block.suppressed_reason == 'build_failed'
    assert evidence['status'] == 'error'
    assert evidence['reason'] == 'build_failed'
    assert evidence['effectiveProfile'] == ''
    assert evidence['tokenCount'] == 0


def test_context_composer_threads_explicit_kimi_profile():
    task = {
        '_userId': 7,
        'config': {'responses': {'promptProfile': 'lean'}},
    }
    messages = [{'role': 'user', 'content': 'do the task'}]

    result = compose_task_context(
        messages,
        user_id=1,
        project_path='',
        project_enabled=False,
        memory_enabled=False,
        search_enabled=False,
        has_real_tools=False,
        task=task,
        model='kimi-k3',
    )

    evidence = task['_promptProfileV1']
    platform_row = next(
        row for row in result.manifest if row['id'] == 'platform_static')
    assert evidence['effectiveProfile'] == 'lean'
    assert platform_row['provenance']['promptProfile'] == evidence
    assert platform_row['injected'] is True


def test_benchmark_v2_prompt_arms_require_matching_applied_evidence():
    task = {'config': {'responses': {'promptProfile': 'lean'}}}
    _platform_block(task)
    evidence = task['_promptProfileV1']
    context_blocks = [{
        'id': 'platform_static',
        'provenance': {'promptProfile': evidence},
    }]

    record = _benchmark_task(
        arm='prompt_lean_kimi', context_blocks=context_blocks)
    assert record['experimentArm'] == 'prompt_lean_kimi'

    with pytest.raises(BenchmarkContractError, match='requires applied'):
        _benchmark_task(arm='prompt_lean_kimi', context_blocks=[])

    mismatched = {**evidence, 'effectiveProfile': 'full'}
    with pytest.raises(BenchmarkContractError, match='requires applied'):
        _benchmark_task(
            arm='combined_v2',
            context_blocks=[{
                'id': 'platform_static',
                'provenance': {'promptProfile': mismatched},
            }],
        )
