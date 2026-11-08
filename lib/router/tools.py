"""Router tool adapter called by execute_coordinator_tool for the Global Router.

Every call has already passed the coordinator binding check; this adapter
requires that binding to be the owner's router home before dispatching to one
owner: forwarding (route_to_project), projects (get_router_projects) or
instructions (router_instruction_*). Creating and cancelling instructions are
owner actions: they require the router's current request to be a genuine human
Turn, resolved exactly like delegation authority.
"""
from __future__ import annotations

from .contract import validate


def _human_source_turn(task: dict, scope: dict, source_turn_id: str) -> dict:
    from .forwarding import _current_request_source, _router_turn
    source = _current_request_source(task, scope)
    if source_turn_id != source['turnId']:
        raise PermissionError('source_turn_id must be the owner\u2019s own message of your current request '
                              f'(Turn {source["turnId"]})')
    return _router_turn(scope['user_id'], scope['conversation_id'], source['turnId'])


def execute_router_tool(task: dict, scope: dict, name: str, arguments: dict) -> dict:
    from .instructions import RouterInstructions
    from .policy import is_router_scope
    if not is_router_scope(scope):
        raise PermissionError('Only the owner\u2019s Global Router can use this tool')
    owner = scope['user_id']
    if name == 'route_to_project':
        from .forwarding import route_to_project
        return route_to_project(task, scope, arguments)
    if name == 'get_router_projects':
        from .projects import router_projects
        return router_projects(owner, arguments)
    if name == 'restart_server':
        from .owner_delegation import restart_server
        return restart_server(task, scope, arguments)
    instructions = RouterInstructions(owner)
    if name == 'router_instruction_create':
        arguments = validate(dict(arguments), 'instructionCreate')
        project = arguments.get('project_path')
        if project is not None:
            from .forwarding import _target_project
            project = _target_project(scope, project)
        turn = _human_source_turn(task, scope, arguments['source_turn_id'])
        return instructions.create(router_conversation_id=scope['conversation_id'], source_turn=turn,
                                   label=arguments['label'], project_path=project,
                                   thread=arguments.get('thread'),
                                   idempotency_key=arguments['idempotency_key'])
    if name == 'router_instruction_list':
        arguments = validate(dict(arguments), 'instructionList')
        if arguments.get('instruction_id'):
            return instructions.applications(arguments['instruction_id'], cursor=arguments.get('after_id'))
        return instructions.page(cursor=arguments.get('after_id'),
                                 include_cancelled=bool(arguments.get('include_cancelled')))
    if name == 'router_instruction_cancel':
        arguments = validate(dict(arguments), 'instructionCancel')
        from .forwarding import _current_request_source
        _current_request_source(task, scope)
        return instructions.cancel(arguments['instruction_id'])
    raise ValueError('Unknown router tool')


__all__ = ['execute_router_tool']
