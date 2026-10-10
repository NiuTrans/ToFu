"""Owned approval proofs and immutable dispatch authorization.

Entry points: request_digest, prepare_approval, validate_approval_progress.
The storage command owns revision/queue atomicity; these pure transitions bind
one reviewed step's frozen arguments and action contract to its approving actor.
"""
from copy import deepcopy
import hashlib
import json

from lib.identity import PrincipalContext
from .errors import AutomationConflict, AutomationError
from .registry import ActionContract
from .conditions import check_json_bounds
from .contract import LIMITS, RUNTIME


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def request_digest(payload):
    return digest({key: value for key, value in payload.items() if key != 'now_ms'})


def validate_approved_dispatch(state, action, arguments):
    grant = state.get('approval')
    if (not isinstance(grant, dict) or grant.get('argumentsDigest') != digest(arguments)
            or grant.get('actionDigest') != digest(action)
            or grant.get('effectKey') != state.get('effectKey')):
        raise AutomationError('approval_evidence_changed', 'Approved action or arguments changed')


def prepare_approval(run, payload):
    check_json_bounds(payload['actor_principal'], max_depth=LIMITS['recordDepth'],
                      max_nodes=LIMITS['recordNodes'])
    if len(canonical(payload['actor_principal']).encode()) > LIMITS['recordBytes']:
        raise AutomationError('invalid_approval_actor', 'Approval actor exceeds the record budget')
    actor = PrincipalContext.from_payload(payload['actor_principal'])
    if (actor.to_payload() != payload['actor_principal'] or 'admin' in actor.scopes
            or actor.owner_user_id != run['ownerId'] or actor.tenant_id != run['tenantId']):
        raise AutomationError('invalid_approval_actor', 'Approval actor escaped its owner or canonical grant')
    for scope in ('automations:read', 'automations:manage', 'automations:execute'):
        actor.require_scope(scope)
    step_id = payload['step_id']
    step = next((item for item in run['snapshot']['steps'] if item['id'] == step_id), None)
    state = run['steps'].get(step_id)
    if step is None or state is None:
        raise AutomationConflict('Approval step does not exist')
    action = ActionContract.from_document(run['actions'][step['action']])
    for scope in action.required_scopes:
        actor.require_scope(scope)
    if (run['status'] != 'waiting_permission' or state['status'] != 'waiting_permission'
            or run['snapshot']['policy']['permissionMode'] != 'manual'
            or action.effect not in {'write', 'agent'} or state.get('approval')
            or run.get('cancelCommand') or payload['now_ms'] / 1000 >= run['deadlineAt']):
        raise AutomationConflict('Approval requires a current unexpired permission wait')
    arguments = state.get('arguments')
    if not isinstance(arguments, dict):
        raise AutomationConflict('Approval requires concrete review arguments')
    if (canonical(json.loads(payload['arguments_json'])) != canonical(arguments)
            or canonical(json.loads(payload['action_json'])) != canonical(action.document())):
        raise AutomationConflict('Reviewed arguments or action changed')
    result = deepcopy(run)
    approved = result['steps'][step_id]
    approved['approval'] = {
        'requestId': payload['request_id'], 'requestDigest': request_digest(payload),
        'actorSubjectId': actor.subject_id, 'approvedAt': payload['now_ms'] / 1000,
        'argumentsDigest': hashlib.sha256(payload['arguments_json'].encode()).hexdigest(),
        'actionDigest': hashlib.sha256(payload['action_json'].encode()).hexdigest(),
        'effectKey': state['effectKey'],
    }
    approved['status'] = 'pending'
    result['status'] = 'running'
    result['updatedAt'] = payload['now_ms'] / 1000
    check_json_bounds(result, max_depth=LIMITS['recordDepth'], max_nodes=LIMITS['recordNodes'])
    if len(canonical(result).encode()) > LIMITS['recordBytes']:
        raise AutomationError('automation_record_too_large', 'Approval evidence exceeds the run budget')
    return result


def validate_approval_progress(current, updated):
    if set(current['steps']) != set(updated['steps']):
        raise AutomationError('step_identity_changed', 'Worker cannot add or remove frozen steps')
    actions = {step['id']: current['actions'][step['action']] for step in current['snapshot']['steps']}
    for step_id, previous in current['steps'].items():
        state = updated['steps'].get(step_id, {})
        action = actions[step_id]
        if (action['effect'] in {'write', 'agent'}
                and previous['status'] in {*RUNTIME['activeEffectStepStates'], 'succeeded'}
                and state.get('status') in {'pending', 'waiting_permission', 'skipped'}):
            raise AutomationError('effect_intent_regressed', 'External intent cannot become dispatchable again')
        if previous.get('effectKey') != state.get('effectKey'):
            raise AutomationError('effect_identity_changed', 'Automation effect identity changed')
        if previous.get('approval') != state.get('approval'):
            raise AutomationError('approval_evidence_changed', 'Only owner approval can authorize a step')
        if previous.get('approval'):
            validate_approved_dispatch(state, action, state.get('arguments'))
    if current['snapshot']['policy']['permissionMode'] != 'manual':
        return
    for step in current['snapshot']['steps']:
        state = updated['steps'].get(step['id'], {})
        action = current['actions'][step['action']]
        if (action['effect'] in {'write', 'agent'}
                and state.get('status') in {'dispatching', 'waiting_agent', 'reconciliation_required', 'succeeded'}
                and not state.get('approval')):
            raise AutomationError('approval_required', 'Manual external effects require owned approval')
