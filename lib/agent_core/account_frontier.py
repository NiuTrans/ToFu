"""Whole shared-account observations, independent of stores and effect recovery.

Entry points: capture_frontier (budget lock held), validate_frontier. The budget
owns all counters and membership; the durable caller owns accepted lineage.
Closed participants collapse into scalar prefixes. Recovery retains one frozen
source cut and a shrinking set of unbound paid actors, never unbounded history.
"""
from copy import deepcopy
from functools import lru_cache
from itertools import chain

from lib.agent_core.budget_snapshot import validate_snapshot
from lib.agent_core.account_frontier_contract import FORMAT, SCHEMA


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    Draft202012Validator.check_schema(SCHEMA)
    return Draft202012Validator(SCHEMA)


def _validate_relationships(value, snapshot):
    actors = value['actors']
    root = snapshot['root_task_id']
    recovery = value.get('recovery')
    pending = recovery['pending_actors'] if recovery is not None else {}
    if recovery is not None and (recovery['source_root_task_id'] == root
            or recovery['source_root_task_id'] in pending or set(pending) & set(actors)
            or any(row['parent_task_id'] != recovery['source_root_task_id'] for row in pending.values())):
        raise ValueError('Execution account recovery membership disagrees')
    # Producer fields come from the budget. Derive scalar bounds from the
    # wire authority, so hot membership cuts need not walk a JSON Schema tree.
    identity = SCHEMA['properties']['actors']['propertyNames']
    fields = SCHEMA['$defs']['actor']['properties']
    for actor_id, row in chain(actors.items(), pending.items()):
        if (not isinstance(actor_id, str) or not identity['minLength'] <= len(actor_id) <= identity['maxLength']
                or not isinstance(row['parent_task_id'], str)
                or len(row['parent_task_id']) > fields['parent_task_id']['maxLength']
                or any(type(row[key]) is not int or not fields[key]['minimum'] <= row[key] <= fields[key]['maximum']
                    for key in ('tokens','tool_calls','model_ordinal','tool_ordinal'))):
            raise ValueError('Execution account actor identity or counters are invalid')
    binding = value.get('actor_rebinding')
    if (value['reason'] == 'actor_rebound') != (binding is not None):
        raise ValueError('Actor rebind requires its exact source and recipient')
    if binding is not None and (recovery is None or set(binding) != {'source_task_id', 'target_task_id'}
            or binding['source_task_id'] in pending or binding['target_task_id'] not in actors
            or binding['target_task_id'] == root):
        raise ValueError('Actor rebind recipient is not an active recovered child')
    if (root not in actors or actors[root]['parent_task_id'] != ''
            or actors[root]['tokens'] != snapshot['root_tokens']
            or sum(row['tokens'] for row in chain(actors.values(), pending.values())) + value['closed_tokens'] != snapshot['tokens']
            or sum(row['tool_calls'] for row in chain(actors.values(), pending.values())) + value['closed_tool_calls'] != snapshot['tool_calls']
            or value['closed_tokens'] < 0 or value['closed_tool_calls'] < 0
            or any(row['model_ordinal'] > snapshot['model_ordinal']
                or row['tool_ordinal'] > snapshot['tool_ordinal'] for row in chain(actors.values(), pending.values()))
            or any(row['attempt_ordinal'] > snapshot['model_ordinal'] for row in value['model_requests'])
            or len({row['attempt_ordinal'] for row in value['model_requests']}) != len(value['model_requests'])
            or (snapshot['settled'] and value['model_requests'])
            or (snapshot['complete'] and (set(actors) != {root} or pending))):
        raise ValueError('Execution account frontier counters or membership disagree')


def validate_frontier(value):
    try:
        _validator().validate(value)
        snapshot = validate_snapshot(value['snapshot'])
        _validate_relationships(value, snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Execution account frontier is invalid') from exc
    except Exception as exc:
        raise ValueError('Execution account frontier violates its schema') from exc
    return deepcopy(value)


def capture_frontier(budget, *, version, reason, actor_rebinding=None):
    """Caller holds the existing budget lock; observe without resolving debt."""
    if (type(version) is not int or not 1 <= version <= SCHEMA['properties']['version']['maximum']
            or reason not in SCHEMA['properties']['reason']['enum']):
        raise ValueError('Execution account frontier version or reason is invalid')
    root = budget._active[budget.root_id]
    snapshot = budget.capture_replay_snapshot(root, owner=root['_userId'])
    if any(type(task.get('_userId')) is not int or task['_userId'] != snapshot['owner_user_id']
            or task.get('_executionBudget') is not budget or task.get('id') != task_id
            for task_id, task in budget._active.items()):
        raise ValueError('Execution account frontier contains a foreign participant')
    actors = {task_id: dict(parent_task_id=budget._attachment_parents.get(task_id, ''),
        tokens=snapshot['root_tokens'] if task_id == budget.root_id else budget._model_actor_tokens(task),
        tool_calls=budget._control_tool_calls[task_id],
        model_ordinal=budget._actor_model_ordinals[task_id],
        tool_ordinal=budget._actor_tool_ordinals[task_id]) for task_id, task in budget._active.items()}
    pending = budget._pending_actor_prefixes or {}
    value = dict(format=FORMAT, version=version, reason=reason,
        snapshot=snapshot, actors=actors, child_ordinal=budget._child_ordinal,
        closed_tokens=snapshot['tokens']-sum(row['tokens'] for row in chain(actors.values(), pending.values())),
        closed_tool_calls=snapshot['tool_calls']-sum(row['tool_calls'] for row in chain(actors.values(), pending.values())),
        model_requests=budget.continuity_snapshot()['model_attempts'])
    if budget._replayed_actor_frontier is not None:
        source = budget._replayed_actor_frontier
        value['recovery'] = dict(source_root_task_id=source['snapshot']['root_task_id'],
            source_frontier_version=source['version'], pending_actors=deepcopy(pending))
    if actor_rebinding is not None:
        value['actor_rebinding'] = deepcopy(actor_rebinding)
    # Budget-owned fields receive derived scalar/membership checks on every
    # cut. The full schema is checked at binding and on every durable ingress.
    # Rewalking static schema for all actors at each attach adds quadratic cost.
    _validate_relationships(value, snapshot)
    return validate_frontier(value) if reason == 'bound' else value


def validate_recovery_transition(previous, current):
    """Unbound paid actors cannot disappear under an unrelated account boundary."""
    before, after = previous.get('recovery'), current.get('recovery')
    if before is None and after is None:
        if current['reason'] == 'actor_rebound':
            raise ValueError('Actor rebind has no recovery source')
        return
    if (before is None or after is None
            or any(before[key] != after[key] for key in ('source_root_task_id', 'source_frontier_version'))):
        raise ValueError('Account recovery source changed within its generation')
    old, new = before['pending_actors'], after['pending_actors']
    removed = set(old) - set(new)
    if (set(new) - set(old) or any(new[key] != old[key] for key in new)
            or len(removed) != (1 if current['reason'] == 'actor_rebound' else 0)):
        raise ValueError('Pending actor prefixes changed without exact rebinding')
    if current['reason'] == 'actor_rebound':
        binding = current['actor_rebinding']; source = binding['source_task_id']; target = binding['target_task_id']
        counters = ('tokens', 'tool_calls', 'model_ordinal', 'tool_ordinal')
        before_actor = previous['actors'].get(target); after_actor = current['actors'].get(target)
        if (removed != {source} or before_actor is None or after_actor is None
                or any(before_actor[key] for key in counters)
                or after_actor != {**old[source], 'parent_task_id': current['snapshot']['root_task_id']}):
            raise ValueError('Paid actor prefix has no matching pristine recipient')
