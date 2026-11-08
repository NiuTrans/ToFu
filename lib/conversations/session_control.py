"""Owner-scoped session observation and control for bound Coordinators.

Entry points: inspect_session, control_session and execute_session_tool. Binding
admission is shared with coordinator_tools; source authority uses project
delegation, or for the Global Router the owner's delegation
(lib.router.owner_delegation, docs/ROUTER.md R9). Queries and commands reuse
conversation_sync/turn_lifecycle, never an executor, route, database or
synthetic human message.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import time


def _text(arguments, name):
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} is required')
    return value


def _target(scope, arguments, *, write=False):
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.repository import ConversationRepository
    from lib.router.policy import is_router_scope
    from lib.router.watchdog.binding import is_watchdog_scope

    if is_watchdog_scope(scope):
        raise PermissionError('The Watchdog cannot control sessions; notify their Coordinator')
    conversation_id = _text(arguments, 'conversation_id')
    snapshot = ConversationRepository().get(
        conversation_id, user_id=scope['user_id'], include_messages=False)
    if snapshot is None:
        raise PermissionError('Conversation not found in this owner scope')
    metadata = getattr(snapshot, 'metadata', snapshot)
    settings = metadata.get('settings') or {}
    if (not is_router_scope(scope) and normalize_project_path(settings.get('projectPath') or '')
            != normalize_project_path(scope['project_path'])):
        raise PermissionError('Conversation belongs to another project; use its Coordinator or the Global Router')
    if write and settings.get('readOnly'):
        raise PermissionError('Read-only conversations cannot be controlled')
    return conversation_id, metadata


def _turn_view(turn):
    return {key: deepcopy(turn[key]) for key in (
        'turnId', 'conversationId', 'laneId', 'actor', 'kind', 'status',
        'currentAttemptId', 'projectionRevision', 'createdAt', 'updatedAt', 'settlement',
    ) if key in turn}


def _attempt_view(attempt):
    return {key: deepcopy(attempt[key]) for key in (
        'attemptId', 'conversationId', 'turnId', 'taskId', 'status', 'operation',
        'createdAt', 'startedAt', 'settledAt', 'error', 'queueBinding',
    ) if key in attempt}


def inspect_session(scope, arguments):
    """Return canonical execution facts; no transcript or private executor config."""
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.turn_lifecycle import LifecycleNotFound

    conversation_id, metadata = _target(scope, arguments)
    owner = scope['user_id']
    snapshot = ConversationSyncService(SidecarConversationSyncRepository()).snapshot(conversation_id, owner)
    turns = snapshot['turns']
    if arguments.get('turn_id'):
        turn_id = _text(arguments, 'turn_id')
        turns = [turn for turn in turns if turn['turnId'] == turn_id]
        if not turns:
            raise LifecycleNotFound('Turn not found in this conversation')
    current_attempts = {turn.get('currentAttemptId') for turn in turns}
    attempts = [attempt for attempt in snapshot['attempts'] if attempt['attemptId'] in current_attempts]
    return {
        'conversation_id': conversation_id, 'title': metadata.get('title', ''),
        'conversation_revision': snapshot['conversationRevision'], 'sync_seq': snapshot['syncSeq'],
        'turns': [_turn_view(turn) for turn in turns],
        'attempts': [_attempt_view(attempt) for attempt in attempts],
        'queue_items': snapshot['queueItems'],
        'active_attempt_phase': snapshot.get('activeAttemptPhase'),
        'read_only': bool((metadata.get('settings') or {}).get('readOnly')),
        'next_step': 'Use the selected Turn currentAttemptId and projectionRevision for control_conversation. '
                     'Resume operations come from settlement.resumeOptions. Read content with get_conversation.',
    }


def control_session(task, scope, arguments, *, acting_principal=None):
    """Resume the exact accepted execution, or stop its exact current Attempt.

    ``acting_principal`` replaces the current request's source principal only
    for the owner's delegation (``lib.router.owner_delegation``): it stands in
    for the owner's message and never becomes the target's execution identity.
    """
    from lib.conversation_sync.execution_identity import restore_execution_identity
    from lib.conversation_sync.runtime import conversation_turn_commands
    from lib.project_dispatch.admission import KEY, TrustedDispatch, repository_for
    from lib.project_dispatch.delegation import source_for_task, worker_control
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.turn_lifecycle import get_attempt, get_turn

    action = _text(arguments, 'action')
    if action not in {'continue', 'checkpoint_resume', 'regenerate', 'stop'}:
        raise ValueError('action must be continue, checkpoint_resume, regenerate or stop')
    conversation_id, _ = _target(scope, arguments, write=True)
    turn_id, attempt_id = _text(arguments, 'turn_id'), _text(arguments, 'attempt_id')
    key = _text(arguments, 'idempotency_key')
    expected = arguments.get('expected_revision')
    if action != 'stop' and (type(expected) is not int or expected < 1):
        raise ValueError('expected_revision must be the observed Turn projectionRevision')
    principal = acting_principal or source_for_task(ProjectDispatchRepository(
        scope['user_id'], scope['project_path']), task)[0]
    owner = scope['user_id']
    turn = get_turn(conversation_id, turn_id, user_id=owner, include_projection=False)
    attempt = get_attempt(attempt_id, user_id=owner, include_execution_config=True)
    if attempt['conversationId'] != conversation_id or attempt['turnId'] != turn_id:
        raise PermissionError('Attempt does not belong to the selected conversation and Turn')
    config = attempt.get('config')
    if not isinstance(config, Mapping):
        raise PermissionError('The accepted execution config is unavailable')
    if (config.get('_turnPrincipalTenantId') or None) != principal.tenant_id:
        raise PermissionError('Conversation execution belongs to another tenant')

    # Pin the action and observed execution, not a mutable "latest" pointer.
    # Re-reading config from this original Attempt also makes lost-ACK replay
    # byte-stable after the successor has become the current Attempt.
    digest = hashlib.sha256(json.dumps([
        owner, scope['conversation_id'], scope['binding_revision'], key,
        conversation_id, turn_id, attempt_id, action, expected, arguments.get('resume_anchor'),
    ], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    command_id = 'session-control:' + digest
    if turn.get('currentAttemptId') != attempt_id:
        if action == 'stop':
            raise ValueError('The current Attempt changed; read get_conversation_status before acting')
        current = get_attempt(turn['currentAttemptId'], user_id=owner)
        if current.get('commandId') != command_id:
            raise ValueError('The current Attempt changed; read get_conversation_status before acting')

    trusted = None
    if KEY in config:
        trusted = TrustedDispatch(deepcopy(config[KEY]))
        # The human's current request may operate any owned session in scope.
        # Automatic follow-through retains the original dispatch request fence.
        if principal.kind != 'user':
            worker_control(repository_for(trusted.binding), task, trusted.checked())

    if action == 'stop':
        # A repeated Stop of a settled Attempt must not plant a fresh
        # conversation-wide withdrawal against work started after that Stop.
        result = (conversation_turn_commands.abort_attempt(attempt_id, owner).value
                  if attempt['status'] in {'pending', 'running'} else
                  {'attemptId': attempt_id, 'status': attempt['status'], 'alreadyTerminal': True})
    else:
        if trusted is not None:
            target_principal = trusted.principal
            public_config = {}
        else:
            target_principal, _ = restore_execution_identity({**config, '_turnOwnerUserId': owner})
            public_config = {name: deepcopy(value) for name, value in config.items() if not name.startswith('_')}
        body = {'commandId': command_id, 'operation': action,
                'expectedProjectionRevision': expected, 'config': public_config}
        if arguments.get('resume_anchor') is not None:
            # Anchors are exact values returned by canonical resumeOptions;
            # the command owner validates them, just as for a browser click.
            body['resumeAnchor'] = arguments['resume_anchor']
        result = conversation_turn_commands.create_attempt(
            conversation_id, turn_id, owner, body, request_started_at=time.time(),
            principal=target_principal, trusted_dispatch=trusted).value
    return {'conversation_id': conversation_id, 'action': action, 'command_id': command_id,
            'turn': _turn_view(result['turn']) if result.get('turn') else None,
            'attempt': _attempt_view(result['attempt']) if result.get('attempt') else None,
            'idempotent_replay': bool(result.get('idempotentReplay')),
            'receipt': {key: value for key, value in result.items()
                        if key in {'aborted', 'attemptId', 'status', 'alreadyTerminal', 'cancelled',
                                   'dispatchWithdrawal', 'queueId', 'deletedTurnIds', 'conversationRevision'}},
            'next_step': 'Read get_conversation_status to observe execution; acceptance is not completion.'}


def execute_session_tool(task, scope, name, arguments):
    """Keep typed conflicts and uncertain mutation outcomes visible to the model."""
    from lib.conversation_sync.service import ConversationSyncNotFound
    from lib.error_envelope import BadRequest
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import LifecycleConflict, LifecycleNotFound

    try:
        if name == 'get_conversation_status':
            result = inspect_session(scope, arguments)
        elif arguments.get('owner_delegation') is not None:
            from lib.router.owner_delegation import delegated_conversation_control
            result = delegated_conversation_control(task, scope, arguments, control=control_session)
        else:
            result = control_session(task, scope, arguments)
    except (PermissionError, ValueError, KeyError, LifecycleNotFound,
            ConversationSyncNotFound, LifecycleConflict, BadRequest, StorageError) as error:
        result = {'error': str(error), 'code': getattr(error, 'code', 'session_control_refused'),
                  'execution_started': getattr(error, 'execution_started',
                                               None if name == 'control_conversation' else False),
                  'recovery': 'Read get_conversation_status. After an uncertain command ACK, retry unchanged '
                              'arguments with the same idempotency_key; never select a new Attempt silently.'}
        if isinstance(error, LifecycleConflict) and error.turn:
            result['turn'] = _turn_view(error.turn)
    return json.dumps(result, ensure_ascii=False)
