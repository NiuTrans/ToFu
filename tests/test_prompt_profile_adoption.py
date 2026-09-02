"""Static-prompt profile resolution, measurement, and adoption evidence."""

from __future__ import annotations

import hashlib

import pytest

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
