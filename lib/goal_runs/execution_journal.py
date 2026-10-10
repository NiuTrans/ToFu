"""Archive a chat Goal's launch and effects through the shared Flow journal.

Entry point: create_goal_execution_journal. The owner-bound orchestration
repository owns admission and durability; ExecutionJournal owns effect identity
and ambiguity. This module freezes chat bindings and model inputs, and never
starts a worker or decides that a crashed invocation can be repeated.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import uuid

import jsonschema

from lib.goal_runs.contract import (
    GOAL_EXECUTION_LAUNCH_FORMAT, GOAL_RUN_CREATED_BY,
    goal_execution_launch_contract, goal_orchestration_id,
    goal_run_id_for_task,
)
from lib.goal_runs.objective import objective_from_task
from lib.goal_runs.service import GoalRunLifecycleError
from lib.goal_runs.worker_context import GoalWorkerContext
from lib.storage.retry import call_with_transient_retry
from lib.orchestration.execution_journal import (
    ExecutionJournal, JOURNAL_EVENTS, RecoveryRefused, fingerprint, read_journal,
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


def create_goal_execution_journal(task, definition, launch, *, runs=None):
    """Claim one immutable launch before admitting any model/tool effect.

    A unique launch witness makes competing starters conflict at the same
    event sequence. An existing journal never authorizes another executor.
    """
    from lib.tasks_pkg.manager._registry import task_user_id
    from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
    run_id = str(task.get('_goalRunId') or '')
    owner = task_user_id(task)
    seed = task.get('_goalRecoverySeed')
    if seed is not None and (seed.definition != definition or seed.launch() != launch):
        raise GoalRunLifecycleError('Goal recovery launch changed its archived definition or policy')
    metadata = {
        'format': GOAL_EXECUTION_LAUNCH_FORMAT,
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

    def publish(event):
        nonlocal cursor
        from lib.goal_runs.journal_codec import encode_event
        # Atomic projection refuses superseded/terminal Goals and competing
        # launch witnesses. Failed or ambiguous writes admit no next effect.
        for packet in encode_event(event):
            # Identical owner/run/sequence/payload projections reconcile lost
            # acknowledgements; conflict verdicts are never retried. Keep the
            # cursor fixed until this exact physical packet is acknowledged.
            accepted = call_with_transient_retry(
                lambda: runs.project_event(run_id, cursor, packet),
                abort_event=task.get('abort_event'),
                label=f'Goal execution event {run_id}/{cursor}')
            if not accepted:
                raise RecoveryRefused('Goal execution event was not durably accepted')
            cursor += 1

    journal = ExecutionJournal(publish, records=seed.records if seed is not None else None,
                               worker_context_policy='conversation')
    journal.begin(definition, launch.initial_context, launch_metadata=metadata,
                  parent_run_id=seed.source_run_id if seed is not None else '')
    if seed is not None:
        from lib.orchestration.run_recovery import record_recovery_claim
        # Ready and the source acknowledgement both precede every leaf effect.
        # Failure here admits no model/tool call; next boot reconciles the ready child.
        call_with_transient_retry(
            lambda: record_recovery_claim(runs, seed.source_run_id, run_id),
            abort_event=task.get('abort_event'),
            label=f'Goal recovery acknowledgement {seed.source_run_id}')
    # Bind only after the durable claim succeeds. Every subsequent leaf uses
    # the archived policy even if the parent's mutable config later changes.
    task['_goalInvocationPolicy'] = deepcopy(metadata['invocation_policy'])
    return journal


def read_goal_execution(events, *, run, owner_user_id, tenant_id=''):
    """Validate frozen chat inputs and settled effects without launching work."""
    from lib.goal_runs.journal_codec import decode_events
    events = list(decode_events(events))
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
    records, parent = read_journal(
        events, definition=run['definition'],
        initial_context=metadata['launch']['initial_context'])
    if parent:
        raise RecoveryRefused('Goal execution requires its archived parent')
    if any(record['kind'] == 'agent' and 'worker_context' not in record['result']
           for record in records.values()):
        raise RecoveryRefused('Goal execution receipt has no worker context')
    return deepcopy(metadata), records
