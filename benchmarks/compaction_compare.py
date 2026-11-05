"""Offline L2 structural comparison: exact outcomes before component clocks.

Provider, archive, phase delivery and objective writes are deterministic ports.
Real history planning, summary acceptance, projection and token estimates run in
both arms. This measures Python compaction work, not provider/storage/task latency.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import copy
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SUMMARY = '### Objective\nComplete the scoped synthetic fixture.\n### Next Steps\nVerify the result.'
VARIANTS = (
    'accepted', 'no_anchor', 'open_batch', 'nothing_foldable', 'summary_empty',
    'summary_error', 'utility_fallback', 'cancel_entry', 'cancel_summary',
    'cancel_archive', 'adaptive_missing', 'utility_adaptive', 'pre_low_yield',
    'pre_cache_negative', 'adoption_low_yield', 'adoption_cache_negative',
    'economic_accepted', 'dispatch_floor', 'managed_context',
)


def make_case(rounds: int, variant: str = 'accepted') -> dict:
    messages = [
        {'role': 'system', 'content': 'Synthetic scoped policy.'},
        {'role': 'user', 'content': 'Finish the scoped benchmark task and verify it.'},
        {'role': 'assistant', 'content': 'Earlier evidence. ' * 2000},
        {'role': 'user', 'content': 'Continue the pending work.'},
    ]
    for index in range(rounds):
        call_id = f'call-{index}'
        messages.extend([
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': 'read_files', 'arguments': json.dumps({'path': f'src/item-{index}.py'}),
                },
            }]},
            {'role': 'tool', 'tool_call_id': call_id,
             'content': f'Fixture {index}: ' + 'synthetic evidence ' * 100},
        ])
    task = {'id': 'compaction-measure', 'convId': 'compaction-fixture',
            '_userId': 41, 'config': {}, 'round_num': rounds}
    options = {'preserve_budget_tokens': 1000}
    if variant == 'managed_context':
        from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
        render_context(messages, [
            context_block('environment', 'Synthetic retained workspace context.'),
        ], ComposeRequest())
    if variant == 'no_anchor':
        messages = [{'role': 'system', 'content': 'No user anchor.'}]
    elif variant == 'open_batch':
        messages.pop()
    elif variant == 'nothing_foldable':
        messages = [{'role': 'user', 'content': 'One short request.'}]
    elif variant.startswith('utility_'):
        task.pop('convId')
        options['_allow_deterministic_summary_fallback'] = True
    if variant in {'adaptive_missing', 'utility_adaptive'}:
        task['config']['compaction'] = {'strategy': 'adaptive'}
        task['_contextEvidenceLedger'] = {'entries': [
            {'id': 'critical-fixture', 'type': 'unfinished', 'text': 'Verify fixture output.'},
        ]}
    if variant == 'cancel_entry':
        task['aborted'] = True
    if variant.startswith(('pre_', 'adoption_', 'economic_')):
        options.update(_proactive_economic=True, _message_tokens_before=100_000)
    return {'rounds': rounds, 'variant': variant, 'messages': messages,
            'task': task, 'options': options}


def run_case(module, case: dict, *, capture: bool = True, allocation: bool = False):
    """Run one independently owned input; setup/capture do not count as CPU work."""
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.commit_round import _turn_diff
    from lib.tasks_pkg.compaction import _archive, _summary_routing, _tokens
    from lib.goal_runs import run_pins

    messages = copy.deepcopy(case['messages'])
    task = copy.deepcopy(case['task'])
    options = dict(case['options'])
    meta, effects = {}, []
    counters = {'summary': 0, 'archive': 0, 'economics': 0}
    variant = case['variant']

    def record(name, *args, **kwargs):
        if capture:
            effects.append((name, copy.deepcopy(args), copy.deepcopy(kwargs)))

    def summarize(source, query, prefix, **kwargs):
        counters['summary'] += 1
        kwargs['usage_out'].update(prompt_tokens=700, completion_tokens=80)
        record('summary', source, query, **kwargs)
        if variant == 'cancel_summary':
            task['aborted'] = True
        if variant in {'summary_error', 'utility_fallback'}:
            raise RuntimeError('synthetic summary failure')
        return None if variant == 'summary_empty' else SUMMARY

    def archive(*args, **kwargs):
        counters['archive'] += 1
        record('archive', *args, **kwargs)
        if variant == 'cancel_archive':
            task['aborted'] = True
        return 'fixture-archive'

    def economics(*args, **kwargs):
        counters['economics'] += 1
        # Explicit decision fixtures, not a second pricing calculation.
        result = dict(dropped_tokens=80_000, payback_rounds=1,
                      cache_read_tokens=10_000, cache_rewrite_tokens=1000,
                      warm_prefix_tokens=10_000)
        if variant == 'pre_low_yield' or (
            variant == 'adoption_low_yield' and counters['economics'] == 2
        ):
            result['dropped_tokens'] = 1
        if variant == 'pre_cache_negative' or (
            variant == 'adoption_cache_negative' and counters['economics'] == 3
        ):
            result['payback_rounds'] = 100
        record('economics', *args, **kwargs)
        return result

    if variant == 'dispatch_floor':
        options.update(_dispatch_prompt_measure=lambda candidate: 100_000,
                       _dispatch_prompt_target=100)
    with ExitStack() as stack:
        bindings = {
            '_archive_transcript': archive,
            '_generate_query_aware_summary': summarize,
            '_get_context_limit': lambda task: 10_000_000,
            '_extract_recently_accessed_files': lambda messages: ['src/fixture.py'],
            '_compaction_trigger_threshold': lambda task: (10_000_000, 10_000_000, None),
            '_summary_cooldowns': {}, '_USER_VERBATIM_AUDIT_LATCH': {'done': True},
            '_proactive_cache_economics': economics,
            '_projected_summary_usage_tokens': lambda *args, **kwargs: 780,
            '_proactive_payback_policy': lambda *args, **kwargs: (4, {'kind': 'fixture'}),
            '_defer_proactive_retry': lambda *args, **kwargs: record('defer', *args, **kwargs),
            'time': SimpleNamespace(monotonic=lambda: 1000.0, time=lambda: 2000.0),
        }
        for name, value in bindings.items():
            stack.enter_context(patch.object(module, name, value))
        # Pricing is an explicit decision fixture like economics above, so the
        # economic variants reach the full path. create= admits baselines that
        # predate the unverified-pricing deferral probe.
        stack.enter_context(patch.object(module, '_main_route_pricing_verified',
                                         lambda *args, **kwargs: True, create=True))
        stack.enter_context(patch.object(manager, 'append_event', lambda task, event:
            record('phase', {k: event[k] for k in ('type', 'phase', 'detail', 'detailKey') if k in event})))
        stack.enter_context(patch.object(_turn_diff, 'build_turn_diff_block', lambda *a, **k: ''))
        stack.enter_context(patch.object(run_pins, 'update_objective_from_receipt',
                                        lambda *a, **k: record('objective', *a, **k)))
        stack.enter_context(patch.object(_archive, 'cancel_compaction_archive',
                                        lambda *a, **k: record('cancel_archive', *a, **k)))
        stack.enter_context(patch.object(_tokens, '_count_tokens_authoritative',
            lambda rows, task: (module._estimate_total_tokens(rows), 'fixture_estimate')))
        stack.enter_context(patch.object(_summary_routing, 'select_summary_route',
                                        lambda *a, **k: {'projectedCostUsd': .001}))
        if allocation:
            tracemalloc.start()
        wall_start, cpu_start = time.perf_counter_ns(), time.process_time_ns()
        try:
            value = module.execute_compact_tool(messages, task, _result_meta=meta, **options)
            error = None
        except Exception as exc:
            value, error = None, (type(exc).__name__, str(exc))
        cpu_ns, wall_ns = time.process_time_ns() - cpu_start, time.perf_counter_ns() - wall_start
        peak = tracemalloc.get_traced_memory()[1] if allocation else None
        if allocation:
            tracemalloc.stop()
    result = {'messages': messages, 'task': task, 'meta': meta, 'value': value,
              'error': error, 'effects': effects, 'calls': counters}
    return result, {'cpu_ns': cpu_ns, 'wall_ns': wall_ns, 'traced_peak_bytes': peak}


def assert_same_work(before: dict, after: dict, case: dict) -> None:
    if before.keys() != after.keys():
        raise ValueError('L2 parity changed the outcome field set')
    for key in before.keys() | after.keys():
        if before.get(key) != after.get(key):
            raise ValueError(f"L2 parity changed {key}: rounds={case['rounds']} variant={case['variant']}")
    accepted = case['variant'] in {
        'accepted', 'utility_fallback', 'utility_adaptive', 'economic_accepted', 'managed_context',
    }
    if bool(after['meta'].get('compacted')) != accepted:
        raise ValueError(f"Fixture did not reach its intended decision: {case['variant']}")
    if accepted:
        if after['calls']['summary'] != 1 or after['calls']['archive'] != 1:
            raise ValueError('Accepted fixture did not summarize/archive exactly once')
        if len(after['messages']) >= len(case['messages']):
            raise ValueError('Accepted fixture did not fold history')
    elif after['messages'] != case['messages']:
        raise ValueError('Declined fixture mutated source history')


def load_baseline(path: Path):
    spec = importlib.util.spec_from_file_location('_frozen_compaction_baseline', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_sources(baseline: Path) -> dict[str, str]:
    paths = subprocess.check_output([
        'git', 'ls-files', '--cached', '--others', '--exclude-standard', '--',
        'lib/tasks_pkg/compaction', 'lib/tasks_pkg/context_composer',
        'lib/token_counter', 'lib/tasks_pkg/message_semantics.py',
        'lib/tool_results/tool_history_pairing.py',
    ], cwd=ROOT, text=True).splitlines()
    files = {ROOT / name for name in paths if name.endswith('.py')}
    files.update((baseline.resolve(), Path(__file__).resolve()))
    return {str(path): digest(path) for path in sorted(files)}


def verify_source_witness(witness: dict[str, str]) -> None:
    if any(digest(Path(path)) != expected for path, expected in witness.items()):
        raise ValueError('Compaction sources changed during the comparison')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=21)
    parser.add_argument('--parity-only', action='store_true')
    args = parser.parse_args()
    if args.samples < 5:
        parser.error('--samples must be at least 5 alternating pairs')
    source_witness = freeze_sources(args.baseline)
    repository_revision = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    from lib.tasks_pkg.compaction._layer2 import _compact as current
    baseline = load_baseline(args.baseline)
    verify_source_witness(source_witness)
    logging.disable(logging.CRITICAL)
    cases = [make_case(count, variant) for count in (2, 16, 64) for variant in VARIANTS]
    work_digests = []
    for case in cases:
        before, _ = run_case(baseline, case)
        after, _ = run_case(current, case)
        assert_same_work(before, after, case)
        work_digests.append(hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest())
    measurements = []
    if not args.parity_only:
        for count in (8, 128, 1024):
            case = make_case(count)
            before, _ = run_case(baseline, case)
            after, _ = run_case(current, case)
            assert_same_work(before, after, case)
            samples = {'baseline': [], 'candidate': []}
            for index in range(args.samples):
                arms = [('baseline', baseline), ('candidate', current)]
                for name, module in arms if index % 2 == 0 else reversed(arms):
                    _, clock = run_case(module, case, capture=False)
                    samples[name].append(clock)
            peaks = {name: run_case(module, case, capture=False, allocation=True)[1]['traced_peak_bytes']
                     for name, module in [('baseline', baseline), ('candidate', current)]}
            measurements.append({'rounds': count, 'input_messages': len(case['messages']),
                'fixture_sha256': hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest(),
                'samples': samples, 'peak_bytes': peaks,
                'median_cpu_ms': {name: statistics.median(row['cpu_ns'] for row in rows) / 1e6
                                  for name, rows in samples.items()},
                'median_wall_ms': {name: statistics.median(row['wall_ns'] for row in rows) / 1e6
                                   for name, rows in samples.items()}})
    verify_source_witness(source_witness)
    report = {'scope': __doc__, 'baseline_sha256': digest(args.baseline),
              'repository_revision': repository_revision, 'python': sys.version,
              'source_witness': source_witness,
              'parity_cases': len(cases), 'outcome_digests': work_digests,
              'measurements': measurements}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'parity_cases': len(cases), 'measurements': [
        {k: row[k] for k in ('rounds', 'median_cpu_ms', 'median_wall_ms', 'peak_bytes')}
        for row in measurements]}, indent=2))


if __name__ == '__main__':
    main()
