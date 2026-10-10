"""Owned provider configuration and immutable vault versions over selected storage.

Entry points: AutomationSkillSources.settings/update/credential. Secrets are
staged outside database transactions, bound by private content seals, and never
projected into public DTOs. Unknown acknowledgements retain recoverable stages.
Dependencies: owned automation records, the credential vault and source contract.
"""
import math
import time
import uuid

from lib import credentials_vault
from lib.automations.contract import LIMITS
from lib.automations.errors import AutomationConflict, AutomationError
from lib.automations.management_contract import validate
from lib.automations.repository import AutomationRepository
from lib.storage.errors import StorageError

from .mtskills_cli import OwnedMTSkillsCredential
from .source_records import (
    SOURCE, SOURCE_NAMESPACE, credential_content_seal, credential_scope,
    source_digest, source_key, source_payload, validate_source_record,
)


def source_view(entity):
    if entity is None:
        return {'id': SOURCE['id'], 'revision': 0, 'environment': 'prod',
                'active': False, 'credentialConfigured': False}
    value = entity.value
    return {'id': SOURCE['id'], 'revision': entity.version,
            'credentialConfigured': value['credentialRef'] is not None,
            **{key: value[key] for key in ('environment', 'active', 'createdAt', 'updatedAt')}}


class AutomationSkillSources:
    def __init__(self, principal, *, client_factory=None, clock=time.time, vault=credentials_vault):
        options = {'client_factory': client_factory} if client_factory is not None else {}
        self.repository = AutomationRepository(principal, **options)
        self.principal = principal
        self.clock = clock
        self.vault = vault

    def binding(self):
        repo = self.repository
        row = repo._factory().query('record.get', {'namespace': SOURCE_NAMESPACE,
            'key': source_key(repo.owner_id, self.principal.tenant_id), 'user_id': repo.owner_id})
        entity = repo._decode(SOURCE['id'], row)
        if entity is not None:
            try:
                validate_source_record(entity.value)
                if entity.version > LIMITS['safeIntegerMax']:
                    raise ValueError('Invalid source revision')
            except (ValueError, TypeError) as exc:
                raise AutomationError('automation_storage_integrity', 'Invalid owned source record') from exc
        return entity

    def settings(self):
        self.principal.require_scope('automations:read')
        return source_view(self.binding())

    def _cleanup_obsolete(self, entity):
        """Reclaim only stages whose CAS can no longer win; never the live ref.

        The vault already bounds this metadata scan. Its note names the expected
        source revision, so a stage from an unknown acknowledgement is retained
        until an authoritative later revision proves it cannot become current.
        """
        prefix = 'skill.' + credential_scope(self.repository.owner_id, self.principal.tenant_id) + '_'
        current = 'skill.' + entity.value['credentialRef'] if entity.value['credentialRef'] else None
        for row in self.vault.list_entries():
            name = row['name']
            if not name.startswith(prefix) or name == current:
                continue
            note = row['note'].split(':')
            if len(note) == 3 and note[0] == 'as' and note[1].isdecimal() and int(note[1]) < entity.version:
                self.vault.delete_entry(name)

    def credential(self):
        self.principal.require_scope('skills:read')
        entity = self.binding()
        if entity is None or not entity.value['active']:
            raise AutomationError('skills_provider_configuration', 'Owned skill source is inactive')
        value = entity.value
        token = self.vault.get_entry('skill.' + value['credentialRef'])
        credential = OwnedMTSkillsCredential(self.repository.owner_id, self.principal.tenant_id,
                                            value['environment'], token)
        credential.require(self.principal)
        if credential_content_seal(self.repository.owner_id, self.principal.tenant_id, token) != value['credentialSeal']:
            raise AutomationError('skills_provider_configuration', 'Owned credential version has changed')
        return entity, credential

    def update(self, body):
        for scope in ('automations:read', 'automations:manage', 'skills:install'):
            self.principal.require_scope(scope)
        # Schema error messages must never echo a rejected credential value.
        if not isinstance(body, dict):
            raise AutomationError('skills_provider_configuration', 'Source configuration requires an object')
        token = body.get('credential')
        if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 8192
                                 or any(not 33 <= ord(char) <= 126 for char in token)):
            raise AutomationError('skills_provider_configuration', 'Invalid skill source credential')
        try:
            validate('SkillSourceUpdateRequest', body)
        except AutomationError as exc:
            # A schema error can quote any rejected field, including a secret
            # pasted into the environment/request ID rather than credential.
            raise AutomationError(exc.code, 'Invalid skill source configuration') from None
        repo = self.repository
        current = self.binding()
        reference = current.value['credentialRef'] if current else None
        seal = current.value['credentialSeal'] if current else None
        if 'credential' in body:
            reference = (credential_scope(repo.owner_id, self.principal.tenant_id) + '_' + uuid.uuid4().hex
                         if token is not None else None)
            seal = credential_content_seal(repo.owner_id, self.principal.tenant_id, token) if token is not None else None
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise AutomationError('invalid_source_clock', 'Skill source clock is invalid')
        payload = {'user_id': repo.owner_id, 'tenant_id': self.principal.tenant_id or '',
            'source_key': source_key(repo.owner_id, self.principal.tenant_id),
            'expected_version': body['expectedRevision'], 'request_id': body['requestId'],
            'actor_subject_id': self.principal.subject_id, 'environment': body['environment'],
            'active': body['active'], 'credential_ref': reference, 'credential_seal': seal, 'now_ms': int(now * 1000)}
        try:
            source_payload(payload)
        except (ValueError, TypeError) as exc:
            raise AutomationError('skills_provider_configuration', 'Invalid owned source configuration') from exc
        digest = source_digest(payload)
        if (current is not None and current.version == payload['expected_version'] + 1
                and current.value['command']['id'] == payload['request_id']
                and current.value['command']['digest'] == digest):
            # Accepted identity is already authoritative. A full vault must not
            # force an identical retry to allocate another disposable version.
            return source_view(current)
        staged = 'skill.' + reference if 'credential' in body and token is not None else None
        if current is not None:
            self._cleanup_obsolete(current)
        if staged:
            try:
                self.vault.create_entry(staged, token, note=f"as:{body['expectedRevision']}:{digest}")
            except ValueError as exc:
                raise AutomationError('skills_provider_configuration', 'Credential staging is unavailable') from exc
        try:
            row = repo._factory(write=True).command('automation.skills.source.update', payload, None)
        except StorageError as exc:
            if exc.code in {'database_conflict', 'database_invalid_payload', 'database_forbidden'}:
                if staged:
                    self.vault.delete_entry(staged)
                if exc.code == 'database_conflict':
                    raise AutomationConflict('Skill source revision or request changed') from exc
                raise
            if exc.code not in {'database_unavailable', 'database_timeout'}:
                raise  # Unknown outcome: retain the immutable stage.
            accepted = self.binding()
            if (accepted is None or accepted.version != payload['expected_version'] + 1
                    or accepted.value['command']['id'] != payload['request_id']
                    or accepted.value['command']['digest'] != digest):
                raise
        else:
            accepted = repo._decode(SOURCE['id'], row)
            if accepted is None:
                raise AutomationError('automation_storage_integrity', 'Skill source acknowledgement is missing')
            validate_source_record(accepted.value)
        # Exact replay may select another request's earlier random stage.
        if staged and staged != 'skill.' + (accepted.value['credentialRef'] or ''):
            self.vault.delete_entry(staged)
        self._cleanup_obsolete(accepted)
        return source_view(accepted)
