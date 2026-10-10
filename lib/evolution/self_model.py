"""Self-model (R4): falsifiable competence factors driving model-based planning.

Responsibility: turn episode history into a tested model of "when do I
succeed" — hypotheses are proposed (by an LLM meta-goal or a caller),
validated against data, and only the admitted ones adjust planning. Entry
points: ``propose_hypotheses`` / ``validate_hypotheses`` /
``predict_success`` / ``list_hypotheses`` / ``path_features_for_candidate``.
Dependencies: the episodes/goals/plans stores, the cost model (baseline
rates), the hypothesis contract.

Honesty rules, pinned by tests:

- Hypotheses are cheap; data is the judge. A hypothesis whose feature is
  not in the deterministic registry, or whose measured direction
  contradicts its claim, is discarded — never debated.
- Insufficient data leaves a hypothesis ``proposed`` for later re-testing;
  it does not adjust planning until admitted.
- Planning-time prediction reuses the same predicates over estimated
  features; that is an honest approximation, documented here, not a hidden
  one.
"""
from __future__ import annotations

import time
import uuid

from .contracts import (
    EvolutionStateError,
    validate_hypothesis,
)
from .episodes import list_episodes, episode_goals
from .goals import get_plan
from .records import (
    NAMESPACE_HYPOTHESES,
    entity_key,
    list_records,
    put_record,
)

MAX_HYPOTHESES = 128
MIN_GROUP_SAMPLES = 3
MIN_LIFT = 0.15
_OUTCOME_WEIGHT = {'success': 1.0, 'partial': 0.5, 'failure': 0.0}


#: Deterministic feature registry. Unregistered features make a hypothesis
#: untestable by construction, so proposals are validated against this list.
def _feature_extractors(reindexed_episodes=frozenset()):
    return {
        'tokens': lambda ep, plan, goal: ep['actuals']['tokens'],
        'wall_clock_seconds': lambda ep, plan, goal: (
            ep['actuals']['wall_clock_seconds']),
        'tool_calls': lambda ep, plan, goal: ep['actuals']['tool_calls'],
        'resource_count': lambda ep, plan, goal: (
            len((plan or {}).get('resource_refs') or [])),
        'stage_count': lambda ep, plan, goal: (
            len((plan or {}).get('stages') or [])),
        'has_history_resource': lambda ep, plan, goal: any(
            r.get('indexer') == 'history'
            for r in (plan or {}).get('resource_refs') or []),
        'stage_signature': lambda ep, plan, goal: '>'.join(
            str(s.get('kind') or 'execute')
            for s in (plan or {}).get('stages') or []) or 'unknown',
        'exploratory': lambda ep, plan, goal: bool(
            goal.get('exploratory')),
        'reindexed': lambda ep, plan, goal: (
            ep.get('episode_id') in reindexed_episodes),
    }


FEATURES = frozenset(_feature_extractors().keys())


def _reindexed_episode_ids(*, user_id) -> frozenset:
    """Episodes touched by an on-demand reindex receipt (refetch.py owns the
    receipt; this read is the falsifiable link from action to outcome)."""
    from .observations import list_observations
    ids = set()
    for note in list_observations(user_id=user_id, kind='reindex_note',
                                  limit=256):
        ids.update(str(item) for item in (note.get('lineage') or []))
    return frozenset(ids)


def path_features_for_candidate(candidate: dict, goal: dict,
                                estimate: dict | None = None) -> dict:
    """Planning-time features for one candidate path; estimated consumption
    stands in for the unknown actuals (documented approximation)."""
    estimate = estimate or {}
    return {
        'tokens': estimate.get('expected_tokens', 0),
        'wall_clock_seconds': estimate.get('expected_wall_clock_seconds', 0),
        'tool_calls': estimate.get('expected_tool_calls', 0),
        'resource_count': len(candidate.get('resource_refs') or []),
        'stage_count': len(candidate.get('stages') or []),
        'has_history_resource': any(
            r.get('indexer') == 'history'
            for r in (candidate.get('resource_refs') or [])),
        'stage_signature': '>'.join(
            str(s.get('kind') or 'execute')
            for s in (candidate.get('stages') or [])) or 'unknown',
        'exploratory': bool(goal.get('exploratory')),
        # Planning-time stand-in: whether the attempt will reindex is unknown
        # before execution, so the honest prediction is False.
        'reindexed': False,
    }


def _predicate(hypothesis: dict, features: dict) -> bool | None:
    value = features.get(hypothesis['feature'])
    if value is None:
        return None
    threshold = hypothesis['threshold']
    operator = hypothesis['operator']
    try:
        if operator == 'gt':
            return float(value) > float(threshold)
        if operator == 'lte':
            return float(value) <= float(threshold)
        if operator == 'eq':
            return value == threshold
        if operator == 'neq':
            return value != threshold
    except (TypeError, ValueError):
        return None
    return None


def _episode_rows(*, user_id, episode_reader=None, goal_reader=None) -> list[dict]:
    selected = (episode_reader or list_episodes)(user_id=user_id, limit=200)
    goals = episode_goals(selected, user_id=user_id, goal_reader=goal_reader)
    extractors = _feature_extractors(
        reindexed_episodes=_reindexed_episode_ids(user_id=user_id))
    rows = []
    for episode in selected:
        goal = goals.get(episode.get('goal_id'))
        if (goal is None or episode.get('status') not in _OUTCOME_WEIGHT
                or episode.get('actuals_complete') is False):
            continue
        plan_doc = get_plan(episode.get('plan_id') or '', user_id=user_id)
        candidate = next((c for c in (plan_doc or {}).get('candidates', [])
                          if c.get('path_id') == episode.get(
                              'selected_path_id')), {})
        rows.append({
            'episode': episode,
            'domain': str(goal.get('domain') or 'general'),
            'weight': _OUTCOME_WEIGHT[episode['status']],
            'features': {name: f(episode, candidate, goal)
                         for name, f in extractors.items()},
        })
    return rows


def propose_hypotheses(proposals: list[dict], *, user_id,
                       now: int | None = None) -> list[dict]:
    """Store caller/LLM-proposed hypotheses as ``proposed`` after shape and
    registry validation; untestable features are refused at intake."""
    ts = int(now if now is not None else time.time())
    saved = []
    for proposal in (proposals or [])[:16]:
        feature = str((proposal or {}).get('feature') or '')
        if feature not in FEATURES:
            continue
        record = validate_hypothesis({
            'schema_version': 1,
            'hypothesis_id': f'hyp-{uuid.uuid4().hex[:12]}',
            'domain': str(proposal.get('domain') or '*')[:80],
            'feature': feature,
            'operator': proposal.get('operator'),
            'threshold': proposal.get('threshold'),
            'direction': proposal.get('direction'),
            'statement': str(proposal.get('statement') or '')[:500],
            'status': 'proposed',
            'evidence': {'samples_match': 0, 'samples_rest': 0,
                         'rate_match': 0.0, 'rate_rest': 0.0, 'lift': 0.0},
            'lineage': [str(e)[:160] for e in
                        (proposal.get('lineage') or [])][:8],
            'created_at': ts,
            'updated_at': ts,
        })
        put_record(NAMESPACE_HYPOTHESES,
                   entity_key(record['hypothesis_id'], user_id,
                              kind='hypothesis'),
                   record, kind='hypothesis')
        saved.append(record)
    return saved


def validate_hypotheses(*, user_id, now: int | None = None, episode_reader=None, goal_reader=None) -> list[dict]:
    """Test every proposed hypothesis against episode data."""
    from .gate_policy import get_thresholds
    thresholds = get_thresholds(user_id=user_id)
    min_samples = int(thresholds['min_group_samples'])
    min_lift = float(thresholds['min_lift'])
    ts = int(now if now is not None else time.time())
    rows = _episode_rows(user_id=user_id, episode_reader=episode_reader, goal_reader=goal_reader)
    verdicts = []
    for hypothesis in list_records(NAMESPACE_HYPOTHESES, user_id,
                                   kind='hypothesis', limit=MAX_HYPOTHESES):
        if hypothesis.get('status') != 'proposed':
            continue
        scoped = [r for r in rows if hypothesis['domain'] == '*'
                  or r['domain'] == hypothesis['domain']]
        match, rest = [], []
        for row in scoped:
            holds = _predicate(hypothesis, row['features'])
            if holds is True:
                match.append(row['weight'])
            elif holds is False:
                rest.append(row['weight'])
        record = dict(hypothesis)
        if len(match) < min_samples or len(rest) < min_samples:
            record['updated_at'] = ts  # insufficient data: retest later
            verdicts.append(_save_hypothesis(record, user_id=user_id))
            continue
        rate_match = sum(match) / len(match)
        rate_rest = sum(rest) / len(rest)
        raw_lift = rate_match - rate_rest
        lift = raw_lift if hypothesis['direction'] == 'helps' else -raw_lift
        record['evidence'] = {
            'samples_match': len(match), 'samples_rest': len(rest),
            'rate_match': round(rate_match, 4),
            'rate_rest': round(rate_rest, 4),
            'lift': round(lift, 4),
        }
        record['status'] = 'admitted' if lift >= min_lift else 'discarded'
        record['updated_at'] = ts
        verdicts.append(_save_hypothesis(record, user_id=user_id))
    return verdicts


def _save_hypothesis(record: dict, *, user_id) -> dict:
    put_record(NAMESPACE_HYPOTHESES,
               entity_key(record['hypothesis_id'], user_id,
                          kind='hypothesis'),
               validate_hypothesis(record), kind='hypothesis')
    return record


def predict_success(domain: str, path_features: dict, *, user_id
                    ) -> dict | None:
    """Model-based success prediction; None when no admitted factor applies,
    so callers keep their heuristic fallback rather than a fabricated one."""
    from .cost_model import estimate_cost
    applied = []
    for hypothesis in list_records(NAMESPACE_HYPOTHESES, user_id,
                                   kind='hypothesis', limit=MAX_HYPOTHESES):
        if hypothesis.get('status') != 'admitted':
            continue
        if hypothesis['domain'] not in ('*', domain):
            continue
        if _predicate(hypothesis, path_features) is not True:
            continue
        applied.append(hypothesis)
    if not applied:
        return None
    base = 1.0 - estimate_cost(domain, user_id=user_id)['failure_rate_prior']
    probability = base
    for hypothesis in applied:
        signed = abs(hypothesis['evidence']['lift'])
        if hypothesis['direction'] == 'hurts':
            signed = -signed
        probability += signed
    return {
        'success_probability': round(min(0.95, max(0.05, probability)), 4),
        'baseline': round(base, 4),
        'factors_applied': [h['hypothesis_id'] for h in applied][:16],
    }


def list_hypotheses(*, user_id, status: str | None = None) -> list[dict]:
    hypotheses = list_records(NAMESPACE_HYPOTHESES, user_id,
                              kind='hypothesis', limit=MAX_HYPOTHESES)
    if status:
        hypotheses = [h for h in hypotheses if h.get('status') == status]
    return hypotheses


def author_hypotheses(domain: str, *, user_id, author=None,
                      now: int | None = None) -> list[dict]:
    """LLM meta-goal: read the domain's feature/outcome digest, propose
    falsifiable factors. The author is an injectable bounded chat turn; the
    data decides what survives — never the author's eloquence."""
    rows = [r for r in _episode_rows(user_id=user_id)
            if r['domain'] == domain]
    if not rows:
        return []
    write = author or _default_hypothesis_author
    digest = '\n'.join(
        f"- {r['episode']['status']}: " + ', '.join(
            f'{k}={v}' for k, v in sorted(r['features'].items()))
        for r in rows[:40])
    proposals = write({'domain': domain, 'digest': digest,
                       'features': sorted(FEATURES),
                       'user_id': user_id})
    if not isinstance(proposals, list):
        raise EvolutionStateError('hypothesis author must return a list')
    for proposal in proposals:
        if isinstance(proposal, dict):
            proposal.setdefault('domain', domain)
            proposal.setdefault('lineage',
                                [r['episode']['episode_id'] for r in rows[:4]])
    return propose_hypotheses(proposals, user_id=user_id, now=now)


def _default_hypothesis_author(request: dict) -> list[dict]:
    import json as _json

    from lib.tasks_pkg.entry import run_chat_sync
    prompt = (
        'You analyze an AI agent\'s settled task episodes and propose '
        'falsifiable success factors.\n'
        f"Domain: {request['domain']}\n"
        f"Available features: {', '.join(request['features'])}\n"
        'Episodes (outcome: features):\n'
        f"{request['digest']}\n"
        'Reply with up to 4 JSON lines, each: {"feature": ..., "operator": '
        '"gt"|"lte"|"eq"|"neq", "threshold": number-or-string, "direction": '
        '"helps"|"hurts", "statement": "one sentence"}. No prose.')
    result = run_chat_sync(
        [{'role': 'user', 'content': prompt}],
        user_id=int(request['user_id']), timeout_s=600.0)
    proposals = []
    for line in (result.content or '').splitlines():
        line = line.strip().strip('`')
        if not line.startswith('{'):
            continue
        try:
            proposals.append(_json.loads(line))
        except ValueError:
            continue
    return proposals
