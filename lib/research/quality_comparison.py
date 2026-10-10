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


def _number(value):
    if not isinstance(value,(int,float)) or isinstance(value,bool):return False
    try:return math.isfinite(value)
    except OverflowError:return False


def system_digest_valid(value):
    return isinstance(value,str) and len(value)==64 and all(char in '0123456789abcdef' for char in value)


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
        average=mean(values)
        standard_error=(math.hypot(*((value-average)/math.sqrt(len(values)*(len(values)-1))
                                    for value in values)) if len(values)>1 else None)
        summaries[key]={'mean':average,'standard_error':standard_error,
                        'minimum':min(values),'maximum':max(values)}
    return {'status': 'descriptive_only' if valid else 'insufficient_evidence',
            'valid_pairs': valid, 'excluded_pairs': excluded,
            'submitted_pair_count':len(pairs),'valid_pair_count':len(valid),
            'excluded_pair_count':len(excluded),'paired_metric_summary':summaries,
            'system_generations':generations,
            'mean_paired_difference': {key:row['mean'] for key,row in summaries.items()},
            'limitations': ['Evidence references require external audit.',
                            'System digests are declared identities; missing legacy digests establish no frozen version.',
                            'No significance or conference acceptance claim is inferred.',
                            'Identical budget ceilings do not imply identical actual spend.',
                            'Standard errors describe supplied valid pairs; independence and representativeness are not established.']}
