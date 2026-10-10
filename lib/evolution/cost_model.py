"""Measured cost priors for self-evolution path planning.

Responsibility: aggregate settled episodes into per-domain cost priors and
answer structured cost estimates for new plans. Entry points:
``recompute_priors`` / ``estimate_cost``. Dependencies: the goals/episodes
stores, the records plumbing, and the cost-priors contract.

Honesty rules (pinned by tests):

- Aborted episodes are censored: they feed neither medians nor failure rate.
- Unknown is never zero: without enough samples the estimate falls back from
  ``measured_history`` (domain bucket) to ``domain_default`` (the owner's
  ``_global`` aggregate) to ``exploratory_flat`` (the goal's own budget
  envelope with a maximum-entropy 0.5 failure prior).
- Priors are medians of receipt-derived actuals; expected information gain
  and utility remain the planner's judgment and are not produced here.
"""
from __future__ import annotations

import statistics
import time

from .contracts import validate_cost_priors
from .records import (
    NAMESPACE_COST_PRIORS,
    entity_key,
    get_record,
    list_records,
    put_record,
)
from .episodes import list_episodes, episode_goals

MIN_SAMPLES = 3
MAX_DOMAINS = 64
GLOBAL_DOMAIN = '_global'
_SETTLED = ('success', 'partial', 'failure')


def _median_int(values: list[int]) -> int:
    return int(statistics.median(values)) if values else 0


def _aggregate(samples: list[dict], domain: str, ts: int) -> dict:
    record = {
        'schema_version': 1,
        'domain': domain,
        'samples': len(samples),
        'tokens_median': _median_int([s['actuals']['tokens'] for s in samples]),
        'wall_clock_median': _median_int(
            [s['actuals']['wall_clock_seconds'] for s in samples]),
        'tool_calls_median': _median_int(
            [s['actuals']['tool_calls'] for s in samples]),
        'failure_rate': (sum(1 for s in samples if s['status'] == 'failure')
                         / len(samples)) if samples else 0.0,
        'updated_at': ts,
    }
    return validate_cost_priors(record)


def recompute_priors(*, user_id, now: int | None = None, episode_reader=None, goal_reader=None) -> list[dict]:
    """Rebuild owner priors from settled episodes; idempotent replace-in-place."""
    ts = int(now if now is not None else time.time())
    selected = (episode_reader or list_episodes)(user_id=user_id, limit=200)
    domains = {key: str(goal.get('domain') or 'general') for key, goal in
        episode_goals(selected, user_id=user_id, goal_reader=goal_reader).items()}
    buckets: dict[str, list[dict]] = {}
    for episode in selected:
        if episode.get('status') not in _SETTLED or episode.get('actuals_complete') is False:
            continue
        domain = domains.get(episode.get('goal_id'))
        if domain is None:
            continue
        buckets.setdefault(domain, []).append(episode)
    if len(buckets) > MAX_DOMAINS:
        kept = sorted(buckets, key=lambda d: len(buckets[d]),
                      reverse=True)[:MAX_DOMAINS]
        buckets = {d: buckets[d] for d in kept}
    records = []
    all_samples = [s for bucket in buckets.values() for s in bucket]
    for domain, samples in sorted(buckets.items()):
        records.append(_aggregate(samples, domain, ts))
    records.append(_aggregate(all_samples, GLOBAL_DOMAIN, ts))
    for record in records:
        put_record(NAMESPACE_COST_PRIORS,
                   entity_key(record['domain'], user_id, kind='cost prior'),
                   record, kind='cost prior')
    return records


def _prior(domain: str, *, user_id) -> dict | None:
    return get_record(NAMESPACE_COST_PRIORS,
                      entity_key(domain, user_id, kind='cost prior'))


def _estimate_from_prior(prior: dict, priors_source: str) -> dict:
    return {
        'expected_tokens': prior['tokens_median'],
        'expected_wall_clock_seconds': prior['wall_clock_median'],
        'expected_tool_calls': prior['tool_calls_median'],
        'failure_rate_prior': prior['failure_rate'],
        'priors_source': priors_source,
    }


def estimate_cost(domain: str, *, user_id,
                  budget_envelope: dict | None = None) -> dict:
    """Structured cost estimate for one candidate path.

    Covers the measurable half of the plan contract's cost_estimate; the
    planner adds expected_information_gain and expected_utility itself.
    """
    domain_prior = _prior(str(domain or 'general'), user_id=user_id)
    if domain_prior and domain_prior.get('samples', 0) >= MIN_SAMPLES:
        return _estimate_from_prior(domain_prior, 'measured_history')
    global_prior = _prior(GLOBAL_DOMAIN, user_id=user_id)
    if global_prior and global_prior.get('samples', 0) >= MIN_SAMPLES:
        return _estimate_from_prior(global_prior, 'domain_default')
    envelope = budget_envelope or {}
    return {
        'expected_tokens': int(envelope.get('max_tokens') or 0),
        'expected_wall_clock_seconds': int(
            envelope.get('max_wall_clock_seconds') or 0),
        'expected_tool_calls': int(envelope.get('max_tool_calls') or 0),
        'failure_rate_prior': 0.5,
        'priors_source': 'exploratory_flat',
    }


def list_priors(*, user_id, limit: int = 65) -> list[dict]:
    return list_records(NAMESPACE_COST_PRIORS, user_id, kind='cost prior',
                        limit=limit)
