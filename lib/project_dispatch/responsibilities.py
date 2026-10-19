"""Finite human standing responsibility; no worker, model loop or delivery store.

ProjectResponsibilities exposes create/list/read/pause/cancel/reconcile. Portable
CAS stores frozen authorization plus conservative occurrence debits. Existing
automation publication owns triggers; ProjectDispatchService owns child effects.
The original authenticated principal is private evidence captured only by create.
"""
from __future__ import annotations
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import uuid

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from lib.identity import PrincipalContext, require_user_id
from lib.storage.errors import StorageError
from .repository import ProjectDispatchError, ProjectDispatchRepository, _identifier, stopped_after
from .service import ProjectDispatchService

_CONTRACTS = Path(__file__).resolve().parents[2] / 'contracts'
CONTRACT = json.loads((_CONTRACTS / 'project_responsibility_v1.schema.json').read_text())
LIMITS = CONTRACT['limits']
NAMESPACE = CONTRACT['namespace']


@lru_cache(maxsize=16)
def validator(definition):
    registry = Registry()
    for name in ('project_dispatch_v1.schema.json', 'automation_v1.schema.json'):
        uri = (_CONTRACTS / name).as_uri()
        registry = registry.with_resource(uri, Resource.from_contents({
            **json.loads((_CONTRACTS / name).read_text()), '$id': uri}))
    return Draft202012Validator({**CONTRACT,
        '$id': (_CONTRACTS / 'project_responsibility_v1.schema.json').as_uri(),
        '$ref': '#/$defs/' + definition}, registry=registry)


def _json(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ProjectDispatchError('Responsibility requires finite UTF-8 JSON') from exc


def validate(value, definition):
    if not isinstance(value, dict) or not validator(definition).is_valid(value):
        raise ProjectDispatchError('Invalid responsibility ' + definition)
    _json(value).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ProjectResponsibilities:
    def __init__(self, owner, project, *, client=None, dispatch_repository=None,
                 dispatch_service=None, automation_factory=None, clock=None):
        from lib.conversations.project_identity import normalize_project_path
        repo = dispatch_repository or ProjectDispatchRepository(owner, project, client=client, clock=clock)
        if (require_user_id(owner, context='responsibility') != repo.owner
                or normalize_project_path(project) != repo.project):
            raise ProjectDispatchError('Responsibility repository binding mismatch')
        self.dispatch_repository = repo
        self.owner, self.project, self.key = repo.owner, repo.project, repo.key
        self.client, self.clock = client or repo.client, clock or repo.clock
        self.dispatch_service = dispatch_service or ProjectDispatchService(repo)
        if automation_factory is None:
            from lib.automations.execution_repository import AutomationExecutionRepository
            automation_factory = lambda principal: AutomationExecutionRepository(principal, client_factory=self.client)
        self.automation_factory = automation_factory

    def _actor(self, principal, tenant=None, *, check_tenant=False):
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'user'
                or principal.require_owner() != self.owner
                or check_tenant and principal.tenant_id != tenant):
            raise PermissionError('Responsibility requires its authenticated human owner and tenant')
        principal.require_scope('chat')

    def _original(self, row):
        payload = row['principal']
        if type(payload.get('owner_user_id')) is not int:
            raise ProjectDispatchError('Original principal owner must be a typed integer')
        principal = PrincipalContext.from_payload(payload)
        self._actor(principal, row['view']['tenant'], check_tenant=True)
        if principal.to_payload() != payload:
            raise ProjectDispatchError('Original principal payload changed')
        return principal

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise ProjectDispatchError('Invalid responsibility clock')
        return now

    def _check(self, value):
        if (not isinstance(value, dict) or set(value) != {'owner', 'projectPath', 'responsibilities'}
                or type(value['owner']) is not int or value['owner'] != self.owner
                or value['projectPath'] != self.project or not isinstance(value['responsibilities'], list)
                or len(value['responsibilities']) > LIMITS['responsibilitiesPerProject']
                or len(_json(value).encode()) > LIMITS['maxRecordBytes']):
            raise ProjectDispatchError('Responsibility record binding/capacity mismatch')
        seen = set()
        for row in value['responsibilities']:
            if (set(row) != {'view', 'principal', 'commandId', 'createdAt', 'bindingRevision'}
                    or type(row['bindingRevision']) is not int or row['bindingRevision'] < 1):
                raise ProjectDispatchError('Invalid private responsibility envelope')
            view = row['view']
            validate(view, 'view')
            self._original(row)
            _identifier(row['commandId'])
            if (type(row['createdAt']) not in (int, float) or not math.isfinite(row['createdAt'])
                    or row['createdAt'] <= 0 or type(view['owner']) is not int or view['owner'] != self.owner
                    or view['projectPath'] != self.project or view['request']['projectPath'] != self.project
                    or view['request']['assignment']['projectPath'] != self.project):
                raise ProjectDispatchError('Responsibility identity changed')
            identity = 'pr_' + _digest([self.owner, view['tenant'], self.project, row['commandId']])
            if view['responsibilityId'] != identity or view['automationId'] != identity or identity in seen:
                raise ProjectDispatchError('Responsibility command binding changed')
            seen.add(identity)
            occurrences, request = view['occurrences'], view['request']
            allowance = request['allowance']
            if (len({o['occurrenceId'] for o in occurrences}) != len(occurrences)
                    or view['runsReserved'] != len(occurrences)
                    or view['tokensReserved'] != sum(o['reservedTokens'] for o in occurrences)
                    or view['runsReserved'] > allowance['maxRuns']
                    or view['tokensReserved'] > allowance['maxTotalTokens']
                    or request['assignment']['budget']['deadline'] != allowance['deadline']
                    or any(o['reservedTokens'] != request['assignment']['budget']['maxTokens']
                           or o['deadline'] > allowance['deadline']
                           or o['commandId'] != 'responsibility-child:' + _digest([identity, o['occurrenceId']])
                           for o in occurrences)):
                raise ProjectDispatchError('Responsibility allowance evidence changed')

    def _load(self):
        record = self.client().query('record.get', {'namespace': NAMESPACE, 'key': self.key})
        if record is None:
            return {'owner': self.owner, 'projectPath': self.project, 'responsibilities': []}, 0
        self._check(record['value'])
        if type(record['version']) is not int or record['version'] < 1:
            raise ProjectDispatchError('Invalid responsibility CAS version')
        return deepcopy(record['value']), record['version']

    def _change(self, change):
        for _ in range(LIMITS['casAttempts']):
            original, version = self._load()
            value = deepcopy(original)
            result = change(value)
            self._check(value)
            if value == original:
                return deepcopy(result)
            try:
                self.client(write=True).command('record.put', {'namespace': NAMESPACE, 'key': self.key,
                    'value': value, 'expected_version': version}, 'project-responsibility:' + uuid.uuid4().hex)
                return deepcopy(result)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
        raise ProjectDispatchError('Responsibility CAS uncertain; read before retrying')

    def _find(self, value, identity):
        _identifier(identity)
        row = next((r for r in value['responsibilities'] if r['view']['responsibilityId'] == identity), None)
        if row is None:
            raise ProjectDispatchError('Responsibility not found in owned project')
        return row

    def _row(self, identity):
        return self._find(self._load()[0], identity)

    def _project_stopped(self, row):
        """Existing project withdrawal evidence fences prior standing authority."""
        coordinator = row['view']['request']['assignment']['coordinatorId']
        return any(change['request']['kind'] == 'stop_project'
                   and change['request']['coordinatorId'] == coordinator
                   and change['tenant'] == row['view']['tenant']
                   and change['createdAt'] >= row['createdAt']
                   for change in self.dispatch_repository.scope_changes())

    def _live(self, row, occurrence=None):
        principal = self._original(row)

        if self._project_stopped(row):
            raise ProjectDispatchError('Standing responsibility withdrawn by project Stop')
        request = deepcopy(row['view']['request']['assignment'])

        self.dispatch_repository.validate_binding(request['coordinatorId'], row['bindingRevision'])
        if occurrence is not None:
            request['budget']['deadline'] = occurrence['deadline']
        # The dispatch owner's source/owner/binding gate is reused, not reimplemented.
        self.dispatch_repository._live_request(request)
        if 'profile' not in request:
            raise ProjectDispatchError('Standing responsibility requires explicit mode/tools/path scope')
        from .scope import validate_profile
        validate_profile(request)
        if any(stopped_after(self.owner, cid, row['createdAt'], client=self.client)
               for cid in (request['coordinatorId'], request['recipientId'])):
            raise ProjectDispatchError('Standing responsibility withdrawn by Stop')
        return principal

    def create(self, request, *, principal, command_id):
        validate(request, 'create')
        _identifier(command_id)
        self._actor(principal)
        assignment = request['assignment']
        if (request['projectPath'] != self.project or assignment['projectPath'] != self.project
                or assignment['budget']['deadline'] != request['allowance']['deadline']
                or assignment['budget']['maxTokens'] > request['allowance']['maxTotalTokens']
                or 'profile' not in assignment or 'reworkOf' in assignment):
            raise ProjectDispatchError('Explicit canonical scope and finite consistent budgets required')
        for scope in ('automations:manage', 'automations:execute', 'automations:read'):
            principal.require_scope(scope)
        identity = 'pr_' + _digest([self.owner, principal.tenant_id, self.project, command_id])
        envelope = {'principal': principal.to_payload(), 'commandId': command_id, 'createdAt': self._now(),
            'bindingRevision': self.dispatch_repository.validate_binding(assignment['coordinatorId']),
            'view': {'responsibilityId': identity, 'revision': 1, 'owner': self.owner, 'tenant': principal.tenant_id,
                'projectPath': self.project, 'request': deepcopy(request), 'state': 'publishing',
                'automationId': identity, 'runsReserved': 0, 'tokensReserved': 0, 'occurrences': []}}
        def insert(value):
            old = next((r for r in value['responsibilities'] if r['view']['responsibilityId'] == identity), None)
            if old is not None:
                if old['view']['request'] != request or old['principal'] != principal.to_payload():
                    raise ProjectDispatchError('Responsibility create command evidence changed')
                return old
            self._live(envelope)
            if len(value['responsibilities']) >= LIMITS['responsibilitiesPerProject']:
                raise ProjectDispatchError('Responsibility capacity reached; no evidence evicted')
            value['responsibilities'].append(deepcopy(envelope))
            return envelope
        row = self._change(insert)
        if row['view']['state'] == 'publishing':
            self._publish(row)
            self._set_state(identity, 'active', only='publishing')
        return self.read(identity, principal=principal)

    def _publish(self, row):
        from lib.automations.definitions import prepare_definition
        principal = self._live(row)
        view, request = row['view'], row['view']['request']
        definition = {'schemaVersion': 1, 'id': view['automationId'], 'name': request['name'], 'enabled': True,
            'trigger': request['trigger'], 'steps': [{'id': 'responsibility', 'action': 'project.responsibility.run',
                'after': [], 'arguments': {'projectPath': self.project, 'responsibilityId': view['responsibilityId']}}],
            'policy': {'maxRuntimeSeconds': request['perRunSeconds'],
                'maxModelTokens': request['assignment']['budget']['maxTokens'],
                'overlap': 'serialize', 'permissionMode': 'auto', 'maxStepAttempts': 1}}
        repo = self.automation_factory(principal)
        existing = repo.get('definition', view['automationId'])
        if existing is not None:
            if existing.value.get('definition') != definition:
                raise ProjectDispatchError('Published responsibility changed; never auto-renew')
            return existing
        return repo.publish(prepare_definition(definition), expected_version=0, now=self._now(),
                            request_id='responsibility-publish:' + view['responsibilityId'])

    def _set_state(self, identity, state, *, only=None):
        def update(value):
            view = self._find(value, identity)['view']
            if (only is None or view['state'] == only) and view['state'] != state:
                view['state'] = state
                view['revision'] += 1
            return view
        return self._change(update)

    def read(self, responsibility_id, *, principal):
        row = self._row(responsibility_id)
        self._actor(principal, row['view']['tenant'], check_tenant=True)
        return deepcopy(row['view'])

    def list(self, *, principal):
        self._actor(principal)
        return [deepcopy(r['view']) for r in self._load()[0]['responsibilities'] if r['view']['tenant'] == principal.tenant_id]

    def _disable(self, row):
        repo = self.automation_factory(self._original(row))
        entity = repo.get('definition', row['view']['automationId'])
        if entity is not None and entity.value['definition']['enabled']:
            repo.pause_definition(entity, now=self._now(), request_id='responsibility-disable:' + row['view']['responsibilityId'])

    def _control(self, body, principal, command_id, state):
        validate(body, 'command')
        _identifier(command_id)
        if body['projectPath'] != self.project:
            raise ProjectDispatchError('Responsibility command project mismatch')
        def update(value):
            row = self._find(value, body['responsibilityId'])
            view = row['view']
            self._actor(principal, view['tenant'], check_tenant=True)
            if view['state'] != state:
                if view['revision'] != body['expectedRevision']:
                    raise ProjectDispatchError('Responsibility revision changed')
                if state == 'paused' and view['state'] in {'cancelled', 'expired', 'exhausted'}:
                    raise ProjectDispatchError('Terminal responsibility cannot resume')
                view['state'] = state
                view['revision'] += 1
            return row
        row = self._change(update)
        self._disable(row)
        return row

    def pause(self, body, *, principal, command_id):
        row = self._control(body, principal, command_id, 'paused')
        return self.read(row['view']['responsibilityId'], principal=principal)

    def cancel(self, body, *, principal, command_id):
        row = self._control(body, principal, command_id, 'cancelled')
        self._cancel_children(row)
        return self.read(row['view']['responsibilityId'], principal=principal)

    def _child(self, row, occurrence):
        child = self.dispatch_repository.find_command(occurrence['commandId'])
        if child is not None:
            expected = deepcopy(row['view']['request']['assignment'])
            expected['budget']['deadline'] = occurrence['deadline']
            principal = self._original(row)
            grant = child['grant']
            if (child['request'] != expected or type(grant['owner']) is not int
                    or grant['owner'] != self.owner or grant['tenant'] != principal.tenant_id
                    or grant['subject'] != principal.subject_id
                    or type(grant.get('bindingRevision')) is not int
                    or grant['bindingRevision'] != row['bindingRevision']):
                raise ProjectDispatchError('Deterministic child differs from frozen human authorization')
        return child

    @staticmethod
    def _settlement(child, execution, *, cancelling=False):
        if execution is None or execution.get('status') == 'unknown':
            return 'unknown'
        if execution['status'] not in {'completed', 'failed', 'cancelled', 'aborted'}:
            return 'unknown' if cancelling else 'active'
        attempts = child['modelAttempts']
        uncertain = (any(a['phase'] == 'dispatch_intent'
            or a['phase'] == 'settled' and not a['usage_complete'] for a in attempts)
            or any(t['phase'] not in {'not_entered', 'executor_returned'} for t in child.get('toolAttempts', [])))
        if uncertain or not attempts and execution['status'] == 'completed':
            return 'unknown'
        if cancelling or execution['status'] in {'cancelled', 'aborted'}:
            return 'cancelled'
        return 'completed' if execution['status'] == 'completed' else 'failed'

    def _withdraw(self, row, occurrence, child, principal):
        revision = child['grant']['revision'] - int(child['grant']['revoked'])
        self.dispatch_service.revoke(child['id'], principal=principal, expected_revision=revision)
        child = self.dispatch_repository.read(child['id'])
        status = ('cancelled' if child['execution'] is None else self._settlement(child,
            self.dispatch_service.view(child['id'], principal=principal)['execution'], cancelling=True))
        self._occurrence_update(row['view']['responsibilityId'], occurrence['occurrenceId'], status, child['id'])
        return status

    def _cancel_children(self, row):
        principal = self._original(row)
        for occurrence in row['view']['occurrences']:
            child = self._child(row, occurrence)
            if child is not None:
                self._withdraw(row, occurrence, child, principal)
            run_id, separator, step_id = occurrence['occurrenceId'].rpartition(':')
            if separator and step_id == 'responsibility' and run_id.startswith('ar_'):
                repo = self.automation_factory(principal)
                run = repo.get('run', run_id)
                if run is not None and run.value['definitionId'] == row['view']['automationId']:
                    repo.cancel(run_id, request_id='responsibility-cancel:' + run_id,
                                reason='responsibility_cancelled', now=self._now())

    def reconcile(self, body, *, principal, command_id):
        validate(body, 'command')
        _identifier(command_id)
        row = self._row(body['responsibilityId'])
        self._actor(principal, row['view']['tenant'], check_tenant=True)
        if body['projectPath'] != self.project or row['view']['revision'] != body['expectedRevision']:
            raise ProjectDispatchError('Responsibility reconcile revision/project changed')
        if row['view']['state'] == 'publishing':
            self._publish(row)
            self._set_state(body['responsibilityId'], 'active', only='publishing')
        elif row['view']['state'] != 'active':
            self._disable(row)
        for occurrence in row['view']['occurrences']:
            self.run_occurrence(body['responsibilityId'], occurrence['occurrenceId'], reconcile=True)
        return self.read(body['responsibilityId'], principal=principal)

    def _occurrence_update(self, identity, occurrence_id, status, assignment_id=None):
        def update(value):
            view = self._find(value, identity)['view']
            occurrence = next(o for o in view['occurrences'] if o['occurrenceId'] == occurrence_id)
            if occurrence['status'] in {'completed', 'failed', 'cancelled'}:
                return occurrence
            if occurrence['status'] != status or assignment_id is not None and occurrence['assignmentId'] != assignment_id:
                view['revision'] += 1
            occurrence['status'] = status
            if assignment_id is not None:
                if occurrence['assignmentId'] not in {None, assignment_id}:
                    raise ProjectDispatchError('Child assignment association changed')
                occurrence['assignmentId'] = assignment_id
            return occurrence
        return self._change(update)

    def _reserve(self, identity, occurrence_id):
        _identifier(occurrence_id)
        now = self._now()
        def reserve(value):
            row = self._find(value, identity)
            view, request = row['view'], row['view']['request']
            old = next((o for o in view['occurrences'] if o['occurrenceId'] == occurrence_id), None)
            if old is not None:
                return old
            if view['state'] != 'active':
                return None
            allowance = request['allowance']
            state = None
            tokens = request['assignment']['budget']['maxTokens']
            if self._project_stopped(row):
                state = 'cancelled'
            elif now >= allowance['deadline']:
                state = 'expired'
            elif view['runsReserved'] >= allowance['maxRuns'] or view['tokensReserved'] + tokens > allowance['maxTotalTokens']:
                state = 'exhausted'
            if state:
                view['state'] = state
                view['revision'] += 1
                return None
            self._live(row)
            occurrence = {'occurrenceId': occurrence_id,
                'commandId': 'responsibility-child:' + _digest([identity, occurrence_id]),
                'reservedTokens': tokens, 'deadline': min(allowance['deadline'], now + request['perRunSeconds']),
                'status': 'reserved', 'assignmentId': None}
            view['occurrences'].append(occurrence)
            view['runsReserved'] += 1
            view['tokensReserved'] += tokens

            view['revision'] += 1
            return occurrence
        return self._change(reserve)

    def run_occurrence(self, identity, occurrence_id, *, reconcile=False, cancelling=False):
        """Adapter/owned reconciliation port. UNKNOWN never authorizes a new child.

        Lost grant ACK reads back the deterministic authorization command. Existing
        grant execution may replay only its original conversation command before
        association; uncertain model/tool effects remain UNKNOWN, fully debited.
        """
        row = self._row(identity)
        occurrence = next((o for o in row['view']['occurrences'] if o['occurrenceId'] == occurrence_id), None)
        if occurrence is None and not reconcile and not cancelling:
            occurrence = self._reserve(identity, occurrence_id)
        if occurrence is None:
            current = self._row(identity)
            if current['view']['state'] != 'active':
                self._disable(current)
            return 'cancelled'
        if occurrence['status'] in {'completed', 'failed', 'cancelled'}:
            return occurrence['status']
        row = self._row(identity)
        principal = self._original(row)
        child = self._child(row, occurrence)
        if (cancelling or row['view']['state'] in {'cancelled', 'expired'} or self._now() >= occurrence['deadline']):
            if child is not None:
                return self._withdraw(row, occurrence, child, principal)
            # An in-flight authorization may have committed. Preserve uncertainty.
            status = 'cancelled' if occurrence['status'] == 'reserved' else 'unknown'
            self._occurrence_update(identity, occurrence_id, status)
            return status
        if child is None and occurrence['status'] == 'reserved' and not reconcile:
            def claim(value):
                current = self._find(value, identity)
                o = next(o for o in current['view']['occurrences'] if o['occurrenceId'] == occurrence_id)
                if o['status'] != 'reserved' or current['view']['state'] != 'active':
                    return False
                self._live(current, o)
                o['status'] = 'authorizing'

                current['view']['revision'] += 1
                return True
            if self._change(claim):
                request = deepcopy(row['view']['request']['assignment'])
                request['budget']['deadline'] = occurrence['deadline']
                try:
                    # Separate durable authorization from physical dispatch so a
                    # concurrent parent cancel can revoke before executor entry.
                    self.dispatch_repository.authorize(request, principal=principal, command_id=occurrence['commandId'])
                except Exception:
                    self._occurrence_update(identity, occurrence_id, 'unknown')
                    raise
                child = self._child(row, occurrence)
        if child is None:
            self._occurrence_update(identity, occurrence_id, 'unknown')
            return 'unknown'
        # Reload controls after cross-owner I/O; cancellation racing authorization
        # withdraws the accepted grant, never creates a replacement.
        row = self._row(identity)
        if (child['grant']['revoked'] or row['view']['state'] in {'cancelled', 'expired'}
                or self._now() >= occurrence['deadline']):
            return self._withdraw(row, occurrence, child, principal)
        try:
            self._live(row, occurrence)
            self.dispatch_repository.validate_own(self.dispatch_repository.admission(child['id']))
        except ProjectDispatchError:
            self._set_state(identity, 'paused', only='active')
            self._disable(self._row(identity))
            return self._withdraw(row, occurrence, child, principal)
        if child['execution'] is None:
            self.dispatch_service.dispatch(child['id'], principal=principal)
        view = self.dispatch_service.view(child['id'], principal=principal)
        child = self.dispatch_repository.read(child['id'])
        status = ('active' if child['execution'] is None else self._settlement(child, view['execution']))
        self._occurrence_update(identity, occurrence_id, status, child['id'])
        return status
