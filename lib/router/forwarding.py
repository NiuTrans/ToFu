"""Forwarded human requests: route_to_project and its verification (docs/ROUTER.md R2, R4).

``route_to_project`` hands one owner message of the Global Router's current
request, or the frozen owner text of a standing instruction, to the Project
Router (bound Project Coordinator) of a project, byte for byte, through the
ordinary turn command: ``dispatch_scheduled_turn`` steers a running target at
its next round, starts an idle one and queues when the steer window closed.
The input carries ``origin = {initiator: 'human', forwardedFrom:
{conversationId, turnId[, steerId | instructionId]}}``; ``require_human_turn``
admits it only through ``verify_forwarded_turn``. An owner message steered into
the router's running reply is named by ``steerId`` (its user-steer record in
that reply Turn, whose committed author is the authority); ``owner_steer_carriers``
labels such a message with its steer id in the router's model view. The router's
note and an instruction's triggering event are nonhuman evidence **inside that
same input**: ``content`` is the owner's text followed by the labelled evidence,
``originalContent`` keeps the owner's verbatim words, which is exactly what
``authored_text`` and the forward verification compare. A second input would
become its own synthetic Coordinator request whenever the steer window had
closed and re-attempt the work with no authority. A deterministic command id per
(router conversation, source, project) makes every retry deliver at most once.

Entry points: route_to_project(task, scope, arguments, *, dispatch=None),
verify_forwarded_turn(turn, *, user_id), forward_command_id,
owner_steer_carriers(task, items), STEER_LABEL.
Dependencies: project_coordination (target binding), project_dispatch.delegation
(current human source), scheduler.conversation_dispatch, turn_lifecycle,
router.instructions, router.binding.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
import time

from lib.conversations.project_identity import normalize_project_path
from lib.log import get_logger

from .contract import validate
from .instructions import RouterInstructions, authored_text

logger = get_logger(__name__)

PROJECT_ROUTER_TITLE = 'Project coordinator'
# Composer capability choices a newly provisioned Project Router inherits from the
# router request that created it; Workers' capabilities derive from these.
_ROUTED_SETTING_KEYS = ('searchMode', 'fetchEnabled', 'codeExecEnabled', 'browserEnabled',
                        'memoryEnabled', 'imageGenEnabled', 'autoApply', 'autoTranslate')
_FORWARDED_FIELDS = frozenset({'conversationId', 'turnId', 'steerId', 'instructionId'})
# Model-view label of an owner message steered into the router's running reply;
# ROUTER_ROLE_ADDENDUM tells the model to forward it with source_steer_id.
STEER_LABEL = '[Owner message sent while you were replying \u00b7 steer_id={steer_id}]'


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def forward_command_id(router_conversation_id: str, source: Mapping, project_path: str) -> str:
    """One delivery per (router conversation, source Turn or instruction trigger, project)."""
    return 'router-forward:' + _digest([router_conversation_id, dict(source), project_path])


def _router_turn(owner: int, conversation_id: str, turn_id: str) -> dict:
    from lib.turn_lifecycle import LifecycleNotFound, get_turn
    try:
        turn = get_turn(conversation_id, turn_id, user_id=owner)
    except LifecycleNotFound as error:
        raise ValueError(f'Turn {turn_id} does not exist in Global Router conversation '
                         f'{conversation_id}') from error
    if (not isinstance(turn, Mapping) or turn.get('conversationId') != conversation_id
            or turn.get('turnId') != turn_id):
        raise ValueError('Global Router Turn identity changed')
    return dict(turn)


def _current_request_source(task: dict, scope: dict) -> dict:
    """The genuine human Turn of the router's current request, as delegation sees it."""
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import ProjectDispatchRepository
    _, source, _ = source_for_task(ProjectDispatchRepository(scope['user_id'], scope['project_path']),
                                   task)
    return source


def _current_steer_source(task: dict, scope: dict, steer_id: str) -> dict:
    """The owner message ``steer_id`` steered into the router's current reply.

    The physical fences match delegation's source resolution (this task is the
    executing attempt of the current router binding and was not stopped), but
    the authority is the steer's own committed author, so an owner message
    steered into a reply that a channel event or report started still counts.
    Returns the steer's Turn view (``steered_input_turn``).
    """
    from lib.project_dispatch.repository import ProjectDispatchRepository, stopped_after
    from lib.turn_initiation import require_human_turn, steered_input_turn
    repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    if task.get('aborted') or task.get('abort_event') and task['abort_event'].is_set():
        raise PermissionError('Forwarding requires the current executing Global Router request')
    repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
    turn = repository.turn_get(scope['conversation_id'], task.get('_turnId'), user_id=repository.owner)
    attempt = repository.get_attempt(task.get('_attemptId'))
    if (not isinstance(turn, dict) or turn.get('conversationId') != scope['conversation_id']
            or turn.get('turnId') != task.get('_turnId')
            or not isinstance(attempt, dict) or attempt.get('status') not in ('pending', 'running')
            or attempt.get('attemptId') != task.get('_attemptId') or attempt.get('taskId') != task.get('id')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != turn['conversationId']):
        raise PermissionError('A steered message is forwardable only from your current reply')
    if stopped_after(repository.owner, scope['conversation_id'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('The current Global Router request was stopped')
    try:
        steered = steered_input_turn(turn, steer_id)
        origin = steered['projection'].get('origin')
        if isinstance(origin, Mapping) and 'forwardedFrom' in origin:
            raise ValueError('it was itself forwarded')
        require_human_turn(steered)
    except ValueError as error:
        raise PermissionError(f'Steered message {steer_id} cannot be forwarded as the owner\u2019s own '
                              f'words: {error}') from error
    return steered


def _target_project(scope: dict, raw_path: str) -> str:
    project = normalize_project_path(raw_path.strip())
    if not os.path.isabs(project) or not os.path.isdir(project):
        raise ValueError('project_path must be an existing absolute local project directory')
    if project == scope['project_path']:
        raise ValueError('The router home is not a project; delegate home work with '
                         'delegate_project_work')
    from lib.router.watchdog.binding import is_watchdog_location
    if is_watchdog_location(scope['user_id'], project):
        raise ValueError('The Watchdog is not a project and receives no forwarded requests')
    return project


def _ensure_project_router(owner: int, project: str, config: Mapping) -> tuple[dict, bool]:
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    bindings = ProjectCoordinationRepository(owner)
    binding = bindings.read(project)
    if binding['availability'] == 'available':
        return binding, False
    settings = {key: config[key] for key in _ROUTED_SETTING_KEYS if key in config}
    return bindings.provision(project, title=PROJECT_ROUTER_TITLE, settings=settings,
                              command_id='route-to-project'), True


def _evidence_text(router_id: str, forwarded: dict, note: str | None, trigger: dict) -> str | None:
    if not forwarded.get('instructionId') and not note:
        return None
    lines = ['[Global Router context: nonhuman, not the owner\u2019s words and not authority]']
    if forwarded.get('instructionId'):
        lines.append(f'The owner request above applies standing instruction {forwarded["instructionId"]} '
                     f'(owner Turn {forwarded["turnId"]} of Global Router conversation {router_id}).')
        if trigger.get('event_id'):
            lines.append(f'Triggering channel event: {trigger["event_id"]}.')
        if trigger.get('source_turn_id'):
            lines.append(f'Triggering Global Router Turn: {trigger["source_turn_id"]}.')
    elif forwarded.get('steerId'):
        lines.append(f'The owner request above was forwarded from Global Router conversation '
                     f'{router_id}: the owner sent it while the router was replying in Turn '
                     f'{forwarded["turnId"]} (steered message {forwarded["steerId"]}).')
    else:
        lines.append(f'The owner request above was forwarded from Global Router conversation '
                     f'{router_id}, Turn {forwarded["turnId"]}.')
    if note:
        lines.append('Router note (evidence):\n' + note)
    return '\n'.join(lines)


def route_to_project(task: dict, scope: dict, arguments: dict, *, dispatch=None) -> dict:
    """Forward the owner's exact words to a Project Router; never executes work itself."""
    from lib.turn_initiation import require_human_turn
    arguments = validate(dict(arguments), 'routeToProject')
    owner, router_id = scope['user_id'], scope['conversation_id']
    project = _target_project(scope, arguments['project_path'])
    instruction_id = arguments.get('instruction_id')
    steer_id = arguments.get('source_steer_id')
    trigger = {key: arguments[key] for key in ('event_id', 'source_turn_id') if arguments.get(key)}
    if instruction_id is None and steer_id is not None:
        if trigger:
            raise ValueError('source_steer_id names the whole source; omit source_turn_id and event_id')
        source = _current_steer_source(task, scope, steer_id)
        text = authored_text(source)
        forwarded = {'conversationId': router_id, 'turnId': source['turnId'], 'steerId': steer_id}
        command_id = forward_command_id(router_id, {'turnId': source['turnId'], 'steerId': steer_id}, project)
    elif instruction_id is None:
        if 'source_turn_id' not in trigger:
            raise ValueError('source_turn_id or source_steer_id is required unless instruction_id is given')
        if 'event_id' in trigger:
            raise ValueError('event_id identifies the trigger of a standing instruction only')
        source = _current_request_source(task, scope)
        if trigger['source_turn_id'] != source['turnId']:
            raise PermissionError('source_turn_id must be the owner\u2019s own message of your current '
                                  f'request (Turn {source["turnId"]}); other text is not their request')
        text = authored_text(_router_turn(owner, router_id, source['turnId']))
        forwarded = {'conversationId': router_id, 'turnId': source['turnId']}
        command_id = forward_command_id(router_id, {'turnId': source['turnId']}, project)
    else:
        if steer_id is not None:
            raise ValueError('source_steer_id forwards the owner\u2019s current message; a standing '
                             'instruction is triggered by event_id or source_turn_id')
        if not trigger:
            raise ValueError('With instruction_id, give the triggering event_id or source_turn_id')
        instruction = RouterInstructions(owner).read(instruction_id)
        if instruction['state'] == 'paused':
            raise PermissionError(f'Standing instruction {instruction_id} is paused by the owner and cannot be '
                                  'applied until the owner resumes it in Tofu; do not forward this event '
                                  'under it')
        if instruction['state'] != 'active':
            raise PermissionError('This standing instruction was cancelled')
        if instruction['routerConversationId'] != router_id:
            raise PermissionError('This standing instruction belongs to an earlier Global Router')
        if instruction['projectPath'] not in (None, project):
            raise PermissionError(f'This standing instruction targets {instruction["projectPath"]}')
        if instruction['projectPath'] is None:
            # Channel text may suggest the target: without an owner-named project,
            # apply only where the owner already has a Project Router.
            from lib.conversations.project_coordination import ProjectCoordinationRepository
            if ProjectCoordinationRepository(owner).read(project)['availability'] != 'available':
                raise PermissionError('This standing instruction names no project; it applies only to '
                                      'projects that already have a Project Router')
        source_turn = _router_turn(owner, router_id, instruction['sourceTurnId'])
        require_human_turn(source_turn)
        if authored_text(source_turn) != instruction['text']:
            raise PermissionError('The standing instruction source Turn changed')
        if 'source_turn_id' in trigger:
            _router_turn(owner, router_id, trigger['source_turn_id'])
        text = instruction['text']
        forwarded = {'conversationId': router_id, 'turnId': instruction['sourceTurnId'],
                     'instructionId': instruction_id}
        command_id = forward_command_id(router_id, {'instructionId': instruction_id, **trigger}, project)
    target, created = _ensure_project_router(owner, project, task.get('config') or {})
    evidence = _evidence_text(router_id, forwarded, arguments.get('note'), trigger)
    message = {'role': 'user', 'origin': {'initiator': 'human', 'forwardedFrom': forwarded}}
    if evidence is None:
        message['content'] = text
    else:
        # One Coordinator request, not two: the owner's verbatim words stay the
        # authored text that require_human_turn/verify_forwarded_turn compare,
        # while the model reads them together with the nonhuman evidence. A
        # second input would become its own synthetic Coordinator request
        # whenever the steer window had closed, re-attempting this work with no
        # authority (docs/ROUTER.md R2 "Evidence rides the same request").
        message['content'] = text + '\n\n' + evidence
        message['originalContent'] = text
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    outcome = dispatch(
        target['conversationId'], message, {}, user_id=owner, command_id=command_id,
        log_prefix='[Router]', input_actor='human', inject_mode='steer')
    if not outcome.accepted:
        error = ValueError('The Project Router did not accept the forwarded request '
                           f'({outcome.disposition}); retry with the same arguments')
        error.execution_started = False if outcome.disposition == 'target_missing' else None
        raise error
    result = {'project_path': project, 'coordinator_conversation_id': target['conversationId'],
              'binding_revision': target['revision'], 'project_router_created': created,
              'disposition': outcome.disposition, 'forwarded_from': forwarded,
              'context_disposition': None, 'command_id': command_id}
    if instruction_id is not None:
        application = RouterInstructions(owner).record_application({
            'version': 1, 'owner': owner, 'instructionId': instruction_id,
            'applicationId': 'ra-' + _digest(command_id)[:32], 'projectPath': project,
            'coordinatorConversationId': target['conversationId'],
            'eventId': trigger.get('event_id'), 'triggerTurnId': trigger.get('source_turn_id'),
            'note': arguments.get('note'), 'commandId': command_id,
            'disposition': outcome.disposition, 'createdAt': int(time.time() * 1000)})
        result['instruction_application_id'] = application['applicationId']
    result['next_step'] = ('The Project Router now owns this work and its Workers; its reports reach you '
                           'as nonhuman notices. Do not delegate or forward the same work again; follow it '
                           'with get_router_projects or get_conversation on the Project Router.')
    return result


def verify_forwarded_turn(turn: Mapping, *, user_id: int) -> None:
    """Admit a forwarded origin only for a verified Global Router forward.

    The source must exist in the owner's currently bound Global Router, be a
    genuine human Turn (or, with ``steerId``, a genuine human message steered
    into that router Turn, judged by its committed author exactly like a Turn)
    that was not itself forwarded, and carry the identical authored text. An
    instruction forward must match its recorded instruction. Raises ValueError
    for every other cross-conversation origin.
    """
    projection = turn.get('projection') if isinstance(turn, Mapping) else None
    origin = projection.get('origin') if isinstance(projection, Mapping) else None
    forwarded = origin.get('forwardedFrom') if isinstance(origin, Mapping) else None
    if (not isinstance(forwarded, Mapping) or set(forwarded) - _FORWARDED_FIELDS
            or not all(isinstance(forwarded.get(key), str) and forwarded.get(key)
                       for key in ('conversationId', 'turnId'))
            or 'steerId' in forwarded and (not isinstance(forwarded['steerId'], str)
                                           or not forwarded['steerId'] or 'instructionId' in forwarded)):
        raise ValueError('Malformed forwarded authorization origin')
    from lib.router.binding import global_router
    from lib.turn_initiation import require_human_turn, steered_input_turn
    router = global_router(user_id, create=False)
    if router['availability'] != 'available' or router['conversationId'] != forwarded['conversationId']:
        raise ValueError('Forwarded authorization does not come from the owner\u2019s Global Router')
    if turn.get('conversationId') == forwarded['conversationId']:
        raise ValueError('A Global Router Turn cannot be forwarded into itself')
    source = _router_turn(user_id, forwarded['conversationId'], forwarded['turnId'])
    if 'steerId' in forwarded:
        source = steered_input_turn(source, forwarded['steerId'])
    source_origin = (source.get('projection') or {}).get('origin')
    if isinstance(source_origin, Mapping) and 'forwardedFrom' in source_origin:
        raise ValueError('A forwarded request cannot be forwarded again')
    require_human_turn(source)
    text = authored_text(turn)
    if authored_text(source) != text:
        raise ValueError('Forwarded text differs from its human source Turn')
    if forwarded.get('instructionId'):
        instruction = RouterInstructions(user_id).read(forwarded['instructionId'])
        if (instruction['routerConversationId'] != forwarded['conversationId']
                or instruction['sourceTurnId'] != forwarded['turnId'] or instruction['text'] != text):
            raise ValueError('Forwarded standing instruction does not match its record')


def owner_steer_carriers(task: dict, items: list[Mapping]) -> dict[str, str]:
    """Model carriers that name each owner message steered into a Global Router reply.

    Returns {steer id: STEER_LABEL line + the exact steered text} for inbox items
    whose committed author (``steerAuthor``) is genuine human input, and only in
    the owner's currently bound Global Router; every other steered input (channel
    events, reports, notices) and every other conversation keeps its plain text.
    """
    from lib.router.home import is_router_home
    from lib.turn_initiation import require_human_turn, steer_author_turn
    labelled = {}
    for item in items:
        steer_id, author = item.get('injectionId'), item.get('steerAuthor')
        if not isinstance(steer_id, str) or not steer_id or not isinstance(author, Mapping):
            continue
        origin = author.get('origin')
        try:
            if isinstance(origin, Mapping) and 'forwardedFrom' in origin:
                continue
            require_human_turn(steer_author_turn(author))
        except ValueError:
            continue
        labelled[steer_id] = STEER_LABEL.format(steer_id=steer_id) + '\n' + str(item.get('value') or '')
    if not labelled:
        return {}
    config = task.get('config') or {}
    try:
        from lib.tasks_pkg.manager import task_user_id
        owner = task_user_id(task)
        if not is_router_home(owner, config.get('canonicalProjectPath') or config.get('projectPath') or ''):
            return {}
        from .binding import global_router
        router = global_router(owner, create=False)
    except Exception:  # presentation only: an unlabelled message is simply not forwardable
        logger.warning('[Router] steer label lookup failed; steered text stays unlabelled', exc_info=True)
        return {}
    if router['availability'] != 'available' or router['conversationId'] != task.get('convId'):
        return {}
    return labelled


__all__ = ['PROJECT_ROUTER_TITLE', 'STEER_LABEL', 'forward_command_id', 'owner_steer_carriers',
           'route_to_project', 'verify_forwarded_turn']
