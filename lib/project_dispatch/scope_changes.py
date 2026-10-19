"""Explicit-human scope removal, never permission expansion or task creation.

ProjectScopeChanges.create/list/view/retry use the dispatch repository's atomic
multi-revoke CAS. This contract owns only change/receipt evidence. Genuine source
validation is shared with dispatch; cancellation and passive originals belong to
existing attempt and ProjectMailbox owners. No scheduler, paid wake, new task
states, regrant, rework, publication or result overwrite is introduced.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator

from lib.identity import PrincipalContext
from .repository import ProjectDispatchError, _identifier, _positive_revision, evidence_owner_matches

from lib.log import get_logger

logger = get_logger(__name__)

CONTRACT = json.loads((Path(__file__).resolve().parents[2] /
                       'contracts/project_scope_change_v1.schema.json').read_text())
LIMITS = CONTRACT['limits']


@lru_cache(maxsize=16)
def _validator(name):
    return Draft202012Validator({**CONTRACT, '$ref': '#/$defs/' + name})


def validate(value, name):
    if not isinstance(value, dict) or not _validator(name).is_valid(value):
        raise ProjectDispatchError('Invalid scope change ' + name)
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectDispatchError('Scope change must be finite UTF-8 JSON') from exc
    if name in ('request', 'change') and size > LIMITS['maxChangeBytes']:
        raise ProjectDispatchError('Scope change byte capacity reached')


def schema(name):
    """Inline local references for HTTP metadata and generated DTO consumers."""
    def inline(value):
        if isinstance(value, dict):
            if '$ref' in value:
                return inline(CONTRACT['$defs'][value['$ref'].split('/')[-1]])
            return {k: inline(v) for k, v in value.items()}
        if isinstance(value, list):
            return [inline(v) for v in value]
        return value
    return inline(deepcopy(CONTRACT['$defs'][name]))


def request_body(name):
    return {'required': True, 'content': {'application/json': {'schema': schema(name)}}}


def query_parameters():
    definition = schema('listQuery')
    return [{'name': k, 'in': 'query', 'required': k in definition['required'], 'schema': v}
            for k, v in definition['properties'].items()]


def parse(name):
    from lib.request_parser import parse_body
    body = parse_body(exact=tuple(CONTRACT['$defs'][name]['properties']))
    validate(body, name)
    return body


def require_principal(repo, principal):
    if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
            or type(principal.owner_user_id) is not int or principal.owner_user_id != repo.owner):
        raise PermissionError('Scope changes require the owning human principal')
    principal.require_scope('chat')


def command_identity(repo, command_id):
    _identifier(command_id)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'project-scope-change:v1:{repo.key}:{command_id}'))


def _find(value, identity):
    _identifier(identity)
    row = next((r for r in value.get('scopeChanges', []) if r['changeId'] == identity), None)
    if row is None:
        raise ProjectDispatchError('Scope change not found in owned project')
    return row


def append_change(repo, value, request, principal, command_id):
    """Pure CAS callback: all validations precede every grant/evidence mutation."""
    require_principal(repo, principal)
    identity = command_identity(repo, command_id)
    old = next((r for r in value.get('scopeChanges', []) if r['changeId'] == identity), None)
    if old is not None:
        if (old['request'] != request or old['tenant'] != principal.tenant_id
                or old['subject'] != principal.subject_id):
            raise ProjectDispatchError('Scope change command reused with changed request or granter')
        return old  # ACK replay preserves evidence, even after source/rebinding changes.
    if len(value.get('scopeChanges', [])) >= LIMITS['changesPerProject']:
        raise ProjectDispatchError('Scope change capacity reached; no evidence evicted')
    repo.validate_scope_change_source(request)
    source = request['authorizationSource']['conversationId']
    if source != request['coordinatorId'] and not any(
            r['request']['recipientId'] == source and r['request']['coordinatorId'] == request['coordinatorId']
            and r['grant']['tenant'] == principal.tenant_id for r in value['assignments']):
        raise ProjectDispatchError('Human source must be coordinator or an owned worker')
    selected = []
    ids = set()

    if request['kind'] == 'shrink' and not request['targets']:
        raise ProjectDispatchError('Scope shrink requires at least one pinned grant')
    for pin in request['targets']:
        _identifier(pin['assignmentId'])
        _positive_revision(pin['grantRevision'])
        if pin['assignmentId'] in ids:
            raise ProjectDispatchError('Duplicate scope change target')
        ids.add(pin['assignmentId'])
        row = repo._find(value, pin['assignmentId'])
        grant = row['grant']
        if (grant['tenant'] != principal.tenant_id or grant['revoked']
                or grant['revision'] != pin['grantRevision']
                or row['request']['coordinatorId'] != request['coordinatorId']):
            raise ProjectDispatchError('Target grant revision, tenant or coordinator changed')
        repo._conversation(row['request']['recipientId'])
        selected.append(row)
    if request['kind'] == 'stop_project':
        current = {r['id'] for r in value['assignments'] if not r['grant']['revoked']
                   and r['grant']['tenant'] == principal.tenant_id
                   and r['request']['coordinatorId'] == request['coordinatorId']}
        if ids != current:
            raise ProjectDispatchError('Project stop requires exact pins for all current grants')
    revocations = [{'assignmentId': r['id'], 'grantRevision': r['grant']['revision'],
                    'revokedRevision': r['grant']['revision'] + 1,
                    'recipientId': r['request']['recipientId'], 'execution': deepcopy(r['execution']),
                    'cancellation': {'state': 'pending' if r['execution'] else 'unbound',
                                     'status': '', 'error': ''}} for r in selected]
    recipients = sorted({request['coordinatorId'], *(r['recipientId'] for r in revocations)} - {source})
    change = {'changeId': identity, 'request': deepcopy(request), 'owner': repo.owner,
              'tenant': principal.tenant_id, 'subject': principal.subject_id, 'createdAt': repo.clock(),
              'revocations': revocations, 'notifications': [
                  {'recipientId': recipient, 'state': 'pending', 'messageId': '', 'error': ''}
                  for recipient in recipients]}
    validate(change, 'change')
    for row in selected:
        row['grant'].update(revoked=True, revision=row['grant']['revision'] + 1)
    value.setdefault('scopeChanges', []).append(change)
    return change


def _check_cancellation(execution, outcome):
    validate(outcome, 'cancellation')
    if ((outcome['state'] == 'unbound') != (execution is None)
            or outcome['state'] == 'signaled' and outcome['status'] != 'abort_signaled'
            or outcome['state'] == 'terminal' and not (
                ProjectScopeChanges._terminal(outcome['status']) or outcome['status'] == 'cancelled')):
        raise ProjectDispatchError('Cancellation receipt cannot prove this execution outcome')


def check_extension(repo, value):
    validate({'scopeChanges': value['scopeChanges']}, 'extension')
    seen = set()
    for change in value['scopeChanges']:
        validate(change, 'change')
        if (type(change['owner']) is not int or change['owner'] != repo.owner
                or change['request']['projectPath'] != repo.project or change['changeId'] in seen):
            raise ProjectDispatchError('Invalid scope change binding/identity')
        seen.add(change['changeId'])

        _positive_revision(change['request']['authorizationSource']['projectionRevision'])
        pins = {p['assignmentId']: p['grantRevision'] for p in change['request']['targets']}
        if (len(pins) != len(change['request']['targets']) or
                len(change['revocations']) != len(pins) or
                {r['assignmentId'] for r in change['revocations']} != set(pins)):
            raise ProjectDispatchError('Scope change target evidence differs')
        for r in change['revocations']:

            _positive_revision(r['revokedRevision'])
            _check_cancellation(r['execution'], r['cancellation'])
            assignment = repo._find(value, r['assignmentId'])
            if (r['grantRevision'] != pins[r['assignmentId']] or
                    type(r['grantRevision']) is not int or
                    r['revokedRevision'] != r['grantRevision'] + 1 or
                    assignment['grant']['revision'] != r['revokedRevision'] or
                    not assignment['grant']['revoked'] or assignment['grant']['tenant'] != change['tenant'] or
                    assignment['request']['recipientId'] != r['recipientId'] or
                    assignment['request']['coordinatorId'] != change['request']['coordinatorId'] or
                    assignment['execution'] != r['execution']):
                raise ProjectDispatchError('Scope change revocation evidence differs')
        recipients = {change['request']['coordinatorId'], *(r['recipientId'] for r in change['revocations'])}
        recipients.discard(change['request']['authorizationSource']['conversationId'])
        if (len(change['notifications']) != len(recipients) or
                {n['recipientId'] for n in change['notifications']} != recipients):
            raise ProjectDispatchError('Scope change notification identities differ')

        for notification in change['notifications']:
            if notification['state'] == 'submitted':
                _identifier(notification['messageId'])

def record_outcome(value, change_id, collection, identity, outcome):
    change = _find(value, change_id)
    if collection == 'revocations':
        validate(outcome, 'cancellation')
        row = next((r for r in change[collection] if r['assignmentId'] == identity), None)
        if row is None:
            raise ProjectDispatchError('Scope change cancellation target not found')

        _check_cancellation(row['execution'], outcome)
        # Late failure/ACK cannot erase committed terminal or signaled evidence.
        rank = {'pending': 0, 'failed': 0, 'signaled': 1, 'unbound': 2, 'terminal': 2}
        if rank[outcome['state']] >= rank[row['cancellation']['state']]:
            row['cancellation'] = outcome
    elif collection == 'notifications':
        validate(outcome, 'notification')

        if outcome['state'] == 'submitted':
            _identifier(outcome['messageId'])
        row = next((r for r in change[collection] if r['recipientId'] == identity), None)
        if row is None or outcome['recipientId'] != identity:
            raise ProjectDispatchError('Scope change notification target not found')
        if row['state'] != 'submitted':
            row.update(outcome)
        elif outcome['state'] == 'submitted' and row['messageId'] != outcome['messageId']:
            raise ProjectDispatchError('Scope change original message is immutable')
    else:
        raise ProjectDispatchError('Unknown scope change receipt collection')
    return change


class ProjectScopeChanges:
    """Bounded application API; reads never cancel, notify, dispatch or spend."""
    def __init__(self, repository, *, commands=None, mailbox_factory=None, publication_lock=None):
        self.repo = repository
        self.commands = commands
        self.mailbox_factory = mailbox_factory
        self.publication_lock = publication_lock

    def _mailbox(self, recipient):
        if self.mailbox_factory is not None:
            return self.mailbox_factory(self.repo.owner, self.repo.project, recipient)
        from lib.conversations.project_messages import ProjectMailbox
        return ProjectMailbox(self.repo.owner, self.repo.project, recipient, client=self.repo.client)

    def _owned(self, change_id, principal):
        require_principal(self.repo, principal)
        change = _find({'scopeChanges': self.repo.scope_changes()}, change_id)
        if change['tenant'] != principal.tenant_id:
            raise PermissionError('Scope change tenant differs')
        return change

    def create(self, request, *, principal, command_id):
        # Existing publication lock serializes withdrawal with canonical writes.
        lock = self.publication_lock
        if lock is None:
            from lib.integration_control import _repo_lock
            lock = _repo_lock
        with lock(self.repo.project):
            change = self.repo.revoke_scope_change(request, principal=principal, command_id=command_id)
        return self.retry(change['changeId'], principal=principal)

    def list(self, *, principal, command_id=None):
        require_principal(self.repo, principal)
        identity = command_identity(self.repo, command_id) if command_id is not None else None
        return [self._view(change) for change in self.repo.scope_changes()
                if change['tenant'] == principal.tenant_id and
                (identity is None or change['changeId'] == identity)]

    def view(self, change_id, *, principal):
        return self._view(self._owned(change_id, principal))

    def _attempt(self, revocation):
        association = revocation['execution']
        attempt = self.repo.get_attempt(association['attemptId'])
        if (not isinstance(attempt, Mapping) or not evidence_owner_matches(attempt, self.repo.owner)
                or attempt.get('attemptId') != association['attemptId']
                or attempt.get('turnId') != association['turnId']
                or attempt.get('conversationId') != revocation['recipientId']):
            raise ProjectDispatchError('Existing cancellation association is unavailable or changed')
        return attempt

    @staticmethod
    def _terminal(status):
        from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS
        return status in set(OPENAPI_SCHEMAS['AttemptStatus']['enum']) - {'pending', 'running'}

    def _cancel(self, revocation):
        attempt = self._attempt(revocation)
        status = attempt.get('status')
        if self._terminal(status):
            return {'state': 'terminal', 'status': status, 'error': ''}
        if status not in ('pending', 'running'):
            raise ProjectDispatchError('Unknown existing attempt status')
        if revocation['cancellation']['state'] == 'signaled':
            return revocation['cancellation']
        commands = self.commands
        if commands is None:
            from lib.conversation_sync.runtime import conversation_turn_commands
            commands = conversation_turn_commands
        # Never fall back to conversation-wide Stop: that affects unrelated grants/tasks.
        outcome = commands.abort_project_attempt(attempt['attemptId'], self.repo.owner)
        response = getattr(outcome, 'value', outcome)
        if not isinstance(response, Mapping) or response.get('attemptId') != attempt['attemptId']:
            raise ProjectDispatchError('Cancellation owner returned no exact receipt')
        # A signal is not terminal. Inspect durable attempt state, not agent text.
        observed = self._attempt(revocation)
        if self._terminal(observed.get('status')):
            return {'state': 'terminal', 'status': observed['status'], 'error': ''}
        if response.get('status') == 'abort_signaled':
            return {'state': 'signaled', 'status': 'abort_signaled', 'error': ''}
        if response.get('status') == 'cancelled' and response.get('cancelled') is True:
            return {'state': 'terminal', 'status': 'cancelled', 'error': ''}
        raise ProjectDispatchError('Cancellation outcome is not durably confirmed')

    def retry(self, change_id, *, principal):
        change = self._owned(change_id, principal)
        for revocation in change['revocations']:
            if revocation['cancellation']['state'] in ('unbound', 'terminal'):
                continue
            try:
                outcome = self._cancel(revocation)
            except Exception as exc:
                logger.debug('retry failure handled: %s', type(exc).__name__)
                outcome = {'state': 'failed', 'status': '', 'error': type(exc).__name__[:256]}
            self.repo.record_scope_change_outcome(change_id, 'revocations', revocation['assignmentId'], outcome)
        source = change['request']['authorizationSource']['conversationId']
        for notification in change['notifications']:
            if notification['state'] == 'submitted':
                continue
            recipient = notification['recipientId']
            try:
                self.repo.validate_scope_change_notification(source, recipient)
                message = self._mailbox(recipient).submit(source, {
                    'kind': 'steer', 'idempotency_key': 'scope-change:' + change_id,
                    'content': f"Scope change {change_id} ({change['request']['kind']}). "
                               "Pinned existing grants were revoked; this passive notice grants no execution authority.\n"
                               + change['request']['note']}, '')
                outcome = {'recipientId': recipient, 'state': 'submitted',
                           'messageId': message['id'], 'error': ''}
            except Exception as exc:
                logger.debug('retry failure handled: %s', type(exc).__name__)
                outcome = {'recipientId': recipient, 'state': 'failed', 'messageId': '',
                           'error': type(exc).__name__[:256]}
            self.repo.record_scope_change_outcome(change_id, 'notifications', recipient, outcome)
        return self.view(change_id, principal=principal)

    def _view(self, change):
        result = {k: deepcopy(change[k]) for k in ('changeId', 'request', 'createdAt', 'revocations', 'notifications')}
        result['authorityRevoked'] = True
        result['stopConfirmed'] = all(r['cancellation']['state'] in ('unbound', 'terminal')
                                      for r in change['revocations'])
        for notification in result['notifications']:
            notification['messageEvidence'] = None
            if notification['state'] != 'submitted':
                continue
            try:
                originals = self._mailbox(notification['recipientId']).load()[0]['messages']
                original = next(m for m in originals if m['id'] == notification['messageId'])
                notification['messageEvidence'] = {k: original[k] for k in ('id', 'revision', 'state', 'decision')}
            except Exception as diagnostic_error:
                logger.debug('_view failure handled: %s', type(diagnostic_error).__name__)
                pass  # Receipt remains submitted; missing ACK/consideration is unknown.
        validate(result, 'view')
        return result
