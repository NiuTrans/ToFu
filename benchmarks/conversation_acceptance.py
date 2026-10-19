"""Assess measured conversation evidence; no timing assertions in unit CI.

Policy owner: conversation_concurrency_contract.json. Used by single-run and
paired reports. A correctness pass, a missing sample, or a microbenchmark must
never be promoted to an interactive-latency/capacity pass.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from benchmarks.conversation_provenance import capture_sources, qualify_pipeline_sources, qualify_provenance

POLICY = json.loads(Path(__file__).with_name('conversation_concurrency_contract.json').read_text())


def assess(report: dict, *, current_sources: dict | None = None) -> dict:
    if current_sources is None:
        try:
            current_sources = capture_sources()
        except (OSError, RuntimeError, ValueError) as exc:
            current_sources = {'capture_error': str(exc)}
    authority = report.get('authority')
    authority = authority if isinstance(authority, dict) else {}
    provenance = qualify_provenance(
        report.get('provenance'), backend=report.get('backend'),
        binary_sha256=report.get('binary_sha256'), authority=authority,
        current_sources=current_sources)
    failures = list(provenance['failures'])
    sources = report.get('provenance', {})
    sources = sources.get('sources_before', {}) if isinstance(sources, dict) else {}
    files = sources.get('files', {}) if isinstance(sources, dict) else {}
    if 'delivery_pipeline' in report:
        pipeline_provenance = qualify_pipeline_sources(report['delivery_pipeline'], sources=sources)
        failures.extend(pipeline_provenance['failures'])
        for key in ('failures', 'frozen_failures'):
            provenance[key].extend(pipeline_provenance[key])
        for key in ('current_source_qualified', 'frozen_build_comparable'):
            provenance[key] = provenance[key] and pipeline_provenance[key]
    if (not isinstance(files, dict)
            or report.get('benchmark_source_sha256') != files.get('benchmarks/conversation_concurrency.py')
            or not report.get('benchmark_source_sha256')):
        reason = 'benchmark source identity does not match provenance receipt'
        failures.append(reason)
        provenance['failures'].append(reason)
        provenance['frozen_failures'].append(reason)
        provenance['current_source_qualified'] = False
        provenance['frozen_build_comparable'] = False
    # Inspect the observed settings as well as a wrapper's verdict. The plain
    # concurrency entry has no wrapper, and old diagnostic reports may omit it.
    requested_profile = report.get('requested_native_profile_every')
    profiled = requested_profile not in (None, 0, '0')
    profiled |= bool(report.get('requested_client_profile')) or 'client_stages_ms' in report
    def profile_enabled(profile):
        if not isinstance(profile, dict):
            return True  # A malformed observation cannot prove profiling off.
        return (bool(profile.get('enabled'))
                or profile.get('sampleEvery') not in (None, 0, '0'))

    for field in ('admission_storage_metrics', 'productive_start_storage_metrics',
                  'terminal_boundary_storage_metrics', 'storage_metrics'):
        envelope = report.get(field, {})
        profiled |= (not isinstance(envelope, dict)
                     or profile_enabled(envelope.get('requestProfile', {})))
    pipeline = report.get('delivery_pipeline', {})
    if isinstance(pipeline, dict):
        coordinator = pipeline.get('coordinator', {})
        profiled |= pipeline.get('delivery_profile_every', 0) not in (0, '0')
        profiled |= (not isinstance(coordinator, dict)
                     or profile_enabled(coordinator.get('performance_profile', {})))
    # Instrumentation eligibility is a separate dimension, not a source verdict.
    latency_eligible = report.get('latency_comparison_eligible') is not False and not profiled
    if not latency_eligible:
        failures.append('diagnostic profiling enabled or source provenance changed; latency comparison is not qualified')
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
    if authority.get('crash_scope') not in ('sigkill', 'in-process-abandon'):
        coverage.append('process-crash mechanism not verified')
    if authority.get('crash_scope') == 'in-process-abandon':
        coverage.append('in-process abandon is not a process-crash durability test')
    if report.get('recover_interrupted'):
        coverage.append('streaming skipped for interrupted recovery')
    if report.get('stream_interval_ms', float('inf')) > POLICY['maximum_stream_interval_ms']:
        coverage.append('scheduled offered load not measured at required cadence')
    if (report.get('rpc_deadline_seconds') != POLICY['rpc_deadline_seconds']
            or authority.get('rpc_deadline_seconds') != POLICY['rpc_deadline_seconds']):
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
            'current_source_qualified': provenance['current_source_qualified'],
            'frozen_build_comparable': provenance['frozen_build_comparable'],
            'provenance': provenance,
            'latency_comparison_eligible': latency_eligible,
            'latency_passed': not failures, 'coverage_passed': not coverage,
            'passed': correctness and not failures and not coverage,
            'metrics': metrics, 'failures': failures, 'coverage_gaps': coverage,
            'uncovered': POLICY['uncovered']}
