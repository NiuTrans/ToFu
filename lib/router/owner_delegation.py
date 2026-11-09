"""The owner's delegation to their Global Router: act as the owner, never speak as them (ROUTER.md R9).

Owner decision 2026-11-06. The owner grants (only in the Tofu UI) or revokes
one delegation that their current Global Router conversation holds. The
grant, its audit records and the restart acceptance live in the lifecycle
approval gate (``lib.lifecycle_approval``), the single decision point for
live-server lifecycle actions; this module adds what only the router knows:

* the owner trigger of an action: the owner's own message of the current
  request, an owner message steered into the reply, or an active standing
  instruction with its triggering event (resolved by the forwarding owner,
  exactly as ``route_to_project`` resolves them);
* ``restart_server``: the Settings restart path (frontend preparation first,
  then the shared ``perform_server_reexec`` lifecycle owner) behind the
  restart-protection signal (``list_running_tasks``), detached from the turn;
* ``delegated_conversation_control``: the grant branch of
  ``control_conversation`` for requests that have no owner message of their
  own (standing instructions, nonhuman turns), limited to Continue and Stop.

The grant never lets the Router answer for the owner, approve a prompt meant
for the owner, forward its own text as the owner's or start Workers: those
keep their human-only gates (``require_human_turn``, ``source_for_task``,
lifecycle approvals) and no model tool can write the grant.

Entry points: router_requester, resolve_trigger, restart_server,
delegated_conversation_control, owner_delegation_state.
Dependencies: lib.lifecycle_approval, lib.router.forwarding,
lib.router.instructions, lib.tasks_pkg.manager.list_running_tasks,
lib.server.server_reexec.
"""
from __future__ import annotations

import threading
import time

from lib.log import get_logger

from .contract import validate

logger = get_logger(__name__)

RESTART_REASON = 'restart'


def _refused(code: str, message: str):
    from lib.lifecycle_approval import DelegationRefused
    return DelegationRefused(code, message)


def router_requester(task: dict, scope: dict) -> dict:
    """The acting Global Router: conversation, binding revision, tenant, Turn and Attempt."""
    from lib.identity import PrincipalContext
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    if principal.owner_user_id not in (None, scope['user_id']):
        raise PermissionError('The task principal belongs to another owner')
    return {'kind': 'global_router', 'conversationId': scope['conversation_id'],
            'bindingRevision': scope['binding_revision'], 'tenant': principal.tenant_id,
            'turnId': task.get('_turnId'), 'attemptId': task.get('_attemptId')}


def resolve_trigger(task: dict, scope: dict, arguments: dict) -> dict:
    """The owner's own authority for this action, with their verbatim words.

    Exactly one trigger: ``source_turn_id`` (the owner's message of the
    current request), ``source_steer_id`` (an owner message steered into the
    current reply) or ``instruction_id`` with ``event_id`` or
    ``source_turn_id`` (an active standing instruction of this router).
    Raises ValueError for a malformed choice and DelegationRefused when the
    named authority is not genuine.
    """
    from . import forwarding
    from .instructions import RouterInstructions, authored_text
    instruction_id = arguments.get('instruction_id')
    steer_id = arguments.get('source_steer_id')
    turn_id = arguments.get('source_turn_id')
    event_id = arguments.get('event_id')
    owner, router_id = scope['user_id'], scope['conversation_id']
    if instruction_id is None:
        if event_id is not None:
            raise ValueError('event_id names the trigger of a standing instruction only')
        if (turn_id is None) == (steer_id is None):
            raise ValueError('Name the owner\u2019s message that asks for this: source_turn_id, or '
                             'source_steer_id, or instruction_id for a standing instruction')
        try:
            if steer_id is not None:
                steered = forwarding._current_steer_source(task, scope, steer_id)
                return {'kind': 'owner_steer', 'turnId': steered['turnId'], 'steerId': steer_id,
                        'text': authored_text(steered)}
            source = forwarding._current_request_source(task, scope)
            if turn_id != source['turnId']:
                raise PermissionError('source_turn_id must be the owner\u2019s own message of your current '
                                      f'request (Turn {source["turnId"]})')
            turn = forwarding._router_turn(owner, router_id, source['turnId'])
            return {'kind': 'owner_message', 'turnId': source['turnId'], 'text': authored_text(turn)}
        except (PermissionError, ValueError) as error:
            raise _refused('owner_delegation_trigger_invalid',
                           f'No owner request authorizes this: {error}. Act only on the owner\u2019s own '
                           'message, an owner message sent while you reply, or an active standing '
                           'instruction.') from error
    if steer_id is not None or (turn_id is None) == (event_id is None):
        raise ValueError('With instruction_id, give exactly one of event_id or source_turn_id')
    from lib.turn_initiation import require_human_turn
    try:
        _live_router_attempt(task, scope)
        instruction = RouterInstructions(owner).read(instruction_id)
        if instruction['state'] != 'active':
            raise PermissionError(f'standing instruction {instruction_id} is {instruction["state"]}')
        if instruction['routerConversationId'] != router_id:
            raise PermissionError('the standing instruction belongs to an earlier Global Router')
        source_turn = forwarding._router_turn(owner, router_id, instruction['sourceTurnId'])
        require_human_turn(source_turn)
        if authored_text(source_turn) != instruction['text']:
            raise PermissionError('the standing instruction source Turn changed')
        if turn_id is not None:
            forwarding._router_turn(owner, router_id, turn_id)
    except (PermissionError, ValueError) as error:
        raise _refused('owner_delegation_trigger_invalid',
                       f'The standing instruction cannot authorize this: {error}') from error
    trigger = {'kind': 'standing_instruction', 'turnId': instruction['sourceTurnId'],
               'instructionId': instruction_id, 'text': instruction['text']}
    trigger.update({'eventId': event_id} if event_id is not None else {'triggerTurnId': turn_id})
    return trigger


def _live_router_attempt(task: dict, scope: dict) -> None:
    """The task is the executing, unstopped attempt of the current Global Router binding.

    The fences ``forwarding._current_steer_source`` applies to steered owner
    messages; owner messages get them from delegation's source resolution.
    """
    from lib.project_dispatch.repository import ProjectDispatchRepository, stopped_after
    repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    if task.get('aborted') or task.get('abort_event') and task['abort_event'].is_set():
        raise PermissionError('Your current reply was stopped')
    repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
    turn = repository.turn_get(scope['conversation_id'], task.get('_turnId'), user_id=repository.owner,
                               include_projection=False)
    attempt = repository.get_attempt(task.get('_attemptId'))
    if (not isinstance(turn, dict) or turn.get('conversationId') != scope['conversation_id']
            or turn.get('turnId') != task.get('_turnId')
            or not isinstance(attempt, dict) or attempt.get('status') not in ('pending', 'running')
            or attempt.get('attemptId') != task.get('_attemptId') or attempt.get('taskId') != task.get('id')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != turn['conversationId']):
        raise PermissionError('Only your current executing reply can act for the owner')
    if stopped_after(repository.owner, scope['conversation_id'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('Your current reply was stopped')


def _restart_protection(router_conversation_id: str, interrupt: bool) -> list[str]:
    """Apply the Settings restart guard; return the conversations a restart interrupts.

    The signal is ``list_running_tasks`` (durable jobs, local carriers and
    protected steps), excluding the Router's own conversation as Settings
    excludes the caller's. A protected step always refuses; other running
    conversations refuse unless the owner asked to interrupt them (the
    Settings force). An unreadable signal never proves idle.
    """
    from lib.tasks_pkg.manager import list_running_tasks
    try:
        running = list_running_tasks(exclude_conv_id=router_conversation_id)
    except Exception as error:
        raise _refused('restart_protection_unavailable',
                       'Active execution could not be verified; the server was not restarted. Retry '
                       f'when storage answers ({type(error).__name__}).') from error
    steps = [item for item in running if item.get('protectedStep')]
    if steps:
        markers = ', '.join(f"{item['protectedStep']} ({item.get('marker') or item['taskId']})" for item in steps)
        raise _refused('restart_protected_step',
                       f'Restart refused: a protected step is in progress ({markers}); a restart now would '
                       'boot from a half-landed checkout. Retry after it finishes.')
    conversations = sorted({item.get('convId') or item['taskId'] for item in running})
    if conversations and not interrupt:
        raise _refused('restart_would_interrupt',
                       f'Restart refused: {len(conversations)} other conversation(s) are running and a '
                       f'restart would interrupt them ({", ".join(conversations)}). Restart anyway only if '
                       'the owner asked for it even while work runs (interrupt_running_conversations=true); '
                       'interrupted conversations resume from their checkpoint by themselves after the '
                       'restart, and the restart-complete notice lists each one\u2019s resume result.')
    return conversations


def _start_detached(work) -> None:
    threading.Thread(target=work, name='tofu-router-restart', daemon=True).start()


def restart_server(task: dict, scope: dict, arguments: dict) -> dict:
    """Restart the Tofu server for the owner through the sanctioned Settings path.

    Order: grant (gate) \u2192 owner trigger \u2192 restart protection \u2192 audit record
    (gate, one transaction) \u2192 detached: frontend preparation \u2192 protection
    again \u2192 gate acceptance (grant, cooldown, stamp) \u2192 the shared lifecycle
    owner stops the old server. A failure before acceptance keeps it serving.
    """
    from lib import lifecycle_approval as gate
    arguments = validate(dict(arguments), 'restartServer')
    owner = scope['user_id']
    requester = router_requester(task, scope)
    target = {'kind': 'server'}
    reason = arguments['reason']
    action_id = gate.delegated_action_id(owner, requester, 'restart', arguments['idempotency_key'])
    prior = gate.delegated_action(owner, action_id)
    if prior is not None:
        return _restart_view(prior, replayed=True)
    gate.check_delegation(owner, requester, 'restart')
    interrupt = bool(arguments.get('interrupt_running_conversations'))
    trigger = None
    try:
        trigger = resolve_trigger(task, scope, arguments)
        _restart_protection(scope['conversation_id'], interrupt)
        if gate.restart_cooldown_remaining() > 0:
            raise _refused('restart_cooldown', 'Another restart was accepted moments ago; nothing ran')
    except gate.DelegationRefused as refusal:
        gate.record_delegated_refusal(owner, requester, scope='restart', reason=reason, trigger=trigger,
                                      target=target, code=refusal.code, detail=str(refusal))
        raise
    record, replayed = gate.begin_delegated_action(
        owner, requester, scope='restart', key=arguments['idempotency_key'], reason=reason,
        trigger=trigger, target=target)
    if not replayed:
        requested_at = time.time()
        _start_detached(lambda: _restart_after_acceptance(
            owner, record['id'], requester, requested_at, interrupt))
    return _restart_view(record, replayed=replayed)


def _restart_next_step(status: str) -> str:
    """What happens next for a restart in ``status``; never a promise the server does not keep."""
    if status in ('accepted', 'restarting'):
        return ('The restart runs detached from this turn: the builds run first and the old server stops '
                'only after they succeed. Tell the owner it is under way, then end your turn. When the restart '
                'ends, whether completed, refused or failed, this conversation receives one nonhuman '
                'restart-complete notice as a new turn, with the outcome, the old and new commit, the build '
                'results and the resume result of each interrupted conversation; interrupted conversations '
                'resume from their checkpoint by themselves. Report the outcome to the owner from that notice. '
                'The owner sees every step in the main coordinator\u2019s "Act for me" log. Never call '
                'restart_server again for the same request.')
    if status == 'refused':
        return ('This request was refused before anything ran: nothing restarted and no notice follows. Tell '
                'the owner why; a new restart needs a new request once the cause is fixed.')
    return (f'This restart already ended ({status}). Its one nonhuman restart-complete notice reaches this '
            'conversation as a new turn if it has not already; report the outcome to the owner from it. Never '
            'call restart_server again for the same request.')


def _restart_view(record: dict, *, replayed: bool) -> dict:
    return {'action_id': record['id'], 'status': record['status'], 'replayed': replayed,
            'execution_started': record['status'] in ('accepted', 'restarting', 'completed'),
            'code': record.get('code'), 'detail': record.get('detail'),
            'next_step': _restart_next_step(record['status'])}


def _prepare(action_id: str) -> tuple[str, dict]:
    """The Settings preparation, observed for the restart-complete notice's build results."""
    from lib.server import server_reexec
    from serverctl_pkg.native_engine import observe_preparation
    phases: list[str] = []

    def observe(progress):
        phase = progress.get('phase') if isinstance(progress, dict) else None
        if isinstance(phase, str) and phase not in phases:
            phases.append(phase)
    started = time.monotonic()
    with observe_preparation(observe):
        error = server_reexec.prepare_server_reexec_frontend()
    return error, {'ok': not error, 'phases': phases, 'seconds': round(time.monotonic() - started, 1),
                   'detail': (error or '')[:2000] or None}


def _restart_after_acceptance(owner: int, action_id: str, requester: dict, requested_at: float,
                              interrupt: bool) -> None:
    from lib import boot_identity
    from lib import lifecycle_approval as gate
    from lib.server import server_reexec
    from lib.server.restart_notice import deliver_restart_notice
    old_code = boot_identity.code_fingerprint()
    try:
        error, preparation = _prepare(action_id)
        if error:
            gate.note_restart_notice(action_id, preparation=preparation, oldCode=old_code)
            gate.settle_delegated_action(owner, action_id, status='failed',
                                         code='restart_preparation_failed', detail=error)
            return
        interrupted = _restart_protection(requester['conversationId'], interrupt)
        accepted = gate.accept_delegated_restart(owner, action_id, requester, interrupted=interrupted,
                                                 old_code=old_code, preparation=preparation)
        if accepted.get('status') != 'restarting':
            return
        if not server_reexec.perform_server_reexec(RESTART_REASON, frontend_prevalidated=True,
                                                   requested_at=requested_at):
            gate.settle_delegated_action(owner, action_id, status='failed', code='restart_bridge_refused',
                                         detail='Every restart bridge refused; the server keeps running')
    except gate.DelegationRefused as refusal:
        gate.note_restart_notice(action_id, oldCode=old_code)
        gate.settle_delegated_action(owner, action_id, status='failed', code=refusal.code,
                                     detail=str(refusal))
    except Exception as error:  # a detached thread has no caller to report to
        logger.exception('[Router] delegated restart %s failed', action_id)
        gate.settle_delegated_action(owner, action_id, status='failed', code='restart_failed',
                                     detail=f'{type(error).__name__}: {error}')
    finally:
        # Every refusal above left the old server serving: tell the requester now.
        # A handed-over restart has no outcome yet, so this delivers nothing.
        deliver_restart_notice(action_id)


def delegated_conversation_control(task: dict, scope: dict, arguments: dict, *, control):
    """``control_conversation`` with ``owner_delegation``: Continue, checkpoint resume or Stop.

    The grant, the owner trigger and the audit record precede ``control``, the
    shared session command (``session_control.control_session``). It runs as
    the owner's delegation, which stands in for an owner message of the
    current request; the target keeps its own accepted execution identity.
    Its outcome settles the record. Regenerate needs the owner's own request.
    """
    from lib import lifecycle_approval as gate
    from lib.identity import PrincipalContext
    from .policy import is_router_scope
    if not is_router_scope(scope):
        raise PermissionError('Only the owner\u2019s Global Router can act under the owner\u2019s delegation')
    authority = validate(dict(arguments['owner_delegation']), 'ownerDelegationAuthority')
    for name in ('conversation_id', 'turn_id', 'attempt_id', 'idempotency_key'):
        if not isinstance(arguments.get(name), str) or not arguments[name].strip():
            raise ValueError(f'{name} is required')
    action = arguments.get('action')
    if action not in ('continue', 'checkpoint_resume', 'stop'):
        raise ValueError('Under the owner\u2019s delegation, only continue, checkpoint_resume and stop are '
                         'available; regenerate needs the owner\u2019s own request, and follow_up belongs '
                         'to the Worker\u2019s own Project Coordinator')
    owner = scope['user_id']
    requester = router_requester(task, scope)
    target = {'kind': 'conversation', 'conversationId': arguments['conversation_id'],
              'turnId': arguments['turn_id'], 'attemptId': arguments['attempt_id'], 'action': action}
    gate.check_delegation(owner, requester, 'conversation_control')
    try:
        trigger = resolve_trigger(task, scope, authority)
    except gate.DelegationRefused as refusal:
        gate.record_delegated_refusal(owner, requester, scope='conversation_control',
                                      reason=authority['reason'], trigger=None, target=target,
                                      code=refusal.code, detail=str(refusal))
        raise
    record, _ = gate.begin_delegated_action(
        owner, requester, scope='conversation_control', key=arguments['idempotency_key'],
        reason=authority['reason'], trigger=trigger, target=target)
    acting = PrincipalContext.user(subject_id='owner-delegation:' + record['id'], owner_user_id=owner,
                                   tenant_id=requester['tenant'])
    command = {name: value for name, value in arguments.items() if name != 'owner_delegation'}
    try:
        result = control(task, scope, command, acting_principal=acting)
    except Exception as error:
        # A replay settles only what no earlier call settled.
        if record['status'] == 'accepted':
            code = getattr(error, 'code', None)
            gate.settle_delegated_action(owner, record['id'], status='failed',
                                         code=code if isinstance(code, str) else type(error).__name__,
                                         detail=str(error))
        raise
    if record['status'] != 'completed':
        gate.settle_delegated_action(owner, record['id'], status='completed')
    return {**result, 'owner_delegation_action_id': record['id']}


def owner_delegation_state(owner: int) -> dict:
    """The owner's grant, whether it covers the current Global Router, and its audit records."""
    from lib import lifecycle_approval as gate
    from lib.router.binding import global_router
    grant = gate.delegation(owner)
    router = global_router(owner, create=False)
    grantee = grant.get('grantee') or {}
    active = (grant['state'] == 'granted' and router['availability'] == 'available'
              and grantee.get('conversationId') == router['conversationId']
              and grantee.get('bindingRevision') == router['revision'])
    return validate({'grant': grant, 'active': active,
                     'router': {key: router[key] for key in ('conversationId', 'revision', 'availability')},
                     'actions': gate.delegated_actions(owner)}, 'ownerDelegationState')


__all__ = ['RESTART_REASON', 'delegated_conversation_control', 'owner_delegation_state', 'resolve_trigger',
           'restart_server', 'router_requester']
