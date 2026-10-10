"""Stateless authenticated automation management application boundary.

Entry point: AutomationManagement. Dependencies: canonical admission, owned
execution repository, explicit clock and project-ownership resolver. HTTP data
cannot supply a principal, trigger origin, event timestamp, or worker fence.
"""
from copy import deepcopy
import time

from .definitions import prepare_definition
from .errors import AutomationConflict, AutomationError
from .execution_repository import AutomationExecutionRepository
from .management_contract import limits, validate
from .registry import builtin_registry
from .repository import VersionedEntity
from .transitions import occurrence_id


def definition_view(entity):
    value = entity.value
    status = ('retired' if value.get('deleted') else 'draft' if not value.get('grant')
              else 'published' if value['definition']['enabled'] else 'paused')
    return {'id': entity.id, 'revision': entity.version, 'definition': value['definition'],
            'digest': value['compiled']['digest'], 'status': status, 'updatedAt': value['updatedAt']}


def run_view(entity):
    value = entity.value
    fields = ('definitionId', 'definitionRevision', 'definitionDigest', 'status',
              'createdAt', 'updatedAt', 'deadlineAt', 'trigger', 'completedAt', 'cancelReason')
    result = {'id': entity.id, 'revision': entity.version,
              **{key: value[key] for key in fields if key in value}}
    step_fields = {'status', 'attempts', 'arguments', 'output', 'error', 'startedAt', 'completedAt'}
    result['steps'] = {key: {field: item for field, item in state.items() if field in step_fields}
                       for key, state in value['steps'].items()}
    for key, state in value['steps'].items():
        if 'approval' in state:
            result['steps'][key]['approval'] = {
                field: state['approval'][field] for field in ('requestId', 'approvedAt')}
    return result


_DEFAULT_PROJECT_RESOLVER = object()


class AutomationManagement:
    def __init__(self, principal, *, client_factory=None, clock=time.time, project_resolver=_DEFAULT_PROJECT_RESOLVER):
        options = {'client_factory': client_factory} if client_factory is not None else {}
        self.repository = AutomationExecutionRepository(principal, **options)
        self.principal = principal
        self.clock = clock
        from .projects import AutomationProjects
        self.projects_repository=AutomationProjects(principal,client_factory=client_factory,clock=clock)
        self.project_resolver=(self.projects_repository.owns
            if project_resolver is _DEFAULT_PROJECT_RESOLVER else project_resolver)

    def _require(self, *scopes):
        self.principal.require_scope('automations:read')
        for scope in scopes:
            self.principal.require_scope(scope)

    def _owned(self, kind, entity_id):
        entity = self.repository.get(kind, entity_id)
        if entity is None:
            raise AutomationError('automation_not_found', 'Automation resource does not exist')
        return entity

    def _project(self, definition):
        project = definition.get('projectId')
        if not project:
            return
        if self.project_resolver is None:
            raise AutomationError('automation_project_unavailable', 'Owned project resolution is unavailable')
        if not self.project_resolver(self.principal, project):
            raise AutomationError('automation_not_found', 'Automation project does not exist')

    @staticmethod
    def _limit(value):
        if type(value) is not int or not 1 <= value <= limits()['pageRows']:
            raise AutomationError('invalid_page_limit', 'Automation page limit must be 1..8')
        return value

    def presentations(self, conversation_id, attempt_id, *, limit=8, after=''):
        self._require()
        from .presentation_query import presentations
        return presentations(self.repository,conversation_id,attempt_id,limit=self._limit(limit),after=after)

    def projects(self,*,limit=8,after=''):
        self._require()
        from .projects import project_view,validate_binding
        rows,cursor=self.projects_repository.repository.page('target',limit=self._limit(limit),after=after)
        items=[project_view(validate_binding(row)) for row in rows]
        return {'items':items,'nextCursor':cursor}

    def project(self,entity_id):
        self._require()
        from .projects import project_view
        row=self.projects_repository.binding(entity_id)
        if row is None:
            raise AutomationError('automation_not_found','Automation project does not exist')
        return project_view(row)

    def update_project(self,body):
        self._require('automations:manage')
        return self.projects_repository.update(body)

    def skill_source(self):
        self._require()
        from lib.skills.automation.source_settings import AutomationSkillSources
        return AutomationSkillSources(self.principal,client_factory=self.repository._factory,clock=self.clock).settings()

    def update_skill_source(self,body):
        self._require('automations:manage','skills:install')
        from lib.skills.automation.source_settings import AutomationSkillSources
        return AutomationSkillSources(self.principal,client_factory=self.repository._factory,clock=self.clock).update(body)

    def actions(self):
        self._require()
        return {'items': builtin_registry().documents()}

    def definitions(self, *, limit=8, after=''):
        self._require()
        rows, cursor = self.repository.page('definition', limit=self._limit(limit), after=after)
        return {'items': [definition_view(row) for row in rows], 'nextCursor': cursor}

    def definition(self, entity_id):
        self._require()
        return definition_view(self._owned('definition', entity_id))

    def publish(self, entity_id, body):
        self._require('automations:manage')
        validate('PublishRequest', body)
        if body['definition']['id'] != entity_id:
            raise AutomationError('automation_identity_mismatch', 'Definition ID must match the requested resource')
        prepared = prepare_definition(body['definition'])
        self._project(prepared.definition)
        saved = self.repository.publish(prepared, expected_version=body['expectedRevision'],
                                        request_id=body['requestId'], now=self.clock())
        return definition_view(saved)

    def inactive(self, entity_id, body, *, retire=False):
        self._require('automations:manage')
        validate('RevisionRequest', body)
        entity = self._owned('definition', entity_id)
        expected = body['expectedRevision']
        command = entity.value.get('publicationCommand', {})
        if entity.version != expected:
            if entity.version != expected + 1 or command.get('id') != body['requestId']:
                raise AutomationConflict('Automation definition revision changed')
            # Reconstruct only the expected CAS envelope. The atomic operation
            # verifies the exact request digest, including actor and mode.
            entity = VersionedEntity(entity.id, expected, entity.document_json)
        operation = self.repository.retire_definition if retire else self.repository.pause_definition
        return definition_view(operation(entity, now=self.clock(), request_id=body['requestId']))

    def _manual_trigger(self, entity_id, body, now):
        event = {'id': 'manual:' + body['requestId'], 'kind': 'manual', 'occurredAt': now,
                 'origin': 'human', 'evidence': {}, 'automationChain': []}
        run_id = occurrence_id(entity_id, event)
        existing = self.repository.get('run', run_id)
        if existing is not None:
            frozen = existing.value['trigger']
            if (existing.value['definitionRevision'] != body['expectedRevision']
                    or any(frozen.get(key) != event[key] for key in
                           ('id', 'kind', 'origin', 'evidence', 'automationChain'))):
                raise AutomationConflict('Manual occurrence request changed')
            return deepcopy(frozen)
        definition = self._owned('definition', entity_id)
        if definition.version != body['expectedRevision']:
            raise AutomationConflict('Automation definition revision changed')
        self._project(definition.value['definition'])
        if definition.value['definition'].get('projectId'):
            event['projectId'] = definition.value['definition']['projectId']
        return event

    def start(self, entity_id, body):
        self._require('automations:execute')
        validate('StartRequest', body)
        now = self.clock()
        event = self._manual_trigger(entity_id, body, now)
        try:
            run = self.repository.admit(entity_id, event, now=now,
                                       expected_definition_version=body['expectedRevision'])
        except AutomationConflict:
            # Concurrent first deliveries may select different server times.
            # Only an accepted occurrence proves a safe readback; never create
            # another run or acknowledge a different frozen revision.
            if self.repository.get('run', occurrence_id(entity_id, event)) is None:
                raise
            event = self._manual_trigger(entity_id, body, now)
            run = self.repository.admit(entity_id, event, now=now,
                                       expected_definition_version=body['expectedRevision'])
        return run_view(run)

    def history(self, entity_id, *, limit=8, before=''):
        self._require()
        self._owned('definition', entity_id)
        rows, cursor = self.repository.run_history(entity_id, limit=self._limit(limit), before=before)
        return {'items': [run_view(row) for row in rows], 'nextCursor': cursor}

    def run(self, entity_id):
        self._require()
        return run_view(self._owned('run', entity_id))

    def cancel(self, entity_id, body):
        self._require('automations:manage')
        validate('CancelRequest', body)
        return run_view(self.repository.cancel(entity_id, request_id=body['requestId'],
                                              reason=body['reason'], now=self.clock()))

    def approve(self, entity_id, body):
        self._require('automations:manage', 'automations:execute')
        validate('ApproveRequest', body)
        return run_view(self.repository.approve(entity_id, step_id=body['stepId'],
            expected_version=body['expectedRevision'], request_id=body['requestId'], now=self.clock()))
