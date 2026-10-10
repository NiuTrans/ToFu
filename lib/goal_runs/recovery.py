"""Rebind restart-interrupted chat Goals through ordinary conversation commands.

The archive proves settled effects, not admission. Owner-bound Goal/Turn reads,
normal command CAS/Stop/queue fences and fresh invocation resources authorize
one successor. Old interrupted runs remain terminal. No standalone worker or
second conversation lifecycle starts here.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import time

from lib.goal_runs.contract import goal_run_id_for_task, goal_orchestration_id, GOAL_RUN_CREATED_BY
from lib.goal_runs.repository import GoalRunRepositoryPort
from lib.conversation_sync.repository import ConversationSyncRepository
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.goal_runs.execution_journal import read_goal_execution
from lib.orchestration.execution_journal import JOURNAL_EVENTS, RecoveryRefused, RecoveryDeferred
from lib.goal_runs.journal_codec import CHUNK_EVENT

GOAL_RECOVERY_SOURCE_CONFIG_KEY = '_goalRecoverySourceRunId'


@dataclass(frozen=True)
class GoalRecoverySeed:
    source_run_id: str
    definition: dict
    metadata: dict
    records: dict

    def launch(self):
        from lib.orchestration.chat_launch import OrchestrationChatFlowLaunchSpec
        fields = deepcopy(self.metadata['launch'])
        fields['tools'] = tuple(fields['tools'])
        return OrchestrationChatFlowLaunchSpec(**fields)


def _events(runs, run_id):
    cursor = 0
    while True:
        page = runs.replay(run_id, cursor)
        if page is None or page.cursor_reset:
            raise RecoveryRefused('Goal recovery history disappeared')
        yield from (event for event in page.events
                    if event.get('type') in JOURNAL_EVENTS or event.get('type') == CHUNK_EVENT)
        if page.caught_up:
            return
        if page.next_cursor <= cursor:
            raise RecoveryRefused('Goal recovery history cursor did not advance')
        cursor = page.next_cursor


def load_goal_recovery_seed(runs, run_id, *, owner_user_id, tenant_id=''):
    run = runs.get_execution_snapshot(run_id)
    error = (run or {}).get('error')
    if (run is None or run.get('status') != 'error'
            or not isinstance(error, dict) or error.get('kind') != 'worker_lost'
            or error.get('source') != 'orchestration.startup_recovery'):
        raise RecoveryRefused('Only a restart-interrupted Goal can be rebound')
    metadata, records = read_goal_execution(
        _events(runs, run_id), run=run, owner_user_id=owner_user_id,
        tenant_id=tenant_id)
    return GoalRecoverySeed(run_id, deepcopy(run['definition']), metadata, records)


@dataclass(frozen=True)
class GoalRecoveryPorts:
    conversation_repository: ConversationSyncRepository
    goal_repository: GoalRunRepositoryPort
    get_attempt: Callable[..., dict]
    commands: ConversationTurnCommandService

    @classmethod
    def defaults(cls, owner_user_id, tenant_id):
        from lib.conversation_sync.repository import SidecarConversationSyncRepository
        from lib.conversation_sync.runtime import conversation_turn_commands
        from lib.goal_runs.repository import SidecarGoalRunRepository
        from lib.turn_lifecycle import get_attempt
        return cls(SidecarConversationSyncRepository(),
                   SidecarGoalRunRepository(owner_user_id, tenant_id=tenant_id),
                   get_attempt, conversation_turn_commands)


def _recovery_command_id(source, source_attempt, predecessor):
    material = source if predecessor == source_attempt else source + ':' + predecessor
    return 'goal-recovery-' + hashlib.sha256(material.encode()).hexdigest()


def _handoff_parent(repository, metadata, turn, owner):
    conversation = metadata['conversation_id']
    input_turn = repository.turn(conversation, turn.get('parentTurnId'), owner)
    if (not input_turn or input_turn.get('actor') != 'virtual_user'
            or input_turn.get('kind') != 'continuation'):
        raise RecoveryRefused('Goal handoff has no authoritative continuation input')
    parent = repository.turn(conversation, input_turn.get('parentTurnId'), owner)
    if not parent or parent.get('laneId') != turn.get('laneId'):
        raise RecoveryRefused('Goal handoff predecessor disappeared')
    predecessor = str(parent.get('currentAttemptId') or parent.get('runId') or '')
    if not predecessor:
        raise RecoveryRefused('Goal handoff predecessor has no attempt')
    return parent, predecessor


def _unfinished_handoff(runs, ports, seed, tail, latest, owner):
    """Prove a bounded accepted-command chain has not admitted any new effect.

    ExecutionJournal.ready precedes every effect. Missing/partial headers or
    inherited receipt copies are preparation, never proof that an agent ran.
    Private accepted config, attempt identity and exact continuation ancestry authorize
    another normal command; a new human turn or terminal decision does not.
    """
    metadata = seed.metadata
    current, allowed_runs = tail, {seed.source_run_id}
    for depth in range(16):
        attempt_id = str(current.get('currentAttemptId') or current.get('runId') or '')
        if attempt_id == metadata['attempt_id']:
            break
        attempt = ports.get_attempt(attempt_id, user_id=owner, include_execution_config=True)
        accepted = attempt.get('config') or {}
        if (not isinstance(accepted, Mapping)
                or str(accepted.get('_turnPrincipalTenantId') or '') != metadata['tenant_id']
                or accepted.get(GOAL_RECOVERY_SOURCE_CONFIG_KEY) != seed.source_run_id
                or accepted.get('_goalObjective') != metadata['objective']
                or accepted.get('_goalContinuationCommand') is not True):
            raise RecoveryRefused('The accepted attempt is not a server-authored Goal handoff')
        parent, predecessor = _handoff_parent(ports.conversation_repository, metadata, current, owner)
        expected = _recovery_command_id(seed.source_run_id, metadata['attempt_id'], predecessor)
        if (attempt.get('turnId') != current.get('turnId')
                or attempt.get('commandId') != expected or attempt.get('operation') != 'generate'):
            raise RecoveryRefused('The lane is not an accepted Goal recovery command chain')
        if depth == 0 and attempt.get('status') in {'pending', 'running'}:
            task_id = str(attempt.get('taskId') or '')
            if task_id:
                return goal_run_id_for_task(task_id), ''
            raise RecoveryDeferred('Goal handoff is still being bound by its accepted executor')
        if (attempt.get('status') != 'interrupted' or current.get('status') != 'interrupted'
                or (current.get('settlement') or {}).get('cause') != 'server_restart'):
            raise RecoveryRefused('A human or terminal executor decision superseded the handoff')
        task_id = str(attempt.get('taskId') or '')
        successor_id = goal_run_id_for_task(task_id) if task_id else ''
        successor = runs.get_execution_snapshot(successor_id) if successor_id else None
        if successor is not None:
            error = successor.get('error') or {}
            if (successor.get('created_by') != GOAL_RUN_CREATED_BY
                    or successor.get('orch_id') != goal_orchestration_id(metadata['conversation_id'])
                    or successor.get('input') != metadata['objective'] or successor.get('definition') != seed.definition
                    or successor.get('status') != 'error' or not isinstance(error, dict)
                    or error.get('kind') != 'worker_lost'
                    or error.get('source') != 'orchestration.startup_recovery'):
                raise RecoveryRefused('Goal handoff run binding or terminal decision changed')
            ready = False
            for event in _events(runs, successor_id):
                if event.get('type') == 'execution_effect_started':
                    raise RecoveryRefused('Goal handoff has admitted an unresolved new effect')
                ready = ready or event.get('type') == 'execution_journal_ready'
            if ready:
                # A crash after ready but before the parent acknowledgement is
                # reconciled to the already durable child; its own election owns replay.
                child = load_goal_recovery_seed(runs, successor_id, owner_user_id=owner,
                                                tenant_id=metadata['tenant_id'])
                if child.launch() != seed.launch() or child.metadata['invocation_policy'] != metadata['invocation_policy']:
                    raise RecoveryRefused('Ready Goal handoff changed its frozen execution policy')
                from lib.orchestration.run_recovery import record_recovery_claim
                record_recovery_claim(runs, seed.source_run_id, successor_id)
                return successor_id, ''
        if successor_id:
            allowed_runs.add(successor_id)
        current = parent
    else:
        raise RecoveryRefused('Goal preparation crash chain exceeds its structural bound')
    if (not latest or latest.get('runId') not in allowed_runs
            or latest.get('status') != 'failed' or latest.get('reason') != 'worker_lost'):
        raise RecoveryRefused('A newer Goal or terminal decision superseded recovery')
    return '', str(tail.get('currentAttemptId') or tail.get('runId'))


def resume_chat_goal(runs, source_run_id, *, owner_user_id, tenant_id='', ports=None):
    # Start the cancellation watermark before reads, so Stop during preparation
    # is observed by the same normal command/dispatch boundary as a human send.
    started_at = time.time()
    ports = ports or GoalRecoveryPorts.defaults(owner_user_id, tenant_id)
    seed = load_goal_recovery_seed(runs, source_run_id,
                                  owner_user_id=owner_user_id, tenant_id=tenant_id)
    metadata = seed.metadata
    conversation_id = metadata['conversation_id']
    from lib.orchestration.run_recovery import recovery_claim_successor
    existing = recovery_claim_successor(runs, source_run_id)
    if existing:
        successor = runs.get_execution_snapshot(existing)
        if (successor is None or successor.get('orch_id') != goal_orchestration_id(conversation_id)
                or successor.get('input') != metadata['objective']):
            raise RecoveryRefused('Claimed Goal successor is missing or changed')
        return existing
    latest = ports.goal_repository.latest_for_conversation(conversation_id)
    attempt = ports.get_attempt(metadata['attempt_id'], user_id=owner_user_id)
    if (attempt.get('turnId') != metadata['turn_id']
            or attempt.get('taskId') != metadata['task_id']
            or attempt.get('status') != 'interrupted'):
        raise RecoveryRefused('Goal source attempt binding changed')
    root = ports.conversation_repository.turn(
        conversation_id, metadata['turn_id'], owner_user_id)
    snapshot = ports.conversation_repository.snapshot(
        conversation_id, owner_user_id, turn_limit=1)
    if not root or root.get('currentAttemptId') != metadata['attempt_id'] or not snapshot:
        raise RecoveryRefused('Goal conversation or source attempt disappeared')
    turns = [turn for turn in snapshot.get('turns', [])
             if turn.get('laneId') == root.get('laneId')]
    tail = max(turns, key=lambda turn: turn['ordinal'], default=None)
    if not tail:
        raise RecoveryRefused('Goal conversation has no lane tail')
    predecessor = metadata['attempt_id']
    if tail.get('turnId') != metadata['turn_id'] and tail.get('runId') != metadata['attempt_id']:
        existing, predecessor = _unfinished_handoff(runs, ports, seed, tail, latest, owner_user_id)
        if existing:
            return existing
    elif (not latest or latest.get('runId') != source_run_id
            or latest.get('status') != 'failed' or latest.get('reason') != 'worker_lost'
            or tail.get('status') not in {'completed', 'failed', 'interrupted'}
            or (tail.get('status') == 'interrupted'
                and (tail.get('settlement') or {}).get('cause') != 'server_restart')):
        raise RecoveryRefused('The conversation advanced or the human stopped the Goal')
    command_id = _recovery_command_id(source_run_id, metadata['attempt_id'], predecessor)
    from lib.goal_runs.continuation import goal_continuation_config
    config = goal_continuation_config(deepcopy(metadata['invocation_policy']['config']))
    if config.get('modelRef'):
        config.pop('_pinned_provider_id', None)
    for key in ('flowDefinition', 'flowBuiltin', 'flowId'):
        config.pop(key, None)
    config.update(autopilot=True, autopilotEnabled=True, planMode=False,
                  autopilotMaxIterations=metadata['launch']['max_iterations'])
    from lib.identity import PrincipalContext
    outcome = ports.commands.create_turn(
        conversation_id, owner_user_id,
        {'commandId': command_id, 'config': config,
         'inputActor': 'virtual_user', 'inputKind': 'continuation',
         'actor': 'assistant', 'kind': 'reply',
         'laneId': root['laneId'], 'parentTurnId': tail['turnId'],
         'requireParentIsLaneTail': True, 'requireParentNotStopped': True,
         'inputTurn': {'role': 'user', 'content': 'Resume the interrupted Goal from committed evidence.',
                       '_isVirtualUser': True, '_goalContinuation': True}},
        request_started_at=started_at,
        principal=PrincipalContext.user(subject_id=f'user:{owner_user_id}',
                                        owner_user_id=owner_user_id, tenant_id=tenant_id or None),
        trusted_goal_objective=metadata['objective'],
        trusted_goal_recovery_source=source_run_id,
        trusted_goal_recovery_abort_after=float(attempt['createdAt']) / 1000.0,
    )
    value = outcome.value
    task_id = str((value.get('attempt') or {}).get('taskId') or '')
    if not task_id or value.get('queued') or value.get('aborted'):
        raise RecoveryRefused('Goal recovery command did not bind an executor')
    successor = goal_run_id_for_task(task_id)
    # The new journal acknowledges this source only after its complete seed is durable.
    return successor


def bind_goal_recovery_task(task, *, runs=None, conversation_repository=None, get_attempt=None):
    """Restore evidence only on the new command's already-bound executor."""
    source = (task.get('config') or {}).get(GOAL_RECOVERY_SOURCE_CONFIG_KEY)
    if not source:
        return None
    from lib.tasks_pkg.manager._registry import task_user_id
    owner, tenant = task_user_id(task), str(task.get('_tenant_id') or '')
    if runs is None:
        from lib.orchestration.run_service import OrchestrationRunService
        from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
        runs = OrchestrationRunService(SidecarOrchestrationRunStore(owner, tenant_id=tenant))
    seed = load_goal_recovery_seed(runs, source, owner_user_id=owner, tenant_id=tenant)
    metadata = seed.metadata
    if (task.get('convId') != metadata['conversation_id']
            or (task.get('config') or {}).get('_goalObjective') != metadata['objective']):
        raise RecoveryRefused('Goal recovery command changed conversation or objective')
    if conversation_repository is None:
        from lib.conversation_sync.repository import SidecarConversationSyncRepository
        conversation_repository = SidecarConversationSyncRepository()
    if get_attempt is None:
        from lib.turn_lifecycle import get_attempt
    attempt = get_attempt(task.get('_attemptId'), user_id=owner, include_execution_config=True)
    accepted = attempt.get('config') or {}
    if (not isinstance(accepted, Mapping)
            or str(accepted.get('_turnPrincipalTenantId') or '') != metadata['tenant_id']
            or accepted.get(GOAL_RECOVERY_SOURCE_CONFIG_KEY) != source):
        raise RecoveryRefused('Goal source pointer differs from the durable accepted attempt')
    if (attempt.get('taskId') != task.get('id') or attempt.get('turnId') != task.get('_turnId')
            or attempt.get('status') not in {'pending', 'running'}
            or attempt.get('operation') != 'generate'):
        raise RecoveryRefused('Goal recovery executor is not the accepted attempt')
    turn = conversation_repository.turn(task['convId'], task['_turnId'], owner)
    if not turn or turn.get('currentAttemptId') != task['_attemptId']:
        raise RecoveryRefused('Goal recovery Turn no longer owns the executor')
    _, predecessor = _handoff_parent(conversation_repository, metadata, turn, owner)
    if attempt.get('commandId') != _recovery_command_id(source, metadata['attempt_id'], predecessor):
        raise RecoveryRefused('Goal recovery command is not the accepted handoff')
    task['messages'] = deepcopy(metadata['messages'])
    task['config'].pop('_goalWorkerState', None)
    task.update(deepcopy(metadata['worker_state']))
    task['_goalInvocationPolicy'] = deepcopy(metadata['invocation_policy'])
    task['_goalRecoverySeed'] = seed
    return seed
