"""Archive a chat Goal's launch and effects through the shared Flow journal.

Entry point: create_goal_execution_journal. The owner-bound orchestration
repository owns admission and durability; ExecutionJournal owns effect identity
and ambiguity. This module freezes chat bindings and model inputs, and never
starts a worker or decides that a crashed invocation can be repeated.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import uuid

import jsonschema

from lib.goal_runs.contract import (
    GOAL_EXECUTION_LAUNCH_FORMAT, GOAL_RUN_CREATED_BY,
    GOAL_ACCOUNT_FRONTIER_FORMAT,
    goal_execution_launch_contract, goal_orchestration_id,
    goal_run_id_for_task,
)
from lib.goal_runs.objective import objective_from_task
from lib.goal_runs.service import GoalRunLifecycleError
from lib.goal_runs.worker_context import GoalWorkerContext
from lib.tasks_pkg.persistence_wait import wait_for_persistence
from lib.orchestration.execution_journal import (
    ExecutionJournal, JOURNAL_EVENTS, RecoveryRefused, JournalRecovery, fingerprint, inspect_journal,
)

_LAUNCH_VALIDATOR = jsonschema.Draft202012Validator(goal_execution_launch_contract())


def _runs_for_task(task):
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    return OrchestrationRunService(SidecarOrchestrationRunStore(
        task_user_id(task), tenant_id=task.get('_tenant_id')))


def _validate(metadata):
    try:
        _LAUNCH_VALIDATOR.validate(metadata)
        fingerprint(metadata)  # Refuse non-JSON inputs before any dispatch.
    except (jsonschema.ValidationError, TypeError, ValueError) as exc:
        raise GoalRunLifecycleError('Goal execution launch is invalid') from exc


def create_goal_execution_journal(task, definition, launch, *, runs=None,
                                  native_claim=None, account_handoff_factory=None):
    """Claim one immutable launch before admitting any model/tool effect.

    A unique launch witness makes competing starters conflict at the same
    event sequence. An existing journal never authorizes another executor.
    """
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
    run_id = str(task.get('_goalRunId') or '')
    owner = task_user_id(task)
    seed = task.get('_goalRecoverySeed')
    pending = seed is not None and seed.pending_actors is not None
    if pending:
        from lib.goal_runs.pending_claim import ClaimedGoalActors
        if not isinstance(native_claim, ClaimedGoalActors) or not callable(account_handoff_factory):
            seed.require_settled()
        native_claim.require_matches(task, seed)
    elif native_claim is not None or account_handoff_factory is not None:
        raise GoalRunLifecycleError('Native Goal claim requires a pending recovery seed')
    elif seed is not None:
        seed.require_settled()
    if seed is not None and (seed.definition != definition or seed.launch() != launch):
        raise GoalRunLifecycleError('Goal recovery launch changed its archived definition or policy')
    from lib.orchestration.recovery_account import prepare_recovery_account
    if pending:
        from lib.agent_core.execution_budget import _ensure_execution_budget
        budget = _ensure_execution_budget(task)
        def restore_account():
            native_claim.require_matches(task, seed)
            budget.restore_pending_frontier(task, seed.pending_actors.account_frontier, owner=owner)
    else:
        restore_account = prepare_recovery_account(task, seed.records,
            snapshot=seed.account_snapshot) if seed is not None else None
    from lib.goal_runs.fee_account import from_task as fee_account_from_task
    metadata = {
        'format': GOAL_EXECUTION_LAUNCH_FORMAT,
        'account_frontier_format': GOAL_ACCOUNT_FRONTIER_FORMAT,
        'fee_account': fee_account_from_task(task),
        'launch_id': uuid.uuid4().hex,
        'run_id': run_id, 'task_id': str(task.get('id') or ''),
        'conversation_id': str(task.get('convId') or ''),
        'owner_user_id': owner, 'tenant_id': str(task.get('_tenant_id') or ''),
        'turn_id': str(task.get('_turnId') or ''),
        'attempt_id': str(task.get('_attemptId') or ''),
        'objective': objective_from_task(task),
        'launch': asdict(launch),
        'messages': deepcopy(task.get('messages') or []),
        'worker_state': GoalWorkerContext.initial_state(task),
        'invocation_policy': deepcopy(seed.metadata['invocation_policy']) if seed is not None else snapshot_conversation_child_policy(
            task, model=launch.model, tools=list(launch.tools),
            project_path=launch.project_path, thinking_enabled=launch.thinking_enabled),
    }
    metadata['launch']['tools'] = list(metadata['launch']['tools'])
    if pending:
        metadata['account_seed'] = deepcopy(seed.pending_actors.account_frontier['snapshot'])
    elif seed is not None and seed.account_snapshot is not None:
        metadata['account_seed'] = deepcopy(seed.account_snapshot)
    elif seed is None:
        from lib.agent_core.execution_budget import _ensure_execution_budget
        metadata['account_seed'] = _ensure_execution_budget(task).capture_replay_snapshot(task, owner=owner)
    _validate(metadata)
    runs = runs if runs is not None else _runs_for_task(task)
    run = runs.get_execution_snapshot(run_id)
    if (run_id != goal_run_id_for_task(metadata['task_id'])
            or run is None or run.get('status') != 'running'
            or run.get('created_by') != GOAL_RUN_CREATED_BY
            or run.get('orch_id') != goal_orchestration_id(metadata['conversation_id'])
            or run.get('input') != metadata['objective']
            or run.get('definition') != definition):
        raise GoalRunLifecycleError('Goal execution launch does not match the active owned run')
    cursor = 0
    while True:
        page = runs.replay(run_id, cursor)
        if page is None or page.cursor_reset:
            raise GoalRunLifecycleError('Goal execution history is unavailable')
        if any(event.get('type') in JOURNAL_EVENTS for event in page.events):
            raise GoalRunLifecycleError('Goal execution already has a journal; replay was refused')
        if page.caught_up:
            cursor = page.next_cursor
            break
        if page.next_cursor <= cursor:
            raise GoalRunLifecycleError('Goal execution history cursor did not advance')
        cursor = page.next_cursor

    from lib.goal_runs.journal_publication import GoalJournalPublisher
    publish = GoalJournalPublisher(task, runs, metadata, cursor=cursor)
    journal = ExecutionJournal(publish, records=seed.records if seed is not None else None,
                               worker_context_policy='conversation',
                               account_handoff_commit=publish.commit_account_handoff)
    journal.begin(definition, launch.initial_context, launch_metadata=metadata,
                  parent_run_id=seed.source_run_id if seed is not None else '')
    if seed is not None and not pending:
        from lib.orchestration.run_recovery import record_recovery_claim
        # Ready and the source acknowledgement both precede every leaf effect.
        # Failure here admits no model/tool call; next boot reconciles the ready child.
        wait_for_persistence(task,
            lambda: record_recovery_claim(runs, seed.source_run_id, run_id),
            label=f'Goal recovery acknowledgement {seed.source_run_id}')
    if seed is not None:
        restore_account()
    from lib.goal_runs.account_journal import install_account_journal
    # The native claim already retired the source in its debt transaction. The
    # caller retains handoff ownership; this journal only binds the common sink.
    try:
        handoff = account_handoff_factory(journal) if pending else None
        if pending:
            from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
            if not isinstance(handoff, NativeGoalHandoff):
                raise GoalRunLifecycleError('Pending Goal preparation requires its native whole-actor sink')
            handoff.bind_recovery_claim(native_claim, seed)
        install_account_journal(task, journal, metadata, native_handoff=handoff)
    except BaseException:
        journal.revoke()
        raise
    # Bind only after the durable claim succeeds. Every subsequent leaf uses
    # the archived policy even if the parent's mutable config later changes.
    task['_goalInvocationPolicy'] = deepcopy(metadata['invocation_policy'])
    return journal


@dataclass(frozen=True)
class GoalExecutionEvidence:
    metadata: dict
    journal: JournalRecovery
    account_frontier: dict | None
    positions: dict | None = None
    consumed_actors: dict | None = None

    def settled(self):
        from lib.goal_runs.account_journal import settled_account_snapshot
        records, parent = self.journal.settled()
        if parent:
            raise RecoveryRefused('Goal execution requires its archived parent')
        account = settled_account_snapshot(self.account_frontier, self.metadata)
        return deepcopy(self.metadata), records, account


def inspect_goal_execution(events, *, run, owner_user_id, tenant_id='', storage=None, source_cursor=None):
    """Validate frozen launch and receipts, preserving unresolved agent identities.

    Inspection does not resolve tools, claim leases, restore budgets or authorize
    a successor. A native frontier preflight is a separate required boundary.
    """
    from lib.goal_runs.journal_codec import decode_events
    positioned = list(decode_events(events, include_positions=True)) if source_cursor is not None else None
    events = [event for _, event in positioned] if positioned is not None else list(decode_events(events))
    headers = [event for event in events
               if event.get('type') == 'execution_journal_start']
    if len(headers) != 1:
        raise RecoveryRefused('Goal execution has no unambiguous launch')
    header = headers[0]
    metadata = header.get('launch_metadata')
    try:
        _validate(metadata)
    except GoalRunLifecycleError as exc:
        raise RecoveryRefused('Goal execution launch is invalid') from exc
    if (metadata['run_id'] != run.get('id')
            or metadata['run_id'] != goal_run_id_for_task(metadata['task_id'])
            or metadata['owner_user_id'] != owner_user_id
            or metadata['tenant_id'] != str(tenant_id or '')
            or run.get('created_by') != GOAL_RUN_CREATED_BY
            or run.get('orch_id') != goal_orchestration_id(metadata['conversation_id'])
            or run.get('input') != metadata['objective']):
        raise RecoveryRefused('Goal execution launch binding changed')
    from lib.orchestration.invocation_recovery import completion_resolver
    journal = inspect_journal(
        events, definition=run['definition'],
        initial_context=metadata['launch']['initial_context'],
        reconcile_agent=completion_resolver(owner_user_id,
            conversation_id=metadata['conversation_id'], parent_task_id=metadata['task_id'],
            allow_missing=True, storage=storage))
    if journal.parent_run_id:
        raise RecoveryRefused('Goal execution requires its archived parent')
    from lib.goal_runs.prepared_recovery import join_consumed_preparations
    journal, consumed = join_consumed_preparations(events, journal)
    if any(record['kind'] == 'agent' and 'worker_context' not in record['result']
           for record in journal.records.values()):
        raise RecoveryRefused('Goal execution receipt has no worker context')
    from lib.goal_runs.account_journal import read_account_frontier
    frontier = read_account_frontier(events, metadata)
    positions = None
    if positioned is not None and journal.pending_agents:
        from lib.goal_runs.pending_recovery import journal_positions
        positions = journal_positions(positioned, journal=journal, source_cursor=source_cursor)
    return GoalExecutionEvidence(deepcopy(metadata), journal, frontier, positions, consumed)


def read_goal_execution(events, *, run, owner_user_id, tenant_id='', include_account=False):
    """Return settled replay evidence; pending inspection does not relax replay."""
    metadata, records, account = inspect_goal_execution(events, run=run,
        owner_user_id=owner_user_id, tenant_id=tenant_id).settled()
    return (metadata, records, account) if include_account else (metadata, records)
