"""Pure occurrence/step transitions, fenced by the existing durable worker.

Entry points: new_run, bind_fence, prepare_step, record_output, record_failure,
request_cancel. These functions return owned documents for repository CAS; no
claim loop, model loop, side effect, or clock is hidden here. A worker claim is
the authority, while the stored fence prevents a stale claim from settling work.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math

from .conditions import check_json_bounds, evaluate_condition, resolve_arguments
from .contract import LIMITS, RUNTIME
from .errors import AutomationConflict, AutomationError
from .registry import ActionContract
from .repository import VersionedEntity


def _now(value: float) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise AutomationError('invalid_timestamp', 'Automation requires a positive finite timestamp')
    return float(value)


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                    sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionFence:
    """Proven canonical worker claim; runtime constructs this after admission."""

    job_id: str
    worker_id: str
    token: int

    def __post_init__(self):
        if (not isinstance(self.job_id, str) or not 1 <= len(self.job_id) <= 200
                or not isinstance(self.worker_id, str) or not 1 <= len(self.worker_id) <= 200
                or type(self.token) is not int or self.token < 1):
            raise AutomationError('invalid_fence', 'Execution requires an explicit durable claim')

    def document(self) -> dict:
        return {'jobId': self.job_id, 'workerId': self.worker_id, 'token': self.token}


def validate_trigger(trigger: dict) -> None:
    """Validate producer evidence before identity computation or persistence."""
    check_json_bounds(trigger)
    required = {'id', 'kind', 'occurredAt', 'origin', 'evidence', 'automationChain'}
    if (not isinstance(trigger, dict) or set(trigger) - (required | {'projectId'})
            or not required <= set(trigger) or not isinstance(trigger['id'], str)
            or not 1 <= len(trigger['id']) <= 256
            or trigger['kind'] not in {'manual', 'schedule', 'task.started', 'skills.updated'}
            or trigger['origin'] not in {'human', 'automation'}
            or not isinstance(trigger['evidence'], dict)):
        raise AutomationError('invalid_trigger', 'Invalid trigger evidence envelope')
    _now(trigger['occurredAt'])
    chain = trigger['automationChain']
    if (not isinstance(chain, list) or len(chain) >= LIMITS['triggerChain']
            or any(not isinstance(item, str) or not 1 <= len(item) <= 160 for item in chain)):
        raise AutomationError('trigger_depth_exceeded', 'Trigger lineage exceeds its bound')


def occurrence_id(definition_id: str, trigger: dict) -> str:
    validate_trigger(trigger)
    return 'ar_' + _digest({'definitionId': definition_id, 'eventKind': trigger['kind'], 'eventId': trigger['id']})


def trigger_digest(trigger: dict) -> str:
    validate_trigger(trigger)
    return _digest(trigger)


def new_run(definition: VersionedEntity, trigger: dict, *, now: float) -> dict:
    """Freeze an occurrence; its identity survives definition edits/redelivery."""
    now = _now(now)
    run_id = occurrence_id(definition.id, trigger)
    chain = trigger['automationChain']
    if definition.id in chain:
        raise AutomationError('recursive_trigger', 'Automation cannot consume its own causal event')
    stored = definition.value
    rule = stored['definition']
    if stored.get('deleted') or (not rule['enabled'] and trigger['kind'] != 'manual'):
        raise AutomationError('automation_disabled', 'Automatic triggers require an enabled rule')
    if rule.get('projectId') and trigger.get('projectId') != rule['projectId']:
        raise AutomationError('trigger_scope_mismatch', 'Trigger belongs to another project')
    expected = rule['trigger']
    if trigger['kind'] != 'manual':
        kind = expected.get('event') if expected['kind'] == 'event' else expected['kind']
        if trigger['kind'] != kind:
            raise AutomationError('trigger_kind_mismatch', 'Trigger does not match this rule')
        if expected['kind'] == 'event' and expected['scope'] not in {'all', trigger['origin']}:
            raise AutomationError('trigger_scope_mismatch', 'Trigger initiator is outside this rule')
    return {
        'contract': RUNTIME['contract'], 'id': run_id, 'definitionId': definition.id,
        'definitionRevision': definition.version, 'definitionDigest': stored['compiled']['digest'],
        'snapshot': deepcopy(rule), 'actions': deepcopy(stored['compiled']['actions']),
        'stepOrder': list(stored['compiled']['stepOrder']), 'trigger': deepcopy(trigger),
        'triggerDigest': trigger_digest(trigger), 'automationChain': [*chain, definition.id],
        'status': 'queued', 'createdAt': now, 'updatedAt': now,
        'deadlineAt': now + rule['policy']['maxRuntimeSeconds'], 'fence': None,
        'steps': {step['id']: {'status': 'pending', 'attempts': 0,
                              'effectKey': f'{run_id}:{step["id"]}'} for step in rule['steps']},
    }


def bind_fence(run: dict, fence: ExecutionFence, *, now: float) -> dict:
    """Bind a proved claim; interrupted external effects become reconciliation."""
    now = _now(now)
    if run['status'] in RUNTIME['terminalRunStates']:
        raise AutomationConflict('A terminal automation cannot acquire an execution fence')
    old = run.get('fence')
    if old:
        if old['jobId'] != fence.job_id or old['token'] > fence.token:
            raise AutomationConflict('Worker fence regressed or changed job identity')
        if old['token'] == fence.token and old['workerId'] != fence.worker_id:
            raise AutomationConflict('One fence cannot belong to two workers')
    result = deepcopy(run)
    result['fence'] = fence.document()
    result['updatedAt'] = now
    if result['status'] not in {'cancelling', 'waiting_permission', 'waiting_agent', 'reconciliation_required'}:
        result['status'] = 'running'
    steps = {step['id']: step for step in result['snapshot']['steps']}
    for step_id, state in result['steps'].items():
        if state['status'] == 'dispatching' and old != fence.document():
            action = result['actions'][steps[step_id]['action']]
            state['status'] = ('pending' if action['recovery'] == 'replay' and result['status'] != 'cancelling'
                               else 'reconciliation_required')
            if state['status'] == 'reconciliation_required' and result['status'] != 'cancelling':
                result['status'] = 'reconciliation_required'
    return result


def _owned(run: dict, fence: ExecutionFence, now: float) -> dict:
    now = _now(now)
    if run.get('fence') != fence.document():
        raise AutomationConflict('A stale worker cannot change automation state')
    if run['status'] in RUNTIME['terminalRunStates']:
        raise AutomationConflict('Automation is already terminal')
    result = deepcopy(run)
    result['updatedAt'] = now
    return result


def prepare_step(run: dict, fence: ExecutionFence, *, now: float) -> tuple[dict, str | None]:
    """Persist resolved arguments and a stable effect identity before invocation."""
    result = _owned(run, fence, now)
    if now >= result['deadlineAt']:
        result = request_cancel(result, reason='deadline_exceeded', now=now)
        return result, None
    if result['status'] in {'waiting_permission', 'cancelling', 'reconciliation_required'}:
        return result, None
    evidence = {'event': result['trigger']['evidence'], 'steps': {
        key: state['output'] for key, state in result['steps'].items()
        if state['status'] == 'succeeded' and 'output' in state}}
    definitions = {step['id']: step for step in result['snapshot']['steps']}
    for step_id in result['stepOrder']:
        state = result['steps'][step_id]
        if state['status'] in {'waiting_agent', 'dispatching'}:
            return result, None
        if state['status'] != 'pending':
            continue
        step = definitions[step_id]
        parents = [result['steps'][key]['status'] for key in step['after']]
        if any(status not in {'succeeded', 'skipped'} for status in parents):
            return result, None
        if 'skipped' in parents or not evaluate_condition(step.get('when', True), evidence):
            state.update(status='skipped', reason='upstream_skipped' if 'skipped' in parents else 'condition_false')
            continue
        arguments = resolve_arguments(step['arguments'], evidence)
        contract = ActionContract.from_document(result['actions'][step['action']])
        if (contract.effect in {'write', 'agent'}
                and result['snapshot']['policy']['permissionMode'] == 'manual'
                and 'approval' not in state):
            state.update(status='waiting_permission', arguments=arguments)
            result['status'] = 'waiting_permission'
            return result, None
        if 'approval' in state:
            from .approval import validate_approved_dispatch
            validate_approved_dispatch(state, contract.document(), arguments)
        if state['attempts'] >= result['snapshot']['policy'].get('maxStepAttempts', 3):
            state.update(status='failed', error={'code': 'step_attempts_exhausted', 'message': 'Step retry budget exhausted'})
            result.update(status='failed', completedAt=now)
            return result, None
        contract.validate(arguments)
        state.update(status='dispatching', arguments=arguments, attempts=state['attempts'] + 1,
                     dispatchedAt=now)
        result['status'] = 'running'
        return result, step_id
    if all(state['status'] in {'succeeded', 'skipped'} for state in result['steps'].values()):
        result.update(status='succeeded', completedAt=now)
    return result, None


def record_output(run: dict, step_id: str, output: dict, fence: ExecutionFence, *, now: float) -> dict:
    result = _owned(run, fence, now)
    check_json_bounds(output)
    state = result['steps'][step_id]
    if state['status'] not in RUNTIME['activeEffectStepStates']:
        raise AutomationConflict('Step has no live effect intent')
    step = next(step for step in result['snapshot']['steps'] if step['id'] == step_id)
    contract = ActionContract.from_document(result['actions'][step['action']])
    contract.validate(output, output=True)
    cancelling = result['status'] == 'cancelling'
    state['output'] = deepcopy(output)
    if contract.effect == 'agent' and output.get('status') in {'queued', 'running'}:
        state['status'] = 'waiting_agent'
        if result['status'] != 'cancelling':
            result['status'] = 'waiting_agent'
    elif contract.effect == 'agent' and output.get('status') in {'failed', 'cancelled'}:
        state.update(status='failed', error={'code': 'agent_' + output['status'], 'message': 'Agent did not complete'})
        result.update(status='cancelled' if result['status'] == 'cancelling' else 'failed', completedAt=now)
    elif step['action'] == 'result.verify' and output.get('passed') is not True:
        state.update(status='failed', error={'code': 'verification_failed', 'message': 'Required checks failed'})
        result.update(status='failed', completedAt=now)
    else:
        state.update(status='succeeded', completedAt=now)
        if result['status'] == 'cancelling':
            result.update(status='cancelled', completedAt=now)
        else:
            result['status'] = 'running'
    if cancelling:
        _settle_cancellation(result, now)
    return result


def _settle_cancellation(result: dict, now: float) -> None:
    active = any(step['status'] in RUNTIME['activeEffectStepStates']
                 for step in result['steps'].values())
    result['status'] = 'cancelling' if active else 'cancelled'
    if active:
        result.pop('completedAt', None)
    else:
        result['completedAt'] = now


def record_failure(run: dict, step_id: str, error: dict, fence: ExecutionFence,
                   *, now: float, uncertain: bool = False, retryable: bool = False) -> dict:
    result = _owned(run, fence, now)
    if (not isinstance(error, dict) or set(error) != {'code', 'message'}
            or any(not isinstance(value, str) or not 1 <= len(value) <= 4000 for value in error.values())):
        raise AutomationError('invalid_action_error', 'Errors require a bounded code and message')
    state = result['steps'][step_id]
    if state['status'] not in RUNTIME['activeEffectStepStates']:
        raise AutomationConflict('A pending or terminal step cannot settle an effect failure')
    state['error'] = deepcopy(error)
    step = next(step for step in result['snapshot']['steps'] if step['id'] == step_id)
    contract = result['actions'][step['action']]
    if (retryable and contract['recovery'] == 'replay' and result['status'] != 'cancelling'
            and state['attempts'] < result['snapshot']['policy'].get('maxStepAttempts', 3)):
        state['status'] = 'pending'
        result['status'] = 'running'
        return result
    state['status'] = 'reconciliation_required' if uncertain else 'failed'
    if result['status'] == 'cancelling':
        _settle_cancellation(result, now)
    else:
        result['status'] = state['status']
    if result['status'] in RUNTIME['terminalRunStates']:
        result['completedAt'] = now
    return result


def request_cancel(run: dict, *, reason: str, now: float) -> dict:
    _now(now)
    if not isinstance(reason, str) or not 1 <= len(reason) <= 200:
        raise AutomationError('invalid_cancel_reason', 'Cancellation requires a bounded reason')
    if run['status'] in RUNTIME['terminalRunStates'] or run.get('cancelCommand'):
        return deepcopy(run)
    result = deepcopy(run)
    result.update(updatedAt=now, cancelReason=reason)
    active = any(state['status'] in RUNTIME['activeEffectStepStates']
                 for state in result['steps'].values())
    result['status'] = 'cancelling' if active else 'cancelled'
    for state in result['steps'].values():
        if state['status'] in RUNTIME['cancellablePendingStepStates']:
            state['status'] = 'cancelled'
    if not active:
        result['completedAt'] = now
    return result
