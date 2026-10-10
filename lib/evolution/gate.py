"""Evolution gate: every mutation is versioned, evidenced, confirmed, revertible.

Responsibility: the lifecycle of evolution mutations (Tier-1 scope:
prompt/routing/budget/retrieval/macro/skill payloads as structured records)
and the capability-gap queue distilled from episode settlements. Entry
points: ``propose_mutation`` / ``confirm_mutation`` / ``reject_mutation`` /
``activate_mutation`` / ``rollback_mutation`` / ``collect_capability_gaps`` /
``list_mutations`` / ``list_capability_gaps``. Dependencies: the episodes
store, the mutation/gap contracts, the records plumbing.

The gate's invariants, pinned by tests:

- No evidence, no mutation: ``evidence_episode_ids`` must reference settled
  episodes of the same owner.
- Confirmation is a distinct act; proposing never activates.
- Activation supersedes the kind's currently active mutation, which becomes
  ``rolled_back`` with its parent chain intact; rollback re-activates the
  parent when one exists.
- Sandbox validation and holdout regression (design doc tiers) plug in ahead
  of ``activate_mutation`` as preconditions; the gate already refuses
  activation from any status but ``confirmed``.
"""
from __future__ import annotations

import hashlib
import time
import uuid

from lib.storage.errors import StorageError

from .contracts import (
    EvolutionStateError,
    validate_capability_gap,
    validate_mutation,
    validate_episode,
)
from .episodes import list_episodes, get_episode, episode_goals, _check_verdict_consistency
from .records import (
    NAMESPACE_CAPABILITY_GAPS,
    NAMESPACE_MUTATIONS,
    entity_key,
    get_record,
    get_versioned_record,
    list_records,
    list_record_page,
    is_record_page_resource_refusal,
    put_record,
)

MAX_MUTATIONS = 128
MAX_GAPS = 64
_TRANSITIONS = {
    'proposed': ('confirmed', 'rejected'),
    'confirmed': ('active', 'rejected'),
    'active': ('rolled_back',),
    'rejected': (),
    'rolled_back': (),
}

#: Structural sandbox: payloads that cannot parse never reach confirmation.
_SANDBOX_RULES = {
    'prompt_variant': ('text', 8000),
    'routing_change': ('model', 200),
    'skill_candidate': ('name', 200),
    'budget_policy': ('policy', 2000),
    'retrieval_policy': ('policy', 2000),
    'macro_tool': ('sequence', 16),
}


def sandbox_validate(kind: str, payload: dict) -> None:
    """Deterministic structural validation, run before confirmation."""
    if kind == 'flow_template':
        from .flow_templates import validate_flow_payload
        validate_flow_payload(payload)
        return
    rule = _SANDBOX_RULES.get(kind)
    if rule is None:
        raise EvolutionStateError(f'no sandbox rule for mutation kind {kind!r}')
    field, bound = rule
    value = (payload or {}).get(field)
    if kind == 'macro_tool':
        if (not isinstance(value, list) or not 1 <= len(value) <= bound
                or not all(isinstance(item, str) and item for item in value)):
            raise EvolutionStateError(
                f'macro_tool payload.sequence must be 1..{bound} tool names')
    elif kind == 'budget_policy':
        if not isinstance(value, str) or len(value) > bound:
            raise EvolutionStateError(
                f'budget_policy payload.policy must be ≤ {bound} chars')
        from .gate_policy import parse_policy_overrides
        parse_policy_overrides(payload)
    elif not isinstance(value, str) or not value.strip() or len(value) > bound:
        raise EvolutionStateError(
            f'{kind} payload.{field} must be a nonempty string ≤ {bound} chars')


def _mutation_key(mutation_id: str, user_id) -> str:
    return entity_key(mutation_id, user_id, kind='mutation')


def _load(mutation_id: str, *, user_id) -> dict:
    record = get_record(NAMESPACE_MUTATIONS, _mutation_key(mutation_id, user_id))
    if record is None:
        raise EvolutionStateError('unknown mutation for this owner')
    return record


def get_mutation(mutation_id: str, *, user_id) -> dict:
    """Read one original owned mutation independently of the public list window."""
    value = validate_mutation(_load(mutation_id, user_id=user_id))
    if value['mutation_id'] != mutation_id:
        raise StorageError('database_integrity', 'Mutation identity differs from its key')
    return value


def _save(record: dict, *, user_id, create=False, expected_version=None) -> dict:
    key = _mutation_key(record['mutation_id'], user_id)
    try:
        acknowledged = put_record(NAMESPACE_MUTATIONS, key, record, kind='mutation',
            expected_version=0 if create else expected_version)
        if (create or expected_version is not None) and acknowledged != (1 if create else expected_version + 1):
            raise StorageError('database_internal', 'Mutation write ACK differs')
    except StorageError:
        if not create and expected_version is None:
            raise
        original, version = get_versioned_record(NAMESPACE_MUTATIONS, key)
        if original != record or version != (1 if create else expected_version + 1):
            raise
    return record



def iter_mutation_history(*, user_id, kind=None):
    """Validate complete owner history in bounded pages; never infer from a cap."""
    cursor = ''
    page_size = MAX_MUTATIONS
    while True:
        try:
            page = list_record_page(NAMESPACE_MUTATIONS, user_id, kind='mutation',
                limit=page_size, after_key=cursor)
        except StorageError as exc:
            if not is_record_page_resource_refusal(exc) or page_size == 1:
                raise
            page_size = max(1, page_size // 2)
            continue
        for row in page:
            try:
                value = validate_mutation(row['value'])
                if _mutation_key(value['mutation_id'], user_id) != row['key']:
                    raise ValueError('mutation identity differs from its key')
            except ValueError as exc:
                raise StorageError('database_integrity', 'Invalid mutation history original') from exc
            if kind is None or value['kind'] == kind:
                yield value
        if len(page) < page_size:
            break
        cursor = page[-1]['key']


def _history_summary(*, user_id, kind, parent_version=None):
    maximum = 0
    active = []
    parents = []
    for value in iter_mutation_history(user_id=user_id, kind=kind):
        maximum = max(maximum, value['version'])
        if value['status'] == 'active':
            active.append(value)
            if len(active) > 1:
                raise EvolutionStateError('mutation history has ambiguous active versions')
        if parent_version and value['version'] == parent_version:
            parents.append(value)
            if len(parents) > 1:
                raise EvolutionStateError('mutation history has ambiguous parent versions')
    return maximum, active[0] if active else None, parents[0] if parents else None


def propose_mutation(kind: str, payload: dict, evidence_episode_ids: list[str],
                     *, user_id, now: int | None = None, episode_reader=None) -> dict:
    # Sampling windows rank candidates; original references authorize proposals.
    del episode_reader
    if not evidence_episode_ids:
        raise EvolutionStateError('mutation needs settled-episode evidence; unknown: []')
    ts = int(now if now is not None else time.time())
    record = validate_mutation({
        'schema_version': 1, 'mutation_id': f'mut-{uuid.uuid4().hex[:16]}',
        'kind': kind, 'payload': payload or {}, 'evidence_episode_ids': evidence_episode_ids,
        'status': 'proposed', 'version': 1, 'parent_version': 0,
        'requires_confirmation': True, 'created_at': ts, 'updated_at': ts,
    })
    originals = []
    for episode_id in dict.fromkeys(record['evidence_episode_ids']):
        try:
            episode = get_episode(episode_id, user_id=user_id)
        except ValueError:
            episode = None
        if episode is None:
            continue
        try:
            validate_episode(episode)
            if episode['episode_id'] != episode_id:
                raise ValueError('episode identity differs from its reference')
        except ValueError as exc:
            raise StorageError('database_integrity', 'Invalid mutation evidence original') from exc
        if (episode['status'] in ('success', 'partial', 'failure')
                and not episode['exploratory'] and episode.get('verdict')
                and type(episode.get('settled_at')) is int):
            _check_verdict_consistency(episode)
            originals.append(episode)
    parents = episode_goals(originals, user_id=user_id)
    settled = {episode['episode_id'] for episode in originals
        if episode['goal_id'] in parents and not parents[episode['goal_id']]['exploratory']}
    missing = [eid for eid in record['evidence_episode_ids'] if eid not in settled]
    if missing:
        raise EvolutionStateError(f'mutation needs settled-episode evidence; unknown: {missing[:4]}')
    maximum, parent, _ = _history_summary(user_id=user_id, kind=kind)
    record['parent_version'] = parent['version'] if parent else 0
    from .mutation_versions import reserve
    record['version'] = reserve(record,user_id=user_id,historical_maximum=maximum)
    validate_mutation(record)
    return _save(record, user_id=user_id, create=True)


def _transition(mutation_id: str, target: str, *, user_id,
                rejection_reason: str | None = None,
                now: int | None = None) -> dict:
    record, physical_version = get_versioned_record(NAMESPACE_MUTATIONS, _mutation_key(mutation_id, user_id))
    if record is None:
        raise EvolutionStateError('unknown mutation for this owner')
    validate_mutation(record)
    if record['mutation_id'] != mutation_id:
        raise EvolutionStateError('mutation identity differs from its key')
    from .comparison import paired_context_fingerprint
    intent = record.get('activation_intent')
    if intent is not None and intent['context_fingerprint'] != paired_context_fingerprint(record):
        raise EvolutionStateError('admitted policy controls changed; activation refused')
    current = record['status']
    if target != 'active' and 'activation_intent' in record:
        raise EvolutionStateError('policy activation is already admitted; finish it before rollback')
    if target not in _TRANSITIONS[current]:
        raise EvolutionStateError(
            f'mutation cannot move {current} → {target}')
    record = dict(record)
    record['status'] = target
    record['updated_at'] = intent['started_at'] if intent else int(now if now is not None else time.time())
    if rejection_reason is not None:
        record['rejection_reason'] = rejection_reason[:500]
    if target == 'active':
        _, previous, _ = _history_summary(user_id=user_id, kind=record['kind'])
        if previous and previous['mutation_id'] != record['mutation_id']:
            if previous['version'] != record['parent_version']:
                raise EvolutionStateError('mutation baseline changed; propose and verify a new candidate')
        elif record['parent_version']:
            from .policy_transitions import applied
            if record['kind']!='budget_policy' or not applied(record,user_id=user_id):
                raise EvolutionStateError('mutation activation parent is unavailable')
        if record['kind'] == 'budget_policy':
            if 'activation_intent' not in record:
                admitted = validate_mutation(dict(record, status='confirmed',
                    activation_intent={'started_at':record['updated_at'],
                        'context_fingerprint':paired_context_fingerprint(record)}))
                _save(admitted, user_id=user_id, expected_version=physical_version)
                physical_version += 1
            record.pop('activation_intent', None)
            from .gate_policy import apply_gate_policy
            apply_gate_policy(record, user_id=user_id,
                              now=record['updated_at'],
                              expected_parent=previous['mutation_id'] if previous else None)
        if record['kind'] == 'flow_template':
            from .flow_templates import materialize_flow_template
            materialize_flow_template(record, user_id=user_id)
        if previous and previous['mutation_id'] != record['mutation_id']:
            superseded = dict(previous, status='rolled_back', updated_at=record['updated_at'])
            _save(validate_mutation(superseded), user_id=user_id)
    return _save(validate_mutation(record), user_id=user_id,
        expected_version=physical_version if target in ('confirmed', 'rejected') or record['kind']=='budget_policy' else None)


def confirm_mutation(mutation_id: str, *, user_id, now: int | None = None) -> dict:
    record = _load(mutation_id, user_id=user_id)
    sandbox_validate(record['kind'], record.get('payload') or {})
    return _transition(mutation_id, 'confirmed', user_id=user_id, now=now)


def reject_mutation(mutation_id: str, *, user_id, reason: str = '',
                    now: int | None = None) -> dict:
    return _transition(mutation_id, 'rejected', user_id=user_id,
                       rejection_reason=reason or 'rejected at the gate', now=now)


def activate_mutation(mutation_id: str, *, user_id, now: int | None = None,
                      allow_unmeasurable: bool = False) -> dict:
    """Activation requires a regression receipt: ``fail`` blocks outright;
    ``unmeasurable`` blocks unless the operator explicitly waives."""
    original = validate_mutation(_load(mutation_id, user_id=user_id))
    if 'activation_intent' in original:
        return _transition(mutation_id, 'active', user_id=user_id, now=now)
    from .holdout import latest_regression, regression_suite, suite_fingerprint, verify_paired_regression
    from .contracts import validate_regression_receipt
    receipt = latest_regression(mutation_id, user_id=user_id)
    if receipt is None:
        raise EvolutionStateError(
            'activation requires a holdout regression receipt first')
    try:validate_regression_receipt(receipt)
    except ValueError as exc:
        raise EvolutionStateError('invalid regression receipt; rerun before activation') from exc
    mutation = _load(mutation_id, user_id=user_id)
    if receipt.get('measurement') == 'paired':
        if receipt['created_at'] > int(time.time() if now is None else now):
            raise EvolutionStateError('paired regression is from the future; rerun before activation')
        verify_paired_regression(mutation, receipt,
            regression_suite(mutation,user_id=user_id,paired=True))
    from .comparison import mutation_fingerprint
    if receipt.get('mutation_fingerprint') != mutation_fingerprint(
            _load(mutation_id, user_id=user_id)):
        raise EvolutionStateError('regression is stale or unbound; rerun before activation')
    if receipt.get('suite_fingerprint') != suite_fingerprint(regression_suite(
            _load(mutation_id, user_id=user_id), user_id=user_id,
            paired=receipt.get('measurement') == 'paired')):
        raise EvolutionStateError('regression suite changed; rerun before activation')
    if receipt.get('verdict') == 'fail':
        raise EvolutionStateError(
            'holdout regression failed; the mutation stays unactivated')
    if (receipt.get('verdict') == 'unmeasurable'
            and not allow_unmeasurable):
        raise EvolutionStateError(
            'regression could not measure this kind; activate with an '
            'explicit allow_unmeasurable waiver')
    return _transition(mutation_id, 'active', user_id=user_id, now=now)


def recover_policy_activations(*, user_id) -> list[str]:
    """Finish already admitted policy intents through the existing maintenance loop.

    This does not admit proposals or launch regression probes. Original mutation
    history is scanned in bounded pages; errors remain visible to maintenance.
    """
    completed = []
    for mutation in iter_mutation_history(user_id=user_id, kind='budget_policy'):
        if 'activation_intent' in mutation:
            activate_mutation(mutation['mutation_id'], user_id=user_id)
            completed.append(mutation['mutation_id'])
    return completed


def rollback_mutation(mutation_id: str, *, user_id, now: int | None = None) -> dict:
    original = _load(mutation_id,user_id=user_id)
    if original['status'] not in ('active','rolled_back'):
        raise EvolutionStateError('rollback requires an active mutation or its interrupted rollback')
    _, active, parent = _history_summary(user_id=user_id, kind=original['kind'],
        parent_version=original['parent_version'])
    if active and active['mutation_id'] not in (original['mutation_id'], parent['mutation_id'] if parent else ''):
        raise EvolutionStateError('another mutation owns this kind; rollback refused')
    if original['parent_version'] and (parent is None or parent['status'] not in ('rolled_back','active')):
        raise EvolutionStateError('rollback parent is unavailable')
    stamp = original['updated_at'] if original['status']=='rolled_back' else int(now if now is not None else time.time())
    if original['kind']=='budget_policy':
        from .policy_transitions import restore
        restored_policy=restore(original,user_id=user_id,now=stamp)
        stamp=restored_policy['updated_at']
    record=validate_mutation(dict(original,status='rolled_back',updated_at=stamp))
    if original['status']!='rolled_back':
        try:_save(record,user_id=user_id)
        except StorageError:
            if _load(mutation_id,user_id=user_id)!=record:raise
    if parent and parent['status']=='rolled_back':
        restored=validate_mutation(dict(parent,status='active',updated_at=stamp))
        try:_save(restored,user_id=user_id)
        except StorageError:
            if _load(parent['mutation_id'],user_id=user_id)!=restored:raise
    return record


def list_mutations(*, user_id, kind: str | None = None) -> list[dict]:
    # All active kinds remain visible; remaining metadata favors later versions.
    import heapq
    mutations = heapq.nlargest(MAX_MUTATIONS,
        iter_mutation_history(user_id=user_id, kind=kind),
        key=lambda value: (value['status'] == 'active', value['updated_at'], value['version'], value['mutation_id']))
    return sorted(mutations, key=lambda m: (m['kind'], -m['version']))


def collect_capability_gaps(*, user_id, now: int | None = None, episode_reader=None) -> list[dict]:
    """Fold episode-reported gaps into the deduplicated proposal queue."""
    ts = int(now if now is not None else time.time())
    queue_by_id = {g['gap_id']: g
                   for g in list_records(NAMESPACE_CAPABILITY_GAPS, user_id,
                                         kind='capability gap', limit=MAX_GAPS)}
    for episode in (episode_reader or list_episodes)(user_id=user_id, limit=200):
        gap = ((episode.get('learning') or {}).get('capability_gap') or {})
        summary = str(gap.get('summary') or '').strip()
        if not summary:
            continue
        digest = hashlib.sha256(summary[:200].encode('utf-8')).hexdigest()[:12]
        gap_id = f'gap-{digest}'
        if gap_id in queue_by_id:
            record = dict(queue_by_id[gap_id])
            lineage = list(record.get('lineage') or [])
            if episode['episode_id'] not in lineage:
                lineage.append(episode['episode_id'])
            record.update({'lineage': lineage[:64], 'samples': len(lineage),
                           'updated_at': ts})
        else:
            record = {
                'schema_version': 1,
                'gap_id': gap_id,
                'summary': summary[:1000],
                'proposed_action': str(gap.get('proposed_action') or '')[:1000],
                'status': 'proposed',
                'lineage': [episode['episode_id']],
                'samples': 1,
                'created_at': ts,
                'updated_at': ts,
            }
        queue_by_id[gap_id] = validate_capability_gap(record)
    queue = list(queue_by_id.values())[:MAX_GAPS]
    for record in queue:
        put_record(NAMESPACE_CAPABILITY_GAPS,
                   entity_key(record['gap_id'], user_id, kind='capability gap'),
                   record, kind='capability gap')
    return queue


def list_capability_gaps(*, user_id, status: str | None = None) -> list[dict]:
    gaps = list_records(NAMESPACE_CAPABILITY_GAPS, user_id,
                        kind='capability gap', limit=MAX_GAPS)
    if status:
        gaps = [g for g in gaps if g.get('status') == status]
    return gaps
