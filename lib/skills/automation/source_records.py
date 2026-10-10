"""Closed owned source-setting records shared by the selected SQL/native ports.

Entry points: source_payload/digest/record, credential_scope, validate_source_record.
Dependencies: machine source/storage contracts and standard library. No vault,
network, filesystem activation, CLI execution or schema-library import in a SQL
transaction. Random credential references are excluded from semantic replay;
the stable private credential seal binds the value chosen by the authorizer.
"""
import hashlib
import json
import re

from lib.automations.contract import LIMITS, storage_schema

_STORAGE = storage_schema()
SOURCE = _STORAGE['x-skillSource']
PROPERTIES = _STORAGE['$defs']['skillSourceUpdate']['properties']
SOURCE_NAMESPACE = _STORAGE['x-namespaces']['skillSource']
ENVIRONMENTS = frozenset(PROPERTIES['environment']['enum'])


def credential_scope(owner, tenant):
    return hashlib.sha256((SOURCE['credentialScopeDomain']+'\n'+str(owner)+'\n'+(tenant or '')).encode()).hexdigest()[:24]


def credential_content_seal(owner, tenant, token):
    return hashlib.sha256(json.dumps([SOURCE['credentialSealDomain'],owner,tenant or '',token],
        ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def source_key(owner, tenant):
    return f'{owner}:{hashlib.sha256((tenant or "").encode()).hexdigest()[:32]}:'+SOURCE['id']


def is_managed_source_credential(name):
    """Reserve source versions from generic authenticated vault reveal/write.

    Match the canonical vault's case/whitespace normalization, including names
    that do not exist yet. Admission is namespace based, never existence based.
    """
    if not isinstance(name,str):
        return False
    normalized=name.strip().lower()
    return normalized.startswith('skill.') and _field(PROPERTIES['credential_ref'],normalized[6:])


def _field(schema, value):
    if 'anyOf' in schema:
        return any(_field(part,value) for part in schema['anyOf'])
    if 'enum' in schema:
        return value in schema['enum']
    kind=schema['type']
    if kind=='null':return value is None
    if kind=='boolean':return type(value) is bool
    if kind=='integer':
        return type(value) is int and schema.get('minimum',0)<=value<=schema.get('maximum',LIMITS['safeIntegerMax'])
    if kind=='string':
        return (isinstance(value,str) and schema.get('minLength',0)<=len(value)<=schema.get('maxLength',LIMITS['safeIntegerMax'])
            and ('pattern' not in schema or re.fullmatch(schema['pattern'],value) is not None))
    raise ValueError('Source setting schema has an unsupported field')


def source_payload(payload):
    if not isinstance(payload,dict) or set(payload)!=set(PROPERTIES):
        raise ValueError('Source settings require closed fields')
    if any(not _field(schema,payload[name]) for name,schema in PROPERTIES.items()):
        raise ValueError('Source setting field violates its contract')
    if payload['source_key']!=source_key(payload['user_id'],payload['tenant_id']):
        raise ValueError('Source setting identity differs')
    reference,seal=payload['credential_ref'],payload['credential_seal']
    if (reference is None)!=(seal is None) or payload['active'] and reference is None:
        raise ValueError('Active source requires a sealed credential reference')
    if reference is not None and not reference.startswith(credential_scope(payload['user_id'],payload['tenant_id'])+'_'):
        raise ValueError('Source credential belongs to another owner or tenant')


def source_digest(payload):
    value={key:item for key,item in payload.items() if key not in {'credential_ref','now_ms'}}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def source_record(payload,current):
    if current is not None:
        validate_source_record(current['value'])
        value=current['value']
        if value['ownerId']!=payload['user_id'] or value['tenantId']!=(payload['tenant_id'] or None):
            raise ValueError('Stored source ownership differs')
        created=value['createdAt']
    else:created=payload['now_ms']
    return {'contract':SOURCE['contract'],'id':SOURCE['id'],'ownerId':payload['user_id'],
        'tenantId':payload['tenant_id'] or None,'environment':payload['environment'],
        'active':payload['active'],'credentialRef':payload['credential_ref'],'credentialSeal':payload['credential_seal'],
        'createdAt':created,'updatedAt':payload['now_ms'],'command':{'id':payload['request_id'],
            'digest':source_digest(payload),'actorSubjectId':payload['actor_subject_id']}}


def validate_source_record(value):
    if (not isinstance(value,dict) or set(value)!={'contract','id','ownerId','tenantId','environment',
            'active','credentialRef','credentialSeal','createdAt','updatedAt','command'}
            or value['contract']!=SOURCE['contract'] or value['id']!=SOURCE['id']
            or type(value['ownerId']) is not int or not 1<=value['ownerId']<=LIMITS['safeIntegerMax']
            or value['tenantId'] is not None and (not isinstance(value['tenantId'],str) or not value['tenantId'] or len(value['tenantId'])>256)
            or value['environment'] not in ENVIRONMENTS or type(value['active']) is not bool
            or any(type(value[field]) is not int or not 1<=value[field]<=LIMITS['safeIntegerMax'] for field in ['createdAt','updatedAt'])):
        raise ValueError('Invalid stored source settings')
    reference,seal=value['credentialRef'],value['credentialSeal']
    if (not _field(PROPERTIES['credential_ref'],reference) or not _field(PROPERTIES['credential_seal'],seal)
            or (reference is None)!=(seal is None) or value['active'] and reference is None
            or reference is not None and not reference.startswith(credential_scope(value['ownerId'],value['tenantId'])+'_')):
        raise ValueError('Stored source credential ownership differs')
    command=value['command']
    if (not isinstance(command,dict) or set(command)!={'id','digest','actorSubjectId'}
            or not _field(PROPERTIES['request_id'],command['id'])
            or not _field(PROPERTIES['actor_subject_id'],command['actorSubjectId'])
            or not isinstance(command['digest'],str) or re.fullmatch('[0-9a-f]{64}',command['digest']) is None):
        raise ValueError('Invalid stored source request identity')
