"""Publish a settled Goal worker's model view through canonical lane recovery.

Turn rows remain the authority for visible history and edits. This bounded,
replace-in-place checkpoint reuses ModelContinuationStore's ownership and
monotonic settled frontier; it never revives an old attempt or repeats a tool.
"""
from __future__ import annotations

from copy import deepcopy
from lib.goal_runs.contract import GOAL_WORKER_STATE_KEYS


def publish_worker_context(task):
    snapshot = task.get('_goalWorkerCheckpoint')
    if not snapshot or not task.get('_attemptId') or not task.get('convId'):
        return False
    from lib.storage.model_continuation import ModelContinuationStore, settled_witness, encode_projection
    from lib.tasks_pkg.model_replay import policy_witness, portable_policy_witness, effort_policy_witness
    owner, conversation = task['_userId'], task['convId']
    ids = task.get('_turnVisibleRunTurnIds') or [task['_turnId']]
    target_id = ids[-1]
    identity = (owner, conversation, task['_attemptId'], target_id)
    publication = task.get('_goalWorkerPublication')
    if not publication or publication['snapshot'] is not snapshot:
        # Bind before I/O. A retry can never relabel an old snapshot with a
        # later visible phase or refresh a failed write's authority witness.
        config = task.get('config') or {}
        publication = {'snapshot': snapshot, 'identity': identity, 'projection': {
            'policy': policy_witness(config), 'portablePolicy': portable_policy_witness(config),
            'effortPolicy': effort_policy_witness(config),
            'messages': snapshot['messages'], 'goalState': snapshot['task_state'],
        }}
        task['_goalWorkerPublication'] = publication

    def retire(status):
        task['_goalContextRecoveryStatus'] = status
        if task.get('_goalWorkerCheckpoint') is snapshot:
            task.pop('_goalWorkerCheckpoint', None)
        if task.get('_goalWorkerPublication') is publication:
            task.pop('_goalWorkerPublication', None)

    if publication['identity'] != identity:
        retire('authority_changed')
        return False
    store = ModelContinuationStore()
    candidate = publication['projection']
    if 'turns' not in candidate:
        candidate = dict(candidate)
        context = store.read_context(user_id=owner, conversation_id=conversation,
                                     turn_id=target_id, replay=False)
        if not isinstance(context, dict) or not context.get('supported'):
            retire('unsupported')
            return False
        rows = context.get('turns') or []
        if not rows or rows[-1].get('turnId') != target_id or rows[-1].get('status') != 'completed':
            retire('authority_changed')
            return False
        candidate['turns'] = [settled_witness(row) for row in rows]
        from lib.tasks_pkg.message_semantics import has_model_media
        if context.get('proofEligible') and not any(has_model_media(m) for m in snapshot['messages']):
            candidate['historyProof'] = context['historyProof']
            candidate['turns'] = candidate['turns'][-1:]
        publication['lane_id'] = rows[-1].get('laneId') or 'main'
        publication['projection'] = candidate
    if encode_projection(candidate) is None:
        retire('over_budget')
        return False
    accepted = store.put(user_id=owner, conversation_id=conversation,
                         lane_id=publication['lane_id'], projection=candidate)
    retire('stored' if accepted else 'authority_changed')
    return accepted


def restore_worker_state(config):
    """Only the validated model-prefix reader may supply a saved checklist."""
    state = config.get('_goalWorkerState') or {}
    return {key: deepcopy(state[key]) for key in GOAL_WORKER_STATE_KEYS if key in state}
