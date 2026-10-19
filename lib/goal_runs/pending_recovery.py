"""Typed interrupted Goal actor evidence on the existing journal/account owners.

Entry: preflight_pending_actors. Reads require a restart-interrupted Goal seed,
exact direct-child membership and native dead-writer observations. This type is
not an executor, wallet authorization, completed receipt or transferable lease.
"""
from copy import deepcopy
from dataclasses import dataclass

from lib.orchestration.execution_journal import RecoveryRefused
from lib.orchestration.invocation_frontier import preflight_registered_actor_accounts
from lib.orchestration.recovery_account import receipt_account


@dataclass(frozen=True)
class PendingGoalActors:
    source_run_id: str
    source_launch_id: str
    account_frontier: dict
    actors: dict
    positions: dict | None = None


def journal_positions(positioned, *, journal, source_cursor):
    """Pin validated logical witnesses to their actual stored reference packets."""
    if type(source_cursor) is not int or source_cursor < 1:
        raise RecoveryRefused('Goal claim requires a complete stored journal cursor')
    launch = account = None
    actors = {}
    previous = -1
    for sequence, event in positioned:
        if type(sequence) is not int or not previous < sequence < source_cursor:
            raise RecoveryRefused('Goal journal physical positions are missing or unordered')
        previous = sequence
        kind = event.get('type')
        if kind == 'execution_journal_start':
            launch = sequence
        elif kind == 'execution_account_frontier':
            account = sequence
        elif kind in {'execution_effect_prepared', 'execution_effect_started', 'execution_effect_completed'} and event.get('kind') == 'agent':
            if kind != 'execution_effect_prepared' or event['key'] in journal.pending_agents:
                actors[event['key']] = sequence
    expected = set(journal.pending_agents) | {key for key, row in journal.records.items() if row['kind'] == 'agent'}
    if launch is None or account is None or launch >= account or set(actors) != expected:
        raise RecoveryRefused('Goal journal positions do not cover its complete actor inventory')
    return dict(source_cursor=source_cursor, launch_sequence=launch, account_sequence=account, actors=actors)


def preflight_pending_actors(evidence, *, storage, frontier_bounds):
    """Join pending identities to one whole account without fabricating results.

    Retain source registration and checkpoint versions for future atomic
    revalidation. No observation is sufficient to admit successor effects.
    """
    metadata, journal = evidence.metadata, evidence.journal
    frontier = evidence.account_frontier
    if not journal.pending_agents or frontier is None or journal.parent_run_id:
        raise RecoveryRefused('Pending Goal actors require their complete launch and account evidence')
    if (not isinstance(frontier_bounds, dict) or set(frontier_bounds) != {
            'header_bytes', 'snapshot_bytes', 'identity_bytes', 'io_timeout_ms'}):
        raise RecoveryRefused('Pending Goal actor inspection requires explicit native read bounds')
    whole = frontier['snapshot']
    for record in journal.records.values():
        if record.get('kind') != 'agent':
            continue
        account = receipt_account(record)
        if (account is None or account['owner_user_id'] != metadata['owner_user_id']
                or account['limits'] != whole['limits'] or not account['usage_known']
                or any(account[key] > whole[key] for key in (
                    'tokens', 'tool_calls', 'model_ordinal', 'tool_ordinal'))):
            raise RecoveryRefused('Pending Goal account does not cover its completed receipts')
    observed = preflight_registered_actor_accounts(storage, frontier,
        list(journal.pending_agents.values()), owner_user_id=metadata['owner_user_id'],
        conversation_id=metadata['conversation_id'], parent_task_id=metadata['task_id'],
        consumed_actors=evidence.consumed_actors,
        **frontier_bounds)
    # Keep the Flow effect key, admission nonce and input digest with the exact
    # native snapshot, including original fee origins and pending tool call IDs.
    actors = {key: observed['actors'][admission['invocation_id']]
              for key, admission in journal.pending_agents.items()}
    return PendingGoalActors(metadata['run_id'], metadata['launch_id'],
        deepcopy(observed['account_frontier']), deepcopy(actors), deepcopy(evidence.positions))
