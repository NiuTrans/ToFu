"""Assess measured conversation evidence; no timing assertions in unit CI.

Policy owner: conversation_concurrency_contract.json. Used by single-run and
paired reports. A correctness pass, a missing sample, or a microbenchmark must
never be promoted to an interactive-latency/capacity pass.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

POLICY = json.loads(Path(__file__).with_name('conversation_concurrency_contract.json').read_text())


def assess(report: dict) -> dict:
    failures = []
    coverage = []
    for field in ('conversations', 'rounds', 'chunk_bytes'):
        required = POLICY['minimum_' + field]
        if report.get(field, 0) < required:
            coverage.append(f'{field}: {report.get(field, 0)} < {required}')
    def measured_restart(field):
        value = report.get(field)
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)

    expected_conversations = report.get('conversations', 0)
    if (not report.get('restart_while_running')
            or not measured_restart('running_restart_ms')
            or report.get('peak_simultaneous_running') != expected_conversations
            or report.get('recovered_running') != expected_conversations):
        coverage.append('running crash/reopen not measured for all requested conversations')
    if (not measured_restart('restart_ms')
            or report.get('recovered_completed') != expected_conversations
            or report.get('recovered_exact_content') != expected_conversations
            or (report.get('carry_stream_events')
                and report.get('recovered_exact_replay') != expected_conversations)):
        coverage.append('post-completion crash/reopen not measured for all requested conversations')
    integrity_code = report.get('integrity_returncode')
    if not isinstance(integrity_code, int) or isinstance(integrity_code, bool) or integrity_code != 0:
        coverage.append('integrity verification not completed successfully')
    if report.get('authority', {}).get('crash_scope') not in ('sigkill', 'in-process-abandon'):
        coverage.append('process-crash mechanism not verified')
    if report.get('authority', {}).get('crash_scope') == 'in-process-abandon':
        coverage.append('in-process abandon is not a process-crash durability test')
    if report.get('recover_interrupted'):
        coverage.append('streaming skipped for interrupted recovery')
    if report.get('stream_interval_ms', float('inf')) > POLICY['maximum_stream_interval_ms']:
        coverage.append('scheduled offered load not measured at required cadence')
    if (report.get('rpc_deadline_seconds') != POLICY['rpc_deadline_seconds']
            or report.get('authority', {}).get('rpc_deadline_seconds') != POLICY['rpc_deadline_seconds']):
        coverage.append('common RPC deadline not verified for this backend')
    if report.get('durability') != 'strict':
        coverage.append('strict durability not verified')
    measurements = report.get('latency_ms', {})
    metrics = {}
    for stage, budget in POLICY['p99_budget_ms'].items():
        summary = measurements.get(stage, {})
        expected = report.get('conversations', 0) * (report.get('rounds', 0) if stage == 'stream_scheduled' else 1)
        value = summary.get('p99')
        complete = expected > 0 and summary.get('count') == expected
        valid = isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        passed = complete and valid and value <= budget
        metrics[stage] = {'p99_ms': value, 'budget_ms': budget, 'samples': summary.get('count', 0),
                          'expected_samples': expected, 'passed': passed}
        if not passed:
            failures.append(f'{stage}: missing/invalid samples or P99 above {budget} ms')
    correctness = bool(report.get('correctness_passed'))
    return {'policy': POLICY['format'], 'correctness_passed': correctness,
            'latency_passed': not failures, 'coverage_passed': not coverage,
            'passed': correctness and not failures and not coverage,
            'metrics': metrics, 'failures': failures, 'coverage_gaps': coverage,
            'uncovered': POLICY['uncovered']}
