"""Human Chat adapter for existing programme, run and coordination owners.

Entry points: available, execute. Uses canonical task identity and Turn reads;
never creates a conversation, model loop, scheduler, transcript or status store.
"""
from copy import deepcopy
import hashlib
import json

from .contracts import EvolutionStateError, validate_program
from lib.tools.exploration import available


def _scope(task):
    from lib.identity import PrincipalContext, require_user_id
    from lib.tasks_pkg.invocation import is_child_invocation
    from lib.turn_lifecycle import get_turn, get_attempt
    from lib.turn_initiation import require_human_turn
    config = task.get('config') or {}
    owner = require_user_id(task.get('_userId'), context='Chat exploration owner')
    conversation = task.get('convId')
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    if (not available(config, conversation_id=conversation, owner=owner)
            or principal.kind != 'user' or principal.require_owner() != owner
            or task.get('_attended') is not True or is_child_invocation(task)
            or task.get('_parentTaskId') or task.get('_transientRuntime')
            or task.get('status') not in ('pending', 'running') or task.get('aborted')
            or (task.get('abort_event') is not None and task['abort_event'].is_set())):
        raise PermissionError('Exploration controls require the current human Chat turn')
    turn = get_turn(conversation, task.get('_turnId'), user_id=owner)
    attempt = get_attempt(task.get('_attemptId'), user_id=owner)
    if (not turn or turn.get('actor') != 'assistant' or turn.get('conversationId') != conversation
            or turn.get('turnId') != task.get('_turnId') or not attempt
            or turn.get('currentAttemptId') != task.get('_attemptId')
            or attempt.get('status') not in ('pending', 'running')
            or attempt.get('taskId') != task.get('id') or attempt.get('attemptId') != task.get('_attemptId')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != conversation):
        raise PermissionError('Chat exploration source attempt is stale or unavailable')
    human = get_turn(conversation, turn.get('parentTurnId'), user_id=owner)
    require_human_turn(human)
    if human.get('conversationId') != conversation or human.get('turnId') != turn.get('parentTurnId'):
        raise PermissionError('Chat exploration source belongs to another conversation')
    return owner, {'conversation_id': conversation, 'turn_id': human['turnId']}


def _view(program_id, *, owner):
    from .programs import read_versioned_program
    from .runs import get_run_snapshot
    program, version = read_versioned_program(program_id, user_id=owner)
    current = program['current']
    run = get_run_snapshot(current['run_id'], user_id=owner) if current else None
    return {'program_id': program['program_id'], 'state_version': version, 'detail_path': '#/evolution/programs/' + program['program_id'],
        'status': program['status'], 'objective': program['objective'], 'acceptance': program['acceptance'],
        'chat_source': program.get('chat_source'), 'advance_mode': program.get('advance_mode', 'continuous'),
        'settled_experiments': program['settled_through'], 'pending_steps': len(program['queue']),
        'next_steps': [{key: step[key] for key in ('kind', 'title')} for step in program['queue'][:6]],
        'steps_have_more': len(program['queue']) > 6,
        'current_experiment': ({'run_id': current['run_id'], 'title': current['step']['title'],
            'status': run.get('status') if run else 'unknown'} if current else None),
        'skill_bindings': program.get('execution_options', {}).get('skill_bindings', []),
        'actuals': program['actuals'], 'actuals_complete': program.get('actuals_complete', True),
        'error': program['error'],
        'execution_started': program['started_at'] is not None if 'started_at' in program else None}


def execute(task, name, args):
    from . import programs
    validate_program(args, 'chat_read' if name == 'get_exploration' else 'chat_write')
    owner, source = _scope(task)
    if name == 'get_exploration':
        if args.get('view') == 'catalog':
            return programs.program_page(user_id=owner, after=args.get('cursor', ''), limit=args.get('limit', 10))
        if args.get('view') == 'experiments':
            return programs.experiment_page(args['program_id'], user_id=owner,
                after=args.get('after', 0), limit=args.get('limit', 10))
        return _view(args['program_id'], owner=owner)
    if name != 'manage_exploration':
        raise ValueError('Unknown exploration control')
    cfg = task.get('config') or {}
    if cfg.get('planMode') or cfg.get('readOnlyPaths') or cfg.get('toolSpecAllowlist'):
        raise PermissionError('This Chat turn is restricted; it cannot grant an independent exploration execution')
    limits = cfg.get('executionLimits')
    if limits is not None:
        from lib.agent_core.execution_limits import validate_execution_limits
        validate_execution_limits(limits)
        if any(value is not None for value in limits.values()):
            # A fresh programme budget cannot reset the shared Chat account or
            # move its absolute deadline. No durable cross-account grant exists.
            raise PermissionError('This Chat has a finite allowance; it cannot authorize a separate long-running exploration')
    key = hashlib.sha256(json.dumps([source['conversation_id'], args.get('idempotency_key')]).encode()).hexdigest()
    action = args['action']
    if action == 'create':
        request = deepcopy(args['request'])
        project = cfg.get('canonicalProjectPath') or cfg.get('projectPath')
        if project:
            request['project_path'] = project
        if cfg.get('modelRef') and 'model' not in request:
            request['model'] = deepcopy(cfg['modelRef'])
        if 'skill_selection' not in request:
            receipts = (task.get('_selectedSkillsSnapshot') or {}).get('receipts') or []
            selected = [row['skill_id'] for row in receipts
                if row.get('status') == 'loaded' and row.get('trigger') in ('explicit', 'bound')]
            if selected:
                request['skill_selection'] = list(dict.fromkeys(selected))
        if isinstance(cfg.get('plugins'), list):
            request['executor_plugins'] = deepcopy(cfg['plugins'])
        restrictions = {key: False for key in ('fetchEnabled', 'codeExecEnabled', 'humanGuidanceEnabled',
            'memoryEnabled', 'preferencesEnabled', 'langCorrectionEnabled', 'humanFeedbackEnabled')
            if cfg.get(key, key not in ('codeExecEnabled', 'humanGuidanceEnabled')) is False}
        if cfg.get('searchMode', 'multi') == 'off':
            restrictions['searchMode'] = 'off'
        if cfg.get('toolSpecDenylist'):
            restrictions['toolSpecDenylist'] = list(cfg['toolSpecDenylist'])
        request['executor_restrictions'] = restrictions
        program = programs.create_program(request, user_id=owner, idempotency_key=key, chat_source=source)
        if args.get('start'):
            # One-time activation makes a lost Chat acknowledgement safe to retry.
            program = programs.command_program(program['program_id'], {'action': 'start'}, user_id=owner)
    else:
        program = programs.get_program(args['program_id'], user_id=owner)
        if action == 'command':
            program = programs.command_program(program['program_id'], args['command'], user_id=owner,
                                               expected_version=args['expected_version'])
        elif action == 'append':
            program = programs.append_steps(program['program_id'], {'steps': args['steps']},
                user_id=owner, idempotency_key=key)
        elif action == 'guide':
            from .coordination import publish
            current = program['current']
            if not current:
                raise EvolutionStateError('There is no current experiment; append an explicit next step instead')
            if current['run_id'] != args['expected_run_id']:
                raise EvolutionStateError('The current experiment changed; read its state before sending guidance')
            update = publish(current['run_id'], {'kind': 'guidance', 'content': args['content']},
                user_id=owner, event_id=key)
            return {**_view(program['program_id'], owner=owner), 'guidance': {'run_id': current['run_id'], 'sequence': update['seq'],
                'status': 'recorded', 'acceptance': 'Pending executor acknowledgement; applies to this experiment only'}}
    return _view(program['program_id'], owner=owner)
