#!/usr/bin/env python3
"""Assess retained storage receipts, never run a benchmark or touch a database.

Authority: contracts/storage_performance_charter_v1.json. Reuses the evidence
catalog's bounded, duplicate-key rejecting reader and immutable output owner,
and the conversation harness's provenance and capacity assessors. Measurements,
qualification and implementation completion are deliberately different facts.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.db_performance_catalog import (  # noqa: E402
    CatalogError, canonical, immutable_write, parse, read_bytes,
)

POLICY_PATH = ROOT / 'contracts/storage_performance_charter_v1.json'
POLICY = parse(read_bytes(POLICY_PATH))
CERTIFICATION = parse(read_bytes(ROOT / 'contracts/storage_harness_certification_v1.json'))
FORMAT = 'tofu.storage-charter-assessment/v1'
MAX_REPORTS = 64
STATUSES = ('FAIL', 'INCOMPARABLE', 'INSUFFICIENT', 'HISTORICAL', 'PASS')


def combine(statuses):
    """A proven current failure cannot be hidden by missing unrelated evidence."""
    values = set(statuses)
    return next((status for status in STATUSES if status in values), 'INSUFFICIENT')


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def cell(status, reasons=(), **fields):
    return {'status': status, 'reasons': list(reasons), **fields}


def volume(report):
    filesystem = report.get('resources', {}).get('tofudb', {}).get('filesystem')
    if not isinstance(filesystem, str) or not filesystem:
        return None
    if filesystem.startswith('fuse'):
        return 'fuse'
    # Do not call an unknown/remote filesystem a local-volume control.
    return 'local' if filesystem in {'xfs', 'ext4', 'ext3', 'btrfs', 'apfs', 'ntfs', 'tmpfs'} else None


def authority(report):
    measurement = report.get('measurement', {})
    explicit = measurement.get('authority', {}).get('tofudb')
    if isinstance(explicit, dict):
        return explicit
    native = report.get('env', {}).get('native', {})
    return {
        'backend': 'tofudb',
        'build_identity': native.get('build_identity'),
        'checkout_source_sha256': native.get('current_checkout_source_sha256'),
        'source_matches_checkout': native.get('source_matches_checkout'),
        'client_mode': measurement.get('native_client'),
    }


def qualify(report, current_sources):
    from benchmarks.conversation_provenance import qualify_provenance

    measurement = report.get('measurement', {})
    problems = []
    if measurement.get('profiling_requested') is not False or report.get('env', {}).get('native', {}).get('profile') is not False:
        problems.append('profile-off execution is not established')
    if measurement.get('native_client') not in ('embedded', 'application'):
        problems.append('embedded/application measurement boundary is not explicit')
    if not isinstance(measurement.get('host_identity'), dict) or not measurement['host_identity']:
        problems.append('same-host identity is missing')
    if not isinstance(measurement.get('collection_id'), str) or not measurement['collection_id']:
        problems.append('independent collection identity is missing')
    if report.get('env', {}).get('sqlite_synchronous') not in (2, 'FULL'):
        problems.append('SQLite FULL durability is not established')
    if measurement.get('equivalent_durability') is not True:
        problems.append('equivalent strict native/SQLite durability is not established')

    runtime = measurement.get('runtime_policy')
    runtime = runtime if isinstance(runtime, dict) else {}
    for phase in ('before', 'after'):
        observed = {}
        for engine in POLICY['required_engines']:
            boundaries = runtime.get(engine)
            value = boundaries.get(phase) if isinstance(boundaries, dict) else None
            observed[engine] = value if isinstance(value, dict) else {}
        native, sqlite, raw = (observed[name] for name in ('tofudb', 'sqlite', 'rawsqlite'))
        if native.get('profiling') is not False or native.get('profile_sample_every') != 0:
            problems.append(f'{phase}: observed native profile-off policy is missing or contradicted')
        if (native.get('wal_durability') != 'strict' or sqlite.get('synchronous') != 2
                or sqlite.get('fastpath_active') is not False or raw.get('synchronous') != 2
                or str(raw.get('journal_mode')).lower() != 'wal'):
            problems.append(f'{phase}: observed equivalent durability/volume policy is missing or contradicted')
    volumes = [report.get('resources', {}).get(engine, {}) for engine in POLICY['required_engines']]
    devices = [row.get('volume_device') for row in volumes]
    if any(type(device) is not int or device < 0 for device in devices):
        problems.append('common authority device is not established')
    elif len(set(devices)) != 1 or len({row.get('filesystem') for row in volumes}) != 1:
        return cell('INCOMPARABLE', ['engine authority volumes differ'])
    order = measurement.get('engine_order', [])
    if len(order) != len(POLICY['required_engines']) or set(order) != set(POLICY['required_engines']):
        problems.append('required three-engine comparison is incomplete')
    if not measurement.get('workload_order'):
        problems.append('workload order is missing')
    if problems:
        return cell('INSUFFICIENT', problems)
    provenance = measurement.get('provenance', {})
    qualifications = []
    for engine in POLICY['required_engines']:
        receipt = provenance.get(engine)
        if not isinstance(receipt, dict):
            return cell('INSUFFICIENT', [f'{engine}: start/end source and artifact witness missing'])
        result = qualify_provenance(
            receipt, backend=engine,
            authority=authority(report) if engine == 'tofudb' else {'backend': engine},
            binary_sha256=measurement.get('native_binary_sha256') if engine == 'tofudb' else None,
            current_sources=current_sources,
        )
        qualifications.append(result)
    if not all(item['frozen_build_comparable'] for item in qualifications):
        return cell('INCOMPARABLE', [reason for item in qualifications for reason in item['frozen_failures']])
    source_sets = [provenance[name]['sources_before'] for name in POLICY['required_engines']]
    if any(snapshot != source_sets[0] for snapshot in source_sets):
        return cell('INCOMPARABLE', ['source inventories differ across engine legs'])
    if measurement.get('benchmark_source_sha256') != source_sets[0]['files']['scripts/tofudb_perf_compare.py']:
        return cell('INCOMPARABLE', ['benchmark identity contradicts the measured source inventory'])
    if not all(item['current_source_qualified'] for item in qualifications):
        return cell('HISTORICAL', [reason for item in qualifications for reason in item['failures']])
    if report.get('summary', {}).get('failed_engines') or report.get('skipped_engines') or report.get('skipped'):
        return cell('FAIL', ['a required engine failed or was skipped'])
    return cell('PASS')


def cohort_quality(reports, qualifications):
    if not reports:
        return cell('INSUFFICIENT', ['no operation reports for this volume/condition'])
    problems = []
    if len(reports) < POLICY['minimum_repeats']:
        problems.append(f"need at least {POLICY['minimum_repeats']} independent repeats")
    ids = [row.get('measurement', {}).get('collection_id') for row in reports]
    if all(isinstance(item, str) for item in ids) and len(ids) != len(set(ids)):
        return cell('INCOMPARABLE', ['duplicate collection identity; copied reports are not repeats'])
    identities = []
    orders = set()
    for report in reports:
        meta = report.get('measurement', {})
        # Unknown identity is a gap, not proof that two known identities conflict.
        if qualifications[id(report)]['status'] == 'INSUFFICIENT':
            continue
        identities.append(canonical({
            'host': meta.get('host_identity'), 'scale': report.get('scale'),
            'work_spec': meta.get('work_spec'), 'large_turn_profile': report.get('large_turn_profile'),
            'client': meta.get('native_client'), 'workloads': meta.get('workload_order'),
            'stress': meta.get('distracted_hardware'),
            'sqlite': report.get('env', {}).get('sqlite_runtime'),
            'artifacts': {key: value.get('artifact_before') for key, value in meta.get('provenance', {}).items()},
            'sources': {key: value.get('sources_before') for key, value in meta.get('provenance', {}).items()},
        }))
        orders.add(tuple(meta.get('engine_order', [])))
    if len(set(identities)) > 1:
        return cell('INCOMPARABLE', ['host, boundary, work, stress, SQLite runtime or artifact identity differs across repeats'])
    if not any(order and tuple(reversed(order)) in orders for order in orders):
        problems.append('reverse engine-order repeat is missing')
    states = [qualifications[id(report)]['status'] for report in reports]
    reasons = [reason for report in reports for reason in qualifications[id(report)]['reasons']]
    if problems:
        states.append('INSUFFICIENT')
    return cell(combine(states), [*reasons, *problems])


def metric_value(row, name):
    for key in name.split('.'):
        row = row.get(key) if isinstance(row, dict) else None
    return row


def metric_cell(reports, quality, workload, metric, baseline, maximum_ratio):
    observations = []
    missing = []
    insufficient = []
    for report in reports:
        rows = report.get('workloads', {}).get(workload, {})
        native, control = rows.get('tofudb', {}), rows.get(baseline, {})
        if native.get('scope') != control.get('scope'):
            return cell('INCOMPARABLE', [f'{workload}: measurement scopes differ'])
        if native.get('error') or control.get('error'):
            return cell('FAIL' if quality['status'] == 'PASS' else quality['status'],
                        [*quality['reasons'], f'{workload}: execution error'], observed_status='FAIL')
        left, right = metric_value(native, metric['name']), metric_value(control, metric['name'])
        if not number(left) or not number(right) or (not metric.get('allow_zero') and (left == 0 or right == 0)):
            missing.append(f"{workload}.{metric['name']}: finite native/{baseline} measurements required")
            continue
        count = metric.get('samples')
        if count:
            counts = [native.get(count), control.get(count)]
            minimum = CERTIFICATION['minimum_tail_samples']
            if any(type(value) is not int or value < minimum for value in counts):
                insufficient.append(f'{workload}.{metric["name"]}: at least {minimum} real samples per engine required')
            if all(type(value) is int for value in counts) and counts[0] != counts[1]:
                insufficient.append(f'{workload}.{metric["name"]}: unequal completed populations')
        ratio = (right / left if metric.get('higher_is_better') else left / right) if left and right else (1.0 if left == right else 0.0 if left == 0 else None)
        passed = ((right <= left * maximum_ratio) if metric.get('higher_is_better') else
                  (left < right * maximum_ratio) if metric.get('strict') else (left <= right * maximum_ratio))
        observations.append({'collection_id': report.get('measurement', {}).get('collection_id'),
                             'native': left, 'baseline': right, 'ratio': ratio, 'passed': passed})
    if missing or not observations:
        return cell('INSUFFICIENT', missing or ['no observations'], observations=observations)
    passes = [item['passed'] for item in observations]
    observed = 'PASS' if all(passes) else 'FAIL' if not any(passes) else 'INSUFFICIENT'
    reasons = list(quality['reasons']) + insufficient
    if observed == 'INSUFFICIENT':
        reasons.append('repeat observations disagree; no favorable subset may be selected')
    elif observed == 'FAIL':
        reasons.append(f'observed ratio exceeds the {maximum_ratio:g} gate' + (' (strict)' if metric.get('strict') else ''))
    status = quality['status'] if quality['status'] != 'PASS' else 'INSUFFICIENT' if insufficient else observed
    return cell(status, reasons, observed_status=observed, observations=observations)


def footprint_cell(reports, quality):
    converted = []
    for report in reports:
        rows = {}
        scopes = []
        for engine in ('tofudb', 'sqlite'):
            footprint = report.get('resources', {}).get(engine, {}).get('footprint', {})
            total = footprint.get('total', {})
            if total.get('complete') is not True or not number(total.get('logical_bytes')):
                return cell('INSUFFICIENT', [f'{engine}: complete authority-plus-projection accounting is missing'])
            timing = footprint.get('timing', {})
            if not timing.get('phase'):
                return cell('INSUFFICIENT', ['footprint lifecycle phase is unknown'])
            scopes.append((timing['phase'], timing.get('steady_state')))
            rows[engine] = {'bytes': total['logical_bytes']}
        if len(set(scopes)) != 1:
            return cell('INCOMPARABLE', ['footprint lifecycle/steady-state scopes differ'])
        converted.append({'measurement': report.get('measurement', {}), 'workloads': {'footprint': rows}})
    return metric_cell(converted, quality, 'footprint', {'name': 'bytes'}, 'sqlite', 1.0)


def capacity_cell(reports, current_sources):
    from benchmarks.conversation_compare import compare

    from benchmarks.conversation_acceptance import assess as assess_leg
    if not reports:
        return cell('INSUFFICIENT', ['paired conversation-capacity receipts missing'])
    results = []
    found = set()
    for report in reports:
        filesystem = report.get('filesystem')
        if not isinstance(filesystem, str):
            return cell('INCOMPARABLE', ['conversation filesystem missing'])
        kind = 'fuse' if filesystem.startswith('fuse') else 'local' if filesystem in {'xfs', 'ext4', 'btrfs', 'apfs', 'ntfs', 'tmpfs'} else None
        if kind is None:
            return cell('INCOMPARABLE', ['conversation volume class unknown'])
        found.add(kind)
        legs = report.get('legs', [])
        if not isinstance(legs, list) or not legs:
            return cell('INSUFFICIENT', ['raw conversation legs are required, not a supplied acceptance boolean'])
        if len(legs) > 128 or any(not isinstance(leg, dict) or not isinstance(leg.get('report'), dict)
                                 or leg.get('backend') not in ('tofudb', 'sqlite') for leg in legs):
            return cell('INCOMPARABLE', ['invalid/bounded conversation leg shape'])
        repeats = {leg.get('repeat') for leg in legs}
        if any(type(item) is not int or not 0 <= item < 64 for item in repeats):
            return cell('INCOMPARABLE', ['invalid conversation repeat index'])
        assessed = compare(legs, max(repeats) + 1, filesystem, current_sources=current_sources)
        checks = [assess_leg(leg['report'], current_sources=current_sources) for leg in legs]
        states = []
        for leg, check in zip(legs, checks):
            if not leg['report'].get('provenance'):
                states.append('INSUFFICIENT')
            elif not check['frozen_build_comparable']:
                states.append('INCOMPARABLE')
            elif not check['current_source_qualified']:
                states.append('HISTORICAL')
            elif not check['coverage_passed'] or not check['latency_comparison_eligible']:
                states.append('INSUFFICIENT')
            elif not check['correctness_passed']:
                states.append('FAIL')
            elif any(metric['samples'] != metric['expected_samples'] or not number(metric['p99_ms'])
                     for metric in check['metrics'].values()):
                states.append('INSUFFICIENT')
            else:
                states.append('PASS' if check['latency_passed'] else 'FAIL')
        if len(repeats) < POLICY['minimum_repeats'] or len(legs) != len(repeats) * 2:
            states.append('INSUFFICIENT')
        status = combine(states)
        if status == 'PASS' and not assessed['passed']:
            status = 'FAIL' if assessed['comparisons'] and all(row['comparable'] for row in assessed['comparisons']) else 'INCOMPARABLE'
        results.append({'volume': kind, 'status': status, 'acceptance': assessed})
    if found != set(POLICY['required_volumes']):
        return cell('INSUFFICIENT', ['conversation capacity needs both local and FUSE volumes'], receipts=results)
    return cell(combine(item['status'] for item in results),
                [reason for item in results for reason in item['acceptance']['failures']], receipts=results)


def validate_operation(report):
    for field in ('measurement', 'env', 'resources', 'workloads'):
        if not isinstance(report.get(field), dict):
            raise CatalogError('operation receipt missing object: ' + field)
    meta = report['measurement']
    for field in ('engine_order', 'workload_order'):
        value = meta.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise CatalogError('operation receipt needs string array: ' + field)
    for field in ('provenance', 'authority'):
        values = meta.get(field, {})
        if not isinstance(values, dict) or any(not isinstance(value, dict) for value in values.values()):
            raise CatalogError('operation receipt needs object values: ' + field)
    if not isinstance(report['env'].get('native', {}), dict) or not isinstance(report.get('summary', {}), dict):
        raise CatalogError('invalid native environment or summary')
    for values in report['resources'].values():
        if not isinstance(values, dict) or not isinstance(values.get('footprint', {}), dict):
            raise CatalogError('invalid resource/footprint entry')
        footprint = values.get('footprint', {})
        if any(not isinstance(footprint.get(field, {}), dict) for field in ('total', 'timing')):
            raise CatalogError('invalid footprint total/timing')
    for values in report['workloads'].values():
        if not isinstance(values, dict) or any(not isinstance(value, dict) for value in values.values()):
            raise CatalogError('invalid workload/engine entry')


def assess(reports, *, current_sources):
    """Pure assessment; current_sources is captured once by the caller, not trusted input."""
    if not isinstance(reports, list) or len(reports) > MAX_REPORTS:
        raise CatalogError(f'at most {MAX_REPORTS} report objects are accepted')
    operations, conversations, seen = [], [], set()
    for report in reports:
        if not isinstance(report, dict):
            raise CatalogError('receipt must be an object')
        try:
            identity = hashlib.sha256(canonical(report)).hexdigest()
        except (TypeError, ValueError) as exc:
            raise CatalogError('receipt is not finite JSON') from exc
        if identity in seen:
            raise CatalogError('duplicate receipt; copies do not constitute independent repeats')
        seen.add(identity)
        if report.get('schema') == 'tofu.perf_compare.v2':
            validate_operation(report)
            operations.append(report)
        elif report.get('format') == 'tofu.conversation-comparison/v1':
            conversations.append(report)
        else:
            raise CatalogError('unsupported receipt; supply raw perf_compare.v2 or conversation-comparison/v1, not a curated summary')
    qualifications = {id(report): qualify(report, current_sources) for report in operations}
    grouped = defaultdict(list)
    unclassified = []
    for report in operations:
        kind = volume(report)
        if kind is None:
            unclassified.append('unknown native filesystem; report not used to establish local/FUSE coverage')
        else:
            grouped[(kind, bool(report['measurement'].get('distracted_hardware')))].append(report)
    all_workloads = sorted({workload for definition in POLICY['dimensions'] for workload in definition.get('workloads', [])})
    dimensions = []
    for definition in POLICY['dimensions']:
        if definition.get('kind') == 'conversation':
            result = capacity_cell(conversations, current_sources)
        elif definition.get('kind') == 'manual':
            result = cell('INSUFFICIENT', definition['unautomated_criteria'])
        else:
            cells = []
            for kind in definition.get('volumes', POLICY['required_volumes']):
                cohort = grouped.get((kind, definition.get('stress_required', False)), [])
                quality = cohort_quality(cohort, qualifications)
                if definition.get('stress_required'):
                    if any(not isinstance(report['measurement'].get('distracted_hardware'), dict)
                           or report['measurement']['distracted_hardware'].get('observed_overlap') is not True
                           for report in cohort):
                        quality = cell('INSUFFICIENT', ['overlapping background load was not observed'])
                if definition.get('kind') == 'footprint':
                    measured = footprint_cell(cohort, quality) if cohort else quality
                    cells.append({'volume': kind, **measured})
                    continue
                for workload in all_workloads if definition.get('all_workloads') else definition['workloads']:
                    for metric in definition['metrics']:
                        measured = metric_cell(cohort, quality, workload, metric, definition['baseline'], definition['maximum_ratio']) if cohort else quality
                        cells.append({'volume': kind, 'workload': workload, 'metric': metric['name'], **measured})
            numeric_status = combine(row['status'] for row in cells)
            gaps = definition.get('unautomated_criteria', [])
            result = cell(combine([numeric_status, 'INSUFFICIENT']) if gaps else numeric_status,
                          gaps, numeric_status=numeric_status, cells=cells)
        dimensions.append({'id': definition['id'], 'owner': definition['owner'], **result})
    status = combine([row['status'] for row in dimensions] + (['INSUFFICIENT'] if unclassified else []))
    return {
        'format': FORMAT, 'policy_sha256': hashlib.sha256(read_bytes(POLICY_PATH)).hexdigest(),
        'target_native_source_sha256': current_sources.get('native_source_sha256'),
        'target_source_inventory_sha256': hashlib.sha256(canonical(current_sources)).hexdigest(),
        'input_sha256': sorted(seen), 'status': status,
        'comprehensive_victory': status == 'PASS', 'dimensions': dimensions,
        'unclassified': unclassified,
        'limits': [POLICY['scope'], 'Repeats are retained individually, not pooled into a speedup score.',
                   'PASS means all declared measurement gates passed, not statistical confidence or a steady-state/power-loss certification.',
                   'A structural explanation is not a numerical waiver. Missing measurements remain visible.'],
    }


def markdown(result):
    lines = ['# Storage charter assessment', '', f"Status: **{result['status']}**; comprehensive victory: **{str(result['comprehensive_victory']).lower()}**.",
             '', '| Dimension | Status | Next evidence/action |', '|---|---|---|']
    for row in result['dimensions']:
        reasons = list(row.get('reasons', []))
        for observation in row.get('cells', []):
            if observation['status'] != 'PASS':
                reasons.extend(observation.get('reasons', []))
        unique = list(dict.fromkeys(reasons))[:3]
        detail = '; '.join(unique) or ('See per-cell retained observations.' if row['status'] != 'PASS' else 'Qualified for the declared scope.')
        detail = detail.replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| {row['id']} | {row['status']} | {detail} |")
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='*', type=Path)
    parser.add_argument('--json-output', type=Path, help='new immutable JSON output path')
    parser.add_argument('--markdown-output', type=Path, help='new immutable Markdown output path')
    args = parser.parse_args(argv)
    try:
        if len(args.reports) > MAX_REPORTS:
            raise CatalogError(f'at most {MAX_REPORTS} input files')
        from benchmarks.conversation_provenance import capture_sources
        sources = capture_sources()
        result = assess([parse(read_bytes(path)) for path in args.reports], current_sources=sources)
        if capture_sources() != sources:
            raise CatalogError('checkout inputs changed during assessment; rerun on stable inputs')
        if args.json_output:
            immutable_write(args.json_output, canonical(result))
        if args.markdown_output:
            immutable_write(args.markdown_output, markdown(result).encode())
        print(markdown(result), end='')
        return 0 if result['comprehensive_victory'] else 1
    except (CatalogError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f'charter assessment refused: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
