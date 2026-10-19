"""Restore shared admission from validated Flow receipts before new effects.

Snapshots are cumulative observations of ExecutionBudget. They are never added
together, and an older settled observation cannot hide later unsettled spending.
The journal and Goal lifecycle remain the effect and takeover authorities.
"""
from lib.agent_core.budget_snapshot import validate_snapshot


FIELD = 'execution_account'


def receipt_account(record):
    from lib.orchestration.execution_journal import RecoveryRefused
    if FIELD not in record:
        if 'account_root_task_id' in (record.get('invocation_completion') or {}):
            raise RecoveryRefused('Invocation completion is missing its execution account')
        return None
    try:
        snapshot = validate_snapshot(record[FIELD])
        identity = record['invocation_completion']['identity']
        if (record.get('kind') != 'agent' or snapshot['owner_user_id'] != identity['user_id']
                or record['invocation_id'] != identity['child_task_id']
                or snapshot['root_task_id'] != record['invocation_completion'].get('account_root_task_id')):
            raise ValueError('Execution account belongs to a different invocation')
        return snapshot
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryRefused('Invalid execution account receipt') from exc


def prepare_recovery_account(task, records, *, snapshot=None):
    """Validate the complete replay set; return an existing-account restore port."""
    from lib.orchestration.execution_journal import RecoveryRefused
    from lib.agent_core.execution_budget import _ensure_execution_budget
    budget = _ensure_execution_budget(task)
    snapshots, missing = [], False
    whole_snapshot = None
    if snapshot is not None:
        try:
            snapshot = validate_snapshot(snapshot)
            if (snapshot['owner_user_id'] != task['_userId'] or snapshot['limits'] != budget.limits
                    or not all(snapshot[key] for key in ('complete', 'settled', 'usage_known'))):
                raise ValueError('whole-account authority changed')
        except ValueError as exc:
            raise RecoveryRefused('Recovered whole-account evidence is invalid') from exc
        snapshots.append(snapshot)
        whole_snapshot = snapshot
    for record in records.values():
        if record.get('kind') != 'agent':
            continue
        snapshot = receipt_account(record)
        if snapshot is None:
            missing = True
        else:
            if snapshot['owner_user_id'] != task['_userId'] or snapshot['limits'] != budget.limits:
                raise RecoveryRefused('Recovered execution account owner or limits changed')
            if not snapshot['usage_known']:
                raise RecoveryRefused('Flow recovery contains unknown execution consumption')
            snapshots.append(snapshot)
    bounded = any(budget.limits[key] is not None for key in ('max_tokens', 'max_tool_calls'))
    if missing and bounded:
        raise RecoveryRefused('Bounded Flow recovery has no spent-account evidence')
    if not snapshots:
        return lambda: None
    # Every fresh generation starts at the previous total, so snapshots from
    # inherited generations remain comparable. Paid rows keep their own origins.
    counters = ('tokens', 'tool_calls', 'model_ordinal', 'tool_ordinal')
    eligible = [row for row in snapshots if row['complete'] and row['settled']
        and all(all(row[key] >= prior[key] for key in counters) for prior in snapshots)]
    if not eligible:
        raise RecoveryRefused('Flow recovery has no complete settled consumption frontier')
    if whole_snapshot is not None and whole_snapshot not in eligible:
        raise RecoveryRefused('Goal whole-account frontier is older than its completed receipts')
    selected = whole_snapshot if whole_snapshot is not None else eligible[-1]
    def restore():
        try:
            budget.restore_replay_snapshot(task, selected, owner=task['_userId'])
        except ValueError as exc:
            raise RecoveryRefused('Flow execution account could not be restored') from exc
    return restore
