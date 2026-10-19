"""Bounded, content-free usage accounting and paired cost/latency statistics.

The comparison owner supplies checked JSON reads and its evidence index.
Provider usage is authoritative; missing receipts remain unknown. Reference
prices are frozen analysis assumptions, never provider invoices.
"""
from __future__ import annotations
from pathlib import Path as Path

from collections import Counter, defaultdict
import json
import math
import random
import re
from statistics import median

from lib.cost import normalize_usage, split_input_tokens
from evaluations.model_request import request_parameter_receipt


TOKEN_FIELDS = ('inputTokens', 'uncachedInputTokens', 'cacheReadTokens',
                'cacheWriteTokens', 'outputTokens', 'reasoningTokens')
MAX_METRIC_LINE_BYTES = 1024 * 1024
_TOKEN = re.compile(r'[0-9a-f]{64}')


def validate_reference_prices(value):
    if value is None:
        return None
    fields = {'inputUsdPerMillion', 'outputUsdPerMillion',
              'cacheReadUsdPerMillion', 'cacheWriteUsdPerMillion'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('reference prices require exactly four USD-per-million rates')
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
           for v in value.values()):
        raise ValueError('reference prices must be finite nonnegative numbers')
    return {k: float(value[k]) for k in sorted(fields)}


def _usage(value):
    if not isinstance(value, dict) or not value:
        return None
    if not any(k in value for k in ('prompt_tokens', 'input_tokens',
                                    'completion_tokens', 'output_tokens')):
        return None
    normalized = normalize_usage(value)
    cold, total = split_input_tokens(value)
    if min(*normalized.values(), cold, total) < 0:
        raise ValueError('negative provider usage')
    if normalized['cache_read'] + normalized['cache_write'] > total:
        raise ValueError('cache usage exceeds total input')
    return dict(zip(TOKEN_FIELDS, (
        total, cold, normalized['cache_read'], normalized['cache_write'],
        normalized['output'], normalized['thinking'])))


def _price(totals, rates):
    if rates is None:
        return None
    return sum(totals[k] * rates[p] for k, p in (
        ('uncachedInputTokens', 'inputUsdPerMillion'),
        ('outputTokens', 'outputUsdPerMillion'),
        ('cacheReadTokens', 'cacheReadUsdPerMillion'),
        ('cacheWriteTokens', 'cacheWriteUsdPerMillion'))) / 1_000_000


def observe_trial_usage(root, trial, *, read_json, add_evidence, rates=None):
    """Use runtime apiRounds once, or host-tagged proxy rows once, never both."""
    out = {k: 0 for k in TOKEN_FIELDS}
    out.update(observedCalls=0, usageReceiptedCalls=0, unknownUsageCalls=0, issues=[],
               compactionCalls=0, compactionOutputTokens=0,
               requestParameters=[], sources=[], pricingSources={})
    parameters = {}
    prices = Counter()
    rows = []
    runtime = trial / 'agent/tofu-kimi-evidence/runtime-evidence.json'
    proxy = trial / 'agent/opencode-evidence/proxy-metrics.jsonl'
    config = trial / 'agent/opencode-evidence/opencode.json'
    if runtime.exists():
        add_evidence(runtime)
        out['sources'].append(str(runtime.relative_to(root)))
        data = read_json(runtime)
        rounds = data.get('apiRounds')
        if not isinstance(rounds, list):
            out['issues'].append('missing_api_rounds')
            rounds = []
        seen = set()
        for row in rounds:
            identity = str(row.get('round'))
            if identity in seen:
                raise ValueError('duplicate runtime API round identity')
            seen.add(identity)
            rows.append(row.get('usage'))
            prices[str((row.get('cost') or {}).get('pricingSource') or 'unknown')] += 1
            receipt = (row.get('usage') or {}).get('_request_parameters')
            if isinstance(receipt, dict):
                receipt = request_parameter_receipt({**receipt, 'thinking': {'type': receipt.get('thinking_type')}})
                parameters[json.dumps(receipt, sort_keys=True)] = receipt
            if identity.startswith('compaction-'):
                out['compactionCalls'] += 1
                parsed = _usage(row.get('usage'))
                out['compactionOutputTokens'] += (parsed or {}).get('outputTokens', 0)
        reported = _usage(data.get('usage'))
        if data.get('finishReason') in {'aborted', 'error', 'network_error'}:
            out['issues'].append('terminal_may_have_unreceipted_usage')
    else:
        expected_token = ''
        if config.exists():
            add_evidence(config)
            cfg = read_json(config)
            from evaluations.codex_kimi_proxy.codex_contract import TRIAL_HEADER
            options = ((cfg.get('provider') or {}).get('benchmark') or {}).get('options') or {}
            expected_token = (options.get('headers') or {}).get(TRIAL_HEADER, '')
            if not isinstance(expected_token, str) or not _TOKEN.fullmatch(expected_token):
                out['issues'].append('invalid_proxy_trial_identity')
                expected_token = ''
            if expected_token:
                host = root / 'opencode-proxy/trials' / (expected_token + '.jsonl')
                if host.exists():
                    proxy = host
        if proxy.exists():
            add_evidence(proxy)
            out['sources'].append(str(proxy.relative_to(root)))
            seen = set()
            with proxy.open('rb') as stream:
                while raw := stream.readline(MAX_METRIC_LINE_BYTES + 1):
                    if len(raw) > MAX_METRIC_LINE_BYTES:
                        raise ValueError('proxy metric exceeds line bound')
                    row = json.loads(raw)
                    if row.get('event') != 'chatCompletion':
                        continue
                    if not expected_token or row.get('trialToken') != expected_token:
                        raise ValueError('proxy usage trial identity mismatch')
                    if row.get('invalidTrial'):
                        out['issues'].append('invalid_proxy_trial')
                    identity = row.get('traceId')
                    if not identity or identity in seen:
                        raise ValueError('missing or duplicate proxy request identity')
                    seen.add(identity)
                    rows.append(row.get('usage'))
                    receipt = row.get('requestParameters')
                    if isinstance(receipt, dict):
                        receipt = request_parameter_receipt(dict(
                            receipt, thinking={'type': receipt.get('thinking_type')}))
                        parameters[json.dumps(receipt, sort_keys=True)] = receipt
                    if row.get('clientDisconnected') or row.get('status') in {'truncated', 'cancelled'}:
                        out['issues'].append('terminal_may_have_unreceipted_usage')
        reported = None
        # Proxy records do not identify summary calls. Zero is not a measurement.
        out['compactionCalls'] = None
        out['compactionOutputTokens'] = None
    for raw in rows:
        out['observedCalls'] += 1
        parsed = _usage(raw)
        if parsed is None:
            out['unknownUsageCalls'] += 1
            continue
        out['usageReceiptedCalls'] += 1
        for key in TOKEN_FIELDS:
            out[key] += parsed[key]
    if reported and any(out[k] != reported[k] for k in TOKEN_FIELDS[:5]):
        out['issues'].append('runtime_usage_does_not_reconcile')
    if not rows:
        out['issues'].append('missing_usage_evidence')
    if out['unknownUsageCalls']:
        out['issues'].append('requests_without_usage_receipt')
    out['issues'] = sorted(set(out['issues']))
    out['usageComplete'] = not out['issues']
    out['knownReferenceCostUsd'] = _price(out, rates)
    out['referenceCostUsd'] = out['knownReferenceCostUsd'] if out['usageComplete'] else None
    out['requestParameters'] = list(parameters.values())[:64]
    out['requestParameterVariants'] = len(parameters)
    out['pricingSources'] = dict(prices)
    return out


def summarize_usage(rows, *, rates=None):
    usages = [r['usageAccounting'] for r in rows]
    total = {k: sum(u[k] for u in usages) for k in TOKEN_FIELDS}
    total.update(
        trials=len(rows), observedCalls=sum(u['observedCalls'] for u in usages),
        usageReceiptedCalls=sum(u['usageReceiptedCalls'] for u in usages),
        unknownUsageCalls=sum(u['unknownUsageCalls'] for u in usages),
        incompleteUsageTrials=sum(not u['usageComplete'] for u in usages),
        knownReferenceCostUsd=_price(total, rates),
        usageComplete=bool(rows) and all(u['usageComplete'] for u in usages))
    total['referenceCostUsd'] = total['knownReferenceCostUsd'] if total['usageComplete'] else None
    total['cacheReadFraction'] = (
        total['cacheReadTokens'] / total['inputTokens'] if total['inputTokens'] else None)
    total['medianObservedCalls'] = median(u['observedCalls'] for u in usages) if usages else None
    return total


def paired_statistics(baseline, candidate, *, task_names, seed, rates=None, samples=10000):
    """Resample whole tasks, keeping repeats and infrastructure spend together."""
    arms = {'baseline': baseline, 'candidate': candidate}
    groups = {}
    for role, arm in arms.items():
        current = defaultdict(list)
        archived = defaultdict(list)
        for row in arm['trials']:
            current[row['task']].append(row)
        for row in arm.get('infrastructureTrials', []):
            archived[row['task']].append(row)
        groups[role] = (current, archived)
    complete_scores = all(a['fullDomain']['allTrialsScored'] for a in arms.values())
    complete_usage = complete_scores and all(
        a['allAttemptUsage']['usageComplete'] for a in arms.values())
    cost_pairs, latency_pairs = [], []
    outcome_groups = defaultdict(lambda: {'tasks': 0, 'baselineKnownCostUsd': 0.0,
                                          'candidateKnownCostUsd': 0.0})
    for task in task_names:
        totals, means = {}, {}
        for role, arm in arms.items():
            current, archived = groups[role]
            rows = current[task] + archived[task]
            totals[role] = sum(r['usageAccounting']['knownReferenceCostUsd'] or 0 for r in rows)
            times = [r.get('agentSeconds') for r in rows]
            means[role] = sum(times) / len(current[task]) if current[task] and times and all(
                type(t) in (float, int) and math.isfinite(t) and t >= 0 for t in times) else None
        cost_pairs.append((totals['baseline'], totals['candidate']))
        if all(t is not None for t in means.values()):
            latency_pairs.append((means['baseline'], means['candidate']))
        if complete_scores:
            a, b = baseline['taskMeans'][task], candidate['taskMeans'][task]
            group = 'candidate_higher' if b > a else 'baseline_higher' if a > b else 'equal'
            outcome_groups[group]['tasks'] += 1
            for role in arms:
                outcome_groups[group][role + 'KnownCostUsd'] += totals[role]
    result = {
        'referencePrices': rates, 'priceKind': 'reference_not_invoice' if rates else 'unpriced',
        'costScope': 'all_current_and_archived_infrastructure_attempts',
        'latencyScope': 'all_attempt_agent_seconds_per_planned_trial_excludes_setup_queue',
        'usageComplete': complete_usage,
        'knownCostRatio': None, 'costRatio95CI': None,
        'meanAgentSecondsDelta95CI': None,
        'bootstrapUnit': 'task_cluster_including_all_repeats',
        'outcomeGroups': dict(outcome_groups),
        'costInference': 'Requires full-domain scored outcomes and complete usage; unknown is never zero.',
    }
    base_cost = sum(a for a, _ in cost_pairs)
    if rates is not None and base_cost > 0:
        result['knownCostRatio'] = sum(b for _, b in cost_pairs) / base_cost
    rng = random.Random(seed)
    cost_ratios, time_deltas = [], []
    for _ in range(samples):
        indices = rng.choices(range(len(task_names)), k=len(task_names))
        if complete_usage and rates and len(task_names) >= 2:
            a = sum(cost_pairs[i][0] for i in indices)
            if a > 0:
                cost_ratios.append(sum(cost_pairs[i][1] for i in indices) / a)
        if complete_scores and len(latency_pairs) == len(task_names) and len(task_names) >= 2:
            time_deltas.append(sum(latency_pairs[i][1] - latency_pairs[i][0] for i in indices) / len(indices))
    for values, key in ((cost_ratios, 'costRatio95CI'), (time_deltas, 'meanAgentSecondsDelta95CI')):
        if len(values) == samples:
            values.sort()
            result[key] = [values[int(samples * .025)], values[min(samples-1, int(samples * .975))]]
    return result
