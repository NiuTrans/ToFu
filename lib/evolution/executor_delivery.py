"""Explicit executor answer through the shared tool-terminal reply port.

The owner-bound frozen stage launch authorizes the live reply. Preparation is
not acceptance or persistence: ordinary terminal checkpoints, command audits,
source capture and independent review remain the only settlement path.
"""
from __future__ import annotations

from copy import deepcopy
from lib.storage.errors import StorageError
from lib.tasks_pkg.tool_delivery import arguments_digest, bind_delivery, require_current_sole_call
from .contracts import EvolutionContractError, EvolutionStateError
from .coordination import authorize_task

NAME = 'evolution_coordinate'
_BINDING_FIELDS = ('run_id', 'task_id', 'stage_launch', 'stage_dispatch', 'attempt', 'episode_id',
    'objective', 'acceptance_requirements', 'context_seed', 'exploration_hypothesis',
    'scientific_protocol', 'scientific_required_outcome', 'require_command_execution',
    'require_requirement_originals', 'budget', 'absolute_deadline_at')


def _current(task):
    from . import stage_launch
    run, role, _ = authorize_task(task)
    if role != 'executing':
        raise EvolutionStateError('result submission requires the current executor')
    dispatch = run.get('stage_dispatch') or {}
    if (dispatch.get('state') != 'admitted'
            or dispatch.get('task_id') != task['id']
            or dispatch.get('intent_id') != (run.get('stage_launch') or {}).get('intent_id')):
        raise EvolutionStateError('result submission requires its matching admitted dispatch')
    launch = stage_launch.read_launch(run.get('stage_launch'), run_id=run['run_id'], user_id=task['_userId'])
    if not launch or launch['stage'] != 'executing':
        raise EvolutionStateError('result submission requires a frozen executing launch')
    return run


def prepare(task, arguments, *, call_id, round_num):
    from .library import _validate
    _validate(arguments, 'goal_coordination_tool')
    if arguments.get('action') != 'submit_result':
        raise EvolutionContractError('result submission requires submit_result')
    content = arguments['content'].strip()
    if not content:
        raise EvolutionContractError('complete result content is required')
    require_current_sole_call(task, call_id, NAME, arguments)
    run = _current(task)
    binding = {key: deepcopy(run.get(key)) for key in _BINDING_FIELDS}

    def verify():
        try:
            current = _current(task)
            return {key: current.get(key) for key in _BINDING_FIELDS} == binding
        except (EvolutionContractError, EvolutionStateError, StorageError, OSError, ValueError, TypeError):
            return False

    acknowledgement = 'executor_submission:' + arguments_digest(arguments)
    bind_delivery(task, call_id=call_id, name=NAME, round_num=round_num,
        arguments=arguments, content=content, acknowledgement=acknowledgement, verify=verify)
    return {'run_id': run['run_id'], 'task_id': task['id'],
            'acknowledgement': acknowledgement, 'acceptance': 'pending independent review'}
