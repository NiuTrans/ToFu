"""Coordinator task board: the Coordinator's own summary of the human's tasks.

Entry points: ``CoordinatorTaskProgress.publish`` (the bound Coordinator's
``update_task_progress`` tool) and ``read`` (HTTP projection). One owner/project
head holds the latest complete board of the current Coordinator binding; a
replaced binding starts without a board. The board is model-authored by design
(docs/PROJECT_COORDINATION.md, "Session-free coordination"): a summary for the
human, not evidence, and it grants no authority. Questions for the human are
ask_human cards in the conversation, never board entries. No model, task,
worker or polling is started here.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import time
import uuid

from lib.conversations.coordinator_reports import CONTRACT
from lib.conversations.project_coordination import ProjectCoordinationRepository
from lib.conversations.project_identity import normalize_project_path, project_channel_key
from lib.identity import require_user_id
from lib.storage.errors import StorageError

POLICY = CONTRACT['taskProgress']


@lru_cache(maxsize=8)
def _validator(definition):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + definition})


def validate_progress(value, definition):
    error = next(_validator(definition).iter_errors(value), None)
    if error is not None:
        where = '/'.join(str(part) for part in error.absolute_path)
        raise ValueError('Invalid task board' + (' at ' + where if where else '') + ': ' + error.message)
    # Character limits alone must not admit invalid Unicode transport strings.
    json.dumps(value, ensure_ascii=False).encode('utf-8')
    return value


def _head_key(owner, project):
    digest = hashlib.sha256(json.dumps(project, ensure_ascii=False).encode()).hexdigest()
    return str(owner) + ':' + digest


class CoordinatorTaskProgress:
    def __init__(self, owner, project, *, client=None, bindings=None, notify=None, clock=time.time):
        self.owner = require_user_id(owner, context='Coordinator task board')
        if not isinstance(project, str) or not project.strip() or len(project) > 4096:
            raise ValueError('Project path is required')
        self.project = normalize_project_path(project.strip())
        self.key = _head_key(self.owner, self.project)
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.bindings = bindings if bindings is not None else ProjectCoordinationRepository(self.owner, client=client)
        self.notify = notify if notify is not None else self._notify
        self.clock = clock

    def _scope(self):
        binding = self.bindings.read(self.project)
        if binding['availability'] != 'available':
            return None, binding
        return {'owner': self.owner, 'projectPath': self.project,
                'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}, binding

    def _head(self):
        row = self.client().query('record.get', {'namespace': POLICY['namespace'], 'key': self.key})
        if row is None:
            return None
        try:
            validate_progress(row['value'], 'progressHead')
            scope = row['value']['scope']
            if scope['owner'] != self.owner or scope['projectPath'] != self.project:
                raise ValueError('Wrong task board owner/project')
            if type(row['version']) is not int or row['version'] < 1:
                raise ValueError('Invalid record version')
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError('database_integrity', 'Invalid Coordinator task board record') from exc
        return row

    def publish(self, scope, request):
        """Replace the board of exactly ``scope``'s binding; unchanged content is a no-op."""
        validate_progress(scope, 'reportScope')
        validate_progress(request, 'progressRequest')
        for _ in range(POLICY['casAttempts']):
            # A board racing a binding replacement must never reach the new team.
            if self._scope()[0] != scope:
                raise PermissionError('Coordinator binding changed')
            row = self._head()
            original = row['value'] if row else None
            if original and original['scope']['bindingRevision'] > scope['bindingRevision']:
                raise PermissionError('Coordinator binding changed')
            previous = original['board'] if original and original['scope'] == scope else None
            if previous and previous['summary'] == request['summary'] and previous['tasks'] == request['tasks']:
                return {'board': deepcopy(previous), 'changed': False}
            board = {'revision': previous['revision'] + 1 if previous else 1,
                     'updatedAt': int(self.clock() * 1000),
                     'summary': request['summary'], 'tasks': deepcopy(request['tasks'])}
            head = validate_progress({'scope': scope, 'board': board}, 'progressHead')
            try:
                self.client(write=True).command('record.put', {
                    'namespace': POLICY['namespace'], 'key': self.key, 'value': head,
                    'expected_version': row['version'] if row else 0,
                }, 'coordinator-progress-cas:' + uuid.uuid4().hex)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                # A lost acknowledgement re-reads its own board as unchanged.
                continue
            self._hint()
            return {'board': deepcopy(board), 'changed': True}
        raise StorageError('database_conflict', 'Task board changed concurrently; send the complete board again')

    def read(self):
        scope, binding = self._scope()
        result = {'projectPath': self.project, 'conversationId': binding['conversationId'],
                  'bindingRevision': binding['revision'], 'board': None}
        if scope is not None:
            row = self._head()
            if row and row['value']['scope'] == scope:
                result['board'] = deepcopy(row['value']['board'])
        return validate_progress(result, 'progressState')

    def _hint(self):
        try:
            self.notify()
        except Exception:
            # Push is a refresh hint; the committed head is the board.
            from lib.log import get_logger
            get_logger(__name__).debug('Coordinator task board push unavailable', exc_info=True)

    def _notify(self):
        from lib.agent_core.push import push_event
        push_event('project', project_channel_key(self.project),
                   {'projectKey': self.project, 'kind': 'coordinator_progress'}, user_id=self.owner)
