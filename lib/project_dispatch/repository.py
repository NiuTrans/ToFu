"""Owner-scoped dispatch authority; never starts tasks or grants tools.

ProjectDispatchRepository commits selected evidence through an owned project CAS.
Legacy records remain passively readable until the first authorized mutation.
The canonical schema owns request/admission validation; conversation metadata,
coordination binding and source-turn ports are rechecked at every admission.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path

from functools import lru_cache
import time
import uuid

from lib.conversations.project_identity import normalize_project_path
from lib.identity import PrincipalContext, require_user_id
from lib.agent_core.storage_errors import StorageError
from lib.turn_initiation import resolve_initiator

CONTRACT = json.loads((Path(__file__).resolve().parents[2] /
                       'contracts/project_dispatch_v1.schema.json').read_text())
NAMESPACE = CONTRACT['namespace']
LIMITS = CONTRACT['limits']


class ProjectDispatchError(ValueError):
    """Malformed request, unavailable authority or conflicting durable evidence."""


@lru_cache(maxsize=16)
def _validator(definition):
    # Ordinary tool admission imports this authority without validating a grant.
    from jsonschema import Draft202012Validator
    return Draft202012Validator({**CONTRACT, '$ref': '#/$defs/' + definition})


def _validate_schema(value, definition):
    if not isinstance(value, dict) or not _validator(definition).is_valid(value):
        raise ProjectDispatchError(f'Invalid dispatch {definition}')
    # JSON Schema permits nonfinite Python floats; durable JSON does not.
    try:
        json.dumps(value, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ProjectDispatchError('Dispatch must be finite UTF-8 JSON') from exc


def batch_requests(request):
    """Validate/derive exact child requests, without reads, effects or authority.

    The original objective/worker briefs remain in the manifest. Every worker
    receives that objective in its immutable assignment brief; the existing
    assignment schema still bounds the complete derived brief.
    """
    _validate_schema(request, 'batchRequest')
    workers = request['workers']
    recipients = [worker['recipientId'] for worker in workers]
    if (len(recipients) != len(set(recipients))
            or request['coordinatorId'] in recipients):
        raise ProjectDispatchError('Batch requires distinct non-coordinator recipients')
    if request['authorizationSource']['conversationId'] != request['coordinatorId']:
        raise ProjectDispatchError('Authorization source must be in coordinator')
    for cap in ('maxTokens', 'maxModelRequests'):
        if sum(worker['budget'][cap] for worker in workers) > request['budget'][cap]:
            raise ProjectDispatchError('Batch child budgets exceed aggregate ' + cap)
    if any(worker['budget']['deadline'] > request['budget']['deadline'] for worker in workers):
        raise ProjectDispatchError('Batch child deadline exceeds aggregate deadline')
    common = {key: request[key] for key in ('projectPath', 'coordinatorId', 'authorizationSource')}
    children = []
    for worker in workers:
        child = copy.deepcopy({**common, **worker})
        child['brief'] = ('Original batch objective:\n' + request['objective']
                          + '\n\nWorker responsibility:\n' + worker['brief'])
        _validate_schema(child, 'request')
        if child.get('profile') is not None:
            from .scope import validate_profile
            validate_profile(child)
        children.append(child)
    return children


def _batch_assignment_identity(batch_id, ordinal):
    """Domain-separated deterministic member IDs; never supplied by callers."""
    return str(uuid.uuid5(uuid.UUID(batch_id), f'worker:{ordinal}'))


def evidence_owner_matches(value, owner):
    """Owner-scoped ports may omit identity, but present evidence must be typed."""
    return all(type(value[key]) is int and value[key] > 0 and value[key] == owner
               for key in ('user_id', 'owner_user_id') if key in value)


def _positive_revision(value):
    if type(value) is not int or value < 1:
        raise ProjectDispatchError('A positive expected revision is required')


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or not value.strip():
        raise ProjectDispatchError('A bounded nonempty identity is required')


def _stop_key(owner, conversation_id):
    owner = require_user_id(owner, context='project dispatch Stop watermark')
    _identifier(conversation_id)
    return f'{owner}:{hashlib.sha256(conversation_id.encode()).hexdigest()}'


def stopped_after(owner, conversation_id, created_at, *, client):
    row = client(write=False).query('record.get', {
        'namespace': CONTRACT['stopNamespace'], 'key': _stop_key(owner, conversation_id)})
    if row is None:
        return False
    value = row.get('value')
    _validate_schema(value, 'stop')
    if value['owner'] != owner or value['conversationId'] != conversation_id:
        raise ProjectDispatchError('Stop watermark identity mismatch')
    return value['occurredAt'] >= created_at


def record_stop(owner, conversation_id, occurred_at, *, client=None):
    """One max-timestamp record per owned conversation; no project/TTL dependency."""
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    value = {'owner': owner, 'conversationId': conversation_id, 'occurredAt': occurred_at}
    _validate_schema(value, 'stop')
    key = _stop_key(owner, conversation_id)
    for _ in range(LIMITS['casAttempts']):
        row = client(write=False).query('record.get', {'namespace': CONTRACT['stopNamespace'], 'key': key})
        if row is not None:
            previous = row.get('value')
            _validate_schema(previous, 'stop')
            if previous['owner'] != owner or previous['conversationId'] != conversation_id:
                raise ProjectDispatchError('Stop watermark identity mismatch')
            if previous['occurredAt'] >= occurred_at:
                return previous['occurredAt']
        try:
            client(write=True).command('record.put', {
                'namespace': CONTRACT['stopNamespace'], 'key': key, 'value': value,
                'expected_version': row['version'] if row else 0}, 'project-dispatch-stop:' + uuid.uuid4().hex)
            return occurred_at
        except StorageError as exc:
            if exc.code != 'database_conflict' and not exc.retryable:
                raise
    raise ProjectDispatchError('Concurrent/uncertain Stop watermark; retry the same Stop')


class ProjectDispatchRepository:
    """Durable evidence scoped by explicit owner and canonical project.

    Injected clients follow get_storage_client(write=False/True); conversations
    follow ConversationRepository.get; turn_get follows get_turn(..., user_id=).
    binding_repository follows ProjectCoordinationRepository.read(project).
    clock returns Unix seconds (the request budget deadline uses that unit).
    """

    def __init__(self, owner: int, project: str, *, client=None,
                 conversations=None, turn_get=None, binding_repository=None,
                 clock=None, attempt_get=None, workspace_get=None, storage_version=2):
        self.owner = require_user_id(owner, context='project dispatch')
        if not isinstance(project, str) or not project.strip() or len(project) > 4096:
            raise ProjectDispatchError('A bounded project path is required')
        self.project = normalize_project_path(project.strip())
        if not self.project:
            raise ProjectDispatchError('A canonical project is required')
        if storage_version not in (1, 2):
            raise ValueError('Unsupported dispatch storage contract')
        self.storage_version = storage_version
        self.binding = {'owner': self.owner, 'projectPath': self.project}
        self.key = f'{self.owner}:{hashlib.sha256(self.project.encode()).hexdigest()}'
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        if conversations is None:
            from lib.conversations.repository import ConversationRepository
            conversations = ConversationRepository(client)
        if turn_get is None:
            from lib.turn_lifecycle import get_turn
            turn_get = get_turn
        if binding_repository is None:
            from lib.conversations.project_coordination import ProjectCoordinationRepository
            binding_repository = ProjectCoordinationRepository(
                self.owner, client=client, conversations=conversations)
        self.client = client
        self.conversations = conversations
        self.turn_get = turn_get

        self.attempt_get = attempt_get

        self.workspace_get = workspace_get
        self.dependency_graph_limit = LIMITS['assignmentsPerProject']
        self.binding_repository = binding_repository
        self.clock = clock if clock is not None else time.time

    def _check_record(self, value, *, segmented=False):
        if (not isinstance(value, dict) or value.get('binding') != self.binding
                or not isinstance(value.get('assignments'), list)
                or not segmented and len(value['assignments']) > LIMITS['assignmentsPerProject']):
            raise ProjectDispatchError('Invalid dispatch record binding/capacity')
        try:
            size = len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                  separators=(',', ':')).encode('utf-8'))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ProjectDispatchError('Invalid dispatch record JSON') from exc
        if not segmented and size > LIMITS['maxRecordBytes']:
            raise ProjectDispatchError('Dispatch record byte capacity reached; no evidence evicted')

        # The separate scope-change contract owns this same-record extension.
        if not segmented:
            _validate_schema({k: v for k, v in value.items() if k != 'scopeChanges'}, 'record')
        else:
            for field, definition in (('assignments', 'assignment'), ('requestBudgets', 'coordinatorRequestBudget'),
                                      ('coordinatorSettlements', 'coordinatorSettlement')):
                for row in value.get(field, []):
                    _validate_schema(row, definition)
        if 'scopeChanges' in value:
            from .scope_changes import check_extension
            check_extension(self, value)
        settlements = value.get('coordinatorSettlements', [])
        budgets = value.get('requestBudgets', [])
        if len({row['id'] for row in budgets}) != len(budgets):
            raise ProjectDispatchError('Duplicate original request budget')
        for row in budgets:
            accounts = {account['taskId']: account for account in row['accounts']}
            if len(accounts) != len(row['accounts']) or row['authorizationSource']['conversationId'] != row['coordinatorId']:
                raise ProjectDispatchError('Invalid original request account identity')
            if any(event['root_task_id'] not in accounts or event['account_nonce'] != accounts[event['root_task_id']]['nonce']
                   for event in row['modelAttempts']):
                raise ProjectDispatchError('Original request occurrence belongs to another account')
            if len({(event['root_task_id'], event['attempt_ordinal']) for event in row['modelAttempts']}) != len(row['modelAttempts']):
                raise ProjectDispatchError('Duplicate original request occurrence')
        sources = {row['request'].get('delegation', {}).get('attemptId') for row in value['assignments']}
        if (len({row['sourceAttemptId'] for row in settlements}) != len(settlements)
                or any(row['sourceAttemptId'] not in sources for row in settlements)):
            raise ProjectDispatchError('Coordinator settlement requires one exact retained request')
        seen = set()
        for assignment in value['assignments']:
            if not isinstance(assignment, dict):
                raise ProjectDispatchError('Invalid dispatch assignment')
            _identifier(assignment.get('id'))
            if assignment['id'] in seen:
                raise ProjectDispatchError('Duplicate dispatch identity')
            seen.add(assignment['id'])
            _validate_schema(assignment.get('request'), 'request')

            if assignment['request'].get('profile') is not None:
                from .scope import validate_profile
                validate_profile(assignment['request'])
            tools = assignment.get('toolAttempts', [])
            if len(tools) > assignment['request'].get('profile', {}).get('maxToolCalls', 0):
                raise ProjectDispatchError('Dispatch tool-attempt capacity exceeded')
            if any(item['tool_name'] not in assignment['request'].get('profile', {}).get('tools', []) for item in tools):
                raise ProjectDispatchError('Dispatch tool occurrence exceeds its granted profile')
            if len({(row['root_task_id'], row['tool_ordinal']) for row in tools}) != len(tools):
                raise ProjectDispatchError('Duplicate dispatch tool occurrence')
            grant = assignment.get('grant')
            if (assignment['request']['projectPath'] != self.project
                    or not isinstance(grant, dict) or grant.get('owner') != self.owner
                    or type(grant.get('owner')) is not int
                    or not isinstance(grant.get('subject'), str) or not grant['subject']
                    or type(grant.get('revoked')) is not bool
                    or type(grant.get('createdAt')) not in (int, float)
                    or not math.isfinite(grant['createdAt'])
                    or not isinstance(assignment.get('modelAttempts'), list)
                    or 'execution' not in assignment or 'review' not in assignment):
                raise ProjectDispatchError('Invalid dispatch grant/assignment')
            _positive_revision(grant.get('revision'))

            if 'bindingRevision' in grant:
                _positive_revision(grant['bindingRevision'])
            _validate_schema(self._admission(assignment), 'admission')
            attempts = assignment['modelAttempts']
            if len(attempts) > assignment['request']['budget']['maxModelRequests']:
                raise ProjectDispatchError('Dispatch model-attempt capacity exceeded')
            occurrences = set()
            for attempt in attempts:
                _validate_schema(attempt, 'modelAttempt')
                occurrence = (attempt['root_task_id'], attempt['attempt_ordinal'])
                if occurrence in occurrences:
                    raise ProjectDispatchError('Duplicate model occurrence')
                occurrences.add(occurrence)
            execution = assignment['execution']
            if execution is not None:
                if not isinstance(execution, dict) or set(execution) != {'turnId', 'attemptId'}:
                    raise ProjectDispatchError('Invalid dispatch execution association')
                _identifier(execution['turnId'])
                _identifier(execution['attemptId'])
            review = assignment['review']
            if review is not None:
                if (not isinstance(review, dict) or review.get('decision') not in
                        ('accept', 'rework', 'reject') or not isinstance(review.get('note'), str)
                        or len(review['note']) > 2048):
                    raise ProjectDispatchError('Invalid dispatch review')
                _positive_revision(review.get('resultRevision'))

        self._check_batches(value, segmented=segmented)

        from .dependencies import check_graph
        check_graph(value['assignments'], max(len(value['assignments']), self.dependency_graph_limit)
                    if segmented else self.dependency_graph_limit)

    def _check_batches(self, value, *, segmented=False):
        batches = value.get('batches', [])
        if not segmented and len(batches) > LIMITS['batchesPerProject']:
            raise ProjectDispatchError('Dispatch batch capacity reached; no evidence evicted')
        rows = {row['id']: row for row in value['assignments']}
        seen, members = set(), set()
        for batch in batches:
            _validate_schema(batch, 'batch')
            _identifier(batch['id'])
            if batch['id'] in seen or batch['id'] in rows:
                raise ProjectDispatchError('Duplicate dispatch batch identity')
            seen.add(batch['id'])
            try:
                expected_ids = [_batch_assignment_identity(batch['id'], ordinal)
                                for ordinal in range(len(batch['request']['workers']))]
            except (ValueError, AttributeError) as exc:
                raise ProjectDispatchError('Invalid dispatch batch identity') from exc
            children = batch_requests(batch['request'])
            if (batch['request']['projectPath'] != self.project
                    or batch['assignmentIds'] != expected_ids
                    or members.intersection(expected_ids)):
                raise ProjectDispatchError('Batch member identities differ or are duplicated')
            members.update(expected_ids)
            authority = None
            for identity, child in zip(expected_ids, children):
                row = rows.get(identity)
                if row is None or row['request'] != child:
                    raise ProjectDispatchError('Batch child request differs or is dangling')
                grant = row['grant']
                _positive_revision(grant.get('bindingRevision'))
                if grant.get('lifecycleVersion') != 1:
                    raise ProjectDispatchError('Batch child lacks versioned authority')
                current = {key: grant.get(key) for key in (
                    'subject', 'tenant', 'owner', 'bindingRevision', 'createdAt', 'lifecycleVersion')}
                if authority is not None and current != authority:
                    raise ProjectDispatchError('Batch child granter/binding epoch differs')
                authority = current

    def get_attempt(self, attempt_id):
        getter = self.attempt_get
        if getter is None:
            from lib.turn_lifecycle import get_attempt
            getter = get_attempt
        return getter(attempt_id, user_id=self.owner)

    def get_workspace(self, *args, **kwargs):
        getter = self.workspace_get
        if getter is None:
            from lib.integration_state_repository import find_workspace
            getter = find_workspace
        return getter(*args, **kwargs)

    def dependency_states(self, assignment, *, value=None):
        from .dependencies import states
        return states(self, assignment, value)

    def _load(self, selected=None):
        if self.storage_version == 2:
            from .segmented import SegmentedDispatch
            for _ in range(LIMITS['casAttempts']):
                try:
                    return SegmentedDispatch(self).load(selected)
                except StorageError as error:
                    if error.code != 'database_conflict' and not error.retryable:
                        raise
            raise ProjectDispatchError('Concurrent/uncertain dispatch snapshot; retry reading')
        row = self.client().query('record.get', {'namespace': NAMESPACE, 'key': self.key})
        if row is None:
            return {'binding': copy.deepcopy(self.binding), 'assignments': []}, 0
        if (not isinstance(row, Mapping) or type(row.get('version')) is not int
                or row['version'] < 1):
            raise StorageError('database_integrity', 'Invalid dispatch record version')
        try:
            self._check_record(row.get('value'))
        except ProjectDispatchError as exc:
            raise StorageError('database_integrity', str(exc)) from exc
        return copy.deepcopy(row['value']), row['version']

    @staticmethod
    def _find(value, assignment_id):
        _identifier(assignment_id)
        assignment = next((a for a in value['assignments'] if a['id'] == assignment_id), None)
        if assignment is None:
            raise ProjectDispatchError('Dispatch assignment not found in owned project')
        return assignment

    def _change(self, update, *, selected=None):
        from .segmented import SegmentedDispatch, Snapshot
        for _ in range(LIMITS['casAttempts']):
            original, version = self._load(selected)
            value = copy.deepcopy(original)
            result = update(value)

            retained = original.get('batches', [])
            if value.get('batches', [])[:len(retained)] != retained:
                raise ProjectDispatchError('Batch manifest is immutable')
            receipts = original.get('coordinatorSettlements', [])
            if value.get('coordinatorSettlements', [])[:len(receipts)] != receipts:
                raise ProjectDispatchError('Coordinator settlement is immutable')
            for old in original.get('requestBudgets', []):
                current = next((row for row in value.get('requestBudgets', []) if row['id'] == old['id']), None)
                if current is None or any(current[key] != old[key] for key in old if key not in ('accounts', 'modelAttempts')):
                    raise ProjectDispatchError('Original request budget authority is immutable')
                for account in old['accounts']:
                    actual = next((row for row in current['accounts'] if row['taskId'] == account['taskId']), None)
                    if actual is None or any(actual[key] != account[key] for key in account if key != 'toolCalls') or actual['toolCalls'] < account['toolCalls']:
                        raise ProjectDispatchError('Original request account cannot be reset')
            self._check_record(value, segmented=self.storage_version == 2)
            if value == original:
                return copy.deepcopy(result)
            try:
                if self.storage_version == 2:
                    store = SegmentedDispatch(self)
                    if not isinstance(version, Snapshot) or version.head['phase'] != 'ready':
                        store.ensure_ready()
                        continue
                    store.commit(original, value, version)
                    return copy.deepcopy(result)
                self.client(write=True).command('record.put', {
                    'namespace': NAMESPACE, 'key': self.key, 'value': value,
                    'expected_version': version,
                }, 'project-dispatch:' + uuid.uuid4().hex)
                return copy.deepcopy(result)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
        raise ProjectDispatchError('Concurrent/uncertain dispatch update; read before retrying')

    def _conversation(self, conversation_id):
        snapshot = self.conversations.get(conversation_id, user_id=self.owner,
                                          include_messages=False)
        metadata = getattr(snapshot, 'metadata', snapshot)
        if isinstance(metadata, Mapping) and isinstance(metadata.get('metadata'), Mapping):
            metadata = metadata['metadata']
        settings = metadata.get('settings') if isinstance(metadata, Mapping) else None
        if (not isinstance(settings, Mapping)
                or normalize_project_path(settings.get('projectPath') or '') != self.project
                or settings.get('archived') or settings.get('readOnly')
                or metadata.get('archived') or metadata.get('readOnly')):
            raise ProjectDispatchError('Conversation unavailable in owned project')
        # Ownership is enforced by the owner-scoped getter; reject contradictory metadata too.
        if not evidence_owner_matches(metadata, self.owner):
            raise ProjectDispatchError('Conversation owner changed')

    def validate_binding(self, coordinator_id, expected_revision=None):
        """Return current typed binding epoch; retained authority must pin it."""
        binding = self.binding_repository.read(self.project)
        if (not isinstance(binding, Mapping) or binding.get('projectPath') != self.project
                or binding.get('conversationId') != coordinator_id
                or binding.get('availability') != 'available'
                or type(binding.get('revision')) is not int or binding['revision'] < 1
                or expected_revision is not None and (
                    type(expected_revision) is not int or expected_revision != binding['revision'])):
            raise ProjectDispatchError('Current coordination binding epoch does not authorize dispatch')
        return binding['revision']

    def _live_request(self, request, *, check_deadline=True):
        if request['projectPath'] != self.project:
            raise ProjectDispatchError('Dispatch project must match canonical project exactly')
        if request['coordinatorId'] == request['recipientId'] and request.get('profile', {}).get('mode') != 'coordinator':
            raise ProjectDispatchError('Coordinator and recipient must be distinct')
        if check_deadline and request['budget']['deadline'] <= self.clock():
            raise ProjectDispatchError('Dispatch grant expired')
        self._conversation(request['coordinatorId'])
        self._conversation(request['recipientId'])
        binding_revision = self.validate_binding(request['coordinatorId'])
        source = request['authorizationSource']
        if source['conversationId'] != request['coordinatorId']:
            raise ProjectDispatchError('Authorization source must be in coordinator')
        self.validate_human_source(source)
        if request.get('delegation') is not None or request.get('profile', {}).get('mode') in ('project', 'coordinator'):
            from .delegation import validate_source
            validate_source(self, request, check_deadline=check_deadline)
        return binding_revision

    def validate_human_source(self, source):
        """Reusable exact genuine-human source fence; caller owns project policy."""
        _validate_schema(source, 'source')

        _positive_revision(source['projectionRevision'])
        turn = self.turn_get(source['conversationId'], source['turnId'], user_id=self.owner)
        if (not isinstance(turn, Mapping) or turn.get('actor') != 'human'
                or turn.get('conversationId') != source['conversationId']
                or turn.get('turnId') != source['turnId']
                or type(turn.get('projectionRevision')) is not int
                or turn['projectionRevision'] != source['projectionRevision']):
            raise ProjectDispatchError('Authorization source changed or is not human')

        if not evidence_owner_matches(turn, self.owner):
            raise ProjectDispatchError('Authorization source owner changed')
        projection = turn.get('projection')
        if not isinstance(projection, Mapping) or resolve_initiator(projection) != 'human':
            raise ProjectDispatchError('Authorization projection is not genuine human input')
        origin = projection.get('origin')
        if origin is not None and (not isinstance(origin, Mapping) or origin.get('initiator') != 'human'
                or any(origin.get(k) for k in ('sourceConversationId', 'brain', 'autopilotRunId',
                                              'boardTaskId', 'scheduledTaskId'))):
            raise ProjectDispatchError('Synthetic authorization origin denied')
        synthetic = ('_peerMessage', '_fromConv', '_brainDispatch', '_brainEpic', '_boardTaskId',
                     'boardTaskId', '_isVirtualUser', '_autopilotRunId', '_proactive',
                     '_proactiveTaskId', '_timer', '_swarmAutoContinue', 'backgroundCommand',
                     '_isCompactionSummary', '_isFlowReview', '_isFlowPlanner', '_flowRunId',
                     '_programSynthetic')
        if (resolve_initiator(turn) != 'human'
                or any(projection.get(k) or turn.get(k) for k in synthetic)
                or projection.get('role', 'user') != 'user'
                or projection.get('actor', 'human') != 'human'):
            raise ProjectDispatchError('Synthetic source cannot grant dispatch authority')

    def command_identity(self, command_id):
        _identifier(command_id)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f'{NAMESPACE}:{self.key}:{command_id}'))

    def find_command(self, command_id):
        """Read an exact scoped command; absence is not proof of no in-flight write."""
        identity = self.command_identity(command_id)
        from .segmented import selection
        return next((row for row in self._load(selection(assignments=[identity]))[0]['assignments'] if row['id'] == identity), None)

    def batch_identity(self, command_id):
        """Batch commands have a separate domain from legacy single grants."""
        _identifier(command_id)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f'{NAMESPACE}:batch:{self.key}:{command_id}'))

    def find_batch(self, command_id):
        """Read exact batch evidence; absence does not exclude an in-flight CAS."""
        identity = self.batch_identity(command_id)
        return next((row for row in self._load(self._selection(batches=[identity]))[0].get('batches', [])
                     if row['id'] == identity), None)

    def read_batch(self, batch_id):
        """Read immutable batch evidence only; never dispatches or renews grants."""
        _identifier(batch_id)
        row = next((row for row in self._load(self._selection(batches=[batch_id]))[0].get('batches', [])
                    if row['id'] == batch_id), None)
        if row is None:
            raise ProjectDispatchError('Dispatch batch not found in owned project')
        return copy.deepcopy(row)

    def authorize_batch(self, request, *, principal, command_id):
        """Admit every child and manifest in one CAS, with no execution effects.

        Exact replay returns the original manifest before checking live authority,
        including after Stop, revoke, expiry or rebinding. Child grants, not caller
        identity or replay, remain the sole authority for subsequent dispatch.
        """
        request = copy.deepcopy(request)
        children = batch_requests(request)
        identity = self.batch_identity(command_id)
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                or principal.owner_user_id != self.owner):
            raise ProjectDispatchError('Dispatch requires matching owning user principal')
        principal.require_scope('chat')
        granter = {'subject': principal.subject_id, 'tenant': principal.tenant_id,
                   'owner': self.owner}
        ids = [_batch_assignment_identity(identity, ordinal) for ordinal in range(len(children))]
        manifest = {'id': identity, 'request': request, 'assignmentIds': ids}

        def add(value):
            old = next((row for row in value.get('batches', []) if row['id'] == identity), None)
            if old is not None:
                if old != manifest or any(
                        any(self._find(value, member)['grant'].get(k) != v for k, v in granter.items())
                        for member in old['assignmentIds']):
                    raise ProjectDispatchError('Batch command identity reused with changed request/granter')
                return old
            if self.storage_version == 1 and len(value.get('batches', [])) >= LIMITS['batchesPerProject']:
                raise ProjectDispatchError('Dispatch batch capacity reached; no evidence evicted')
            if self.storage_version == 1 and len(value['assignments']) + len(children) > LIMITS['assignmentsPerProject']:
                raise ProjectDispatchError('Dispatch assignment capacity reached; no evidence evicted')
            if any(row['id'] in ids for row in value['assignments']):
                raise ProjectDispatchError('Batch child identity already exists without manifest')
            created_at = self.clock()
            if request['budget']['deadline'] <= created_at:
                raise ProjectDispatchError('Dispatch batch expired')
            epoch, assignments = None, []
            for member, child in zip(ids, children):
                revision = self._live_request(child)
                if epoch is not None and revision != epoch:
                    raise ProjectDispatchError('Batch coordinator binding epoch changed during admission')
                epoch = revision
                predecessor = child.get('reworkOf')
                if predecessor is not None:
                    prior = self._find(value, predecessor['assignmentId'])
                    if (prior['grant']['tenant'] != principal.tenant_id
                            or prior['grant']['revision'] != predecessor['grantRevision']
                            or prior['review'] is None or prior['review']['decision'] != 'rework'
                            or prior['review']['resultRevision'] != predecessor['resultRevision']):
                        raise ProjectDispatchError('Rework requires the exact owned review/grant/result revision')
                assignment = {'id': member, 'request': copy.deepcopy(child),
                              'grant': {**granter, 'bindingRevision': epoch, 'lifecycleVersion': 1,
                                        'revision': 1, 'revoked': False, 'createdAt': created_at},
                              'execution': None, 'review': None, 'modelAttempts': []}
                self._check_stops(assignment)
                assignments.append(assignment)
            # Binding is a separate authority; recheck the shared epoch after all
            # worker/source reads. Live dispatch rechecks it again before effects.
            self.validate_binding(request['coordinatorId'], epoch)
            value['assignments'].extend(assignments)
            from .dependencies import check_graph
            check_graph(value['assignments'], max(len(value['assignments']), self.dependency_graph_limit)
                        if self.storage_version == 2 else self.dependency_graph_limit)
            for assignment in assignments:
                if any(state['status'] in ('invalid', 'unknown')
                       for state in self.dependency_states(assignment, value=value)):
                    raise ProjectDispatchError('Dependency requires exact completed current prerequisite evidence')
            value.setdefault('batches', []).append(copy.deepcopy(manifest))
            return manifest
        required = list(ids)
        for child in children:
            required.extend(ref['assignmentId'] for ref in child.get('dependencies', []))
            if child.get('reworkOf'):
                required.append(child['reworkOf']['assignmentId'])
        return self._change(add, selected=self._selection(assignments=required, batches=[identity]))

    def authorize(self, request, principal, command_id):
        _validate_schema(request, 'request')
        if request.get('delegation') is not None or request.get('profile', {}).get('mode') in ('project', 'coordinator'):
            raise ProjectDispatchError('Project worker policy is issued by the current Coordinator task')

        if request.get('profile') is not None:
            from .scope import validate_profile
            validate_profile(request)
        _identifier(command_id)
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                or principal.owner_user_id != self.owner):
            raise ProjectDispatchError('Dispatch requires matching owning user principal')
        principal.require_scope('chat')
        request = copy.deepcopy(request)
        assignment_id = self.command_identity(command_id)
        granter = {'subject': principal.subject_id, 'tenant': principal.tenant_id,
                   'owner': self.owner}
        return self._authorize_request(request, granter, assignment_id)

    def authorize_from_coordinator(self, request, *, task, command_id):
        """An accepted human tool invocation may delegate its current request.

        This is an internal tool-admission port, not a principal conversion.
        Source, policy and physical attempt are server-issued and revalidated;
        synthetic callers cannot acquire this authority by supplying an owner.
        """
        from .delegation import source_for_task, policy_for_task, check_allowance, issued_worker_policy
        from .continuation import original_delegation, original_granter, ensure_open
        principal, source, attempt = source_for_task(self, task)
        policy = policy_for_task(self, task, attempt)
        issued = issued_worker_policy(self, task)
        expected = original_delegation(task, policy, repository=self)
        if (request.get('authorizationSource') != source or request.get('delegation') != expected
                or request.get('model') != issued.model
                or request.get('profile', {}).get('mode') != 'project'
                or request['profile']['tools'] != list(issued.tools)):
            raise PermissionError('Worker request differs from its issued Coordinator policy')
        _validate_schema(request, 'request')
        from .scope import validate_profile
        validate_profile(request)
        granter = original_granter(task, principal)
        def check(value):
            source_for_task(self, task)
            ensure_open(value, expected['attemptId'])
            check_allowance(self, value, request)
        return self._authorize_request(copy.deepcopy(request), granter, self.command_identity(command_id), check=check)

    def _authorize_request(self, request, granter, assignment_id, *, check=None):
        def add(value):
            old = next((a for a in value['assignments'] if a['id'] == assignment_id), None)
            if old is not None:
                if old['request'] != request or any(old['grant'].get(k) != v for k, v in granter.items()):
                    raise ProjectDispatchError('Command identity reused with changed request/granter')
                return old
            if check is not None:
                check(value)
            binding_revision = self._live_request(request)

            predecessor = request.get('reworkOf')
            if predecessor is not None:
                old = self._find(value, predecessor['assignmentId'])
                if (old['grant']['tenant'] != granter['tenant']
                        or old['grant']['revision'] != predecessor['grantRevision']
                        or old['review'] is None or old['review']['decision'] != 'rework'
                        or old['review']['resultRevision'] != predecessor['resultRevision']):
                    raise ProjectDispatchError('Rework requires the exact owned review/grant/result revision')
            if self.storage_version == 1 and len(value['assignments']) >= LIMITS['assignmentsPerProject']:
                raise ProjectDispatchError('Dispatch assignment capacity reached; no evidence evicted')
            assignment = {'id': assignment_id, 'request': request,
                          'grant': {**granter, 'bindingRevision': binding_revision,
                                    'lifecycleVersion': 1, 'revision': 1, 'revoked': False,
                                    'createdAt': self.clock()},
                          'execution': None, 'review': None, 'modelAttempts': []}
            value['assignments'].append(assignment)

            from .dependencies import check_graph
            check_graph(value['assignments'], max(len(value['assignments']), self.dependency_graph_limit)
                        if self.storage_version == 2 else self.dependency_graph_limit)
            if any(state['status'] in ('invalid', 'unknown')
                   for state in self.dependency_states(assignment, value=value)):
                raise ProjectDispatchError('Dependency requires exact completed current prerequisite evidence')
            return assignment
        selected = self._selection(assignments=[assignment_id] + [ref['assignmentId'] for ref in request.get('dependencies', [])])
        if request.get('reworkOf'):
            selected['assignments'].append(request['reworkOf']['assignmentId'])
        delegation = request.get('delegation') or {}
        if delegation:
            selected['sources'].append(delegation['attemptId'])
            budget = delegation.get('policy', {}).get('requestBudgetId')
            if budget:
                selected['requestBudgets'].append(budget)
        return self._change(add, selected=selected)

    def read(self, assignment_id):
        return copy.deepcopy(self._find(self._load(self._selection(assignments=[assignment_id]))[0], assignment_id))

    @staticmethod
    def _selection(**kwargs):
        from .segmented import selection
        return selection(**kwargs)

    def _assignment_selection(self, assignment_id, *, capacity=False):
        selected = self._selection(assignments=[assignment_id])
        if capacity and self.storage_version == 2:
            row = self.read(assignment_id)
            delegation = row['request'].get('delegation') or {}
            budget = delegation.get('policy', {}).get('requestBudgetId')
            if budget:
                selected['requestBudgets'].append(budget)
            elif delegation:
                selected['sources'].append(delegation['attemptId'])
        return selected

    def list(self):
        return copy.deepcopy(self._load()[0]['assignments'])

    def history_page(self, cursor=None):
        from .history import page
        return page(self, cursor)

    def _admission(self, assignment):
        return {'version': 1, 'owner': self.owner, 'tenant': assignment['grant'].get('tenant'),
                'projectPath': self.project, 'assignmentId': assignment['id'],
                'revision': assignment['grant']['revision'],
                'recipientId': assignment['request']['recipientId']}

    def admission(self, assignment_id):
        admission = self._admission(self.read(assignment_id))
        _validate_schema(admission, 'admission')
        return admission

    def _check_stops(self, assignment):
        if any(stopped_after(self.owner, conversation_id, assignment['grant']['createdAt'], client=self.client)
               for conversation_id in (assignment['request']['coordinatorId'], assignment['request']['recipientId'])):
            raise ProjectDispatchError('Grant withdrawn by durable conversation Stop')

    def validate_grant_binding(self, assignment):
        """Legacy records are evidence only; never manufacture a binding epoch."""
        epoch = assignment['grant'].get('bindingRevision')
        if type(epoch) is not int or epoch < 1:
            raise ProjectDispatchError('Legacy grant lacks binding epoch; fresh human authorization required')
        return self.validate_binding(assignment['request']['coordinatorId'], epoch)

    def validate_own(self, admission, *, value=None):
        """Check live own authority even when prerequisites defer executor birth."""
        _validate_schema(admission, 'admission')
        if admission['owner'] != self.owner or admission['projectPath'] != self.project:
            raise ProjectDispatchError('Admission owner/project mismatch')
        value = self._load(self._selection(assignments=[admission['assignmentId']]))[0] if value is None else value
        assignment = self._find(value, admission['assignmentId'])
        if admission != self._admission(assignment) or assignment['grant']['revoked']:
            raise ProjectDispatchError('Admission tenant/recipient/revision mismatch or grant revoked')
        self.validate_grant_binding(assignment)
        self._check_stops(assignment)
        self._live_request(assignment['request'])
        return copy.deepcopy(assignment)

    def validate(self, admission):
        value = self._load(self._selection(assignments=[admission['assignmentId']]))[0]
        assignment = self.validate_own(admission, value=value)
        if any(state['status'] != 'ready' for state in self.dependency_states(assignment, value=value)):
            raise ProjectDispatchError('Dispatch dependencies are not ready')
        return assignment

    def mutate(self, assignment_id, update_callback, *, check=None):
        """Trusted internal ledger seam; callback may retry, never perform side effects.

        In-place callbacks or callbacks returning a replacement dict are supported.
        Authority and execution/review identity remain immutable through this seam.
        """
        def update(value):
            assignment = self._find(value, assignment_id)
            candidate = copy.deepcopy(assignment)
            replacement = update_callback(candidate)
            if replacement is not None:
                candidate = replacement
            if not isinstance(candidate, dict) or any(candidate.get(k) != assignment.get(k)
                    for k in ('id', 'request', 'grant', 'execution', 'review', 'publication')):
                raise ProjectDispatchError('Internal mutation cannot change dispatch authority/association')
            index = value['assignments'].index(assignment)
            value['assignments'][index] = copy.deepcopy(candidate)
            if check is not None:
                check(value)
            return value['assignments'][index]
        return self._change(update, selected=self._assignment_selection(assignment_id, capacity=check is not None))

    def bind_execution(self, assignment_id, turn_id, attempt_id):
        _identifier(turn_id)
        _identifier(attempt_id)
        association = {'turnId': turn_id, 'attemptId': attempt_id}
        def bind(value):
            assignment = self._find(value, assignment_id)
            if assignment['execution'] is not None:
                if assignment['execution'] != association:
                    raise ProjectDispatchError('Execution association is immutable')
                return assignment
            self.validate(self._admission(assignment))
            assignment['execution'] = association
            return assignment
        return self._change(bind, selected=self._assignment_selection(assignment_id))

    def revoke(self, assignment_id, expected_revision):
        """Return the existing execution association for the caller's cancellation port."""
        _positive_revision(expected_revision)
        def revoke(value):
            assignment = self._find(value, assignment_id)
            grant = assignment['grant']
            if grant['revoked'] and grant['revision'] == expected_revision + 1:
                return assignment['execution']
            if grant['revision'] != expected_revision or grant['revoked']:
                raise ProjectDispatchError('Grant revision changed')
            grant.update(revision=expected_revision + 1, revoked=True)
            return assignment['execution']
        return self._change(revoke, selected=self._assignment_selection(assignment_id))

    def validate_scope_change_source(self, request):
        """Scope removal admits an owned worker's human input, never synthetic context."""
        if request['projectPath'] != self.project:
            raise ProjectDispatchError('Scope change requires exact canonical project')
        self._conversation(request['coordinatorId'])
        self._conversation(request['authorizationSource']['conversationId'])
        binding = self.binding_repository.read(self.project)
        if (not isinstance(binding, Mapping) or binding.get('projectPath') != self.project
                or binding.get('conversationId') != request['coordinatorId']
                or binding.get('availability') != 'available'):
            raise ProjectDispatchError('Current coordination binding changed')
        self.validate_human_source(request['authorizationSource'])

    def validate_scope_change_notification(self, source, recipient):
        """Passive receipts cannot migrate to archived or rebound conversations."""
        self._conversation(source)
        self._conversation(recipient)

    def revoke_scope_change(self, request, *, principal, command_id):
        """One CAS validates all pins, revokes them and retains immutable evidence."""
        from .scope_changes import append_change, validate
        validate(request, 'request')
        request = copy.deepcopy(request)
        _identifier(command_id)
        return self._change(lambda value: append_change(
            self, value, request, principal, command_id))

    def scope_changes(self):
        """Bounded scope-removal evidence; no execution or delivery."""
        return copy.deepcopy(self._load()[0].get('scopeChanges', []))

    def record_scope_change_outcome(self, change_id, collection, identity, outcome):
        """Trusted receipt-only CAS; identity and revoked grants cannot be edited."""
        from .scope_changes import record_outcome
        return self._change(lambda value: record_outcome(
            value, change_id, collection, identity, copy.deepcopy(outcome)))

    def request_publication(self, assignment_id, *, principal, expected_revision, result_revision, expected_checkpoint_sha):
        """A separate explicit human command; review alone cannot publish."""

        _identifier(expected_checkpoint_sha)
        _positive_revision(expected_revision)
        _positive_revision(result_revision)
        def publish(value):
            row = self._find(value, assignment_id)
            if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                    or principal.owner_user_id != self.owner or principal.tenant_id != row['grant']['tenant']):
                raise PermissionError('Publication requires the owning human and tenant')
            principal.require_scope('chat')
            if (row['request'].get('profile', {}).get('mode') != 'files'
                    or row['grant']['revoked'] or row['grant']['revision'] != expected_revision
                    or row['execution'] is None or row['review'] != {
                        'decision': 'accept', 'note': (row['review'] or {}).get('note', ''),
                        'resultRevision': result_revision}):
                raise ProjectDispatchError('Publication requires an accepted exact completed file-task result')
            self.validate_own(self._admission(row), value=value)
            old = row.get('publication')
            if old is not None:
                if (old['resultRevision'] != result_revision or old['grantRevision'] != expected_revision

                        or old['checkpointSha'] != expected_checkpoint_sha
                        or old['subject'] != principal.subject_id):
                    raise ProjectDispatchError('Publication authorization is immutable')
                return row
            row['publication'] = {'resultRevision': result_revision, 'grantRevision': expected_revision,
                                  'checkpointSha': expected_checkpoint_sha,
                                  'subject': principal.subject_id, 'requestedAt': self.clock()}
            return row
        return self._change(publish, selected=self._assignment_selection(assignment_id))

    def validate_publication(self, binding, task_id):
        """Publication reuses live grant/source checks without model/tool spending."""
        row = self.validate(binding)
        publication = row.get('publication')
        if (row['request'].get('profile', {}).get('mode') not in ('files', 'project') or publication is None
                or publication['grantRevision'] != binding['revision'] or row['review'] is None
                or row['review']['decision'] != 'accept'
                or row['review']['resultRevision'] != publication['resultRevision']):
            raise ProjectDispatchError('Project publication requires explicit human authorization')
        if row['request'].get('profile', {}).get('mode') == 'project' and (
                row['review'].get('actor') != 'coordinator' or publication['subject'] !=
                'coordinator:' + row['request']['delegation']['taskId']):
            raise ProjectDispatchError('Project worker publication requires its source Coordinator review')
        if any(attempt['phase'] == 'dispatch_intent' or not attempt['usage_complete']
               for attempt in row['modelAttempts']):
            raise ProjectDispatchError('Unconfirmed paid execution cannot publish')
        if any(attempt['phase'] not in CONTRACT['terminalToolPhases']
               for attempt in row.get('toolAttempts', [])):
            raise ProjectDispatchError('Unconfirmed tool execution cannot publish')
        execution = row['execution']
        if execution is None:
            raise ProjectDispatchError('Publication has no execution association')
        if self.attempt_get is None:
            from lib.turn_lifecycle import get_attempt
            attempt = get_attempt(execution['attemptId'], user_id=self.owner)
        else:
            attempt = self.attempt_get(execution['attemptId'], user_id=self.owner)
        turn = self.turn_get(row['request']['recipientId'], execution['turnId'], user_id=self.owner)
        if (not isinstance(attempt, Mapping) or not isinstance(turn, Mapping)
                or attempt.get('attemptId') != execution['attemptId']
                or attempt.get('turnId') != execution['turnId']
                or attempt.get('conversationId') != row['request']['recipientId']
                or attempt.get('taskId') != task_id or attempt.get('status') != 'completed'
                or turn.get('turnId') != execution['turnId']

                or turn.get('actor') != 'assistant'
                or turn.get('conversationId') != row['request']['recipientId']
                or type(turn.get('projectionRevision')) is not int
                or turn.get('projectionRevision') != publication['resultRevision']
                or not all(evidence_owner_matches(item, self.owner) for item in (turn, attempt))):
            raise ProjectDispatchError('Publication execution/result identity changed')
        return row

    def record_review(self, assignment_id, decision, note, result_revision, expected_revision):
        """Record an immutable review of a result revision, not delivery or completion.

        expected_revision fences the grant revision; review does not modify it.
        """
        _positive_revision(result_revision)
        _positive_revision(expected_revision)
        if decision not in ('accept', 'rework', 'reject') or not isinstance(note, str) or len(note) > 2048:
            raise ProjectDispatchError('Invalid report review')
        review = {'decision': decision, 'note': note, 'resultRevision': result_revision}
        def record(value):
            assignment = self._find(value, assignment_id)
            if assignment['review'] is not None:
                if assignment['review'] != review:
                    raise ProjectDispatchError('Report review/result revision is immutable')
                return assignment
            if assignment['grant']['revision'] != expected_revision:
                raise ProjectDispatchError('Grant revision changed')
            if assignment['execution'] is None:
                raise ProjectDispatchError('Review requires an execution association')
            assignment['review'] = review
            return assignment
        return self._change(record, selected=self._assignment_selection(assignment_id))


__all__ = ['ProjectDispatchRepository', 'ProjectDispatchError', 'batch_requests']
