"""Coordinator reports use canonical Turns plus one small attention projection.

Entry points: publish/read/mark_read and recover_pending_reports. Portable CAS
stores a transient publication intent before Turn ingestion; boot recovery uses
the same idempotent command. The head stores pointers/read position, never a
second transcript. No model, task, worker or periodic polling is started here.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import uuid

from lib.conversations.project_coordination import ProjectCoordinationRepository
from lib.conversations.project_identity import normalize_project_path, project_channel_key
from lib.identity import require_user_id
from lib.storage.errors import StorageError

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts/project_coordination_v1.schema.json').read_text())
POLICY = CONTRACT['reporting']


@lru_cache(maxsize=8)
def _validator(definition):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                '$ref': '#/$defs/' + definition})


def validate_report(value, definition):
    if next(_validator(definition).iter_errors(value), None) is not None:
        raise ValueError('Invalid Coordinator report ' + definition)
    # Character limits alone must not admit invalid Unicode transport strings.
    json.dumps(value, ensure_ascii=False).encode('utf-8')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _head_key(owner, project):
    return str(owner) + ':' + _digest(project)


def _intent_key(scope, request):
    return _head_key(scope['owner'], scope['projectPath']) + ':' + _digest([scope, request['idempotency_key']])


class CoordinatorReports:
    def __init__(self, owner, project, *, client=None, bindings=None, append_turn=None, get_turn=None, notify=None):
        self.owner = require_user_id(owner, context='Coordinator reports')
        if not isinstance(project, str) or not project.strip() or len(project) > 4096:
            raise ValueError('Project path is required')
        self.project = normalize_project_path(project.strip())
        self.key = _head_key(self.owner, self.project)
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.bindings = bindings if bindings is not None else ProjectCoordinationRepository(self.owner, client=client)
        self.append_turn = append_turn
        self.get_turn = get_turn
        self.notify = notify if notify is not None else self._notify

    def _scope(self, expected=None):
        binding = self.bindings.read(self.project)
        if binding['availability'] != 'available':
            if expected is not None:
                raise PermissionError('Coordinator is unavailable')
            return None, binding
        scope = {'owner': self.owner, 'projectPath': self.project,
                 'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}
        if expected is not None and scope != expected:
            raise PermissionError('Coordinator binding changed')
        return scope, binding

    def _record(self, namespace, key, definition):
        row = self.client().query('record.get', {'namespace': namespace, 'key': key})
        if row is not None:
            try:
                validate_report(row['value'], definition)
                scope = row['value']['scope']
                if scope['owner'] != self.owner or scope['projectPath'] != self.project:
                    raise ValueError('Wrong report owner/project')
                if definition == 'reportHead' and row['value']['readOrdinal'] > row['value']['latest']['ordinal']:
                    raise ValueError('Report read position exceeds published history')
                if type(row['version']) is not int or row['version'] < 1:
                    raise ValueError('Invalid record version')
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageError('database_integrity', 'Invalid Coordinator report record') from exc
        return row

    def _put(self, namespace, key, value, version):
        return self.client(write=True).command('record.put', {
            'namespace': namespace, 'key': key, 'value': value, 'expected_version': version,
        }, 'coordinator-report-cas:' + uuid.uuid4().hex)

    def _delete_intent(self, key, version):
        self.client(write=True).command('record.delete', {
            'namespace': POLICY['pendingNamespace'], 'key': key, 'expected_version': version,
        }, 'coordinator-report-retire:' + uuid.uuid4().hex)

    def _retain_intent(self, scope, request):
        key = _intent_key(scope, request)
        value = {'scope': scope, 'request': deepcopy(request)}
        for attempt in range(POLICY['casAttempts'] + 1):
            self._scope(scope)
            row = self._record(POLICY['pendingNamespace'], key, 'reportIntent')
            if row:
                if row['value'] != value:
                    raise ValueError('Report idempotency key was reused with different content')
                return key, row
            if attempt == POLICY['casAttempts']:
                break
            try:
                self._put(POLICY['pendingNamespace'], key, value, 0)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
        raise StorageError('database_conflict', 'Report publication is concurrent; retry the same key')

    @staticmethod
    def _content(request):
        return request['title'] + '\n\n' + request['message']

    def publish(self, scope, request):
        validate_report(scope, 'reportScope')
        validate_report(request, 'reportRequest')
        if not request['title'].strip() or not request['message'].strip() or not request['idempotency_key'].strip():
            raise ValueError('Report title, message and idempotency key must not be blank')
        self._scope(scope)
        key, intent = self._retain_intent(scope, request)
        return self._deliver(key, intent)

    def _deliver(self, key, intent):
        value = intent['value']; scope, request = value['scope'], value['request']
        validate_report(value, 'reportIntent')
        if type(intent['version']) is not int or intent['version'] < 1:
            raise StorageError('database_integrity', 'Invalid report intent version')
        if key != _intent_key(scope, request):
            raise StorageError('database_integrity', 'Report intent identity changed')
        self._scope(scope)
        from lib.turn_lifecycle import LifecycleConflict
        append_turn = self.append_turn
        if append_turn is None:
            from lib.turn_lifecycle import append_settled_turn
            append_turn = append_settled_turn
        try:
            turn = append_turn(scope['coordinatorId'], user_id=self.owner,
                command_id='coordinator-report:' + _digest([scope, request['idempotency_key']]),
                actor='assistant', kind='coordinator_report.' + request['kind'],
                projection={'content': self._content(request), 'origin': {'initiator': 'brain'}})['turn']
        except LifecycleConflict as exc:
            # A semantic conflict definitively rejects this append. Uncertain
            # transport/storage failures instead retain the intent for replay.
            if exc.code != 'database_conflict':
                raise
            self._delete_intent(key, intent['version'])
            raise ValueError('Report idempotency key conflicts with its original message') from exc
        # A transport receipt is not authority to adopt another Turn on replay.
        if (turn['conversationId'] != scope['coordinatorId'] or turn['actor'] != 'assistant'
                or turn['kind'] != 'coordinator_report.' + request['kind']
                or turn['projection'].get('content') != self._content(request)):
            # This is a confirmed conflicting replay, not an uncertain commit.
            # Retire only its transient intent so the original key can retry.
            self._delete_intent(key, intent['version'])
            raise ValueError('Report idempotency key conflicts with its original message')
        pointer = {key: turn[key] for key in ('turnId', 'ordinal', 'createdAt')}
        pointer.update(title=request['title'], kind=request['kind'])
        validate_report(pointer, 'reportPointer')
        self._change_head(scope, pointer=pointer)
        # Intent is reconstructible transport state only after both canonical
        # message and attention pointer have committed. Lost ACK replays safely.
        self._delete_intent(key, intent['version'])
        self._hint(scope['coordinatorId'])
        return {'conversationId': scope['coordinatorId'], 'turnId': turn['turnId'], 'published': True}

    def _change_head(self, scope, *, pointer=None, read_id=None):
        for _ in range(POLICY['casAttempts']):
            self._scope(scope)
            row = self._record(POLICY['headNamespace'], self.key, 'reportHead')
            original = row['value'] if row else None
            if original and original['scope']['bindingRevision'] > scope['bindingRevision']:
                raise PermissionError('Coordinator binding changed')
            current = deepcopy(original) if original and original['scope'] == scope else None
            if pointer is not None:
                if current is None:
                    current = {'scope': scope, 'latest': pointer, 'readOrdinal': -1}
                elif pointer['ordinal'] > current['latest']['ordinal']:
                    current['latest'] = pointer
                elif pointer['ordinal'] == current['latest']['ordinal'] and pointer != current['latest']:
                    raise StorageError('database_integrity', 'Report ordinal has conflicting identity')
            elif current and current['latest']['turnId'] == read_id:
                current['readOrdinal'] = max(current['readOrdinal'], current['latest']['ordinal'])
            if current is None or current == original:
                return
            validate_report(current, 'reportHead')
            try:
                self._put(POLICY['headNamespace'], self.key, current, row['version'] if row else 0)
                return
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
        raise StorageError('database_conflict', 'Report attention changed; refresh before retrying')

    def read(self):
        scope, binding = self._scope()
        result = {'projectPath': self.project, 'conversationId': binding['conversationId'],
                  'bindingRevision': binding['revision'], 'latest': None, 'unread': False}
        if scope is None:
            return result
        row = self._record(POLICY['headNamespace'], self.key, 'reportHead')
        if not row or row['value']['scope'] != scope:
            return result
        head = row['value']; pointer = head['latest']
        get_turn = self.get_turn
        if get_turn is None:
            from lib.turn_lifecycle import get_turn
        turn = get_turn(scope['coordinatorId'], pointer['turnId'], user_id=self.owner)
        if (not turn or turn['conversationId'] != scope['coordinatorId']
                or turn['actor'] != 'assistant' or turn['ordinal'] != pointer['ordinal']
                or turn['kind'] != 'coordinator_report.' + pointer['kind']):
            raise StorageError('database_integrity', 'Coordinator report message is unavailable')
        self._scope(scope)  # A read racing replacement must not show old-team content.
        result['latest'] = {**pointer, 'content': turn['projection'].get('content', '')}
        result['unread'] = pointer['ordinal'] > head['readOrdinal']
        validate_report(result, 'reportState')
        return result

    def mark_read(self, request):
        validate_report(request, 'reportReadRequest')
        if normalize_project_path(request['path']) != self.project:
            raise PermissionError('Report belongs to another project')
        scope = {'owner': self.owner, 'projectPath': self.project,
                 'coordinatorId': request['conversationId'], 'bindingRevision': request['bindingRevision']}
        self._scope(scope)
        # A stale tab acknowledging A must never acknowledge newer report B.
        self._change_head(scope, read_id=request['turnId'])
        self._hint(scope['coordinatorId'])
        return self.read()

    def _hint(self, conversation_id):
        try:
            self.notify(conversation_id)
        except Exception:
            # Push is a hint, never the acknowledgement for a durable message.
            from lib.log import get_logger
            get_logger(__name__).debug('Coordinator report push unavailable', exc_info=True)

    def _notify(self, conversation_id):
        from lib.agent_core.push import push_event
        from lib.conversations.change_notifications import notify_conv_changed
        notify_conv_changed(conversation_id, user_id=self.owner)
        push_event('project', project_channel_key(self.project),
                   {'projectKey': self.project, 'kind': 'coordinator_report'}, user_id=self.owner)


def recover_pending_reports(*, client=None, repository_factory=CoordinatorReports):
    """Bounded startup pages through the existing project recovery lifecycle.

    This only publishes already-recorded report intents. It never asks a model,
    creates a conversation, dispatches a task or extends execution authority.
    """
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client()
    cursor = ''; recovered = 0
    while True:
        page = client.query('record.list', {'namespace': POLICY['pendingNamespace'],
                            'prefix': '', 'after_key': cursor, 'limit': POLICY['recoveryPageSize']}, deadline=30.0)
        if not isinstance(page, list) or len(page) > POLICY['recoveryPageSize']:
            raise StorageError('database_integrity', 'Invalid report recovery page')
        if not page:
            return recovered
        for row in page:
            key = row['key']
            if not isinstance(key, str) or key <= cursor:
                raise StorageError('database_integrity', 'Invalid report recovery cursor')
            cursor = key
            validate_report(row['value'], 'reportIntent')
            scope = row['value']['scope']
            repository = repository_factory(scope['owner'], scope['projectPath'])
            try:
                repository._deliver(key, row)
                recovered += 1
            except PermissionError:
                # Rebinding withdraws pending transport; any already committed
                # message remains in the old conversation's canonical history.
                repository._delete_intent(key, row['version'])
            except Exception:
                from lib.log import get_logger
                get_logger(__name__).warning('Coordinator report remains pending: %s', key, exc_info=True)
