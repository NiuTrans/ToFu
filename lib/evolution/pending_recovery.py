"""Cold restoration of one provably unsubmitted generic stage.

The original experiment and absolute budget survive. This policy never restores
an admitted executor, reruns hooks, creates episodes or submits a task.
Each replacement must independently prove its own unsubmitted birth.
"""
import hashlib
import json
import time

from .contracts import EvolutionStateError


def prepare_replacement(run, *, user_id):
    from .review_recovery import owner_state, process_owner
    marker = run.get('stage_dispatch') or {}
    owner = process_owner()
    if (run.get('domain') == 'research' or run['status'] not in ('executing', 'reviewing')
            or marker.get('state') != 'bound' or marker.get('task_id') != run['task_id']
            or marker.get('intent_id') != (run.get('stage_launch') or {}).get('intent_id')
            or not owner or owner_state(run.get('stage_owner')) != 'dead'):
        raise EvolutionStateError('cold pending stage restoration is not authorized')
    from .stage_inputs import read_input
    value = read_input(run, user_id=user_id)
    if value['task_id'] != run['task_id']:
        raise EvolutionStateError('prepared input does not name the original carrier')
    from .episodes import get_episode
    episode = get_episode(run['episode_id'], user_id=user_id)
    voided_chain=[]
    for receipt in reversed(run['receipts']):
        if not receipt.get('submission_voided'):break
        voided_chain.append(receipt)
    voided_chain.reverse()
    if marker.get('replaces_task_id'):
        if (not voided_chain or voided_chain[-1]['task_id']!=marker['replaces_task_id']
                or len({row['task_id'] for row in voided_chain})!=len(voided_chain)
                or run['task_id'] in {row['task_id'] for row in voided_chain}
                or any(row['stage']!=run['status'] or row.get('actuals_complete') is not True
                       or row['actuals']['tokens'] or row['actuals']['tool_calls']
                       or row.get('terminal_verdict') or row.get('outcome_available')
                       for row in voided_chain)):
            raise EvolutionStateError('pending restoration lineage is incomplete or changed')
    elif voided_chain:
        raise EvolutionStateError('pending restoration lineage has no replacement binding')
    episode_task_id=voided_chain[0]['task_id'] if voided_chain else run['task_id']
    if (not episode or episode['status'] != 'running'
            or episode['goal_id'] != run['goal_id'] or episode['plan_id'] != run['plan_id']
            or (run['status'] == 'executing' and episode['task_id'] != episode_task_id)):
        raise EvolutionStateError('original pending stage episode is no longer running')
    if run['status'] == 'reviewing':
        from .review_recovery import validate_frozen_review
        validate_frozen_review(run, user_id)
    from lib.tasks_pkg.manager.prepared import read_interrupted_birth
    try:
        read_interrupted_birth(run['task_id'], user_id=user_id,
                              birth_version=marker['birth_version'])
    except ValueError as exc:
        raise EvolutionStateError(str(exc)) from exc
    identity = json.dumps([run['run_id'], run['task_id'], marker['prepared_input']['sha256'], owner],
                          sort_keys=True, separators=(',', ':'))
    replacement_id = 'prepared-' + hashlib.sha256(identity.encode()).hexdigest()[:48]
    voided = {'task_id': run['task_id'], 'stage': run['status'],
              'submission_voided': True, 'actuals_complete': True,
              'actuals': {'tokens': 0, 'tool_calls': 0,
                         'wall_clock_seconds': max(0, int(time.time() - value['created_at']))}}
    return value, replacement_id, owner, voided
