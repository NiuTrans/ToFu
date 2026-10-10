"""Weight distillation (R6): verified trajectories become training data.

Responsibility: build dataset receipts from checker-verified episodes and
route a distilled model into production through the ordinary routing
mutation. Entry points: ``build_trajectory_dataset`` /
``propose_distilled_model``. Dependencies: the episodes/goals stores, the
distillation contract, the gate.

Honesty boundary, pinned by tests: only episodes whose verdict was produced
by a checker enter training data — human-assessed or unassessed outcomes
never do, because the weight layer must inherit the same receipt discipline
as every other store. The trainer itself is an injectable seam; with no
trainer configured the proposal path fails typed, not silently.
"""
from __future__ import annotations

import hashlib
import json
import time

from .contracts import EvolutionStateError, validate_distillation
from .episodes import list_episodes
from .gate import propose_mutation
from .goals import list_goals
from .records import (
    NAMESPACE_DISTILLATIONS,
    entity_key,
    put_record,
)

MAX_DATASET_SAMPLES = 200


def build_trajectory_dataset(*, user_id, domain: str,
                             now: int | None = None) -> dict:
    """Assemble and receipt one verified-trajectory dataset for a domain."""
    goals = {g.get('goal_id'): g for g in list_goals(user_id=user_id,
                                                     limit=200)}
    entries = []
    for episode in list_episodes(user_id=user_id, limit=200):
        if (episode.get('status') not in ('success', 'partial')
                or episode.get('actuals_complete') is False):
            continue
        verdict = episode.get('verdict') or {}
        if verdict.get('assessed_by') != 'checker':
            continue
        goal = goals.get(episode.get('goal_id'))
        if goal is None or str(goal.get('domain') or 'general') != domain:
            continue
        entries.append({
            'episode_id': episode['episode_id'],
            'statement': str(goal.get('statement') or ''),
            'selected_path': episode.get('selected_path_id'),
            'outcome': episode['status'],
            'actuals': episode.get('actuals'),
            'evidence_refs': verdict.get('evidence_refs') or [],
        })
        if len(entries) >= MAX_DATASET_SAMPLES:
            break
    digest = hashlib.sha256(
        json.dumps(entries, sort_keys=True).encode('utf-8')).hexdigest()
    record = validate_distillation({
        'schema_version': 1,
        'dataset_id': f'ds-{digest[:12]}',
        'domain': str(domain or 'general')[:80],
        'samples': len(entries),
        'verdict_floor': 'checker',
        'episode_ids': [e['episode_id'] for e in entries],
        'sha256': digest,
        'created_at': int(now if now is not None else time.time()),
    })
    put_record(NAMESPACE_DISTILLATIONS,
               entity_key(record['dataset_id'], user_id,
                          kind='distillation dataset'),
               record, kind='distillation dataset')
    return record


def propose_distilled_model(dataset: dict, model_ref: str, *,
                            evidence_episode_ids: list[str], user_id,
                            trainer=None, now: int | None = None) -> dict:
    """Train (via the injectable trainer seam) and route the distilled model
    as an ordinary routing_change mutation behind the gate."""
    if not model_ref or not isinstance(model_ref, str):
        raise EvolutionStateError('a distilled model needs a model ref')
    if not (dataset.get('episode_ids') or []):
        raise EvolutionStateError('cannot distill from an empty dataset')
    run_trainer = trainer or _missing_trainer
    run_trainer(dataset, model_ref)
    return propose_mutation(
        'routing_change',
        {'model': model_ref, 'domain': dataset['domain']},
        list(evidence_episode_ids)[:32] or list(dataset['episode_ids'])[:32],
        user_id=user_id, now=now)


def _missing_trainer(dataset: dict, model_ref: str) -> None:
    raise EvolutionStateError(
        'no trainer configured for weight distillation; the seam is '
        'explicit rather than a hidden no-op')
