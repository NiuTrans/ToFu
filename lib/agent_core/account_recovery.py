"""Rebind prepaid actor prefixes on the existing shared ExecutionBudget.

These ports change accounting only. The application recovery owner must prove
accepted source/successor lineage, native versions and original wallet debt.
No lease, child lifecycle, tool result, price calculation or store lives here.
"""
from copy import deepcopy
from lib.agent_core.account_frontier import validate_frontier
from lib.cost import observed_usage_totals


def pending_actor_tools(budget, state, *, task, owner, source_task_id, count, counter_policy):
    """Validate the untouched acknowledged actor prefix; caller holds its lock.

    Native paid snapshots can precede the Goal's tool charges. Domain code must
    prove the complete original batch before consuming this accounting view.
    """
    if (type(count) is not int or count <= 0
            or budget.recovered_actor_prefix_tokens(task, owner=owner) is None
            or (budget._rebound_actor_sources or {}).get(task['id']) != source_task_id
            or callable(budget._tool_observer)):
        raise ValueError('Pending actor tools require their acknowledged unobserved source')
    original = budget._replayed_actor_frontier
    prefix = original['actors'][source_task_id]
    current = budget.capture_actor_account(owner=owner, task=task, counter_policy=counter_policy)
    if any(current[key] != prefix[key] for key in ('tokens', 'tool_calls', 'model_ordinal', 'tool_ordinal')):
        raise ValueError('Pending actor tools cannot rewind new spending')
    charged = prefix['tool_calls'] - state['tool_calls']
    if (charged not in (0, count) or state['control_tool_calls'] != state['tool_calls']
            or dict(state, tool_calls=current['tool_calls'], control_tool_calls=current['control_tool_calls']) != current):
        raise ValueError('Pending actor tool batch differs from its original account')
    return dict(source_root_task_id=original['snapshot']['root_task_id'], charged_tool_calls=charged)


def reconcile_actor_tools(budget, state, *, task, owner, source_task_id, count):
    """Account only proven unpaid calls; existing Goal cuts own durability."""
    proof = pending_actor_tools(budget, state, task=task, owner=owner,
        source_task_id=source_task_id, count=count, counter_policy=state['counter_policy'])
    unpaid = count - proof['charged_tool_calls']
    if not unpaid:
        return
    cap = budget.limits['max_tool_calls']
    actor_cap = (task.get('config', {}).get('executionLimits') or {}).get('max_tool_calls')
    if ((cap is not None and budget._tool_calls + unpaid > cap)
            or (actor_cap is not None and budget._control_tool_calls[task['id']] + unpaid > actor_cap)):
        raise ValueError('Reconciled actor tools exceed their original budget')
    budget._tool_calls += unpaid
    budget._control_tool_calls[task['id']] += unpaid
    task['_executionToolsAdmitted'] = budget._control_tool_calls[task['id']]
    budget._publish_account_frontier('tools_reconciled')

def _owner(budget, task, owner):
    if (type(owner) is not int or owner < 1 or task.get('_userId') != owner
            or task.get('_executionBudget') is not budget
            or budget._active.get(task.get('id')) is not task
            or not budget.authorizes_model_owner(task, owner)):
        raise ValueError('Recovered actor prefix requires its exact owned account')


def restore_pending_frontier(budget, task, frontier, *, owner):
    candidate = validate_frontier(frontier)
    snapshot = candidate['snapshot']; source_root = snapshot['root_task_id']
    pending = {key: row for key, row in candidate['actors'].items() if key != source_root}
    with budget._lock:
        _owner(budget, task, owner)
        if (task['id'] != budget.root_id or budget.root_id in candidate['actors']
                or snapshot['owner_user_id'] != owner or snapshot['limits'] != budget.limits
                or snapshot['complete'] or not snapshot['settled'] or not snapshot['usage_known']
                or candidate['model_requests'] or not pending
                or candidate.get('recovery', {}).get('pending_actors')
                or any(row['parent_task_id'] != source_root for row in pending.values())):
            raise ValueError('Pending recovery requires one settled direct actor frontier')
        if budget._replayed_actor_frontier is not None:
            if budget._replayed_actor_frontier != candidate:
                raise ValueError('Recovered actor frontier changed')
            return
        if budget._account_frontier_observer is not None:
            raise ValueError('Pending account restoration must precede durable observation')
        # No old aggregate display may pin max(usage, model_tokens) while paid
        # descendant consumption is moved out of the root's historical bucket.
        displayed, _ = observed_usage_totals(task.get('usage'))
        if displayed > snapshot['root_tokens']:
            raise ValueError('Pending recovery root includes descendant display usage')
        pending = deepcopy(pending)
        rebound = {}
        budget._restore_spent_prefix(task, snapshot)
        budget._control_tool_calls[budget.root_id] -= sum(row['tool_calls'] for row in pending.values())
        budget._replayed_actor_frontier = candidate
        budget._pending_actor_prefixes = pending
        budget._rebound_actor_sources = rebound


def _prepare_actor_prefix(budget, task, *, source_task_id, owner):
    """Caller holds the account lock; validation does not mutate any counter."""
    _owner(budget, task, owner)
    identity = task['id']; source = budget._replayed_actor_frontier
    if (source is None or not isinstance(source_task_id, str)
            or identity == budget.root_id or identity in source['actors']
            or task.get('_executionBudgetControlTaskId', identity) != identity
            or task.get('_parentTaskId') != budget.root_id
            or budget._attachment_parents.get(identity) != budget.root_id
            or budget._account_frontier_failed or budget._account_frontier_publishing):
        raise ValueError('Actor prefix has no accepted direct recovery binding')
    bound = budget._rebound_actor_sources.get(identity)
    if bound is not None:
        if bound != source_task_id:
            raise ValueError('Actor prefix changed its source')
        return None  # Never rewind any new spending after the first ACK.
    prefix = budget._pending_actor_prefixes.get(source_task_id)
    displayed, _ = observed_usage_totals(task.get('usage'))
    if (prefix is None or displayed or budget._model_tokens[identity]
            or budget._actor_model_ordinals[identity] or budget._actor_tool_ordinals[identity]
            or budget._control_tool_calls[identity] or task.get('_executionToolsAdmitted')
            or task.get('_executionActualsComplete') is False
            or any(row['task']['id'] == identity for row in budget._model_leases.values())
            or any(row['control_task_id'] == identity for row in budget._tool_attempts.values())
            or budget._restored_descendant_tokens < prefix['tokens']
            or budget._model_tokens[budget.root_id] < prefix['tokens']):
        raise ValueError('Actor prefix requires an unused source and pristine recipient')
    return prefix


def _move_actor_prefix(budget, task, source_task_id, prefix):
    identity = task['id']
    # The global token/tool totals stay fixed. Only the actor-local slice
    # moves from prior-generation debt to its current registered recipient.
    budget._model_tokens[budget.root_id] -= prefix['tokens']
    budget._restored_descendant_tokens -= prefix['tokens']
    budget._model_tokens[identity] = prefix['tokens']
    budget._control_tool_calls[identity] = prefix['tool_calls']
    budget._actor_model_ordinals[identity] = prefix['model_ordinal']
    budget._actor_tool_ordinals[identity] = prefix['tool_ordinal']
    task['_executionToolsAdmitted'] = prefix['tool_calls']
    budget._rebound_actor_sources[identity] = source_task_id
    del budget._pending_actor_prefixes[source_task_id]


def rebind_actor_prefix(budget, task, *, source_task_id, owner):
    with budget._lock:
        prefix = _prepare_actor_prefix(budget, task, source_task_id=source_task_id, owner=owner)
        if prefix is None:
            return
        if budget._actor_rebind_observer is not None:
            raise ValueError('Atomic actor recovery requires the complete actor set')
        _move_actor_prefix(budget, task, source_task_id, prefix)
        # A failed ACK fences all subsequent admissions; paid counters and the
        # consumed source binding remain in place and cleanup can still join.
        budget._publish_account_frontier('actor_rebound', actor_rebinding=dict(
            source_task_id=source_task_id, target_task_id=task['id']))
