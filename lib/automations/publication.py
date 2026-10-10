"""Prepared publication bindings shared by repository and SQL authority.

Entry points: publication_record, validate_publication_record, request_digest,
subscription_key and clock_job_id. User data first passes prepare_definition;
these helpers prove serialization, action versions, grants and scoped identity.
No storage, scheduler loop, clock probe or executor is imported on discovery.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from lib.identity import PrincipalContext
from .conditions import check_json_bounds
from .contract import LIMITS, definition_schema
from .errors import AutomationError


def canonical(value) -> str:
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',', ':'))


@lru_cache(maxsize=1)
def catalog_json() -> str:
    return canonical(json.loads((Path(__file__).resolve().parents[2] /
        'contracts/automation_actions_v1.json').read_text(encoding='utf-8')))


def publication_record(prepared, publisher: PrincipalContext) -> dict:
    publisher.require_scope('automations:manage')
    scopes = {'automations:read','automations:execute'}
    for action in prepared.action_contracts.values():
        scopes.update(action['requiredScopes'])
    for scope in scopes:
        publisher.require_scope(scope)
    grant = PrincipalContext.system(subject_id=f'automation:{publisher.require_owner()}:{prepared.definition["id"]}',
        owner_user_id=publisher.owner_user_id,tenant_id=publisher.tenant_id,scopes=scopes)
    return {'definition':prepared.definition,
        'compiled':{'digest':prepared.digest,'stepOrder':list(prepared.ordered_step_ids),'actions':prepared.action_contracts},
        'grant':{'principal':grant.to_payload(),'approvedBy':publisher.subject_id}}


def validate_publication_record(record: dict, definition_json: str, actions_json: str, *, owner: int, tenant: str) -> str:
    """Verify the prepared binding, without introducing another rule interpreter."""
    check_json_bounds(record,max_depth=LIMITS['recordDepth'],max_nodes=LIMITS['recordNodes'])
    try:
        if (set(record) != {'definition','compiled','grant'}
                or set(record['compiled']) != {'digest','stepOrder','actions'}
                or set(record['grant']) != {'principal','approvedBy'}
                or not isinstance(definition_json,str)
                or len(definition_json.encode()) > LIMITS['definitionBytes']
                or canonical(json.loads(definition_json)) != canonical(record['definition'])):
            raise ValueError('Invalid prepared publication envelope')
        definition, compiled = record['definition'],record['compiled']
        entity_id = definition['id']
        if (not isinstance(entity_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}',entity_id)
                or type(definition['enabled']) is not bool
                or compiled['digest'] != hashlib.sha256((definition_json+'\n'+actions_json).encode()).hexdigest()
                or canonical(json.loads(actions_json)) != canonical(compiled['actions'])):
            raise ValueError('Invalid prepared definition identity/digest')
        trigger = definition['trigger']
        if trigger['kind'] not in {'event','schedule','manual'}:
            raise ValueError('Invalid trigger kind')
        if trigger['kind'] == 'event' and (trigger['event'] not in {'task.started','skills.updated'}
                or trigger['scope'] not in {'all','human','automation'}):
            raise ValueError('Invalid event subscription')
        steps = definition['steps']
        if not isinstance(steps,list) or not 1 <= len(steps) <= definition_schema()['properties']['steps']['maxItems']:
            raise ValueError('Invalid prepared step count')
        ids = [step['id'] for step in steps]
        order = compiled['stepOrder']
        if (not isinstance(order,list) or any(not isinstance(key,str) for key in ids+order)
                or len(set(ids)) != len(ids) or len(order) != len(ids) or set(order) != set(ids)):
            raise ValueError('Invalid prepared dependency order')
        positions = {key:index for index,key in enumerate(order)}
        if any(not isinstance(step['after'],list) or any(not isinstance(parent,str) for parent in step['after']) for step in steps):
            raise ValueError('Invalid prepared dependencies')
        if any(parent not in positions or positions[parent] >= positions[step['id']]
               for step in steps for parent in step['after']):
            raise ValueError('Invalid prepared dependency order')
        catalogue = {action['id']:action for action in json.loads(catalog_json())['actions']}
        actions = compiled['actions']
        if set(actions) != {step['action'] for step in steps} or any(
                key not in catalogue or value != catalogue[key] for key,value in actions.items()):
            raise ValueError('Publication requires closed supported action versions')
        principal = PrincipalContext.from_payload(record['grant']['principal'])
        required = {'automations:read','automations:execute'}
        for action in actions.values():
            required.update(action['requiredScopes'])
        if (principal.kind != 'system' or principal.subject_id != f'automation:{owner}:{entity_id}'
                or principal.require_owner() != owner
                or (principal.tenant_id or '') != tenant or principal.scopes != frozenset(required)
                or principal.to_payload() != record['grant']['principal']
                or not isinstance(record['grant']['approvedBy'],str)
                or not 1 <= len(record['grant']['approvedBy']) <= 256):
            raise ValueError('Publication requires an exact scoped execution grant')
    except (KeyError,TypeError,ValueError,PermissionError) as exc:
        raise AutomationError('invalid_publication','Invalid prepared publication binding') from exc
    return entity_id


def request_digest(payload: dict) -> str:
    # Float spellings vary across JSON encoders. The original definition JSON
    # is a byte proof; the remaining digest basis contains strings and integers.
    record = payload['record']
    basis = {'definition_key':payload['definition_key'],'expected_version':payload['expected_version'],
        'mode':payload['mode'],'actorSubjectId':payload['actor_subject_id'],'definitionJson':payload['definition_json'],'actionsJson':payload['actions_json']}
    if record is not None:
        basis.update(stepOrder=record['compiled']['stepOrder'],grant=record['grant'])
    return hashlib.sha256(canonical(basis).encode()).hexdigest()


def subscription_key(prefix: str, definition: dict) -> str | None:
    trigger = definition['trigger']
    if not definition['enabled'] or trigger['kind'] != 'event':
        return None
    project = definition.get('projectId')
    project_key = hashlib.sha256(project.encode()).hexdigest() if project else '*'
    return f'{prefix}{trigger["event"]}:{trigger["scope"]}:{project_key}:{definition["id"]}'


def clock_job_id(key: str, revision: int) -> str:
    return 'automation-clock:' + hashlib.sha256(f'{key}:{revision}'.encode()).hexdigest()
