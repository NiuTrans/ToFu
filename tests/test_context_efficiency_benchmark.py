from __future__ import annotations

import json
from unittest import mock

import pytest

from benchmarks.context_efficiency.analyze import (
    compare_arm,
    freeze_candidate,
    summarize_arm,
)
from benchmarks.context_efficiency.dataset import (
    BenchmarkTask,
    SPLIT_SIZES,
    build_frozen_manifest,
)
from benchmarks.context_efficiency.runtime import (
    PUBLIC_GPT56_SOL_PRICING,
    _TOFU_DRIVER,
    _base_reset_and_diff,
    _codex_usage,
    _read_inference,
    arm_config,
    build_agent_prompt,
)
from lib.benchmark_contract import public_price_cost_from_usage
from lib.context_telemetry import PROMPT_PROFILE_EVIDENCE_VERSION


pytestmark = pytest.mark.unit


def _task(index: int, language: str = 'Go') -> BenchmarkTask:
    return BenchmarkTask(
        instance_id=f'owner__repo-{index}', repo=f'owner/repo-{index % 20}',
        base_commit='abc', problem_statement='fix it', hints_text='',
        patch='diff --git a/x b/x\n+old\n-new\n+new', test_patch='',
        fail_to_pass=('test',), pass_to_pass=(), version='1',
        image='swebench/image:latest', eval_script='true', log_parser='x',
        eval_type='pytest', language=language,
        difficulty=('easy', 'medium', 'hard')[index % 3])


def _record(task_id: str, passed: bool, *, cost: float = 1,
            schema: int = 100, latency: int = 100,
            compactions: int = 0, program_runs: list[dict] | None = None,
            prompt_tokens: int = 100,
            optimization_decisions: list[dict] | None = None,
            multi_agent_calls: int = 0,
            prompt_profile: str = 'full',
            ) -> dict:
    prompt_evidence = {
        'contractVersion': PROMPT_PROFILE_EVIDENCE_VERSION,
        'requestedProfile': prompt_profile,
        'resolvedProfile': prompt_profile,
        'effectiveProfile': prompt_profile,
        'status': 'applied',
        'reason': '',
        'model': 'kimi-k3',
        'charCount': 100,
        'tokenCount': 20,
        'sha256': 'a' * 64,
    }
    return {
        'taskId': task_id,
        'oracle': {'passed': passed},
        'cost': {
            'actualCostUsd': 0,
            'publicApiShadowCostUsd': cost,
            'usage': {'prompt_tokens': prompt_tokens, 'cache_read_tokens': 50},
        },
        'latencyMs': latency,
        'contextTelemetry': {
            'rounds': [{
                'toolSchemaTokens': schema, 'stablePrefixTokens': 20,
                'promptProfile': prompt_evidence}],
            'programRuns': program_runs or [],
            'optimizationDecisions': optimization_decisions or [],
            'multiAgentCalls': multi_agent_calls,
        },
        'compactions': [{}] * compactions,
    }


def test_frozen_splits_are_deterministic_disjoint_and_sized():
    tasks = [_task(index, ('Go', 'Rust', 'Java')[index % 3])
             for index in range(300)]
    first = build_frozen_manifest(tasks, 'sha')
    second = build_frozen_manifest(tasks, 'sha')
    assert first == second
    seen = set()
    for stage, expected in SPLIT_SIZES.items():
        ids = {row['taskId'] for row in first['splits'][stage]}
        assert len(ids) == expected
        assert not seen & ids
        seen |= ids


def test_arm_config_is_nested_and_candidate_can_be_loaded(tmp_path):
    assert arm_config('tofu-routed')['tools']['nativeExposure'] == 'routed'
    assert arm_config('tofu-prompt-lean')['responses'][
        'promptProfile'] == 'lean'
    assert arm_config('tofu-effort-medium')['thinkingDepth'] == 'medium'
    assert arm_config('tofu-effort-low')['thinkingDepth'] == 'low'
    assert arm_config('tofu-multi-agent')[
        'orchestration']['multiAgent'] == 'auto'
    assert arm_config('tofu-control')['responses'] == {
        'promptProfile': 'full'}
    assert arm_config('tofu-control')['orchestration'] == {
        'multiAgent': 'off'}
    candidate = tmp_path / 'candidate.json'
    candidate.write_text(json.dumps({'config': {
        'tools': {'nativeExposure': 'routed'}}}))
    assert arm_config('tofu-candidate', candidate) == {
        'tools': {'nativeExposure': 'routed'}}
    assert 'Work alone' in build_agent_prompt(_task(1))
    assert 'root agent alone may edit files' in build_agent_prompt(
        _task(1), allow_subagents=True)


def test_codex_usage_and_frozen_official_price_card():
    usage, tool_calls, error = _codex_usage([
        {'type': 'item.completed', 'item': {'type': 'command_execution'}},
        {'type': 'turn.completed', 'usage': {
            'input_tokens': 1000, 'cached_input_tokens': 600,
            'output_tokens': 100, 'reasoning_output_tokens': 80}},
    ])
    assert not error
    assert tool_calls == 1
    assert usage['cache_read_tokens'] == 600
    priced = public_price_cost_from_usage(
        usage, PUBLIC_GPT56_SOL_PRICING)
    assert priced['costUsd'] == 0.0053


def test_tofu_driver_recovery_path_compiles_and_preserves_task_lookup():
    compile(_TOFU_DRIVER, '<tofu_driver>', 'exec')
    assert '/api/v1/tasks/by-conv/' in _TOFU_DRIVER
    assert 'build_result({}, state, task_id, request_error)' in _TOFU_DRIVER


def test_patch_capture_filters_agent_and_compiler_scratch_files():
    _, diff = _base_reset_and_diff(_task(1))
    assert 'git add -A' not in diff
    assert 'git add -u' in diff
    assert 'git ls-files --others --exclude-standard -z' in diff
    assert '.tmp/*' in diff
    assert 'JOURNAL.md' in diff


def test_missing_usage_with_http_error_is_retryable_infrastructure(tmp_path):
    (tmp_path / 'model_patch.diff').write_text('diff --git a/x b/x\n')
    (tmp_path / 'inference.json').write_text(json.dumps({
        'status': 'error', 'error': 'HTTPError: HTTP Error 500'}))
    outcome = _read_inference(tmp_path, 0, None, backend='Tofu')
    assert outcome.patch
    assert outcome.infrastructure_error is True


def test_recovered_usage_keeps_deadline_as_agent_outcome(tmp_path):
    (tmp_path / 'model_patch.diff').write_text('diff --git a/x b/x\n')
    (tmp_path / 'inference.json').write_text(json.dumps({
        'status': 'running',
        'error': 'HTTPError: HTTP Error 500',
        'usage': {'prompt_tokens': 123},
        'round_usage': [{'round': 1, 'usage': {'prompt_tokens': 123}}],
    }))
    outcome = _read_inference(tmp_path, 0, None, backend='Tofu')
    assert outcome.usage['prompt_tokens'] == 123
    assert outcome.infrastructure_error is False


def test_analyzer_selects_only_predeclared_passing_arms():
    baseline = {
        't1': _record('t1', True, cost=2, schema=100, latency=100),
        't2': _record('t2', False, cost=2, schema=100, latency=100),
    }
    routed = {
        't1': _record('t1', True, cost=1, schema=40, latency=90),
        't2': _record('t2', False, cost=1, schema=40, latency=90),
    }
    explicit = {
        't1': _record('t1', True, cost=1, schema=100, latency=90),
        't2': _record('t2', False, cost=1, schema=100, latency=90),
    }
    ptc = {
        't1': _record('t1', True, cost=1, schema=100, latency=110),
        't2': _record('t2', False, cost=1, schema=100, latency=110),
    }
    candidate = freeze_candidate({
        'tofu-control': baseline,
        'tofu-routed': routed,
        'tofu-explicit': explicit,
        'tofu-ptc': ptc,
    })
    assert candidate['config']['cache']['gpt56BreakpointMode'] == 'explicit'
    assert candidate['config']['tools']['nativeExposure'] == 'routed'
    assert candidate['config']['tools']['programmaticCalling'] == 'off'
    assert summarize_arm(routed)['medianToolSchemaTokens'] == 40
    assert compare_arm(routed, baseline)['qualityNotLower'] is True


def test_benchmark_can_explicitly_trust_authenticated_proxy_probe_block():
    from lib import proxy
    from lib.subscription_routes import Route

    routes = [
        Route('direct', 'direct', 'direct'),
        Route('pool:bench', 'proxy bench', 'proxy',
              proxy_url='http://proxy.invalid:8080'),
    ]
    with mock.patch.dict('os.environ', {
            'TOFU_SUBSCRIPTION_TRUST_CONFIGURED_PROXY': '1'}), \
            mock.patch.object(proxy, 'subscription_route_specs',
                              return_value=routes), \
            mock.patch('lib.subscription_routes.manager.candidates') as pick:
        selected = proxy.subscription_route_candidates(
            'https://chatgpt.com/backend-api/codex/responses')
    assert [route.route_id for route in selected] == ['pool:bench']
    pick.assert_not_called()


def test_ptc_candidate_requires_clean_observed_protocol_evidence():
    baseline = {'t1': _record('t1', True, cost=2, latency=100)}
    no_route = {'t1': _record('t1', True, cost=1, latency=90)}
    candidate = freeze_candidate({
        'tofu-control': baseline, 'tofu-ptc': no_route})
    assert candidate['config']['tools']['programmaticCalling'] == 'off'
    assert candidate['evidence']['tofu-ptc']['protocolClean'] is False

    clean_run = [{
        'callId': 'prog-1', 'source': 'openai_ptc',
        'status': 'completed', 'childCallCount': 3,
        'admittedCallCount': 3, 'rejectedCallCount': 0,
        'continuationCount': 1,
        'rawOutputBytes': 128, 'outputBytes': 128,
        'outputTruncated': False,
        'limits': {
            'maxCalls': 16, 'maxContinuations': 4,
            'maxOutputBytes': 1_048_576,
        },
    }]
    observed = {'t1': _record(
        't1', True, cost=1, latency=90, program_runs=clean_run)}
    candidate = freeze_candidate({
        'tofu-control': baseline, 'tofu-ptc': observed})
    assert candidate['config']['tools']['programmaticCalling'] == 'auto'
    summary = candidate['evidence']['tofu-ptc']['summary']
    assert summary['programRuns'] == summary['ptcProgramRuns'] == 1
    assert summary['completedProgramRuns'] == 1
    assert summary['completedPtcProgramRuns'] == 1
    assert summary['programRejectedCalls'] == 0
    assert summary['programOutputTruncations'] == 0

    truncated = json.loads(json.dumps(observed))
    truncated['t1']['contextTelemetry']['programRuns'][0][
        'outputTruncated'] = True
    candidate = freeze_candidate({
        'tofu-control': baseline, 'tofu-ptc': truncated})
    assert candidate['config']['tools']['programmaticCalling'] == 'off'
    assert candidate['evidence']['tofu-ptc']['protocolClean'] is False
    assert candidate['evidence']['tofu-ptc']['summary'][
        'programOutputTruncations'] == 1

    local_only = json.loads(json.dumps(observed))
    local_only['t1']['contextTelemetry']['programRuns'][0][
        'source'] = 'execute_program'
    candidate = freeze_candidate({
        'tofu-control': baseline, 'tofu-ptc': local_only})
    assert candidate['config']['tools']['programmaticCalling'] == 'off'
    summary = candidate['evidence']['tofu-ptc']['summary']
    assert summary['programRuns'] == 1
    assert summary['ptcProgramRuns'] == 0


def test_candidate_can_select_lean_effort_and_exercised_multi_agent():
    baseline = {'t1': _record(
        't1', True, cost=3, latency=120, prompt_tokens=300)}
    lean = {'t1': _record(
        't1', True, cost=2, latency=110, prompt_tokens=180,
        prompt_profile='lean')}
    medium = {'t1': _record(
        't1', True, cost=1.5, latency=100, prompt_tokens=220)}
    low = {'t1': _record(
        't1', True, cost=1, latency=95, prompt_tokens=200)}
    multi = {'t1': _record(
        't1', True, cost=2, latency=90, prompt_tokens=250,
        optimization_decisions=[{'multiAgent': 'read_only'}],
        multi_agent_calls=2)}
    candidate = freeze_candidate({
        'tofu-control': baseline,
        'tofu-prompt-lean': lean,
        'tofu-effort-medium': medium,
        'tofu-effort-low': low,
        'tofu-multi-agent': multi,
    })
    assert candidate['config']['responses']['promptProfile'] == 'lean'
    assert candidate['config']['orchestration']['multiAgent'] == 'auto'
    assert candidate['config']['thinkingDepth'] == 'low'
    assert candidate['evidence']['tofu-prompt-lean'][
        'promptTokenReduction'] == pytest.approx(0.4)
    assert candidate['evidence']['tofu-prompt-lean'][
        'profileAdoptionClean'] is True
    assert candidate['evidence']['tofu-multi-agent'][
        'taskGateExercised'] is True
    assert candidate['evidence']['tofu-multi-agent']['summary'][
        'multiAgentCalls'] == 2

    unobserved = json.loads(json.dumps(multi))
    unobserved['t1']['contextTelemetry']['multiAgentCalls'] = 0
    rejected = freeze_candidate({
        'tofu-control': baseline, 'tofu-multi-agent': unobserved})
    assert rejected['config']['orchestration']['multiAgent'] == 'off'
    assert rejected['evidence']['tofu-multi-agent'][
        'taskGateExercised'] is False

    missing_adoption = json.loads(json.dumps(lean))
    del missing_adoption['t1']['contextTelemetry']['rounds'][0][
        'promptProfile']
    rejected_prompt = freeze_candidate({
        'tofu-control': baseline,
        'tofu-prompt-lean': missing_adoption,
    })
    assert rejected_prompt['config']['responses']['promptProfile'] == 'full'
    assert rejected_prompt['evidence']['tofu-prompt-lean'][
        'profileAdoptionClean'] is False

    malformed_adoption = json.loads(json.dumps(lean))
    malformed_adoption['t1']['contextTelemetry']['rounds'][0][
        'promptProfile']['tokenCount'] = 'many'
    malformed_prompt = freeze_candidate({
        'tofu-control': baseline,
        'tofu-prompt-lean': malformed_adoption,
    })
    assert malformed_prompt['config']['responses']['promptProfile'] == 'full'
    assert malformed_prompt['evidence']['tofu-prompt-lean'][
        'profileAdoptionClean'] is False
