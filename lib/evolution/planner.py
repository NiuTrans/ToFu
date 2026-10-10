"""Deterministic path planning for the self-evolution module.

Responsibility: turn candidate path skeletons into a contract-valid, ranked
plan whose cost halves come from measured priors. Entry points:
``build_plan`` / ``suggest_candidates``. Dependencies: the cost model, the
indexer seam, the goals/plans/episodes stores, and the plan contract.

The planner owns no LLM call and no execution: candidate skeletons come from
the caller (a model, a human, or ``suggest_candidates``' deterministic
history replay), and ranking is arithmetic over contract fields. Expected
utility/information-gain stay the proposer's judgment; the planner clamps
them to [0, 1] and records the ranking key so the selection is auditable
without re-running the scorer.
"""
from __future__ import annotations

from copy import deepcopy
import math
import time
import uuid

from lib.log import get_logger

from .contracts import EvolutionStateError, validate_plan
from .cost_model import estimate_cost
from .episodes import list_episodes
from .goals import get_plan, save_plan
from .indexers import HistoryIndexer, index_all
from .self_model import path_features_for_candidate, predict_success

logger = get_logger(__name__)

MAX_CANDIDATES = 16
_FLAT_UTILITY = 0.5


def load_active_policy(*, user_id):
    """The active planner-policy program (R3), or None.

    A policy is gate-installed skill code exporting
    ``score(candidate, context) -> float``; loading executes code that
    passed its own sandbox tests and human confirmation — the same trust
    boundary as any installed skill. Any failure degrades to the
    deterministic ranker, never to a broken plan.
    """
    from .records import NAMESPACE_POLICY, entity_key, get_record
    record = get_record(NAMESPACE_POLICY,
                        entity_key('active', user_id, kind='planner policy'))
    if not record or not record.get('code'):
        return None
    namespace: dict = {}
    try:
        exec(compile(record['code'], '<planner-policy>', 'exec'), namespace)
    except Exception as exc:
        # Untrusted policy code failing degrades to the deterministic ranker.
        logger.debug('[Evolution:Planner] active policy load failed: %s', exc)
        return None
    score = namespace.get('score')
    return score if callable(score) else None


def _cost_ratio(cost_estimate: dict, budget_envelope: dict) -> float:
    ceiling = max(1, int((budget_envelope or {}).get('max_tokens') or 0))
    return float(cost_estimate['expected_tokens']) / float(ceiling)


def _rank_key(candidate: dict, budget_envelope: dict) -> float:
    """Ranking = expected utility × information-gain weight / cost ratio.

    The gain enters as (0.5 + gain/2): a flat 0.5 gain is neutral (factor
    0.75 for every candidate), so gain only separates candidates that
    actually differ in what they teach the system.
    """
    estimate = candidate['cost_estimate']
    ratio = max(_cost_ratio(estimate, budget_envelope), 1e-9)
    gain_weight = 0.5 + float(estimate.get('expected_information_gain', 0.5)) / 2.0
    return round(estimate['expected_utility'] * gain_weight / ratio, 6)


def _clamp_unit(value, default=_FLAT_UTILITY) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(1.0, number))


def build_plan(goal: dict, candidates: list[dict], *, user_id,
               plan_id: str | None = None, now: int | None = None,
               persist: bool = True, policy=None) -> dict:
    """Assemble, rank, and validate a plan; persists as ``proposed``.

    Each candidate supplies title/stages and optionally resource_refs,
    expected_information_gain, expected_utility, and summary. The measurable
    cost fields always come from the cost model — caller-supplied costs are
    discarded, so prose estimates cannot enter a plan.
    """
    if not isinstance(goal, dict) or not goal.get('goal_id'):
        raise EvolutionStateError('planning requires an admitted goal record')
    if not 1 <= len(candidates or []) <= MAX_CANDIDATES:
        raise EvolutionStateError(
            f'plan admits 1..{MAX_CANDIDATES} candidates, got {len(candidates or [])}')
    envelope = goal.get('budget_envelope') or {}
    domain = str(goal.get('domain') or 'general')
    score_fn = policy if policy is not None else load_active_policy(
        user_id=user_id)
    seen = set()
    assembled = []
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict) or not raw.get('title') or not raw.get('stages'):
            raise EvolutionStateError('every candidate needs a title and stages')
        path_id = str(raw.get('path_id') or f'path-{index + 1}')
        if path_id in seen:
            raise EvolutionStateError(f'duplicate candidate path_id {path_id!r}')
        seen.add(path_id)
        estimate = estimate_cost(domain, user_id=user_id, budget_envelope=envelope)
        # R4: when the self-model has admitted factors for this domain, its
        # prediction refines both the failure prior and the utility default.
        # The proposer's explicit utility judgment still wins when present.
        features = path_features_for_candidate(
            {'resource_refs': raw.get('resource_refs') or [],
             'stages': raw.get('stages') or []}, goal, estimate)
        prediction = predict_success(domain, features, user_id=user_id)
        if prediction is not None:
            estimate['failure_rate_prior'] = round(
                1.0 - prediction['success_probability'], 4)
        estimate['expected_information_gain'] = _clamp_unit(
            raw.get('expected_information_gain'))
        estimate['expected_utility'] = _clamp_unit(
            raw.get('expected_utility'),
            default=(prediction['success_probability']
                     if prediction is not None else _FLAT_UTILITY))
        candidate = {
            'path_id': path_id,
            'title': str(raw['title'])[:500],
            'summary': str(raw.get('summary') or '')[:2000],
            'stages': [{'kind': str(s.get('kind') or 'execute')[:80],
                        'detail': str(s.get('detail') or '')[:1000]}
                       for s in raw['stages'][:16]],
            'resource_refs': deepcopy(raw.get('resource_refs') or [])[:64],
            'cost_estimate': estimate,
        }
        candidate['utility_over_cost'] = _rank_key(candidate, envelope)
        if score_fn is not None:
            try:
                policy_value = score_fn(
                    deepcopy(candidate),
                    {'domain': domain, 'budget_envelope': envelope,
                     'goal_statement': str(goal.get('statement') or '')})
                scored = float(policy_value)
                if not isinstance(policy_value, bool) and math.isfinite(scored) and scored >= 0:
                    candidate['utility_over_cost'] = scored
            except Exception as exc:
                # policy misbehavior degrades to the arithmetic rank
                logger.debug('[Evolution:Planner] policy score failed: %s', exc)
        assembled.append(candidate)
    assembled.sort(key=lambda c: (-c['utility_over_cost'], c['path_id']))
    ts = int(now if now is not None else time.time())
    plan = validate_plan({
        'schema_version': 1,
        'plan_id': plan_id or f'plan-{uuid.uuid4().hex[:16]}',
        'goal_id': goal['goal_id'],
        'plan_revision': 1,
        'candidates': assembled,
        'selected_path_id': assembled[0]['path_id'],
        'selection_rationale': (
            f"top utility/cost {assembled[0]['utility_over_cost']} via "
            f"{assembled[0]['cost_estimate']['priors_source']} priors"),
        'requires_confirmation': True,
        'status': 'proposed',
        'created_at': ts,
    })
    if persist:
        save_plan(plan, user_id=user_id)
    return plan


def suggest_candidates(goal: dict, *, user_id,
                       indexers=None) -> list[dict]:
    """Deterministic no-LLM candidates: replay winning local paths, then a
    baseline direct path carrying the merged resource index."""
    query = str(goal.get('statement') or '')
    indexer_list = list(indexers) if indexers is not None else [HistoryIndexer()]
    history = HistoryIndexer().index(query, user_id=user_id, limit=8)
    episodes = {e['episode_id']: e
                for e in list_episodes(user_id=user_id, limit=200)}
    candidates = []
    for ref in history:
        episode = episodes.get(ref['locator'])
        if not episode or episode.get('status') not in ('success', 'partial'):
            continue
        past_plan = get_plan(episode.get('plan_id') or '', user_id=user_id)
        past = next((c for c in (past_plan or {}).get('candidates', [])
                     if c.get('path_id') == episode.get('selected_path_id')),
                    None)
        if not past:
            continue
        candidates.append({
            'path_id': f"replay-{episode['episode_id']}",
            'title': f"Replay local {episode['status']} path",
            'summary': f"adapted from episode {episode['episode_id']}",
            'stages': deepcopy(past.get('stages') or []),
            'resource_refs': [ref],
            'expected_utility': 0.7 if episode['status'] == 'success' else 0.55,
            'expected_information_gain': _clamp_unit(ref.get('relevance')),
        })
    resources = index_all(query, indexer_list, user_id=user_id, limit=16)
    candidates.append({
        'path_id': 'baseline',
        'title': 'Inspect, implement and verify the goal',
        'summary': ('Generic milestone scaffold, not a researched or completed plan. '
                    'The executor must refine it from inspected context and report observed evidence.'),
        'stages': [
            {'kind': 'inspect', 'detail': (
                'Inspect the current context and constraints for this objective: ' + query)[:1000]},
            {'kind': 'plan', 'detail': (
                'Translate each requirement into an acceptance check. Decompose broad scope '
                'into actionable milestones with dependencies and concrete deliverables; '
                'identify unresolved authorization or information without inventing answers.')},
            {'kind': 'implement', 'detail': (
                'Produce the requested deliverables incrementally, preserving existing work. '
                'Use observed context rather than assuming files, sources or capabilities exist.')},
            {'kind': 'verify', 'detail': (
                'Run relevant checks against each acceptance criterion, capture real receipts, '
                'and repair observed failures. Mark unavailable evidence unknown, not passed.')},
            {'kind': 'deliver', 'detail': (
                'Provide usable artifact references, the verification results, and remaining '
                'limitations. A plan or self-reported success alone does not establish completion.')},
        ],
        'resource_refs': resources,
        'expected_utility': _FLAT_UTILITY,
        'expected_information_gain': _FLAT_UTILITY,
    })
    return candidates[:MAX_CANDIDATES]
