"""Pending stage dispatch evidence. Runs own the CAS and task-engine submission.

A confirmed task birth and committed binding do not establish dispatch. Only a
live pending carrier in its recorded process may resume the bound handoff.
Pending cold restoration has a separate proof port; runs still own every CAS.
No hooks, worker, loop or independent store lives here.
"""
from .contracts import EvolutionStateError


def confirm_birth(task, user_id, *, value=None, deadline_seconds=0.5):
    if value is None:
        from lib.storage import get_storage_client
        value = get_storage_client().query('task_results.replay_get',
            {'key': task['id'], 'user_id': user_id}, deadline=deadline_seconds)
    if (not value or value.get('task_id') != task['id']
            or value.get('user_id') != int(user_id) or value.get('status') != 'pending'
            or value.get('conv_id') != '' or type(value.get('version')) is not int
            or value['version'] < 1):
        raise EvolutionStateError('stage birth checkpoint was not confirmed; dispatch refused')
    return value


def bound_marker(run, task, birth):
    return {'task_id': task['id'], 'intent_id': run['stage_launch']['intent_id'],
            'birth_version': birth['version'], 'state': 'bound'}


def validate_bound(run, task, user_id, *, birth=None):
    from .review_recovery import process_owner
    marker = run.get('stage_dispatch') or {}
    binding = (task.get('config') or {}).get('evolution') or {}
    if (run.get('domain') == 'research' or run['status'] not in ('executing', 'reviewing')
            or marker.get('state') != 'bound' or marker.get('task_id') != run['task_id']
            or marker.get('intent_id') != (run.get('stage_launch') or {}).get('intent_id')
            or task.get('id') != run['task_id'] or task.get('_userId') != int(user_id)
            or task.get('status') != 'pending' or task.get('_workerStartedAt')
            or binding.get('run_id') != run['run_id'] or binding.get('stage') != run['status']):
        raise EvolutionStateError('stage dispatch refused: original pending carrier or binding changed')
    if birth is None:
        # An authenticated durable row is not a resident execution session.
        # Resume only the same live carrier; never manufacture a replacement.
        owner = process_owner()
        if not owner or run.get('stage_owner') != owner:
            raise EvolutionStateError('stage dispatch refused: original process is unavailable')
    birth = confirm_birth(task, user_id, value=birth)
    if birth['version'] != marker.get('birth_version'):
        raise EvolutionStateError('stage dispatch refused: pending birth version changed')
