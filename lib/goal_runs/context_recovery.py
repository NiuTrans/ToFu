"""Publish a settled Goal worker's model view through canonical lane recovery.

Turn rows remain the authority for visible history and edits. This bounded,
replace-in-place checkpoint reuses ModelContinuationStore's ownership and
monotonic settled frontier; it never revives an old attempt or repeats a tool.
"""
from __future__ import annotations

from copy import deepcopy
from lib.goal_runs.contract import GOAL_WORKER_STATE_KEYS


def publish_worker_context(task):
    snapshot = task.pop('_goalWorkerCheckpoint', None)
    if not snapshot or not task.get('_attemptId') or not task.get('convId'):
        return False
    from lib.storage.model_continuation import ModelContinuationStore, settled_witness, encode_projection
    from lib.tasks_pkg.model_replay import policy_witness, portable_policy_witness, effort_policy_witness
    owner, conversation = task['_userId'], task['convId']
    ids = task.get('_turnVisibleRunTurnIds') or [task['_turnId']]
    target_id = ids[-1]
    store = ModelContinuationStore()
    context = store.read_context(user_id=owner, conversation_id=conversation,
                                 turn_id=target_id, replay=False)
    if not isinstance(context, dict) or not context.get('supported'):
        return False
    rows = context.get('turns') or []
    if not rows or rows[-1].get('turnId') != target_id or rows[-1].get('status') != 'completed':
        return False
    config = task.get('config') or {}
    candidate = {
        'policy': policy_witness(config), 'portablePolicy': portable_policy_witness(config),
        'effortPolicy': effort_policy_witness(config),
        'turns': [settled_witness(row) for row in rows],
        'messages': snapshot['messages'], 'goalState': snapshot['task_state'],
    }
    from lib.tasks_pkg.message_semantics import has_model_media
    if context.get('proofEligible') and not any(has_model_media(m) for m in snapshot['messages']):
        candidate['historyProof'] = context['historyProof']
        candidate['turns'] = candidate['turns'][-1:]
    if encode_projection(candidate) is None:
        task['_goalContextRecoveryStatus'] = 'over_budget'
        return False
    accepted = store.put(user_id=owner, conversation_id=conversation,
                         lane_id=rows[-1].get('laneId') or 'main', projection=candidate)
    task['_goalContextRecoveryStatus'] = 'stored' if accepted else 'authority_changed'
    return accepted


def restore_worker_state(config):
    """Only the validated model-prefix reader may supply a saved checklist."""
    state = config.get('_goalWorkerState') or {}
    return {key: deepcopy(state[key]) for key in GOAL_WORKER_STATE_KEYS if key in state}
