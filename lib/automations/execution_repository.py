"""Operational admission, progress and cancellation backed by atomic operations.

Entry point: AutomationExecutionRepository. Permission grants are explicit,
definition-bound, and narrowed at publication. The canonical worker job and
run are admitted together; a live authority fence guards every progress write.
"""

from __future__ import annotations

import hashlib
import json

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from .definitions import PreparedDefinition
from .contract import RUNTIME, storage_input_validator
from .errors import AutomationConflict, AutomationError
from .repository import AutomationRepository, VersionedEntity
from .transitions import ExecutionFence, new_run, occurrence_id, trigger_digest, _now


class AutomationExecutionRepository(AutomationRepository):
    def _definition_update(self, payload):
        from .publication import request_digest
        digest = request_digest(payload)
        if payload['request_id'] is None:
            payload['request_id'] = 'ap_' + digest
        if (type(payload['expected_version']) is not int
                or next(storage_input_validator('definitionUpdate').iter_errors(payload),None) is not None):
            raise AutomationError('invalid_publication','Invalid automation publication request')
        try:
            row = self._factory(write=True).command('automation.definition.update',payload,None)
        except StorageError as exc:
            if exc.code == 'database_conflict':
                raise AutomationConflict(exc.message) from exc
            if exc.code not in {'database_timeout','database_unavailable','database_busy'}:
                raise
            row = self._factory().query('record.get',{'namespace':'automation.definitions.v1',
                'key':payload['definition_key'],'user_id':self.owner_id})
            command = row['value'].get('publicationCommand') if row else None
            if (row is None or row['version'] != payload['expected_version']+1
                    or command is None or command['id'] != payload['request_id'] or command['digest'] != digest):
                raise
        entity_id = payload['definition_key'][len(self._prefix):]
        return self._decode(entity_id,row)

    def publish(self, prepared: PreparedDefinition, *, expected_version: int, now: float, request_id: str | None = None):
        from datetime import datetime,timezone
        from lib.scheduler.calendar import CronSchedule
        from .publication import publication_record,validate_publication_record
        now = _now(now)
        record = publication_record(prepared,self.principal)
        validate_publication_record(record,prepared.definition_json,prepared.action_contracts_json,
            owner=self.owner_id,tenant=self.principal.tenant_id or '')
        rule = prepared.definition
        first_due = None
        if rule['enabled'] and rule['trigger']['kind'] == 'schedule':
            try:
                slot = CronSchedule(rule['trigger']['schedule'],rule['trigger']['timezone']).next_after(
                    datetime.fromtimestamp(now,timezone.utc))
            except (ValueError,OverflowError,OSError) as exc:
                raise AutomationError('invalid_timestamp','Publication clock is outside the calendar range') from exc
            if slot is None:
                raise AutomationError('schedule_exhausted','Schedule has no next representable UTC slot')
            first_due = int(slot.timestamp()*1000)
        return self._definition_update({'user_id':self.owner_id,'tenant_id':self.principal.tenant_id or '',
            'definition_key':self._key(rule['id']),'expected_version':expected_version,
            'request_id':request_id,'actor_subject_id':self.principal.subject_id,'mode':'publish','record':record,
            'definition_json':prepared.definition_json,'actions_json':prepared.action_contracts_json,
            'first_due_ms':first_due,'now_ms':int(now*1000)})

    def _set_definition_inactive(self, entity: VersionedEntity, *, mode: str, now: float, request_id: str | None):
        from copy import deepcopy
        from .publication import canonical
        self.principal.require_scope('automations:manage')
        value = entity.value
        if value.get('ownerId') != self.owner_id or value.get('tenantId') != self.principal.tenant_id:
            raise AutomationError('automation_owner_mismatch','Definition is outside this repository')
        definition = deepcopy(value['definition'])
        definition['enabled'] = False
        return self._definition_update({'user_id':self.owner_id,'tenant_id':self.principal.tenant_id or '',
            'definition_key':self._key(entity.id),'expected_version':entity.version,
            'request_id':request_id,'actor_subject_id':self.principal.subject_id,'mode':mode,
            'record':None,'definition_json':canonical(definition),
            'actions_json':json.dumps(value['compiled']['actions'],allow_nan=False,sort_keys=True,separators=(',',':')),
            'first_due_ms':None,'now_ms':int(_now(now)*1000)})

    def retire_definition(self, entity: VersionedEntity, *, now: float, request_id: str | None = None):
        return self._set_definition_inactive(entity,mode='retire',now=now,request_id=request_id)

    def pause_definition(self, entity: VersionedEntity, *, now: float, request_id: str | None = None):
        return self._set_definition_inactive(entity,mode='pause',now=now,request_id=request_id)

    def _command(self, name, payload):
        # Natural identity + exact CAS readback at the authority replaces a
        # permanent command receipt on every progress tick.
        try:
            return self._factory(write=True).command(name, payload, None)
        except StorageError as exc:
            if exc.code == 'database_conflict':
                raise AutomationConflict(exc.message) from exc
            if exc.code in {'database_timeout', 'database_unavailable', 'database_busy'}:
                row = self._factory().query('record.get', {
                    'namespace': 'automation.runs.v1', 'key': payload['run_key'],
                    'user_id': self.owner_id,
                })
                if row is not None and name == 'automation.run.cancel':
                    command = row['value'].get('cancelCommand')
                    if (command and command['id'] == payload['request_id']
                            and command['reason'] != payload['reason']):
                        raise AutomationConflict('Automation cancellation request evidence changed') from exc
                    if row['value']['status'] in RUNTIME['terminalRunStates']:
                        return {'run': row}
                    if command:
                        job = self._factory().query('worker_job.get', {
                            'task_id': row['value']['jobId'], 'user_id': self.owner_id})
                        if job is not None and job['cancelSequence']:
                            return {'run': row}
                if row is not None and name == 'automation.run.approve':
                    from .approval import request_digest
                    grant = row['value']['steps'].get(payload['step_id'], {}).get('approval')
                    if (grant and grant['requestId'] == payload['request_id']
                            and grant['requestDigest'] == request_digest(payload)):
                        return {'accepted': True, 'alreadyApproved': True, 'run': row}
                if row is not None and name == 'automation.run.admit':
                    if row['value'].get('triggerDigest') == payload['run']['triggerDigest']:
                        job = self._factory().query('worker_job.get', {
                            'task_id': row['value']['jobId'], 'user_id': self.owner_id})
                        if job is not None:
                            return {'created': False, 'run': row, 'job': job}
                elif (name == 'automation.run.advance' and row is not None
                      and row['version'] == payload['expected_version'] + 1
                      and json.dumps(row['value'], sort_keys=True) == json.dumps(payload['run'], sort_keys=True)):
                    return row
            raise

    def admit(self, definition_id: str, trigger: dict, *, now: float,
              producer_fence: ExecutionFence | None = None,
              expected_definition_version: int | None = None) -> VersionedEntity:
        self.principal.require_scope('automations:execute')
        if (expected_definition_version is not None and
                (type(expected_definition_version) is not int or expected_definition_version < 1)):
            raise AutomationError('revision_required', 'A positive definition revision is required')
        run_id = occurrence_id(definition_id, trigger)
        existing = self.get('run', run_id)
        if existing is not None:
            if (expected_definition_version is not None and
                    existing.value['definitionRevision'] != expected_definition_version):
                raise AutomationConflict('Automation occurrence revision changed')
            if existing.value['triggerDigest'] != trigger_digest(trigger):
                raise AutomationConflict('Automation occurrence evidence changed')
            job = self._factory().query('worker_job.get', {
                'task_id': existing.value.get('jobId', ''), 'user_id': self.owner_id})
            if job is None:
                raise AutomationError('automation_storage_integrity', 'Accepted automation has no durable job')
            return existing
        definition = self.get('definition', definition_id)
        if definition is None:
            raise AutomationError('automation_not_found', 'Automation does not exist')
        if expected_definition_version is not None and definition.version != expected_definition_version:
            raise AutomationConflict('Automation definition revision changed')
        value = definition.value
        grant = value.get('grant', {}).get('principal')
        if not isinstance(grant, dict):
            raise AutomationError('automation_grant_required', 'Publish an authorized definition before running it')
        principal = PrincipalContext.from_payload(grant)
        if (principal.owner_user_id != self.owner_id or principal.tenant_id != self.principal.tenant_id
                or 'admin' in principal.scopes):
            raise AutomationError('automation_grant_invalid', 'Automation execution grant is invalid')
        for scope in principal.scopes:
            self.principal.require_scope(scope)
        run = new_run(definition, trigger, now=now)
        key = self._key(run_id)
        run.update(ownerId=self.owner_id, tenantId=self.principal.tenant_id,
                   jobId='automation-run:' + hashlib.sha256(key.encode()).hexdigest(),
                   principal=grant)
        payload = {
            'user_id': self.owner_id, 'tenant_id': self.principal.tenant_id or '',
            'run_key': key, 'run': run, 'now_ms': int(now * 1000),
            'definition_key': self._key(definition_id), 'definition_revision': definition.version,
            'definition_digest': value['compiled']['digest'],
            'producer_fence':producer_fence.document() if producer_fence is not None else None,
        }
        result = self._command('automation.run.admit', payload)
        return self._decode(run_id, result['run'])

    def advance(self, entity: VersionedEntity, run: dict, fence: ExecutionFence, *, now: float):
        self.principal.require_scope('automations:execute')
        payload = {'user_id': self.owner_id, 'tenant_id': self.principal.tenant_id or '',
                   'run_key': self._key(entity.id), 'run': run, 'now_ms': int(now * 1000),
                   'expected_version': entity.version, 'worker_id': fence.worker_id,
                   'fencing_token': fence.token}
        result = self._command('automation.run.advance', payload)
        saved = self._decode(entity.id, result)
        if run['trigger']['id'].startswith('task-start:'):
            from .presentation_index import completed_presentations
            pointers = list(completed_presentations(entity.value,saved.value))
            if pointers:
                from lib.conversations import notify_conv_changed
                notify_conv_changed(pointers[0]['conversationId'],user_id=self.owner_id)
        return saved

    def cancel(self, run_id: str, *, request_id: str, reason: str, now: float):
        self.principal.require_scope('automations:manage')
        payload = {'user_id': self.owner_id, 'tenant_id': self.principal.tenant_id or '',
                   'run_key': self._key(run_id), 'request_id': request_id, 'reason': reason,
                   'now_ms': int(_now(now) * 1000)}
        if next(storage_input_validator('cancel').iter_errors(payload), None) is not None:
            raise AutomationError('invalid_cancel_request', 'Cancellation requires a bounded request ID, reason and timestamp')
        try:
            result = self._command('automation.run.cancel', payload)
        except StorageError as exc:
            if exc.code == 'database_not_found':
                raise AutomationError('automation_not_found', 'Automation run does not exist') from exc
            raise
        return self._decode(run_id, result['run'])

    def approve(self, run_id: str, *, step_id: str, expected_version: int, request_id: str, now: float):
        from .approval import canonical
        from .registry import ActionContract
        for scope in ('automations:read', 'automations:manage', 'automations:execute'):
            self.principal.require_scope(scope)
        entity = self.get('run', run_id)
        if entity is None:
            raise AutomationError('automation_not_found', 'Automation run does not exist')
        run = entity.value
        step = next((s for s in run['snapshot']['steps'] if s['id'] == step_id), None)
        if step is None:
            raise AutomationConflict('Approval step does not exist')
        action = ActionContract.from_document(run['actions'][step['action']])
        scopes = {'automations:read', 'automations:manage', 'automations:execute', *action.required_scopes}
        for scope in scopes:
            self.principal.require_scope(scope)
        arguments = run['steps'][step_id].get('arguments')
        if not isinstance(arguments, dict):
            raise AutomationConflict('Approval requires concrete review arguments')
        action.validate(arguments)
        actor = PrincipalContext(kind=self.principal.kind, subject_id=self.principal.subject_id,
            owner_user_id=self.owner_id, tenant_id=self.principal.tenant_id, scopes=frozenset(scopes))
        payload = {'user_id': self.owner_id, 'tenant_id': self.principal.tenant_id or '',
            'run_key': self._key(run_id), 'step_id': step_id, 'expected_version': expected_version,
            'request_id': request_id, 'now_ms': int(_now(now)*1000), 'actor_principal': actor.to_payload(),
            'arguments_json': canonical(arguments), 'action_json': canonical(action.document())}
        if next(storage_input_validator('approve').iter_errors(payload), None) is not None:
            raise AutomationError('invalid_approval_request', 'Approval requires a step, revision and bounded request ID')
        result = self._command('automation.run.approve', payload)
        return self._decode(run_id, result['run'])
