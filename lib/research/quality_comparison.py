"""Read-only paired research-quality comparison; never launches paid work.

Inputs are externally adjudicated, paired run receipts. This module validates
comparability and reports raw paired differences, not acceptance probabilities
or statistical significance. Production model-review scores are not outcomes.
"""
from __future__ import annotations

import math
from collections import Counter
from statistics import mean

METRICS = ('supported_claim_precision', 'counterevidence_recall',
           'useful_hypothesis_rate', 'reproduction_success_rate')
MAX_PAIRS = 100
MATCHED_IDENTITIES=('task_digest','corpus_digest','model','budget_digest')
EFFICIENCY_FORMAT = 'tofu.research-efficiency/v1'
# Units and measurement admission are shared by every comparison consumer.
EFFICIENCY_METRICS = ('wall_seconds', 'human_active_seconds', 'human_interventions')


def _number(value):
    if not isinstance(value,(int,float)) or isinstance(value,bool):return False
    try:return math.isfinite(value)
    except OverflowError:return False


def system_digest_valid(value):
    return isinstance(value,str) and len(value)==64 and all(char in '0123456789abcdef' for char in value)


def _paired_summary(values):
    average = mean(values)
    error = None
    if len(values) > 1:
        scale = math.sqrt(len(values) * (len(values) - 1))
        deviations = [value - average for value in values]
        error = math.hypot(*(delta / scale if _number(delta)
                            else value / scale - average / scale
                            for value, delta in zip(values, deviations)))
    return {'mean': average, 'standard_error': error,
            'minimum': min(values), 'maximum': max(values)}


def _efficiency_comparison(pairs, quality_pairs):
    """Compare declared end-to-end effort only for independently audited pairs.

    Missing measurements stay unknown. A protocol digest freezes measurement
    boundaries, including waits and the definition of human intervention; its
    authenticity still requires external audit of the referenced records.
    """
    requested = any(isinstance(pair, dict) and any(
        isinstance(pair.get(arm), dict) and 'efficiency' in pair[arm]
        for arm in ('control', 'treatment')) for pair in pairs)
    quality = {row['input_index']: row for row in quality_pairs}
    valid, excluded = [], []
    for index, pair in enumerate(pairs if requested else ()):
        if index not in quality:
            excluded.append({'index': index, 'reasons': ['quality_pair_excluded']})
            continue
        reasons = []
        if 'system_digests' not in quality[index]:
            reasons.append('unfrozen_system_generations')
        measurements = {}
        for arm in ('control', 'treatment'):
            row = pair[arm].get('efficiency')
            if not isinstance(row, dict):
                reasons.append(arm + '_missing_efficiency')
                continue
            measurements[arm] = row
            if row.get('format') != EFFICIENCY_FORMAT:
                reasons.append(arm + '_invalid_efficiency_format')
            if not system_digest_valid(row.get('measurement_protocol_digest')):
                reasons.append(arm + '_invalid_measurement_protocol')
            refs = row.get('evidence_refs')
            if (not isinstance(refs, list) or not refs
                    or not all(isinstance(ref, str) and ref.strip() for ref in refs)):
                reasons.append(arm + '_missing_efficiency_evidence')
            for key in EFFICIENCY_METRICS:
                value = row.get(key)
                if (not _number(value) or value < 0
                        or (key == 'wall_seconds' and value == 0)
                        or (key == 'human_interventions' and type(value) is not int)):
                    reasons.append(arm + '_invalid_' + key)
        if len(measurements) == 2 and (
                measurements['control'].get('measurement_protocol_digest')
                != measurements['treatment'].get('measurement_protocol_digest')):
            reasons.append('unmatched_measurement_protocol')
        if reasons:
            excluded.append({'index': index, 'pair_id': pair['pair_id'], 'reasons': reasons})
            continue
        valid.append({'index': index, 'pair_id': pair['pair_id'],
            'measurement_protocol_digest': measurements['control']['measurement_protocol_digest'],
            **{arm: {key: measurements[arm][key] for key in EFFICIENCY_METRICS}
               for arm in ('control', 'treatment')},
            'evidence_refs': {arm: list(measurements[arm]['evidence_refs'])
                              for arm in ('control', 'treatment')},
            'differences': {key: measurements['treatment'][key] - measurements['control'][key]
                            for key in EFFICIENCY_METRICS}})
    if len({row['measurement_protocol_digest'] for row in valid}) > 1:
        excluded.extend({'index': row['index'], 'pair_id': row['pair_id'],
                         'reasons': ['mixed_measurement_protocols']} for row in valid)
        valid = []
    return {
        'status': ('unavailable' if not requested else
                   'descriptive_only' if valid and not excluded else 'incomplete'),
        'submitted_pair_count': len(pairs), 'measured_pair_count': len(valid),
        'valid_pairs': valid, 'excluded_pairs': excluded,
        'measurement_protocol_digest': valid[0]['measurement_protocol_digest'] if valid else None,
        'paired_metric_summary': {key: _paired_summary([row['differences'][key] for row in valid])
                                  for key in EFFICIENCY_METRICS} if valid else {},
        'quality_regression_pair_ids': [row['pair_id'] for row in valid
            if any(value < 0 for value in quality[row['index']]['differences'].values())],
        'limitations': [
            'Measurements and protocol digests are declared; evidence references require external audit.',
            'Missing human effort is unknown, never zero; lower effort alone does not establish better research.',
            'Quality regressions are observed paired decreases, not significance tests.',
            'No aggregate throughput, conference acceptance or autonomous completion claim is inferred.'],
    }


def compare_quality(pairs: list[dict]) -> dict:
    """Validate complete matched pairs; invalid pairs stay visible, never imputed.

    Each arm declares identical frozen corpus, task, model and budget identity,
    actual spend and independently adjudicated metrics with evidence references.
    This validates receipts only; the caller must audit referenced evidence.
    """
    if not isinstance(pairs, list) or len(pairs) > MAX_PAIRS:
        raise ValueError('quality comparison requires at most 100 pairs')
    valid, excluded = [], []
    identity_counts = Counter(pair['pair_id'] for pair in pairs
        if isinstance(pair, dict) and isinstance(pair.get('pair_id'), str) and pair['pair_id'])
    versioned=any(isinstance(pair,dict) and any(isinstance(pair.get(arm),dict)
        and 'system_digest' in pair[arm] for arm in ('control','treatment')) for pair in pairs)
    for index, pair in enumerate(pairs):
        reasons = []
        if not isinstance(pair, dict):
            excluded.append({'index': index, 'reasons': ['malformed_pair']})
            continue
        identity = pair.get('pair_id')
        if not isinstance(identity, str) or not identity or identity_counts[identity] > 1:
            reasons.append('missing_or_duplicate_pair_id')
        control, treatment = pair.get('control'), pair.get('treatment')
        if not isinstance(control, dict) or not isinstance(treatment, dict):
            excluded.append({'index': index, 'reasons': reasons + ['missing_arm']})
            continue
        for key in MATCHED_IDENTITIES:
            if not isinstance(control.get(key), str) or not control[key] or control[key] != treatment.get(key):
                reasons.append('unmatched_' + key)
        for name, arm in (('control', control), ('treatment', treatment)):
            if versioned:
                digest=arm.get('system_digest')
                if not system_digest_valid(digest):
                    reasons.append(name+'_missing_or_invalid_system_digest')
            if arm.get('adjudication_kind') != 'independent_evidence_audit':
                reasons.append(name + '_not_independently_audited')
            refs = arm.get('evidence_refs')
            if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref for ref in refs):
                reasons.append(name + '_missing_evidence')
            spend, ceiling = arm.get('actual_cost'), arm.get('cost_ceiling')
            if not _number(spend) or not _number(ceiling) or not 0 <= spend <= ceiling:
                reasons.append(name + '_invalid_budget')
            metrics = arm.get('metrics')
            if not isinstance(metrics, dict) or any(not _number(metrics.get(key)) or not 0 <= metrics[key] <= 1 for key in METRICS):
                reasons.append(name + '_missing_or_invalid_metrics')
        if control.get('cost_ceiling') != treatment.get('cost_ceiling'):
            reasons.append('unmatched_cost_ceiling')
        if reasons:
            excluded.append({'index': index, 'pair_id': identity, 'reasons': reasons})
        else:
            valid.append({'pair_id': identity,'input_index':index,
                **({'system_digests':{'control':control['system_digest'],'treatment':treatment['system_digest']}} if versioned else {}), 'differences': {
                key: treatment['metrics'][key] - control['metrics'][key] for key in METRICS},
                'cost_difference': treatment['actual_cost'] - control['actual_cost']})
    cohorts={tuple(row['system_digests'][arm] for arm in ('control','treatment')) for row in valid} if versioned else set()
    if len(cohorts)>1:
        excluded.extend({'index':row['input_index'],'pair_id':row['pair_id'],
            'reasons':['mixed_system_generations'],'system_digests':row['system_digests']} for row in valid)
        valid=[]
    generations=valid[0]['system_digests'] if versioned and valid else None
    summaries={}
    for key in METRICS if valid else ():
        values=[row['differences'][key] for row in valid]
        summaries[key] = _paired_summary(values)
    return {'status': 'descriptive_only' if valid else 'insufficient_evidence',
            'valid_pairs': valid, 'excluded_pairs': excluded,
            'submitted_pair_count':len(pairs),'valid_pair_count':len(valid),
            'excluded_pair_count':len(excluded),'paired_metric_summary':summaries,
            'system_generations':generations,
            'efficiency': _efficiency_comparison(pairs, valid),
            'mean_paired_difference': {key:row['mean'] for key,row in summaries.items()},
            'limitations': ['Evidence references require external audit.',
                            'System digests are declared identities; missing legacy digests establish no frozen version.',
                            'No significance or conference acceptance claim is inferred.',
                            'Identical budget ceilings do not imply identical actual spend.',
                            'Standard errors describe supplied valid pairs; independence and representativeness are not established.']}
