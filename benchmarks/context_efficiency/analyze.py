"""Analyze benchmark JSONL evidence and freeze an evidence-backed candidate."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Iterable

from lib.benchmark_contract import acceptance_decision, read_jsonl
from lib.context_telemetry import prompt_profile_evidence_matches

from .runtime import ARM_CONFIGS


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def load_stage_records(results_dir: Path, stage: str) -> dict[str, dict[str, dict]]:
    runs: dict[str, list[tuple[int, dict[str, dict]]]] = {}
    stage_dir = results_dir / stage
    for path in sorted(stage_dir.glob('*.jsonl')):
        records = read_jsonl(path)
        if not records:
            continue
        arm = str(records[0].get('experimentArm') or '')
        if not arm:
            continue
        tasks: dict[str, dict] = {}
        for record in records:
            if record.get('recordType') == 'task':
                tasks[str(record['taskId'])] = record
        created_at = int(records[0].get('createdAt') or 0)
        runs.setdefault(arm, []).append((created_at, tasks))
    return {
        arm: max(arm_runs, key=lambda item: item[0])[1]
        for arm, arm_runs in runs.items()
    }


def _telemetry_values(record: dict, field: str) -> list[float]:
    telemetry = record.get('contextTelemetry') or {}
    rounds = telemetry.get('rounds') or []
    return [float(row[field]) for row in rounds
            if isinstance(row, dict) and row.get(field) is not None]


def summarize_arm(records: dict[str, dict]) -> dict:
    rows = list(records.values())
    graded = [row for row in rows if row.get('oracle', {}).get('passed') is not None]
    resolved = sum(row.get('oracle', {}).get('passed') is True for row in graded)
    public_cost = sum(
        float((row.get('cost') or {}).get('publicApiShadowCostUsd') or 0)
        for row in rows)
    actual_cost = sum(
        float((row.get('cost') or {}).get('actualCostUsd') or 0)
        for row in rows)
    latencies = [float(row.get('latencyMs') or 0) for row in rows]
    schema_tokens = [value for row in rows
                     for value in _telemetry_values(row, 'toolSchemaTokens')]
    stable_prefix = [value for row in rows
                     for value in _telemetry_values(row, 'stablePrefixTokens')]
    usages = [(row.get('cost') or {}).get('usage') or {} for row in rows]
    all_program_runs = [run for row in rows
                        for run in ((row.get('contextTelemetry') or {}).get(
                            'programRuns') or [])
                        if isinstance(run, dict)]
    optimization_decisions = [decision for row in rows
                              for decision in ((row.get(
                                  'contextTelemetry') or {}).get(
                                      'optimizationDecisions') or [])
                              if isinstance(decision, dict)]
    prompt_profile_by_task: dict[str, list[dict]] = {}
    for task_id, row in records.items():
        telemetry = row.get('contextTelemetry') or {}
        samples = [
            round_row.get('promptProfile')
            for round_row in telemetry.get('rounds') or []
            if isinstance(round_row, dict)
            and isinstance(round_row.get('promptProfile'), dict)
        ]
        if samples:
            prompt_profile_by_task[str(task_id)] = samples
    prompt_profiles = [
        sample for samples in prompt_profile_by_task.values()
        for sample in samples
    ]

    def _profile_counts(field: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for sample in prompt_profiles:
            value = str(sample.get(field) or '')
            if value:
                counts[value] = counts.get(value, 0) + 1
        return counts

    valid_prompt_profiles = sum(
        prompt_profile_evidence_matches(
            sample,
            expected_profile=str(sample.get('effectiveProfile') or ''),
        )
        for sample in prompt_profiles
    )
    # Local ToolScript/execute_program runs share the canonical UI timeline,
    # but cannot prove that the provider's hosted OpenAI PTC route was used.
    program_runs = [run for run in all_program_runs
                    if run.get('source') == 'openai_ptc']
    program_rejections = sum(
        int(run.get('rejectedCallCount') or 0) for run in program_runs)
    program_output_truncations = sum(
        bool(run.get('outputTruncated')) for run in program_runs)
    program_budget_violations = sum(
        int(run.get('admittedCallCount') or 0)
        > int((run.get('limits') or {}).get('maxCalls') or 16)
        or int(run.get('continuationCount') or 0)
        > int((run.get('limits') or {}).get('maxContinuations') or 4)
        or int(run.get('outputBytes') or 0)
        > int((run.get('limits') or {}).get('maxOutputBytes') or 1_048_576)
        for run in program_runs)
    prompt_tokens = sum(int(usage.get('prompt_tokens') or 0) for usage in usages)
    completion_tokens = sum(
        int(usage.get('completion_tokens') or 0) for usage in usages)
    cache_read = sum(int(usage.get('cache_read_tokens') or 0) for usage in usages)
    api_rounds = sum(
        int((row.get('contextTelemetry') or {}).get('apiRounds') or 0)
        for row in rows)
    tool_calls = sum(
        int((row.get('contextTelemetry') or {}).get('toolCalls') or 0)
        for row in rows)
    gateway_only_decisions = [
        decision for decision in optimization_decisions
        if decision.get('programmaticExposure') == 'gateway_only'
    ]
    return {
        'tasks': len(rows),
        'graded': len(graded),
        'infrastructureFailures': len(rows) - len(graded),
        'resolved': resolved,
        'resolvedRate': resolved / len(graded) if graded else None,
        'actualCostUsd': round(actual_cost, 6),
        'publicApiShadowCostUsd': round(public_cost, 6),
        'publicApiShadowCostPerResolvedUsd': (
            round(public_cost / resolved, 6) if resolved else None),
        'medianLatencyMs': statistics.median(latencies) if latencies else None,
        'p90LatencyMs': _percentile(latencies, 0.90),
        'promptTokens': prompt_tokens,
        'completionTokens': completion_tokens,
        'totalTokens': prompt_tokens + completion_tokens,
        'cacheReadTokens': cache_read,
        'cacheReadShare': cache_read / prompt_tokens if prompt_tokens else None,
        'apiRounds': api_rounds,
        'apiRoundsPerTask': api_rounds / len(rows) if rows else None,
        'toolCalls': tool_calls,
        'medianToolSchemaTokens': (
            statistics.median(schema_tokens) if schema_tokens else None),
        'medianStablePrefixTokens': (
            statistics.median(stable_prefix) if stable_prefix else None),
        'compactions': sum(len(row.get('compactions') or []) for row in rows),
        'programRuns': len(all_program_runs),
        'localProgramRuns': sum(
            run.get('source') in ('execute_program', 'local_toolscript')
            for run in all_program_runs),
        'completedProgramRuns': sum(
            run.get('status') == 'completed' for run in all_program_runs),
        'ptcProgramRuns': len(program_runs),
        'completedPtcProgramRuns': sum(
            run.get('status') == 'completed' for run in program_runs),
        'programChildCalls': sum(
            int(run.get('childCallCount') or 0) for run in program_runs),
        'programRejectedCalls': program_rejections,
        'programOutputTruncations': program_output_truncations,
        'programBudgetViolations': program_budget_violations,
        'optimizationDecisions': len(optimization_decisions),
        'gatewayOnlyDecisions': len(gateway_only_decisions),
        'gatewayOnlyHiddenDirectTools': sum(
            int(decision.get('programmaticHiddenDirectToolCount') or 0)
            for decision in gateway_only_decisions),
        'ptcAutoDecisions': sum(
            decision.get('programmaticCalling') == 'auto'
            for decision in optimization_decisions),
        'multiAgentReadOnlyDecisions': sum(
            decision.get('multiAgent') == 'read_only'
            for decision in optimization_decisions),
        'multiAgentCalls': sum(
            int((row.get('contextTelemetry') or {}).get(
                'multiAgentCalls') or 0)
            for row in rows),
        'promptProfileTasksObserved': len(prompt_profile_by_task),
        'promptProfileSamples': len(prompt_profiles),
        'validPromptProfileSamples': valid_prompt_profiles,
        'requestedPromptProfiles': _profile_counts('requestedProfile'),
        'resolvedPromptProfiles': _profile_counts('resolvedProfile'),
        'effectivePromptProfiles': _profile_counts('effectiveProfile'),
    }


def _prompt_profile_adoption_clean(summary: dict, expected: str) -> bool:
    samples = int(summary.get('promptProfileSamples') or 0)
    expected_counts = {expected: samples}
    return bool(
        samples > 0
        and summary.get('promptProfileTasksObserved') == summary.get('tasks')
        and summary.get('validPromptProfileSamples') == samples
        and summary.get('requestedPromptProfiles') == expected_counts
        and summary.get('resolvedPromptProfiles') == expected_counts
        and summary.get('effectivePromptProfiles') == expected_counts
    )


def _paired(candidate: dict[str, dict], baseline: dict[str, dict]) -> tuple[
        list[dict], list[dict]]:
    task_ids = sorted(set(candidate) & set(baseline))
    pairs = [(candidate[task_id], baseline[task_id]) for task_id in task_ids]
    pairs = [pair for pair in pairs if (
        pair[0].get('oracle', {}).get('passed') is not None
        and pair[1].get('oracle', {}).get('passed') is not None)]
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def compare_arm(candidate: dict[str, dict], baseline: dict[str, dict]) -> dict:
    left, right = _paired(candidate, baseline)
    candidate_resolved = sum(
        row.get('oracle', {}).get('passed') is True for row in left)
    baseline_resolved = sum(
        row.get('oracle', {}).get('passed') is True for row in right)
    candidate_cost = sum(
        float((row.get('cost') or {}).get('publicApiShadowCostUsd') or 0)
        for row in left)
    baseline_cost = sum(
        float((row.get('cost') or {}).get('publicApiShadowCostUsd') or 0)
        for row in right)
    candidate_latency = _percentile(
        (row.get('latencyMs') or 0 for row in left), 0.90)
    baseline_latency = _percentile(
        (row.get('latencyMs') or 0 for row in right), 0.90)
    candidate_rounds = sum(
        int((row.get('contextTelemetry') or {}).get('apiRounds') or 0)
        for row in left)
    baseline_rounds = sum(
        int((row.get('contextTelemetry') or {}).get('apiRounds') or 0)
        for row in right)
    candidate_prompt_tokens = sum(
        int(((row.get('cost') or {}).get('usage') or {}).get(
            'prompt_tokens') or 0)
        for row in left)
    baseline_prompt_tokens = sum(
        int(((row.get('cost') or {}).get('usage') or {}).get(
            'prompt_tokens') or 0)
        for row in right)
    return {
        'pairs': len(left),
        'candidateResolved': candidate_resolved,
        'baselineResolved': baseline_resolved,
        'qualityNotLower': candidate_resolved >= baseline_resolved,
        'candidatePublicCostUsd': round(candidate_cost, 6),
        'baselinePublicCostUsd': round(baseline_cost, 6),
        'costNotHigher': candidate_cost <= baseline_cost,
        'candidateP90LatencyMs': candidate_latency,
        'baselineP90LatencyMs': baseline_latency,
        'latencyNotHigher': bool(
            candidate_latency is not None and baseline_latency is not None
            and candidate_latency <= baseline_latency),
        'candidateApiRounds': candidate_rounds,
        'baselineApiRounds': baseline_rounds,
        'apiRoundsNotHigher': candidate_rounds <= baseline_rounds,
        'apiRoundReduction': (
            1 - candidate_rounds / baseline_rounds
            if baseline_rounds else None),
        'candidatePromptTokens': candidate_prompt_tokens,
        'baselinePromptTokens': baseline_prompt_tokens,
        'promptTokenReduction': (
            1 - candidate_prompt_tokens / baseline_prompt_tokens
            if baseline_prompt_tokens else None),
    }


def _deep_merge(target: dict, update: dict) -> dict:
    result = json.loads(json.dumps(target))
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def freeze_candidate(grouped: dict[str, dict[str, dict]]) -> dict:
    if 'tofu-control' not in grouped:
        raise ValueError('tofu-control results are required to freeze a candidate')
    baseline = grouped['tofu-control']
    baseline_summary = summarize_arm(baseline)
    config = json.loads(json.dumps(ARM_CONFIGS['tofu-control']))
    selected: list[str] = []
    evidence: dict[str, dict] = {}

    def decision(arm: str) -> tuple[dict | None, dict | None]:
        if arm not in grouped:
            evidence[arm] = {'selected': False, 'reason': 'not_run'}
            return None, None
        comparison = compare_arm(grouped[arm], baseline)
        summary = summarize_arm(grouped[arm])
        evidence[arm] = {'comparison': comparison, 'summary': summary}
        return comparison, summary

    comparison, summary = decision('tofu-explicit')
    if comparison and comparison['pairs'] and comparison['qualityNotLower'] \
            and comparison['costNotHigher']:
        config = _deep_merge(config, {
            'cache': {'gpt56BreakpointMode': 'explicit'}})
        selected.append('cache.gpt56BreakpointMode=explicit')
        evidence['tofu-explicit']['selected'] = True
    elif comparison:
        evidence['tofu-explicit']['selected'] = False
        evidence['tofu-explicit']['reason'] = 'quality_or_cost_gate_failed'

    comparison, summary = decision('tofu-routed')
    baseline_schema = baseline_summary['medianToolSchemaTokens']
    routed_schema = summary['medianToolSchemaTokens'] if summary else None
    schema_reduction = None
    if baseline_schema and routed_schema is not None:
        schema_reduction = 1 - routed_schema / baseline_schema
    if 'tofu-routed' in evidence:
        evidence['tofu-routed']['schemaReduction'] = schema_reduction
    if comparison and comparison['pairs'] and comparison['qualityNotLower'] \
            and schema_reduction is not None and schema_reduction >= 0.50:
        config = _deep_merge(config, {
            'tools': {'nativeExposure': 'routed'}})
        selected.append('tools.nativeExposure=routed')
        evidence['tofu-routed']['selected'] = True
    elif comparison:
        evidence['tofu-routed']['selected'] = False
        evidence['tofu-routed']['reason'] = (
            'quality_gate_or_50pct_schema_reduction_failed')

    comparison, summary = decision('tofu-evidence')
    if comparison and comparison['pairs'] and comparison['qualityNotLower'] \
            and summary and summary['compactions'] > 0:
        config = _deep_merge(config, {
            'compaction': {'evidenceLedger': True}})
        selected.append('compaction.evidenceLedger=true')
        evidence['tofu-evidence']['selected'] = True
    elif comparison:
        evidence['tofu-evidence']['selected'] = False
        evidence['tofu-evidence']['reason'] = (
            'quality_gate_failed_or_no_compaction_observed')

    comparison, ptc_summary = decision('tofu-ptc')
    ptc_protocol_clean = bool(
        ptc_summary and ptc_summary['ptcProgramRuns'] > 0
        and ptc_summary['completedPtcProgramRuns']
        == ptc_summary['ptcProgramRuns']
        and ptc_summary['programRejectedCalls'] == 0
        and ptc_summary['programOutputTruncations'] == 0
        and ptc_summary['programBudgetViolations'] == 0)
    if 'tofu-ptc' in evidence:
        evidence['tofu-ptc']['protocolClean'] = ptc_protocol_clean
    if comparison and comparison['pairs'] and comparison['qualityNotLower'] \
            and comparison['costNotHigher'] and comparison['latencyNotHigher'] \
            and ptc_protocol_clean:
        config = _deep_merge(config, {
            'tools': {'programmaticCalling': 'auto'}})
        selected.append('tools.programmaticCalling=auto')
        evidence['tofu-ptc']['selected'] = True
    elif comparison:
        evidence['tofu-ptc']['selected'] = False
        evidence['tofu-ptc']['reason'] = (
            'quality_cost_latency_or_protocol_evidence_gate_failed')

    comparison, lean_summary = decision('tofu-prompt-lean')
    prompt_reduction = None
    if baseline_summary['promptTokens'] and lean_summary:
        prompt_reduction = 1 - (
            lean_summary['promptTokens'] / baseline_summary['promptTokens'])
    if 'tofu-prompt-lean' in evidence:
        evidence['tofu-prompt-lean']['promptTokenReduction'] = prompt_reduction
    prompt_profile_adoption_clean = bool(
        lean_summary
        and _prompt_profile_adoption_clean(baseline_summary, 'full')
        and _prompt_profile_adoption_clean(lean_summary, 'lean')
    )
    if 'tofu-prompt-lean' in evidence:
        evidence['tofu-prompt-lean'][
            'profileAdoptionClean'] = prompt_profile_adoption_clean
    if comparison and comparison['pairs'] and comparison['qualityNotLower'] \
            and comparison['costNotHigher'] and prompt_reduction is not None \
            and prompt_reduction > 0 and prompt_profile_adoption_clean:
        config = _deep_merge(config, {
            'responses': {'promptProfile': 'lean'}})
        selected.append('responses.promptProfile=lean')
        evidence['tofu-prompt-lean']['selected'] = True
    elif comparison:
        evidence['tofu-prompt-lean']['selected'] = False
        evidence['tofu-prompt-lean']['reason'] = (
            'quality_cost_prompt_token_or_adoption_gate_failed')

    effort_eligible = []
    for arm in ('tofu-effort-medium', 'tofu-effort-low'):
        comparison, _summary = decision(arm)
        if comparison and comparison['pairs'] and comparison['qualityNotLower'] \
                and comparison['costNotHigher']:
            effort_eligible.append((
                comparison['candidatePublicCostUsd'],
                comparison['candidateP90LatencyMs'] or float('inf'),
                0 if arm == 'tofu-effort-low' else 1,
                arm,
            ))
    if effort_eligible:
        _cost, _latency, _preference, winner = min(effort_eligible)
        effort = ARM_CONFIGS[winner]['thinkingDepth']
        config['thinkingDepth'] = effort
        selected.append(f'thinkingDepth={effort}')
        evidence[winner]['selected'] = True
        for _cost, _latency, _preference, arm in effort_eligible:
            if arm != winner:
                evidence[arm]['selected'] = False
                evidence[arm]['reason'] = 'eligible_but_higher_cost_or_latency'
    else:
        for arm in ('tofu-effort-medium', 'tofu-effort-low'):
            if arm in evidence and 'comparison' in evidence[arm]:
                evidence[arm]['selected'] = False
                evidence[arm].setdefault(
                    'reason', 'quality_or_cost_gate_failed')

    comparison, multi_summary = decision('tofu-multi-agent')
    multi_agent_exercised = bool(
        multi_summary and multi_summary['multiAgentReadOnlyDecisions'] > 0
        and multi_summary['multiAgentCalls'] > 0)
    if 'tofu-multi-agent' in evidence:
        evidence['tofu-multi-agent']['taskGateExercised'] = (
            multi_agent_exercised)
    if comparison and comparison['pairs'] and comparison['qualityNotLower'] \
            and comparison['costNotHigher'] and comparison['latencyNotHigher'] \
            and multi_agent_exercised:
        config = _deep_merge(
            config, {'orchestration': {'multiAgent': 'auto'}})
        selected.append('orchestration.multiAgent=auto')
        evidence['tofu-multi-agent']['selected'] = True
    elif comparison:
        evidence['tofu-multi-agent']['selected'] = False
        evidence['tofu-multi-agent']['reason'] = (
            'quality_cost_latency_or_task_gate_evidence_failed')

    workset_arms = [arm for arm in ('tofu-ws64', 'tofu-ws96', 'tofu-ws128')
                    if arm in grouped]
    eligible = []
    workset_compactions = baseline_summary['compactions']
    for arm in workset_arms:
        comparison, summary = decision(arm)
        workset_compactions += summary['compactions'] if summary else 0
        if comparison and comparison['pairs'] and comparison['qualityNotLower']:
            eligible.append((
                -comparison['candidateResolved'],
                comparison['candidatePublicCostUsd'], arm))
    if eligible and workset_compactions > 0:
        _neg_resolved, _cost, winner = min(eligible)
        config = _deep_merge(config, {'compaction': {
            'workingSetTokens': ARM_CONFIGS[winner][
                'compaction']['workingSetTokens']}})
        selected.append(
            'compaction.workingSetTokens='
            + str(ARM_CONFIGS[winner]['compaction']['workingSetTokens']))
        evidence[winner]['selected'] = True
    elif workset_arms:
        for arm in workset_arms:
            evidence[arm]['selected'] = False
            evidence[arm].setdefault(
                'reason', 'no_compaction_observed_or_quality_gate_failed')

    return {
        'contractVersion': 'tofu-context-efficiency-candidate/v1',
        'frozenAt': int(time.time() * 1000),
        'selectionRule': (
            'predeclared single-factor quality/cost/schema/latency gates'),
        'selectedOptimizations': selected,
        'config': config,
        'baseline': baseline_summary,
        'evidence': evidence,
    }


def analyze_stage(
    results_dir: Path, stage: str, *, baseline_arm: str = 'tofu-control',
) -> dict:
    grouped = load_stage_records(results_dir, stage)
    report = {
        'stage': stage,
        'baselineArm': baseline_arm,
        'arms': {arm: summarize_arm(records)
                 for arm, records in sorted(grouped.items())},
        'comparisons': {},
    }
    if baseline_arm in grouped:
        report['comparisons'] = {
            arm: compare_arm(records, grouped[baseline_arm])
            for arm, records in sorted(grouped.items())
            if arm != baseline_arm
        }
    if 'tofu-candidate' in grouped and 'codex-mechanism' in grouped:
        candidate, baseline = _paired(
            grouped['tofu-candidate'], grouped['codex-mechanism'])
        if candidate:
            report['acceptance'] = acceptance_decision(
                candidate_oracles=[row['oracle']['passed'] for row in candidate],
                baseline_oracles=[row['oracle']['passed'] for row in baseline],
                candidate_public_cost_usd=sum(
                    row['cost']['publicApiShadowCostUsd'] for row in candidate),
                baseline_public_cost_usd=sum(
                    row['cost']['publicApiShadowCostUsd'] for row in baseline),
                candidate_p90_latency_ms=_percentile(
                    (row['latencyMs'] for row in candidate), 0.90) or 0,
                baseline_p90_latency_ms=_percentile(
                    (row['latencyMs'] for row in baseline), 0.90) or 0,
            )
    return report


def _markdown(report: dict) -> str:
    lines = [f"# Context-efficiency results: {report['stage']}", '']
    lines.append('| Arm | Resolved | Graded | Infra | Shadow cost | p90 latency | Schema tokens |')
    lines.append('|---|---:|---:|---:|---:|---:|---:|')
    for arm, row in report['arms'].items():
        lines.append(
            f"| {arm} | {row['resolved']} | {row['graded']} | "
            f"{row['infrastructureFailures']} | "
            f"${row['publicApiShadowCostUsd']:.4f} | "
            f"{row['p90LatencyMs'] or 0:.0f} ms | "
            f"{row['medianToolSchemaTokens'] or 0:.0f} |")
    if report.get('acceptance'):
        lines.extend(['', '## Acceptance', '', '```json',
                      json.dumps(report['acceptance'], ensure_ascii=False,
                                 indent=2), '```'])
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--results-dir', type=Path, required=True)
    parser.add_argument('--stage', required=True)
    parser.add_argument('--baseline-arm', default='tofu-control')
    parser.add_argument('--report-out', type=Path)
    parser.add_argument('--candidate-out', type=Path)
    args = parser.parse_args(argv)
    report = analyze_stage(
        args.results_dir, args.stage, baseline_arm=args.baseline_arm)
    if args.report_out:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        args.report_out.with_suffix('.md').write_text(_markdown(report))
    if args.candidate_out:
        candidate = freeze_candidate(load_stage_records(
            args.results_dir, args.stage))
        args.candidate_out.parent.mkdir(parents=True, exist_ok=True)
        args.candidate_out.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + '\n')
        report['candidate'] = candidate
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
