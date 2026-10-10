"""Holdout suite and regression receipts for the evolution gate.

Responsibility: keep a bounded suite of settled goals as regression probes,
score proposed mutations against them, and watch active mutations for
post-activation degradation. Entry points: ``build_holdout_suite`` /
``run_regression`` / ``monitor_active_mutations`` / ``list_holdout``.
Dependencies: the episodes/goals stores, the checkers (deterministic
re-probe), the gate (rollback on breach), the holdout/regression contracts.

A mutation kind whose payload cannot change a deterministic probe's result
(e.g. routing changes versus test commands) measures nothing: the receipt is
``unmeasurable`` and activation then needs the gate's explicit operator
waiver — never a silent pass. Post-activation history is descriptive;
automatic rollback requires a fresh, bound, complete paired comparison and
restores the parent chain through the ordinary gate.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from .contracts import validate_holdout_item, validate_regression_receipt
from .episodes import list_episodes, episode_goals
from .comparison import compare_pairs, mutation_fingerprint, paired_context_fingerprint
from .contracts import EvolutionStateError
from .records import (
    NAMESPACE_HOLDOUT,
    NAMESPACE_REGRESSIONS,
    entity_key,
    get_record,
    list_records,
    put_record,
)

MAX_HOLDOUT_ITEMS = 32
PER_DOMAIN_ITEMS = 4
_OUTCOME_WEIGHT = {'success': 1.0, 'partial': 0.5, 'failure': 0.0}


def build_holdout_suite(*, user_id, now: int | None = None, episode_reader=None, goal_reader=None) -> list[dict]:
    """Keep the newest machine-checkable settled episodes per domain."""
    ts = int(now if now is not None else time.time())
    selected = (episode_reader or list_episodes)(user_id=user_id, limit=200)
    goals = episode_goals(selected, user_id=user_id, goal_reader=goal_reader)
    candidates = []
    for episode in selected:
        goal = goals.get(episode.get('goal_id'))
        if not goal or episode.get('status') not in _OUTCOME_WEIGHT:
            continue
        criteria = goal.get('criteria') or []
        if not any(c.get('kind') == 'test_command' for c in criteria):
            continue
        candidates.append((int(episode.get('settled_at') or 0), episode, goal))
    candidates.sort(key=lambda row: -row[0])
    items, per_domain = [], {}
    for _, episode, goal in candidates:
        domain = str(goal.get('domain') or 'general')
        if per_domain.get(domain, 0) >= PER_DOMAIN_ITEMS:
            continue
        if len(items) >= MAX_HOLDOUT_ITEMS:
            break
        per_domain[domain] = per_domain.get(domain, 0) + 1
        digest = hashlib.sha256(
            episode['episode_id'].encode('utf-8')).hexdigest()[:12]
        items.append(validate_holdout_item({
            'schema_version': 1,
            'item_id': f'ho-{digest}',
            'domain': domain,
            'statement': str(goal.get('statement') or '')[:4000],
            'criteria': [{'criterion_id': str(c.get('criterion_id') or ''),
                          'kind': c.get('kind'),
                          'params': c.get('params') or {}}
                         for c in criteria][:8],
            'baseline_weight': _OUTCOME_WEIGHT[episode['status']],
            'lineage': [episode['episode_id']],
            'created_at': ts,
        }))
    for item in items:
        put_record(NAMESPACE_HOLDOUT,
                   entity_key(item['item_id'], user_id, kind='holdout'),
                   item, kind='holdout')
    return items


def list_holdout(*, user_id) -> list[dict]:
    return list_records(NAMESPACE_HOLDOUT, user_id, kind='holdout',
                        limit=MAX_HOLDOUT_ITEMS)


def _default_probe(mutation: dict, item: dict, *, user_id):
    """No installed runner yet applies a candidate in an isolated workspace.

    Rechecking the current checkout cannot observe a prompt/tool/routing change.
    Historical ranking replay observes selection bias, not counterfactual success.
    Keep the gate closed until an injected candidate-aware probe provides evidence.
    """
    return 'skip'


def regression_suite(mutation: dict, *, user_id, paired: bool) -> list[dict]:
    items = list_holdout(user_id=user_id)
    if paired:
        items = [i for i in items if not set(i['lineage']).intersection(
            mutation.get('evidence_episode_ids') or [])]
    return items


def suite_fingerprint(items: list[dict]) -> str:
    # Refresh timestamps are not experiment identity; requirements and lineage are.
    stable = [{k: v for k, v in item.items() if k != 'created_at'}
              for item in sorted(items, key=lambda i: i['item_id'])]
    return hashlib.sha256(json.dumps(stable, sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def run_regression(mutation: dict, *, user_id, probe=None, paired_probe=None,
                   now: int | None = None) -> dict:
    """Persist regression evidence; paired_probe returns baseline/candidate arms.

    The trusted probe must apply both versions independently with identical
    model, checker and budgets. Evidence/training lineage is excluded. Missing
    or failed probes stay unknown. Historical probes cannot establish a gain.
    """
    from .gate import _load
    try:
        stored = _load(mutation.get('mutation_id'), user_id=user_id)
    except (EvolutionStateError, ValueError):
        stored = None
    if stored is None or mutation_fingerprint(stored) != mutation_fingerprint(mutation):
        raise EvolutionStateError('regression requires the owner\'s unchanged mutation')
    if probe is not None and paired_probe is not None:
        raise EvolutionStateError('choose historical or paired regression, not both')
    from .gate_policy import get_thresholds
    margin = float(get_thresholds(user_id=user_id)['regression_margin'])
    ts = int(now if now is not None else time.time())
    run = probe or (lambda m, i: _default_probe(m, i, user_id=user_id))
    items = regression_suite(stored, user_id=user_id, paired=paired_probe is not None)
    frozen_suite = suite_fingerprint(items)
    results = []
    pairs = []
    for item in items:
        try:
            if paired_probe is not None:
                from copy import deepcopy
                pair = {'item_id': item['item_id'],
                        **paired_probe(deepcopy(mutation), deepcopy(item))}
                # A probe cannot rename cases or smuggle invalid metric values.
                pair['item_id'] = item['item_id']
                compare_pairs([pair])
                pairs.append(pair)
                outcome = pair['candidate']['outcome']
                if pair['baseline']['outcome'] == 'skip':
                    outcome = 'skip'
            else:
                outcome = run(mutation, item)
        except Exception:
            outcome = 'skip'
            if paired_probe is not None:
                pairs.append({'item_id': item['item_id'], **{
                    arm: {'outcome': 'skip', 'tokens': None, 'wall_clock_seconds': None}
                    for arm in ('baseline', 'candidate')}})
        results.append({'item_id': item['item_id'],
                        'outcome': outcome if outcome in ('pass', 'fail', 'skip')
                                   else 'skip'})
    measured = [r for r in results if r['outcome'] != 'skip']
    measured_ids = {r['item_id'] for r in measured}
    baseline_items = [i for i in items if i['item_id'] in measured_ids]
    baseline = (sum(i['baseline_weight'] for i in baseline_items) / len(baseline_items)
                ) if baseline_items else 0.0
    if not measured:
        verdict, score = 'unmeasurable', 0.0
    else:
        score = sum(1.0 if r['outcome'] == 'pass' else 0.0
                    for r in measured) / len(measured)
        verdict = 'pass' if score >= baseline - margin else 'fail'
        if verdict == 'pass' and len(measured) != len(items):
            verdict = 'unmeasurable'
    comparison = None
    if paired_probe is not None:
        comparison = compare_pairs(pairs)
        complete = [p for p in pairs if all(p[a]['outcome'] != 'skip'
                                          for a in ('baseline', 'candidate'))]
        baseline = (sum(p['baseline']['outcome'] == 'pass' for p in complete)
                    / len(complete)) if complete else 0.0
        score = (sum(p['candidate']['outcome'] == 'pass' for p in complete)
                 / len(complete)) if complete else 0.0
        verdict = ('fail' if comparison['losses'] else
                   'unmeasurable' if comparison['conclusion'] == 'inconclusive'
                   else 'pass')
    receipt = validate_regression_receipt({
        'schema_version': 1,
        'receipt_id': f'reg-{uuid.uuid4().hex[:12]}',
        'mutation_id': mutation['mutation_id'],
        'verdict': verdict,
        'score': round(score, 4),
        'baseline': round(baseline, 4),
        'item_results': results[:32],
        'created_at': ts,
        'measurement': 'paired' if paired_probe is not None else 'historical',
        'mutation_fingerprint': mutation_fingerprint(stored),
        'suite_fingerprint': frozen_suite,
        **({'comparison': comparison, 'paired_context_fingerprint':paired_context_fingerprint(stored)}
           if comparison is not None else {}),
    })
    put_record(NAMESPACE_REGRESSIONS,
               entity_key(mutation['mutation_id'], user_id,
                          kind='regression receipt'),
               receipt, kind='regression receipt')
    return receipt


def latest_regression(mutation_id: str, *, user_id) -> dict | None:
    return get_record(NAMESPACE_REGRESSIONS,
                      entity_key(mutation_id, user_id,
                                 kind='regression receipt'))



def verify_paired_regression(mutation, receipt, items):
    """One recomputable receipt boundary for activation and automatic rollback."""
    try:
        validate_regression_receipt(receipt)
        if (receipt.get('measurement')!='paired' or receipt['mutation_id']!=mutation['mutation_id']
                or receipt.get('mutation_fingerprint')!=mutation_fingerprint(mutation)
                or receipt.get('paired_context_fingerprint')!=paired_context_fingerprint(mutation)
                or receipt.get('suite_fingerprint')!=suite_fingerprint(items)):
            raise ValueError('paired controls or suite differ')
        comparison=compare_pairs(receipt['comparison']['pairs'])
        pairs=comparison['pairs'];results=receipt['item_results']
        expected={item['item_id'] for item in items}
        outcomes={pair['item_id']:pair['candidate']['outcome'] if pair['baseline']['outcome']!='skip' else 'skip' for pair in pairs}
        if (comparison!=receipt['comparison'] or len(expected)!=len(items)
                or len(pairs)!=len(expected) or {pair['item_id'] for pair in pairs}!=expected
                or len(results)!=len(pairs) or len({row['item_id'] for row in results})!=len(results)
                or {row['item_id']:row['outcome'] for row in results}!=outcomes):
            raise ValueError('paired coverage or outcomes differ')
        complete=[pair for pair in pairs if all(pair[arm]['outcome']!='skip' for arm in ('baseline','candidate'))]
        baseline=round(sum(pair['baseline']['outcome']=='pass' for pair in complete)/len(complete),4) if complete else 0.0
        score=round(sum(pair['candidate']['outcome']=='pass' for pair in complete)/len(complete),4) if complete else 0.0
        verdict='fail' if comparison['losses'] else 'unmeasurable' if comparison['conclusion']=='inconclusive' else 'pass'
        if receipt['baseline']!=baseline or receipt['score']!=score or receipt['verdict']!=verdict:
            raise ValueError('paired score or verdict differs')
        return comparison
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        raise EvolutionStateError('paired regression is invalid, inconsistent or unbound; rerun measurement') from exc


def _current_paired_regression(mutation, *, user_id, now, min_samples):
    """Current complete paired losses may authorize ordinary automatic rollback."""
    receipt=latest_regression(mutation['mutation_id'],user_id=user_id)
    if receipt is None:return False,'missing_regression'
    try:
        validate_regression_receipt(receipt)
        if (receipt.get('measurement')!='paired'
                or not mutation['updated_at']<=receipt['created_at']<=now):
            return False,'unbound_or_stale_regression'
        comparison=verify_paired_regression(mutation,receipt,regression_suite(mutation,user_id=user_id,paired=True))
        if comparison['measured']!=len(comparison['pairs']) or comparison['measured']<min_samples:
            return False,'incomplete_or_inconsistent_pairs'
        failed=comparison['losses']>0
        return failed,'current_paired_regression' if failed else 'no_paired_regression'
    except (EvolutionStateError,ValueError,KeyError,TypeError):
        return False,'invalid_regression_receipt'


def monitor_active_mutations(*, user_id, now: int | None = None, episode_reader=None, goal_reader=None) -> list[dict]:
    """Auto-rollback drill: post-activation degradation reverts the mutation.

    Temporal outcome changes are descriptive alerts, never causal evidence.
    Only complete, current, recomputable paired regression receipts can trigger
    rollback. Missing measurements stay unknown; no probe is launched here.
    """
    from .gate import list_mutations, rollback_mutation
    from .gate_policy import get_thresholds
    thresholds = get_thresholds(user_id=user_id)
    margin = float(thresholds['regression_margin'])
    min_samples = int(thresholds['monitor_min_samples'])
    selected = (episode_reader or list_episodes)(user_id=user_id, limit=200)
    goals = {key: str(goal.get('domain') or 'general') for key, goal in
        episode_goals(selected, user_id=user_id, goal_reader=goal_reader).items()}
    episodes = [e for e in selected
                if e.get('status') in _OUTCOME_WEIGHT and e['goal_id'] in goals]
    report = []
    for mutation in list_mutations(user_id=user_id):
        if mutation.get('status') != 'active':
            continue
        activated_at = int(mutation.get('updated_at') or 0)
        payload_domain = str((mutation.get('payload') or {}).get('domain') or '')
        relevant = [e for e in episodes
                    if not payload_domain
                    or goals.get(e.get('goal_id')) == payload_domain]
        before = [_OUTCOME_WEIGHT[e['status']] for e in relevant
                  if int(e.get('settled_at') or 0) < activated_at]
        after = [_OUTCOME_WEIGHT[e['status']] for e in relevant
                 if int(e.get('settled_at') or 0) >= activated_at]
        entry = {'mutation_id': mutation['mutation_id'],
                 'before_samples': len(before), 'after_samples': len(after),
                 'before_rate': round(sum(before)/len(before),4) if before else None,
                 'after_rate': round(sum(after)/len(after),4) if after else None,
                 'observed_decline': False, 'breach': False}
        if len(before)>=min_samples and len(after)>=min_samples:
            entry['observed_decline'] = sum(after)/len(after) < sum(before)/len(before)-margin
        failed,basis=_current_paired_regression(mutation,user_id=user_id,
            now=int(time.time() if now is None else now),min_samples=min_samples)
        entry['rollback_evidence']=basis
        if failed:
            rollback_mutation(mutation['mutation_id'],user_id=user_id,now=now)
            entry['breach']=True
        report.append(entry)
    return report
