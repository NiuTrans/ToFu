"""Automatic curriculum (R2): the task distribution expands at the frontier.

Responsibility: read the frontier — capability gaps, data-starved
hypotheses, repeated-failure anti-patterns, mined friction clusters,
premise objections, and domain success rates — and admit bounded probe
goals through the ordinary intake. Entry points: ``frontier_digest`` /
``propose_probes``. Dependencies: the goals/episodes stores, the gate's gap
queue, the self-model, anti-patterns, cost priors, the observation ledger.

Discipline, pinned by tests:

- A probe is an ordinary goal: it enters the same confirm/launch/settle
  machinery. There is no parallel curriculum runtime.
- Probes default to exploratory learning rights (weights + cost priors
  only). A probe inherits machine-checkable criteria only when one is
  derivable from the failed goals it retries — then it earns full rights.
- Admission is deduplicated against existing goals and capped per run and
  in total; curiosity is bounded by construction, not by vigilance.
"""
from __future__ import annotations

import hashlib
import time

from .cost_model import estimate_cost, list_priors
from .episodes import list_episodes, episode_goals
from .gate import list_capability_gaps
from .goals import admit_goal, get_goal, list_goals
from .self_model import list_hypotheses
from .strategies import list_anti_patterns

MAX_PROBES_PER_RUN = 4
MAX_OPEN_PROBES = 8
_FLOOR_ENVELOPE = {'max_tokens': 20000, 'max_wall_clock_seconds': 600,
                   'max_tool_calls': 30}


def _probe_envelope(domain: str, *, user_id) -> dict:
    estimate = estimate_cost(domain, user_id=user_id)
    envelope = {}
    for field, floor in _FLOOR_ENVELOPE.items():
        measured = int(estimate.get(f'expected_{field.removeprefix("max_")}')
                       or 0)
        envelope[field] = max(measured, floor)
    return envelope


def frontier_digest(*, user_id) -> dict:
    """The frontier, in one bounded, model-free digest."""
    from .observations import cluster_observations
    gaps = [g for g in list_capability_gaps(user_id=user_id)
            if g.get('status') == 'proposed']
    gaps.sort(key=lambda g: -int(g.get('samples') or 0))
    pending = list_hypotheses(user_id=user_id, status='proposed')
    rates = {p['domain']: {'samples': p['samples'],
                           'failure_rate': p['failure_rate']}
             for p in list_priors(user_id=user_id)}
    return {
        'capability_gaps': gaps[:4],
        'pending_hypotheses': pending[:4],
        'anti_patterns': list_anti_patterns(user_id=user_id)[:4],
        'domain_rates': rates,
        # Failure-free struggle, clustered: walls the receipt stream missed.
        'friction': cluster_observations(
            user_id=user_id, kinds=('friction',))[:4],
        # Premise objections and other actor-recorded noticings.
        'objections': cluster_observations(
            user_id=user_id,
            kinds=('premise_objection', 'consumer_friction', 'anomaly',
                   'edge_case'))[:4],
    }


def _goal_domains(*, user_id) -> dict:
    return {g.get('goal_id'): g for g in list_goals(user_id=user_id,
                                                    limit=200)}


def _retry_criteria(pattern: dict, *, user_id, episode_reader=None, goal_reader=None) -> list[dict] | None:
    """Inherit machine-checkable criteria from the goals the anti-pattern
    actually failed on (its lineage) — the retry re-runs the same check the
    failed attempts owed, not an arbitrary recent one."""
    episodes = {e['episode_id']: e
                for e in (episode_reader or list_episodes)(user_id=user_id, limit=200)}
    goals = episode_goals(list(episodes.values()), user_id=user_id, goal_reader=goal_reader)
    for episode_id in (pattern.get('lineage') or []):
        episode = episodes.get(episode_id)
        if not episode:
            continue
        goal = goals.get(episode.get('goal_id')) or {}
        criteria = [c for c in (goal.get('criteria') or [])
                    if c.get('kind') == 'test_command']
        if criteria:
            return criteria[:2]
    return None


def _deterministic_probes(digest: dict, *, user_id, episode_reader=None, goal_reader=None) -> list[dict]:
    probes = []
    for gap in digest['capability_gaps']:
        probes.append({
            'marker': 'probe:gap',
            'domain': 'general',
            'statement': (f"[probe:gap] Acquire capability to handle: "
                          f"{gap['summary'][:200]}"),
            'criteria': None,
            'lineage': gap.get('lineage') or [],
        })
    for hypothesis in digest['pending_hypotheses']:
        domain = str(hypothesis.get('domain') or '*')
        probes.append({
            'marker': 'probe:hypothesis',
            'domain': 'general' if domain == '*' else domain,
            'statement': (
                f"[probe:hypothesis] Generate evidence for: "
                f"{hypothesis['statement'][:180]}"),
            'criteria': None,
            'lineage': hypothesis.get('lineage') or [],
        })
    for pattern in digest['anti_patterns']:
        domain = str(pattern.get('domain') or 'general')
        probes.append({
            'marker': 'probe:anti-pattern',
            'domain': domain,
            'statement': (f"[probe:anti-pattern] Retry {domain} work while "
                          f"avoiding: {pattern['advice'][:180]}"),
            'criteria': _retry_criteria(pattern, user_id=user_id, episode_reader=episode_reader, goal_reader=goal_reader),
            'lineage': pattern.get('lineage') or [],
        })
    for cluster in digest.get('friction') or []:
        probes.append({
            'marker': 'probe:friction',
            'domain': 'general',
            'statement': (f"[probe:friction] Investigate and relieve repeated "
                          f"struggle: {cluster['statement'][:180]}"),
            'criteria': None,
            'lineage': cluster.get('observation_ids') or [],
            'cluster_count': int(cluster.get('count') or 0),
        })
    for cluster in digest.get('objections') or []:
        probes.append({
            'marker': 'probe:objection',
            'domain': 'general',
            'statement': (f"[probe:objection] Examine the premise objection "
                          f"with evidence: {cluster['statement'][:180]}"),
            'criteria': None,
            'lineage': cluster.get('observation_ids') or [],
            'cluster_count': int(cluster.get('count') or 0),
        })
    return probes


def _open_probe_count(*, user_id) -> int:
    return sum(1 for g in list_goals(user_id=user_id, limit=200)
               if str(g.get('statement') or '').startswith('[probe:')
               and g.get('status') in ('draft', 'admitted', 'active'))


def _frontier_score(domain: str, digest: dict, *, user_id,
                    cluster_count: int = 0) -> float:
    """Two exploration axes, max-combined:

    - **Learnability band**: score 1.0 at 50% predicted success, 0.0 at the
      extremes. Hopeless (≈0) and solved (≈1) regions teach the least, so
      they are proposed last. Unknown lands mid-band on purpose.
    - **Surprise**: corroborated struggle/objections (``cluster_count``)
      mark where reality protests the current model. A wall with protest
      outranks a quiet wall — the band alone would walk away from it.
    """
    from .self_model import predict_success
    prediction = predict_success(domain, {}, user_id=user_id)
    if prediction is not None:
        probability = prediction['success_probability']
    else:
        rates = (digest.get('domain_rates') or {}).get(domain) or {}
        if rates.get('samples'):
            probability = 1.0 - float(rates.get('failure_rate') or 0.5)
        else:
            probability = 0.5
    learnability = 1.0 - abs(probability - 0.5) * 2.0
    surprise = min(1.0, 0.25 * max(0, int(cluster_count or 0)))
    return round(max(learnability, surprise), 4)


def propose_probes(*, user_id, author=None, limit: int = MAX_PROBES_PER_RUN,
                   now: int | None = None, episode_reader=None, goal_reader=None) -> list[dict]:
    """Admit up to ``limit`` deduplicated probe goals through the intake.

    ``author`` is an optional LLM seam: it receives the digest plus the
    deterministic candidates and may return refined payloads of the same
    shape; every payload is validated the same way regardless of origin.
    """
    if _open_probe_count(user_id=user_id) >= MAX_OPEN_PROBES:
        return []
    digest = frontier_digest(user_id=user_id)
    candidates = _deterministic_probes(digest, user_id=user_id, episode_reader=episode_reader, goal_reader=goal_reader)
    candidates.sort(
        key=lambda c: _frontier_score(str(c.get('domain') or 'general'),
                                      digest, user_id=user_id,
                                      cluster_count=c.get('cluster_count')
                                      or 0),
        reverse=True)
    if author is not None:
        refined = author({'digest': digest, 'candidates': candidates,
                          'user_id': user_id})
        if isinstance(refined, list) and refined:
            candidates = [c for c in refined if isinstance(c, dict)]
    existing = {str(g.get('statement') or '')
                for g in list_goals(user_id=user_id, limit=200)}
    ts = int(now if now is not None else time.time())
    admitted = []
    for candidate in candidates:
        if len(admitted) >= max(1, int(limit)):
            break
        statement = str(candidate.get('statement') or '')[:4000]
        if not statement.startswith('[probe:') or statement in existing:
            continue
        criteria = candidate.get('criteria') or []
        domain = str(candidate.get('domain') or 'general')[:80]
        digest_hex = hashlib.sha256(statement.encode('utf-8')).hexdigest()[:10]
        payload = {
            'schema_version': 1,
            'goal_id': f'probe-{digest_hex}',
            'statement': statement,
            'domain': domain,
            'exploratory': not criteria,
            'criteria': criteria,
            'budget_envelope': _probe_envelope(domain, user_id=user_id),
            'status': 'draft',
            'created_at': ts,
        }
        if get_goal(payload['goal_id'], user_id=user_id) is not None:
            continue
        admitted.append(admit_goal(payload, user_id=user_id))
        existing.add(statement)
    return admitted
