"""Strategy library for the self-evolution module.

Responsibility: distill settled episodes into reusable strategy templates and
anti-patterns, and retrieve them at planning time. Entry points:
``distill_strategies`` / ``retrieve_strategies`` / ``list_anti_patterns``.
Dependencies: the goals/plans/episodes stores, the indexer tokenizer, the
strategy/anti-pattern contracts.

Distillation is deterministic and receipt-bound: templates group successful
paths by (domain, stage-kind signature), anti-patterns require the same
failure signature twice — one incident is never a pattern. Exploratory
episodes contribute nothing, matching their restricted learning rights.
"""
from __future__ import annotations

import hashlib
import time

from .contracts import validate_anti_pattern, validate_strategy
from .episodes import list_episodes, episode_goals
from .goals import get_plan
from .indexers import _terms
from .records import (
    MAX_LIST_LIMIT,
    NAMESPACE_ANTI_PATTERNS,
    NAMESPACE_STRATEGIES,
    entity_key,
    list_records,
    put_record,
)

MAX_TEMPLATES = 64
MAX_ANTI_PATTERNS = 64
MAX_APPLICABILITY_TERMS = 32
_OUTCOME_WEIGHT = {'success': 1.0, 'partial': 0.5}


def _selected_stages(episode: dict, *, user_id) -> list[dict] | None:
    plan = get_plan(episode.get('plan_id') or '', user_id=user_id)
    candidate = next((c for c in (plan or {}).get('candidates', [])
                      if c.get('path_id') == episode.get('selected_path_id')),
                     None)
    return (candidate or {}).get('stages') or None


def _signature(stages: list[dict] | None) -> str:
    if not stages:
        return 'unknown'
    return '>'.join(str(s.get('kind') or 'execute') for s in stages)[:200]


def distill_strategies(*, user_id, now: int | None = None, episode_reader=None, goal_reader=None) -> dict:
    """Rebuild templates and anti-patterns from settled episodes."""
    ts = int(now if now is not None else time.time())
    successes: dict[tuple, list] = {}
    failures: dict[tuple, list] = {}
    observations: dict[tuple, list] = {}
    episodes = sorted((episode_reader or list_episodes)(user_id=user_id, limit=200),
                      key=lambda e: (-int(e.get('settled_at') or 0), e['episode_id']))
    goals = episode_goals(episodes, user_id=user_id, goal_reader=goal_reader)
    for episode in episodes:
        if episode.get('exploratory'):
            continue
        goal = goals.get(episode.get('goal_id'))
        if goal is None:
            continue
        domain = str(goal.get('domain') or 'general')
        status = episode.get('status')
        stages = (_selected_stages(episode, user_id=user_id)
                  if status in ('success', 'partial', 'failure') else None)
        if stages:
            key = (domain, _signature(stages))
            observations.setdefault(key, []).append(episode)
        if status in _OUTCOME_WEIGHT:
            if stages is None:
                continue
            key = (domain, _signature(stages))
            successes.setdefault(key, []).append((episode, goal, stages))
        elif status == 'failure':
            receipts = episode.get('stage_receipts') or []
            failed = next((r for r in receipts
                           if r.get('status') in ('error', 'failed', 'failure', 'fail')
                           and str(r.get('stage') or '').strip()), None)
            if failed is None:
                continue  # A failed verdict does not identify a failed stage.
            key = (domain, f"failed:{str(failed['stage'])[:80]}")
            failures.setdefault(key, []).append(episode)

    def _digest(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]

    templates = []
    for (domain, signature), rows in sorted(successes.items()):
        # One bounded, reconstructible denominator: failures count too.
        observed = observations[(domain, signature)][:64]
        lineage = [episode['episode_id'] for episode in observed]
        terms = sorted(set().union(
            *[_terms(str(goal.get('statement') or '')) for _, goal, _ in rows])
        )[:MAX_APPLICABILITY_TERMS]
        rate = sum(_OUTCOME_WEIGHT.get(e['status'], 0.0) for e in observed) / len(observed)
        templates.append(validate_strategy({
            'schema_version': 1,
            'template_id': f"tmpl-{domain}-{_digest(signature)}",
            'domain': domain,
            'stages': [{'kind': str(s.get('kind') or 'execute')[:80],
                        'detail': str(s.get('detail') or '')[:1000]}
                       for s in rows[0][2][:16]],
            'applicability_terms': terms,
            'lineage': lineage,
            'samples': len(observed),
            'success_rate': round(rate, 4),
            'created_at': ts,
            'updated_at': ts,
        }))

    anti_patterns = []
    for (domain, signature), episodes in sorted(failures.items()):
        episodes = episodes[:64]
        if len(episodes) < 2:
            continue
        anti_patterns.append(validate_anti_pattern({
            'schema_version': 1,
            'failure_evidence_version': 1,
            'pattern_id': f"anti-{domain}-{_digest(signature)}",
            'domain': domain,
            'signature': signature,
            'advice': (f"avoid paths that break at {signature.removeprefix('failed:')} "
                       f"in {domain}; failed {len(episodes)} times"),
            'lineage': [e['episode_id'] for e in episodes][:64],
            'samples': len(episodes),
            'created_at': ts,
            'updated_at': ts,
        }))

    for record in templates[:MAX_TEMPLATES]:
        put_record(NAMESPACE_STRATEGIES,
                   entity_key(record['template_id'], user_id, kind='strategy'),
                   record, kind='strategy')
    for record in anti_patterns[:MAX_ANTI_PATTERNS]:
        put_record(NAMESPACE_ANTI_PATTERNS,
                   entity_key(record['pattern_id'], user_id, kind='anti-pattern'),
                   record, kind='anti-pattern')
    return {'templates': templates[:MAX_TEMPLATES],
            'anti_patterns': anti_patterns[:MAX_ANTI_PATTERNS]}


def retrieve_strategies(query: str, *, user_id, domain: str | None = None,
                        limit: int = 4) -> list[dict]:
    """Term-overlap retrieval over applicability_terms, success-rate ranked."""
    query_terms = _terms(query)
    if not query_terms:
        return []
    scored = []
    for template in list_records(NAMESPACE_STRATEGIES, user_id, kind='strategy',
                                 limit=MAX_TEMPLATES):
        if domain and template.get('domain') != domain:
            continue
        overlap = query_terms & set(template.get('applicability_terms') or [])
        if not overlap:
            continue
        coverage = len(overlap) / len(query_terms)
        scored.append((coverage * float(template.get('success_rate') or 0),
                       template))
    scored.sort(key=lambda item: (-item[0], item[1]['template_id']))
    return [template for _, template in scored[:max(1, min(int(limit or 4), 16))]]


def list_strategies(*, user_id, domain: str | None = None) -> list[dict]:
    templates = list_records(NAMESPACE_STRATEGIES, user_id, kind='strategy',
                             limit=MAX_TEMPLATES)
    if domain:
        templates = [t for t in templates if t.get('domain') == domain]
    return templates


def list_anti_patterns(*, user_id, domain: str | None = None) -> list[dict]:
    patterns = list_records(NAMESPACE_ANTI_PATTERNS, user_id,
                            kind='anti-pattern', limit=MAX_LIST_LIMIT)
    patterns = [p for p in patterns if type(p.get('failure_evidence_version')) is int
                and p['failure_evidence_version'] == 1]
    if domain:
        patterns = [p for p in patterns if p.get('domain') == domain]
    return patterns[:MAX_ANTI_PATTERNS]
