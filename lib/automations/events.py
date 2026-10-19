"""Task-start evidence and indexed subscription probes, without a resident loop.

Entry points: task_start_context, task_started_job, subscription_prefixes.
The physical worker supplies server-owned context; the storage start transaction
freezes the actual timestamp and enqueues transport only for a subscribed event.
Dependencies: automation contract, explicit identity, controlled turn initiation.
"""
from copy import deepcopy
import hashlib

from lib.identity import PrincipalContext
from lib.turn_initiation import INITIATOR_HUMAN, INITIATOR_UNKNOWN, resolve_initiator
from .contract import LIMITS, storage_input_validator, storage_schema, task_started_context_is_valid
from .errors import AutomationError


def validate_start_context(value: dict) -> None:
    if not task_started_context_is_valid(value):
        raise AutomationError('invalid_task_start_context', 'Invalid task-start automation context')


def task_start_context(principal: PrincipalContext, message: dict, *,
                       automation_chain=(), project_id: str | None = None) -> dict | None:
    """Capture trusted task fields once, before caller config/hooks can mutate."""
    principal.require_owner(context='automation task-start producer')
    initiator = resolve_initiator(message)
    if initiator == INITIATOR_UNKNOWN:
        return None
    if not isinstance(automation_chain, (tuple, list)):
        raise AutomationError('invalid_task_start_context', 'Automation lineage must be an explicit sequence')
    value = {'tenantId': principal.tenant_id or '',
             'origin': 'human' if initiator == INITIATOR_HUMAN else 'automation',
             'automationChain': list(automation_chain)}
    if project_id is not None:
        value['projectId'] = project_id
    validate_start_context(value)
    return value


def subscription_prefixes(owner: int, context: dict) -> tuple[str, ...]:
    validate_start_context(context)
    prefix = f'{owner}:{hashlib.sha256(context["tenantId"].encode()).hexdigest()[:32]}:task.started:'
    projects = ['*']
    if context.get('projectId'):
        projects.append(hashlib.sha256(context['projectId'].encode()).hexdigest())
    return tuple(f'{prefix}{scope}:{project}:'
                 for scope in ('all', context['origin']) for project in projects)


def task_started_job(owner: int, context: dict, *, attempt_id: str, task_id: str,
                     conversation_id: str, turn_id: str, started_at_ms: int) -> tuple[str, dict]:
    validate_start_context(context)
    if type(started_at_ms) is not int or not 0 < started_at_ms <= LIMITS['safeIntegerMax']//1000*1000:
        raise AutomationError('invalid_task_start_evidence', 'Invalid actual task-start timestamp')
    event_id = 'task-start:' + hashlib.sha256(f'{attempt_id}:{task_id}'.encode()).hexdigest()
    scoped = f'{owner}:{hashlib.sha256(context["tenantId"].encode()).hexdigest()[:32]}:{event_id}'
    job_id = 'automation-event:' + hashlib.sha256(scoped.encode()).hexdigest()
    principal = PrincipalContext.system(subject_id=job_id, owner_user_id=owner,
        tenant_id=context['tenantId'] or None, scopes={'automations:read', 'automations:execute'})
    event = {'id': event_id, 'kind': 'task.started', 'occurredAt': started_at_ms/1000,
             'origin': context['origin'], 'automationChain': deepcopy(context['automationChain']),
             'evidence': {'startedAt': started_at_ms/1000, 'taskId': task_id,
                          'attemptId': attempt_id, 'turnId': turn_id, 'conversationId': conversation_id}}
    if context.get('projectId'):
        event['projectId'] = context['projectId']
    document = {'contract': storage_schema()['$defs']['taskStartedJob']['properties']['contract']['const'],
                'schemaVersion': 1, 'principal': principal.to_payload(), 'event': event,
                'delivery': initial_delivery_cursor()}
    if next(storage_input_validator('taskStartedJob').iter_errors(document), None):
        raise AutomationError('invalid_task_start_evidence', 'Invalid task-start event evidence')
    return job_id, document



def initial_delivery_cursor() -> dict:
    return {'index': 0, 'afterKey': '', 'processed': 0}


def validate_event_job(owner: int, tenant: str, job_id: str, payload: dict) -> tuple[str, ...]:
    """Prove the reserved natural identity and immutable source, including upgrades."""
    if next(storage_input_validator('taskStartedJob').iter_errors(payload), None):
        raise AutomationError('invalid_event_job', 'Invalid event-delivery payload')
    event = payload['event'];evidence = event['evidence']
    context = {'tenantId': tenant, 'origin': event['origin'], 'automationChain': event['automationChain']}
    if 'projectId' in event: context['projectId'] = event['projectId']
    expected_id, expected = task_started_job(owner, context, attempt_id=evidence['attemptId'],
        task_id=evidence['taskId'], conversation_id=evidence['conversationId'], turn_id=evidence['turnId'],
        started_at_ms=round(evidence['startedAt']*1000))
    frozen = {key: value for key, value in payload.items() if key != 'delivery'}
    expected.pop('delivery')
    from .publication import canonical
    if job_id != expected_id or canonical(frozen) != canonical(expected):
        raise AutomationError('invalid_event_job', 'Event delivery escaped its immutable producer identity')
    prefixes = subscription_prefixes(owner, context)
    cursor = payload.get('delivery', initial_delivery_cursor())
    if (cursor['index'] > len(prefixes)
            or (cursor['index'] == len(prefixes) and cursor['afterKey'])
            or (cursor['afterKey'] and not cursor['afterKey'].startswith(prefixes[cursor['index']]))):
        raise AutomationError('invalid_event_cursor', 'Event cursor escaped its indexed scope')
    return prefixes


def validate_delivery_advance(prefixes: tuple[str, ...], current: dict, next_cursor: dict) -> None:
    if next(storage_input_validator('eventDeliveryCursor').iter_errors(next_cursor), None):
        raise AutomationError('invalid_event_cursor', 'Invalid event cursor')
    delta = next_cursor['processed']-current['processed']
    same = (next_cursor['index'] == current['index'] and current['index'] < len(prefixes)
        and next_cursor['afterKey'].startswith(prefixes[current['index']])
        and next_cursor['afterKey'] > current['afterKey'] and delta > 0)
    following = (next_cursor['index'] == current['index']+1 and next_cursor['index'] <= len(prefixes)
        and next_cursor['afterKey'] == '')
    if not (0 <= delta <= storage_schema()['x-event']['batchRows'] and (same or following)):
        raise AutomationError('invalid_event_cursor', 'Event cursor must advance one bounded indexed batch')
