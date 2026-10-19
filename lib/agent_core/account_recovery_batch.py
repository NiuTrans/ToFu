"""One accounted transfer of a whole retired actor set, through a prebound sink.

Entry: rebind_all_actor_prefixes, delegated by ExecutionBudget. The ordinary
account owner supplies the lock, counters, capture and admission fences. The
application sink owns the atomic native handoff and durable Goal event writes.
No repository, actor lease or second account lifecycle exists here.
"""
import json

from lib.agent_core.account_frontier import capture_frontier
from lib.agent_core.account_recovery import _move_actor_prefix, _prepare_actor_prefix


def _charge_cut(cut, remaining):
    # Stream JSON sizing so a complete additional serialized copy is unnecessary.
    for part in json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(cut):
        remaining -= len(part.encode('utf-8'))
        if remaining < 0:
            raise ValueError('Atomic actor recovery evidence exceeds its byte allowance')
    return remaining


def _prepare_bindings(budget, bindings, *, owner, expected_observer):
    if ((expected_observer is not None and budget._actor_rebind_observer != expected_observer)
            or budget._actor_rebind_observer is None or budget._account_frontier_failed
            or budget._account_frontier_publishing or not isinstance(bindings, (list, tuple))
            or not bindings or budget._replayed_actor_frontier is None):
        raise ValueError('Atomic actor recovery requires its prebound durable sink')
    prepared = []
    identities, sources = set(), set()
    for row in bindings:
        if not isinstance(row, (list, tuple)) or len(row) != 2 or not isinstance(row[0], dict):
            raise ValueError('Atomic actor recovery requires task/source pairs')
        task, source = row
        prefix = _prepare_actor_prefix(budget, task, source_task_id=source, owner=owner)
        if task['id'] in identities or source in sources:
            raise ValueError('Atomic actor recovery has duplicate recipients or sources')
        identities.add(task['id']); sources.add(source)
        prepared.append((task, source, prefix))
    original = budget._replayed_actor_frontier
    expected = set(original['actors']) - {original['snapshot']['root_task_id']}
    if sources != expected:
        raise ValueError('Atomic actor recovery requires every original pending actor')
    if all(prefix is None for _, _, prefix in prepared):
        return prepared  # Exact acknowledged retries never rewind newly spent work.
    snapshot = original['snapshot']
    if (any(prefix is None for _, _, prefix in prepared)
            or set(budget._active) != identities | {budget.root_id}
            or budget._model_leases or budget._tool_attempts
            or budget.consumption() != (snapshot['tokens'], snapshot['tool_calls'])
            or budget._model_ordinal != snapshot['model_ordinal']
            or budget._tool_ordinal != snapshot['tool_ordinal']
            or sum(prefix['tokens'] for _, _, prefix in prepared) > budget._restored_descendant_tokens):
        raise ValueError('Atomic actor recovery requires an unused complete successor generation')
    return prepared


def validate_actor_recipients(budget, bindings, *, owner, expected_observer):
    """Read-only complete-set admission; the transfer repeats this same check."""
    with budget._lock:
        prepared = _prepare_bindings(budget, bindings, owner=owner, expected_observer=expected_observer)
        if any(prefix is None for _, _, prefix in prepared):
            raise ValueError("Actor recipient preparation requires unused pending prefixes")


def rebind_all_actor_prefixes(budget, bindings, *, owner, expected_observer=None):
    with budget._lock:
        prepared = _prepare_bindings(budget, bindings, owner=owner, expected_observer=expected_observer)
        if all(prefix is None for _, _, prefix in prepared):
            return
        cuts = []
        remaining = budget._actor_rebind_bytes
        try:
            for task, source, prefix in prepared:
                _move_actor_prefix(budget, task, source, prefix)
                budget._account_frontier_version += 1
                cut = capture_frontier(budget, version=budget._account_frontier_version,
                    reason='actor_rebound', actor_rebinding=dict(source_task_id=source, target_task_id=task['id']))
                remaining = _charge_cut(cut, remaining)
                cuts.append(cut)
            budget._account_frontier_publishing = True
            budget._actor_rebind_observer(tuple(cuts))
        except BaseException:
            # Delivery can be unknown. Preserve all charged counters, fence
            # execution and leave reconciliation to the durable composition.
            budget._account_frontier_failed = True
            budget._mark_observation_incomplete()
            raise
        finally:
            budget._account_frontier_publishing = False
