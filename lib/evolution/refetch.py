"""On-demand reindex: resource gathering is a loop action, not intake-only.

Responsibility: let a goal gather fresh material *after* planning, the way a
person goes back for more sources when stuck — three lawful triggers, one
bounded core. Entry points: ``reindex_for_run`` / ``reindex_maintenance`` /
``REINDEX_TOOL_DEFINITION``. Dependencies: the indexers seam (``index_all``),
the material shelf, the observation ledger.

Triggers:

1. **Actor-initiated** — a bound executor/coordinator calls the
   ``evolution_reindex`` tool with refined queries and a reason.
2. **Settle-triggered** — a ``needs_attention`` run earns one bounded reindex
   derived from its objective and failed-criterion feedback, so the owner's
   follow-up starts with fresh ammunition.
3. **Freshness pass** — a terminal run whose goal has been stalled earns one
   re-query, landing newer sources on its shelf.

Discipline, pinned by tests:

- Reindex only adds untrusted reference material; the shelf invariant is
  untouched and plans/goals/criteria are never rewritten.
- Every reindex writes a ``reindex_note`` observation as its receipt (actor,
  queries, landed count); at most ``MAX_REFETCH_PER_RUN`` per run.
- Deterministic and idempotent: the same trigger never records twice.
"""
from __future__ import annotations

import hashlib
import time

from .contracts import EvolutionContractError, EvolutionStateError
from .indexers import (
    AcademicIndexer,
    HistoryIndexer,
    ProjectIndexer,
    index_all,
)
from .observations import list_observations, observation_id, record_observation
from .records import NAMESPACE_RUNS, list_records

MAX_REFETCH_QUERIES = 3
MAX_REFETCH_REFS = 8
MAX_REFETCH_PER_RUN = 3
FRESHNESS_PER_CYCLE = 1
SETTLE_REFETCH_PER_CYCLE = 1
FRESHNESS_MIN_AGE_S = 3 * 24 * 3600

_KIND_BY_INDEXER = {'academic': 'paper', 'project': 'repository'}

REINDEX_TOOL_DEFINITION = {
    'type': 'function',
    'function': {
        'name': 'evolution_reindex',
        'description': (
            'Gather fresh sources for the CURRENT goal when stuck: missing '
            'context, repeated truncation, or the plan needs newer material. '
            'Pass up to 3 refined queries informed by what you learned since '
            'planning, plus a short reason. Useful papers, repositories and '
            'web references land on the goal bookshelf as untrusted '
            'reference links. Bounded: at most 3 calls per run.'),
        'parameters': {
            'type': 'object',
            'required': ['queries', 'reason'],
            'properties': {
                'queries': {
                    'type': 'array', 'minItems': 1,
                    'maxItems': MAX_REFETCH_QUERIES,
                    'items': {'type': 'string', 'minLength': 2,
                              'maxLength': 200}},
                'reason': {'type': 'string', 'minLength': 1,
                           'maxLength': 300},
            },
            'additionalProperties': False,
        },
    },
}


def _default_indexers():
    return (HistoryIndexer(), ProjectIndexer(), AcademicIndexer())


def _reindex_notes(run_id: str, *, user_id) -> list[dict]:
    return [n for n in list_observations(user_id=user_id, kind='reindex_note',
                                         limit=256)
            if run_id in (n.get('lineage') or [])]


def _land_refs(run_id, refs, query, *, user_id, task_id) -> tuple[int, int]:
    """Shelf the URL-shaped refs; returns (saved, skipped). Fail-soft per
    material: a full shelf truncates the landing, never the pass."""
    from .library import save_material
    saved = skipped = 0
    for ref in refs:
        locator = str(ref.get('locator') or '')
        if not locator.startswith(('http://', 'https://')):
            skipped += 1  # history episode refs stay in their own store
            continue
        title = (str(ref.get('provenance') or 'reindexed source'))[:240]
        try:
            save_material(run_id, {
                'kind': _KIND_BY_INDEXER.get(str(ref.get('indexer') or ''),
                                             'other'),
                'title': title,
                'url': locator,
                'note': f"on-demand reindex for: {query}"[:500],
            }, user_id=user_id, task_id=task_id, origin='agent')
        except (EvolutionContractError, EvolutionStateError, ValueError,
                TypeError):
            skipped += 1
            continue
        saved += 1
    return saved, skipped


def reindex_for_run(run_id, queries, *, user_id, actor, task_id='',
                    reason='', indexers=None, now=None) -> dict:
    """The one bounded core every trigger funnels through."""
    from .runs import get_run
    run = get_run(run_id, user_id=user_id)
    if run is None:
        raise EvolutionStateError('run not found for this owner')
    ts = int(now if now is not None else time.time())
    clean = [' '.join(str(q or '').split())[:200]
             for q in (queries or [])][:MAX_REFETCH_QUERIES]
    clean = [q for q in clean if len(q) >= 2]
    if not clean:
        raise EvolutionContractError('reindex needs at least one query')
    notes = _reindex_notes(run_id, user_id=user_id)
    query_digest = hashlib.sha256('|'.join(clean).encode('utf-8')).hexdigest()[:12]
    salt = f'{actor}|{run_id}|{query_digest}'
    obs_id = observation_id('reindex_note', {'type': 'run', 'id': run_id},
                            'receipt', salt=salt)
    if any(o['observation_id'] == obs_id for o in notes):
        return {'run_id': run_id, 'queries': clean, 'saved': 0, 'skipped': 0,
                'already_done': True}
    if len(notes) >= MAX_REFETCH_PER_RUN:
        raise EvolutionStateError(
            'reindex budget for this run reached; use the materials already '
            'collected or start a follow-up goal')
    use_indexers = tuple(indexers) if indexers is not None else _default_indexers()
    saved = skipped = 0
    for query in clean:
        refs = index_all(query, use_indexers, user_id=user_id,
                         limit=MAX_REFETCH_REFS)
        landed, missed = _land_refs(run_id, refs, query, user_id=user_id,
                                    task_id=task_id)
        saved += landed
        skipped += missed
    episode_id = str(run.get('episode_id') or '')
    lineage = [run_id] + ([episode_id] if episode_id else [])
    record_observation({
        'schema_version': 1,
        'observation_id': obs_id,
        'kind': 'reindex_note',
        'statement': (f"on-demand reindex ({actor}) landed {saved} new "
                      f"source(s) for queries: {'; '.join(clean)}"
                      + (f" — reason: {' '.join(str(reason).split())[:200]}"
                         if reason else ''))[:1000],
        'subject': {'type': 'run', 'id': run_id},
        'lineage': lineage,
        'source': {'actor': actor, **({'task_id': task_id} if task_id else {})},
        'status': 'open',
        'created_at': ts,
        'updated_at': ts,
    }, user_id=user_id, now=ts)
    return {'run_id': run_id, 'queries': clean, 'saved': saved,
            'skipped': skipped, 'already_done': False}


def _terminal_runs(*, user_id, limit: int = 50) -> list[dict]:
    runs = list_records(NAMESPACE_RUNS, user_id, kind='run', limit=limit)
    return [r for r in runs
            if r.get('status') in ('completed', 'needs_attention', 'failed',
                                   'cancelled')]


def _settle_triggered(*, user_id, indexers, now: int) -> list[dict]:
    """One bounded reindex per needs_attention run, objective + failed
    criteria as the derived queries."""
    done = []
    for run in _terminal_runs(user_id=user_id):
        if run.get('status') != 'needs_attention':
            continue
        if _reindex_notes(run['run_id'], user_id=user_id):
            continue
        queries = [str(run.get('objective') or '')[:160]]
        for result in ((run.get('review') or {}).get('criterion_results')
                       or []):
            if result.get('outcome') in ('fail', 'unknown'):
                queries.append(str(result.get('evidence_ref') or '')[:160])
        try:
            done.append(reindex_for_run(
                run['run_id'], queries, user_id=user_id, actor='miner',
                indexers=indexers, now=now))
        except (EvolutionContractError, EvolutionStateError):
            continue
        if len(done) >= SETTLE_REFETCH_PER_CYCLE:
            break
    return done


def _freshness_pass(*, user_id, indexers, now: int) -> list[dict]:
    """One re-query per long-stalled terminal run per cycle."""
    done = []
    runs = sorted(_terminal_runs(user_id=user_id),
                  key=lambda r: (int(r.get('created_at') or 0), r['run_id']))
    for run in runs:
        if now - int(run.get('created_at') or 0) < FRESHNESS_MIN_AGE_S:
            continue
        if _reindex_notes(run['run_id'], user_id=user_id):
            continue
        try:
            done.append(reindex_for_run(
                run['run_id'], [str(run.get('objective') or '')[:160]],
                user_id=user_id, actor='miner', indexers=indexers, now=now))
        except (EvolutionContractError, EvolutionStateError):
            continue
        if len(done) >= FRESHNESS_PER_CYCLE:
            break
    return done


def reindex_maintenance(*, user_id, indexers=None,
                        now: int | None = None) -> dict:
    """Maintenance-cycle entry: settle-triggered, then freshness."""
    ts = int(now if now is not None else time.time())
    use = tuple(indexers) if indexers is not None else None
    settled = _settle_triggered(user_id=user_id, indexers=use, now=ts)
    fresh = _freshness_pass(user_id=user_id, indexers=use, now=ts)
    return {'settle_triggered': len(settled), 'freshness': len(fresh)}
