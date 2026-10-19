"""Goal composition for the shared budget's whole-account durable observations.

The existing journal owns writes/order and run lifecycle. These reads establish
spent consumption and live membership, never child completion, wallet settlement
or permission to restart a model/tool. No parallel account or storage is created.
"""
from copy import deepcopy

from lib.agent_core.account_frontier import FORMAT, validate_frontier, validate_recovery_transition
from lib.agent_core.budget_snapshot import validate_snapshot
from lib.orchestration.execution_journal import RecoveryRefused

EVENT = 'execution_account_frontier'


def install_account_journal(task, journal, metadata, *, native_handoff=None):
    from lib.agent_core.execution_budget import _ensure_execution_budget
    budget = _ensure_execution_budget(task)
    def observe(frontier):
        if (task.get('id') != metadata['task_id'] or task.get('_userId') != metadata['owner_user_id']
                or frontier['snapshot']['root_task_id'] != metadata['task_id']
                or frontier['snapshot']['owner_user_id'] != metadata['owner_user_id']):
            raise RecoveryRefused('Goal account observation binding changed')
        journal.publish_account_frontier(frontier, launch_id=metadata['launch_id'])
    options = {}
    if native_handoff is not None:
        if (native_handoff.task is not task or native_handoff.budget is not budget
                or native_handoff.journal is not journal):
            raise RecoveryRefused('Goal native handoff belongs to another journal or task')
        options = dict(actor_rebind_observer=native_handoff.commit_frontiers,
                       actor_rebind_bytes=native_handoff.evidence_bytes)
    budget.bind_account_frontier_observer(observe, **options)


def read_account_frontier(events, metadata):
    """Inspect one complete sequence without resolving any unfinished actor."""
    events = list(events)
    observed = [event for event in events if event.get('type') == EVENT]
    if metadata.get('account_frontier_format') != FORMAT:
        if observed:
            raise RecoveryRefused('Goal account observations have no launch binding')
        return None
    previous = None
    try:
        bound = False
        for event in events:
            if event.get('type') == EVENT:
                bound = True
            elif event.get('type') == 'execution_effect_started' and not bound:
                raise ValueError('effect precedes account binding')
        for version, event in enumerate(observed, 1):
            # Stored events may additionally carry transport sequence/time fields.
            if event.get('launch_id') != metadata['launch_id']:
                raise ValueError('account launch changed')
            current = validate_frontier(event['frontier']); snapshot = current['snapshot']
            if (current['version'] != version
                    or snapshot['owner_user_id'] != metadata['owner_user_id']
                    or snapshot['root_task_id'] != metadata['task_id']
                    or (version == 1) != (current['reason'] == 'bound')):
                raise ValueError('account sequence or identity changed')
            if version == 1 and 'account_seed' in metadata:
                seed = validate_snapshot(metadata['account_seed'])
                if (seed['owner_user_id'] != snapshot['owner_user_id']
                        or seed['limits'] != snapshot['limits']
                        or any(seed[key] != snapshot[key] for key in (
                            'tokens', 'tool_calls', 'model_ordinal', 'tool_ordinal'))):
                    raise ValueError('restored account changed its spent prefix')
            if previous is not None:
                validate_recovery_transition(previous, current)
                before = previous['snapshot']
                if (snapshot['limits'] != before['limits']
                        or any(snapshot[key] < before[key] for key in (
                            'tokens', 'tool_calls', 'model_ordinal', 'tool_ordinal'))
                        or current['child_ordinal'] < previous['child_ordinal']
                        or (not before['usage_known'] and snapshot['usage_known'])):
                    raise ValueError('account history regressed')
            previous = current
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryRefused('Goal account observations could not be verified') from exc
    if previous is None and any(event.get('type') == 'execution_effect_started' for event in events):
        raise RecoveryRefused('Goal effects have no acknowledged account frontier')
    return deepcopy(previous)


def recovery_account_snapshot(events, metadata):
    """Choose exact whole-account totals; active actors still require takeover."""
    return settled_account_snapshot(read_account_frontier(events, metadata), metadata)


def settled_account_snapshot(frontier, metadata):
    """Validate one inspected whole cut for ordinary spent-prefix restoration."""
    candidate = frontier['snapshot'] if frontier is not None else metadata.get('account_seed')
    if candidate is None:
        return None
    try:
        snapshot = validate_snapshot(candidate)
        if snapshot['owner_user_id'] != metadata['owner_user_id']:
            raise ValueError('account seed owner changed')
        if not all(snapshot[key] for key in ('complete', 'settled', 'usage_known')):
            raise ValueError('account has unresolved participants or consumption')
    except ValueError as exc:
        raise RecoveryRefused('Goal account is not a complete settled frontier') from exc
    return snapshot
