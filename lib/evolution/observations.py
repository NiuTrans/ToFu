"""Observation ledger: recorded noticings with zero learning rights.

Responsibility: give the loop's unavoidable by-products — anomalies, premise
objections, consumer friction, edge cases, mined friction, reindex receipts —
a first-class durable carrier, so they survive settlement compression instead
of dying in task transcripts. Entry points: ``record_observation`` /
``list_observations`` / ``cluster_observations`` / ``set_observation_status``.
Dependencies: contracts and records only; no model, no task engine.

Discipline, pinned by tests:

- Observations never affect verdicts, strategies, mutations, planning or
  settlement. They feed the frontier digest and the owner, never behavior.
- Recording is idempotent under a deterministic id; a full ledger refuses
  new records — user state is never evicted to make room.
- Clustering is computed at read time; records themselves never mutate into
  a "clustered" state.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import time

from .contracts import (
    EvolutionStateError,
    validate_observation,
)
from .records import (
    NAMESPACE_OBSERVATIONS,
    entity_key,
    get_record,
    list_records,
    put_record,
)

MAX_OBSERVATIONS = 256


def observation_id(kind: str, subject: dict, statement: str, *,
                   salt: str = '') -> str:
    """Deterministic id, so re-mining and retries never duplicate records."""
    digest = hashlib.sha256('|'.join([
        kind, str((subject or {}).get('type') or ''),
        str((subject or {}).get('id') or ''), salt, statement,
    ]).encode('utf-8')).hexdigest()[:16]
    return f'obs-{digest}'


def record_observation(payload: dict, *, user_id,
                       now: int | None = None) -> dict:
    """Validate and persist one noticing; identical re-record is a no-op."""
    record = validate_observation(deepcopy(payload))
    ts = int(now if now is not None else time.time())
    record.setdefault('created_at', ts)
    record['updated_at'] = ts
    key = entity_key(record['observation_id'], user_id, kind='observation')
    existing = get_record(NAMESPACE_OBSERVATIONS, key)
    if existing is not None:
        comparable = lambda r: {k: v for k, v in r.items()
                                if k not in ('updated_at',)}
        if comparable(existing) != comparable(record):
            raise EvolutionStateError(
                'observation id already belongs to different content')
        return existing
    if len(list_records(NAMESPACE_OBSERVATIONS, user_id, kind='observation',
                        limit=MAX_OBSERVATIONS)) >= MAX_OBSERVATIONS:
        raise EvolutionStateError(
            'observation ledger capacity reached; dismiss or promote first')
    put_record(NAMESPACE_OBSERVATIONS, key, record, kind='observation')
    return record


def list_observations(*, user_id, kind: str | None = None,
                      status: str | None = None, limit: int = 128) -> list[dict]:
    rows = list_records(NAMESPACE_OBSERVATIONS, user_id, kind='observation',
                        limit=MAX_OBSERVATIONS)
    if kind:
        rows = [r for r in rows if r.get('kind') == kind]
    if status:
        rows = [r for r in rows if r.get('status') == status]
    rows.sort(key=lambda r: (-int(r.get('updated_at') or 0),
                             r['observation_id']))
    return rows[:max(1, min(int(limit or 128), MAX_OBSERVATIONS))]


def cluster_observations(*, user_id, kinds: tuple | None = None,
                         status: str = 'open') -> list[dict]:
    """Group same-signature noticings at read time, strongest first."""
    groups: dict[tuple, list] = {}
    for record in list_observations(user_id=user_id, status=status,
                                    limit=MAX_OBSERVATIONS):
        if kinds and record.get('kind') not in kinds:
            continue
        subject = record.get('subject') or {}
        key = (str(record.get('kind') or ''), str(subject.get('type') or ''),
               str(subject.get('id') or ''))
        groups.setdefault(key, []).append(record)
    clusters = []
    for (kind, subject_type, subject_id), records_ in groups.items():
        records_.sort(key=lambda r: (-int(r.get('updated_at') or 0),
                                     r['observation_id']))
        digest = hashlib.sha256(
            f'{kind}|{subject_type}|{subject_id}'.encode('utf-8')
        ).hexdigest()[:12]
        clusters.append({
            'cluster_id': f'cl-{digest}',
            'kind': kind,
            'subject': {'type': subject_type, 'id': subject_id},
            'count': len(records_),
            'observation_ids': [r['observation_id']
                                for r in records_[:8]],
            'statement': records_[0]['statement'],
            'latest_at': int(records_[0].get('updated_at') or 0),
        })
    clusters.sort(key=lambda c: (-c['count'], -c['latest_at'], c['cluster_id']))
    return clusters


def set_observation_status(obs_id: str, status: str, *, user_id,
                           now: int | None = None) -> dict:
    """Owner-driven park/promote bookkeeping; never a behavior change."""
    if status not in ('open', 'dismissed', 'promoted'):
        raise EvolutionStateError(f'unknown observation status {status!r}')
    key = entity_key(obs_id, user_id, kind='observation')
    record = get_record(NAMESPACE_OBSERVATIONS, key)
    if record is None:
        raise EvolutionStateError('unknown observation for this owner')
    record = dict(record, status=status,
                  updated_at=int(now if now is not None else time.time()))
    put_record(NAMESPACE_OBSERVATIONS, key, record, kind='observation')
    return record


def _frame_goal_id(record: dict) -> str:
    subject = record.get('subject') or {}
    digest = hashlib.sha256(
        f"frame|{record.get('kind')}|{subject.get('type')}|{subject.get('id')}"
        .encode('utf-8')).hexdigest()[:12]
    return f'frame-{digest}'


def promote_observation(obs_id: str, *, user_id, now: int | None = None) -> dict:
    """Turn an open noticing into one draft frame-change goal through the
    ordinary intake. Same-signature open siblings ride along and are marked
    promoted with it. Authorization stays with the owner: the draft only
    becomes work after the ordinary plan/confirm/launch path — promotion
    itself executes nothing.
    """
    from .goals import admit_goal, get_goal
    key = entity_key(obs_id, user_id, kind='observation')
    record = get_record(NAMESPACE_OBSERVATIONS, key)
    if record is None:
        raise EvolutionStateError('unknown observation for this owner')
    goal_id = _frame_goal_id(record)
    if record.get('status') == 'promoted':
        existing = get_goal(goal_id, user_id=user_id)
        if existing is not None:
            return {'goal': existing, 'promoted': [], 'already_promoted': True}
        raise EvolutionStateError(
            'observation was promoted but its goal is missing')
    ts = int(now if now is not None else time.time())
    from .curriculum import _FLOOR_ENVELOPE  # lazy: curriculum reads us
    goal = admit_goal({
        'schema_version': 1,
        'goal_id': goal_id,
        'statement': f"[frame-change] {record['statement']}"[:4000],
        'domain': 'general',
        'exploratory': True,
        'criteria': [],
        'budget_envelope': dict(_FLOOR_ENVELOPE),
        'status': 'draft',
        'created_at': ts,
    }, user_id=user_id, now=ts)
    subject = record.get('subject') or {}
    signature = (record.get('kind'), subject.get('type'), subject.get('id'))
    siblings = [r for r in list_observations(user_id=user_id, status='open',
                                             limit=MAX_OBSERVATIONS)
                if (r.get('kind'), (r.get('subject') or {}).get('type'),
                    (r.get('subject') or {}).get('id')) == signature]
    for sibling in siblings:
        set_observation_status(sibling['observation_id'], 'promoted',
                               user_id=user_id, now=ts)
    return {'goal': goal,
            'promoted': [s['observation_id'] for s in siblings]}


def propose_frame_changes(*, user_id, now: int | None = None,
                          limit: int = 1) -> list:
    """Maintenance pass: auto-promote corroborated premise objections
    (cluster count >= 2) into draft frame-change goals. Singletons stay
    visible in the digest; only corroboration earns a proposal.
    """
    proposed = []
    for cluster in cluster_observations(user_id=user_id,
                                        kinds=('premise_objection',)):
        if int(cluster.get('count') or 0) < 2:
            continue
        try:
            result = promote_observation(cluster['observation_ids'][0],
                                         user_id=user_id, now=now)
        except EvolutionStateError:
            continue
        if not result.get('already_promoted'):
            proposed.append(result['goal']['goal_id'])
        if len(proposed) >= max(1, int(limit or 1)):
            break
    return proposed
