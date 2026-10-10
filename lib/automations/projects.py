"""Durable owned project bindings for automation scope, never file-write grants.

Entry points: AutomationProjects and resolve_task_project. Dependencies: the
selected storage port and explicit PrincipalContext. Root identity is immutable;
one deterministic binding record provides registration and direct root lookup.
No recent-project cache, global selection, SQL, watcher or background pool.
"""
import hashlib
import math
import os
from pathlib import Path
import re
import time
from types import MappingProxyType

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from .contract import LIMITS, STORAGE_NAMESPACES, storage_schema
from .errors import AutomationConflict, AutomationError
from .publication import canonical
from .repository import AutomationRepository

_PROJECT = MappingProxyType(storage_schema()['x-project'])
_PROJECT_INPUT = MappingProxyType({name:MappingProxyType(field) for name,field in
    storage_schema()['$defs']['projectUpdate']['properties'].items()})


def project_id(root):
    return 'project_' + hashlib.sha256(root.encode('utf-8')).hexdigest()


def canonical_root(value, *, must_exist=False, follow_aliases=True):
    if (not isinstance(value,str) or not value or '\x00' in value
            or len(value)>_PROJECT['rootCharacters']
            or not Path(value).is_absolute()):
        raise AutomationError('invalid_project_root','Project root must be an absolute bounded path')
    try:
        # Retirement addresses the registered root string even when its
        # filesystem entry has disappeared or now points at another target.
        resolved=(Path(value).resolve(strict=must_exist) if follow_aliases
                  else Path(os.path.normpath(value)))
        if must_exist and not resolved.is_dir():
            raise ValueError('not a directory')
    except (OSError,ValueError,RuntimeError) as exc:
        raise AutomationError('invalid_project_root','Project root is unavailable') from exc
    root=str(resolved)
    if len(root)>_PROJECT['rootCharacters']:
        raise AutomationError('invalid_project_root','Canonical project root exceeds its bound')
    return root


def project_payload(payload):
    """Validate the backend-neutral request and immutable natural binding key."""
    properties=_PROJECT_INPUT
    if not isinstance(payload,dict) or set(payload)!=set(properties):
        raise ValueError('Invalid project registration fields')
    for name,schema in properties.items():
        value=payload[name];kind=schema['type']
        if kind=='integer':
            valid=type(value) is int and schema['minimum']<=value<=schema['maximum']
        elif kind=='boolean':
            valid=type(value) is bool
        else:
            valid=isinstance(value,str) and schema.get('minLength',0)<=len(value)<=schema.get('maxLength',128)
            if valid and 'pattern' in schema: valid=re.fullmatch(schema['pattern'],value) is not None
        if not valid: raise ValueError('Invalid project registration '+name)
    root=payload['root']
    if '\x00' in root:
        raise ValueError('Project root contains a null character')
    prefix=f"{payload['user_id']}:{hashlib.sha256(payload['tenant_id'].encode()).hexdigest()[:32]}:"
    identity=project_id(root)
    if payload['project_key']!=prefix+identity:
        raise ValueError('Project registration escaped its owner/root')
    return identity


def project_digest(payload):
    return hashlib.sha256(canonical({k:v for k,v in payload.items() if k!='now_ms'}).encode()).hexdigest()


def project_record(payload,current=None):
    identity=project_payload(payload)
    if current is not None:
        old=current['value']
        if (old.get('contract')!=_PROJECT['contract']
                or old.get('id')!=identity or type(old.get('ownerId')) is not int
                or old['ownerId']!=payload['user_id'] or old.get('tenantId')!=(payload['tenant_id'] or None)
                or old.get('root')!=payload['root'] or type(old.get('createdAt')) is not int):
            raise ValueError('Stored project registration identity differs')
        created=old['createdAt']
    else:
        created=payload['now_ms']
    return {'contract':_PROJECT['contract'],'id':identity,
        'ownerId':payload['user_id'],'tenantId':payload['tenant_id'] or None,
        'root':payload['root'],'label':payload['label'],'active':payload['active'],
        'createdAt':created,'updatedAt':payload['now_ms'],
        'command':{'id':payload['request_id'],'digest':project_digest(payload),
                   'actorSubjectId':payload['actor_subject_id']}}


def project_view(entity):
    value=entity.value
    return {'revision':entity.version,**{key:value[key] for key in
        ('id','root','label','active','createdAt','updatedAt')}}


def validate_binding(entity):
    if entity is not None:
        value=entity.value
        if (value.get('contract')!=_PROJECT['contract']
                or type(value.get('active')) is not bool or not isinstance(value.get('root'),str)
                or not value['root'] or len(value['root'])>_PROJECT['rootCharacters'] or '\x00' in value['root']
                or not isinstance(value.get('label'),str) or not 1<=len(value['label'])<=_PROJECT['labelCharacters']
                or any(type(value.get(field)) is not int or not 1<=value[field]<=LIMITS['safeIntegerMax']
                       for field in ('createdAt','updatedAt'))
                or entity.version>LIMITS['safeIntegerMax']
                or project_id(value['root'])!=entity.id):
            raise AutomationError('automation_storage_integrity','Invalid project binding')
    return entity


class AutomationProjects:
    def __init__(self,principal,*,client_factory=None,clock=time.time):
        options={'client_factory':client_factory} if client_factory is not None else {}
        self.repository=AutomationRepository(principal,**options)
        self.principal=principal
        self.clock=clock

    def binding(self,identity):
        """Internal ownership lookup; HTTP services separately enforce scopes."""
        if not isinstance(identity,str) or not re.fullmatch(r'project_[0-9a-f]{64}',identity):
            return None
        repo=self.repository
        row=repo._factory().query('record.get',{'namespace':STORAGE_NAMESPACES['target'],
            'key':repo._key(identity),'user_id':repo.owner_id})
        return validate_binding(repo._decode(identity,row))

    def owns(self,principal,identity):
        if principal!=self.principal:
            raise PermissionError('Project resolver principal changed')
        row=self.binding(identity)
        return row is not None and row.value['active']

    def update(self,body):
        from .management_contract import validate
        self.principal.require_scope('automations:read')
        self.principal.require_scope('automations:manage')
        validate('ProjectUpdateRequest',body)
        root=canonical_root(body['root'],must_exist=body['active'],follow_aliases=body['active'])
        repo=self.repository;identity=project_id(root)
        now=self.clock()
        if isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now):
            raise AutomationError('invalid_project_clock','Project clock is invalid')
        payload={'user_id':repo.owner_id,'tenant_id':self.principal.tenant_id or '',
            'project_key':repo._key(identity),'expected_version':body['expectedRevision'],
            'request_id':body['requestId'],'actor_subject_id':self.principal.subject_id,
            'root':root,'label':body['label'],'active':body['active'],'now_ms':int(now*1000)}
        try:
            project_payload(payload)
        except ValueError as exc:
            raise AutomationError('invalid_project_binding',str(exc)) from exc
        try:
            row=repo._factory(write=True).command('automation.project.update',payload,None)
        except StorageError as exc:
            if exc.code=='database_conflict':
                raise AutomationConflict(str(exc)) from exc
            if exc.code not in {'database_unavailable','database_timeout'}:
                raise
            readback=self.binding(identity)
            if (readback is None or readback.version!=payload['expected_version']+1
                    or readback.value.get('command',{}).get('digest')!=project_digest(payload)
                    or readback.value['command'].get('id')!=payload['request_id']):
                raise
            return project_view(readback)
        return project_view(repo._decode(identity,row))


def resolve_task_project(principal,config,*,project=None,client_factory=None):
    """Freeze one explicitly registered root; caller config cannot assert an ID."""
    if not isinstance(principal,PrincipalContext):
        raise TypeError('Task project resolution requires PrincipalContext')
    bindings=AutomationProjects(principal,client_factory=client_factory)
    root=config.get('projectPath')
    resolved=canonical_root(root) if root else None
    identity=project_id(resolved) if resolved else None
    if project is not None:
        row=bindings.binding(project)
        if row is None or not row.value['active'] or resolved is not None and row.value['root']!=resolved:
            raise AutomationError('automation_not_found','Owned task project is unavailable')
        return project
    if identity is None:
        return None
    row=bindings.binding(identity)
    return identity if row is not None and row.value['active'] else None
