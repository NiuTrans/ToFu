"""Prepare owned Research action continuation before task registration.

Entries: prepare_action_continuation/resume_action. Preparation restores original
policy/workspace/tool epoch and transfers a proven-dead claim. Explicit resume
registers the original task and performs admission inside one started worker.
The typed context is passed directly to run_research_action, never from HTTP JSON.
Dependencies: Research journals/workspace and the shared Paper loop policy.
"""
from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from copy import deepcopy
from typing import TYPE_CHECKING

from lib.identity import require_user_id
from lib.storage.errors import StorageError


if TYPE_CHECKING:
    from .execution_owner import ExecutionClaim
    from lib.paper.agent_usage import PaperAgentUsageMeter
    from lib.paper.tools import PaperToolEpochV2


@dataclass(frozen=True)
class ActionContinuation:
    task_id: str
    user_id: int
    ordinal: int
    claim: ExecutionClaim
    checkpoint_digest: str
    model_name: str
    meter: PaperAgentUsageMeter
    definition: dict
    epoch: PaperToolEpochV2
    bindings: list
    binding_problems: list

    def task_fields(self):
        return action_task_fields(self.definition)

    def validate_task(self, task):
        fields = self.task_fields()
        if (task.get('task_id') != self.task_id or task.get('user_id') != self.user_id
                or any(task.get(key) != value for key, value in fields.items() if key != 'tool_rounds')):
            raise StorageError('database_conflict', 'Research continuation changed its original task policy')
        self.claim.assert_current()


def prepare_action_continuation(task_id, *, user_id, checkpoint_ordinal):
    """Validate original intent and current workspace before ownership transfer."""
    from lib.storage import get_storage_client
    from lib.paper.agent_loop_policy import paper_agent_checkpoint_limits
    from .request_context import RequestContexts
    from .model_accounting import ModelCalls
    from .execution_owner import ActionExecutionOwners
    from .workspace import load_workspace, validate_selection_source
    from .action_observations import action_observation_basis
    from .action import build_action_tool_epoch, require_experiment_decision_rule

    owner = require_user_id(user_id, context='research continuation owner')
    client = get_storage_client(write=True)
    identity = {'user_id': owner, 'task_id': task_id}
    contexts = RequestContexts.open(client, **identity)
    request = contexts.load_request(checkpoint_ordinal) if contexts else None
    definition = request.get('action_definition') if request else None
    if definition is None:
        raise StorageError('database_conflict', 'Research continuation original definition is unavailable')
    workspace = load_workspace(definition['direction'], definition['lang'], user_id=owner)
    if workspace['revision'] != definition['expected_revision']:
        raise StorageError('database_conflict', 'Research continuation workspace revision changed')
    if definition['action'] == 'experiment':
        validate_selection_source(workspace, user_id=owner)
        require_experiment_decision_rule(workspace)
    owners = ActionExecutionOwners.open(client, **identity)
    if owners is None or owners.load_basis() != action_observation_basis(workspace):
        raise StorageError('database_conflict', 'Research continuation scientific basis changed or is missing')
    calls = ModelCalls.open(client, **identity)
    intent = calls.load_model_intent(checkpoint_ordinal)
    epoch, bindings, problems = build_action_tool_epoch(
        action=definition['action'], user_id=owner, workspace=workspace,
        confirm_external_writes=definition['confirm_external_writes'], model=intent['requested_model'])
    claim, material, meter, original = owners.recover_checkpoint(checkpoint_ordinal,
        tool_epoch_hash=epoch.epoch_hash, expected_limits=paper_agent_checkpoint_limits())
    if original != definition:
        raise StorageError('database_integrity', 'Research continuation original definition changed')
    del material  # Admission retains scalar identity, not another full private checkpoint.
    return ActionContinuation(task_id, owner, checkpoint_ordinal, claim,
        claim.value['recovery']['material_digest'], intent['requested_model'], meter,
        original, epoch, bindings, problems)


def action_task_fields(definition):
    fields = deepcopy(definition)
    fields['model'] = fields.pop('model_override')
    fields.pop('tool_epoch_hash')
    fields['tool_rounds'] = []
    return fields


def resume_action(task_id, *, user_id):
    """Re-register an owned task and admit continuation inside its actual worker.

    A scheduler rejection precedes ownership transfer. Admission refusal removes
    only the pending carrier and publishes no invented terminal settlement. All
    same-process duplicate requests wait on the same admitted worker's future.
    """
    from lib.storage import get_storage_client
    from .model_accounting import ModelCalls
    from .request_context import RequestContexts
    from .action_runtime import _production, _research_action_runtime
    from .action import run_research_action
    owner = require_user_id(user_id, context='research action resume owner')
    existing = _research_action_runtime.get_owned(task_id, user_id=owner)
    if existing is not None:
        pending = existing.get('_researchResumeAdmission')
        if pending is not None:
            result = pending.result()
            return dict(result, deduped=True)
        if existing['status'] in ('pending', 'running'):
            return {'task_id': task_id, 'resumed': False, 'deduped': True}
        raise StorageError('database_conflict', 'Terminal Research action cannot resume')
    client = get_storage_client(write=True)
    identity = {'user_id': owner, 'task_id': task_id}
    calls = ModelCalls.open(client, **identity)
    if calls is None:
        raise LookupError('Research action not found')
    ordinal = calls.latest_model_ordinal()
    if not ordinal:
        raise StorageError('database_conflict', 'Research action has no admitted checkpoint')
    request = RequestContexts.open(client, **identity).load_request(ordinal)
    definition = request.get('action_definition') if request else None
    if definition is None:
        raise StorageError('database_conflict', 'Research continuation original definition is unavailable')
    admitted = Future()
    task, duplicate = _production.claim_restored_task(task_id, user_id=owner,
        meta={'direction':definition['direction'], 'lang':definition['lang'], 'action':definition['action']},
        fields=dict(action_task_fields(definition), _researchResumeAdmission=admitted))
    if duplicate:
        pending = task.get('_researchResumeAdmission')
        if pending is not None:
            return dict(pending.result(), deduped=True)
        if task['status'] in ('pending', 'running'):
            return {'task_id':task_id, 'resumed':False, 'deduped':True}
        raise StorageError('database_conflict', 'Terminal Research action cannot resume')

    def worker():
        try:
            if task['abort_event'].is_set():
                raise StorageError('database_conflict', 'Research continuation was cancelled before admission')
            continuation = prepare_action_continuation(task_id, user_id=owner, checkpoint_ordinal=ordinal)
        except Exception as exc:
            _research_action_runtime.remove_owned(task_id, user_id=owner)
            admitted.set_exception(exc)
            return
        # A stop arriving during preparation retains acknowledged cumulative usage.
        _research_action_runtime.update_fields(task_id, fields={
            '_researchUsageSnapshot':continuation.meter.snapshot()})
        admitted.set_result({'task_id':task_id, 'resumed':True, 'deduped':False})
        run_research_action(task, continuation=continuation)
    try:
        _production.submit_detached(task_id, worker)
    except Exception as exc:
        if admitted.done():
            return admitted.result()
        _research_action_runtime.remove_owned(task_id, user_id=owner)
        admitted.set_exception(exc)
        raise
    return admitted.result()
