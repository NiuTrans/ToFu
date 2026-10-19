"""Coordinator adapters over existing conversation and project-dispatch owners.

Creation is empty/create-only. Delegation derives a bounded grant from the current
accepted human request; dispatch and observation reuse ProjectDispatchService.
This module never synthesizes human principals or starts a model loop.
"""
from __future__ import annotations

import hashlib
import json


def _scope(task: dict) -> dict:
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    scope = coordinator_scope(task)
    if not scope:
        raise PermissionError('Only the bound project Coordinator can use this tool')
    binding = ProjectCoordinationRepository(scope['user_id']).read(scope['project_path'])
    if (binding['availability'] != 'available'
            or binding['conversationId'] != scope['conversation_id']
            or binding['revision'] != scope['binding_revision']):
        raise PermissionError('Coordinator binding changed; start a new authorized turn')
    return scope


def _create(scope: dict, arguments: dict) -> dict:
    from lib.conversations.repository import ConversationRepository
    from lib.storage import get_storage_client
    title = arguments.get('title')
    key = arguments.get('idempotency_key')
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ValueError('title must contain 1–160 characters')
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ValueError('idempotency_key must contain 1–200 characters')
    digest = hashlib.sha256(json.dumps(
        [scope['user_id'], scope['project_path'], scope['conversation_id'],
         scope['binding_revision'], key],
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    conversation_id = 'worker-' + digest[:24]
    request_digest = hashlib.sha256(json.dumps(
        [digest, title.strip()], separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    settings = {'projectPath': scope['project_path'],
                '_coordinatorSource': scope['conversation_id'], '_coordinatorCreationKey': digest,
                '_coordinatorCreationRequest': request_digest}
    repository = ConversationRepository()
    existing = repository.get(conversation_id, user_id=scope['user_id'], include_messages=False)

    if existing is None:
        source = repository.get(scope['conversation_id'], user_id=scope['user_id'], include_messages=False)
        source = getattr(source, 'metadata', source) or {}
        folder = (source.get('settings') or {}).get('folderId')
        initial_settings = {**settings, **({'folderId': folder} if folder else {})}
        get_storage_client(write=True).command('conversation.create', {
            'conv_id': conversation_id, 'user_id': scope['user_id'],
            'title': title.strip(), 'settings': initial_settings,
        }, 'coordinator-worker-' + digest)
        # A command acknowledgement is not proof that our header won a race.
        existing = repository.get(conversation_id, user_id=scope['user_id'], include_messages=False)
    if existing is not None and hasattr(existing, 'metadata'):
        existing = existing.metadata
    if existing is None:
        raise ValueError('Conversation creation could not be confirmed; retry the same idempotency_key')
    actual_settings = existing.get('settings') or {}
    # Title and ordinary settings remain editable. Only the original request
    # marker and owner/project/binding identity decide idempotent replay; losing
    # a marker or reparenting never silently adopts an unrelated conversation.
    if any(actual_settings.get(name) != value for name, value in settings.items()):
        raise ValueError('Conversation creation key conflicts with an existing conversation')
    return {'conversation_id': conversation_id, 'created_only': True,
            'execution_started': False, 'project_path': scope['project_path'],
            'next_step': ('Authorize an assignment for this conversation through the existing '
                          'human Project dispatch controls, then call dispatch_project_assignment. '
                          'Creation grants no execution rights.')}


def execute_coordinator_tool(task: dict, name: str, arguments: dict) -> str:
    """Handle explicit create/dispatch/read; all results preserve owner scope."""
    from lib.storage.errors import StorageError
    try:
        scope = _scope(task)
        if name == 'create_conversation':
            result = _create(scope, arguments)
        elif name in {'delegate_project_work', 'await_project_workers'}:
            from .coordinator_workers import delegate, await_workers
            result = (delegate if name == 'delegate_project_work' else await_workers)(task, scope, arguments)
        elif name == 'review_project_work':
            from lib.project_dispatch.repository import ProjectDispatchRepository
            from lib.project_dispatch.service import ProjectDispatchService
            result = ProjectDispatchService(ProjectDispatchRepository(scope['user_id'], scope['project_path'])).review_for_coordinator(task, scope, arguments)
        elif name == 'get_project_workers':
            from .coordinator_workers import get_workers
            result = get_workers(task, scope, arguments)
        elif name in {'finish_project_request', 'cancel_project_work'}:
            from lib.project_dispatch.repository import ProjectDispatchRepository
            repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
            if name == 'finish_project_request':
                from lib.project_dispatch.continuation import finish_request
                result = finish_request(repository, task, arguments)
            else:
                from lib.project_dispatch.service import ProjectDispatchService
                result = ProjectDispatchService(repository).cancel_for_coordinator(task, scope, arguments)
        elif name == 'report_to_user':
            from lib.conversations.coordinator_reports import CoordinatorReports
            result = CoordinatorReports(scope['user_id'], scope['project_path']).publish({
                'owner': scope['user_id'], 'projectPath': scope['project_path'],
                'coordinatorId': scope['conversation_id'], 'bindingRevision': scope['binding_revision'],
            }, arguments)
        elif name in {'dispatch_project_assignment', 'get_project_assignment'}:
            from lib.identity import PrincipalContext
            from lib.project_dispatch.repository import ProjectDispatchRepository
            from lib.project_dispatch.service import ProjectDispatchService
            repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
            service = ProjectDispatchService(repository)
            assignment_id = str(arguments.get('assignment_id') or '')
            assignment = repository.read(assignment_id)
            principal = PrincipalContext.from_payload(task['_principalContext'])
            if (assignment['request']['coordinatorId'] != scope['conversation_id']
                    or assignment['grant']['tenant'] != principal.tenant_id):
                raise PermissionError('Assignment belongs to another Coordinator or tenant')
            binding = repository.admission(assignment_id)
            if name == 'dispatch_project_assignment':
                revision = arguments.get('expected_revision')
                if type(revision) is not int or revision != binding['revision']:
                    raise ValueError('expected_revision must match the current human grant revision')
                result = service.dispatch_authorized(binding)
            else:
                result = service.view_for_coordinator(assignment_id, scope=scope)
        else:
            raise ValueError('Unknown Coordinator tool')
        return json.dumps(result, ensure_ascii=False)
    except (PermissionError, ValueError, KeyError, StorageError) as error:
        return json.dumps({'error': str(error),
                           'execution_started': None if name in {'dispatch_project_assignment', 'delegate_project_work', 'review_project_work'} else False,
                           'recovery': ('Read get_project_assignment before retrying an uncertain dispatch.'
                                        if name == 'dispatch_project_assignment' else
                                        'Retry delegate_project_work with unchanged arguments and the same idempotency_key; it reuses any accepted worker.'
                                        if name == 'delegate_project_work' else '')}, ensure_ascii=False)
