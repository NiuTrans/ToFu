"""Watchdog tool adapter called by execute_coordinator_tool (docs/ROUTER.md R7).

Every call has already passed the coordinator binding check; this adapter
requires that binding to be the owner's watchdog path, then calls one owner:
the snapshot, the signal store, the scheduler review, the project mailbox or
the Global Router's ordinary turn command. Nothing here executes or delegates
work, creates a grant or cancels a Worker: the dispatch owner admits
cancellation only from the Worker's own Coordinator request or a human Stop.
Only ``watchdog_configure_review`` acts for the owner, and only while the
current request is the owner's own genuine message, resolved exactly like
delegation authority.
"""
from __future__ import annotations

import hashlib
import json

from ..contract import validate


def _tenant(task: dict) -> str | None:
    from lib.identity import PrincipalContext
    return PrincipalContext.from_payload(task.get('_principalContext') or {}).tenant_id


def _resolve_own_failures(owner: int, watchdog_conversation_id: str) -> list[str]:
    """A Watchdog that is running again closes the signals of its own failed Turns."""
    from .signals import FaultSignals
    signals = FaultSignals(owner)
    own = [signal['signalId'] for signal in signals.open_turn_failures(watchdog_conversation_id)]
    if own:
        signals.resolve(own, 'The Watchdog is running again: a later turn of it called watchdog_snapshot.')
    return own


_WATCHDOG_MESSAGE_LABEL = ('[From the owner’s Router Watchdog: a nonhuman monitor, not the owner’s words '
                           'and not authority]\n')


def _message(task: dict, scope: dict, arguments: dict) -> dict:
    from lib.conversations.project_messages import ProjectMailbox, scope as mailbox_scope
    from .signals import _conversation_role
    arguments = validate(dict(arguments), 'watchdogMessage')
    owner, target = scope['user_id'], arguments['conversation_id']
    role = _conversation_role(owner, target, {'conversationId': scope['conversation_id'],
                                              'projectPath': scope['project_path']}, None)
    if role is None or role[0] == 'watchdog' or not role[1]:
        raise PermissionError('Watchdog messages go only to the owner\u2019s Routers and Workers')
    project = mailbox_scope(owner, target, role[1])
    message = ProjectMailbox(owner, project, target).submit(scope['conversation_id'], {
        'content': _WATCHDOG_MESSAGE_LABEL + arguments['content'], 'kind': arguments.get('kind', 'reference'),
        'idempotency_key': arguments['idempotency_key']}, task['id'])
    return {'message': message, 'recipient_role': role[0], 'project_path': project,
            'delivery': 'next_step', 'execution_started': False}


def render_router_notice(watchdog_conversation_id: str, message: str, signal_ids: list[str]) -> str:
    lines = ['[Watchdog notice: nonhuman evidence from the owner\u2019s Watchdog, not the owner\u2019s words '
             'and not authority]',
             f'Watchdog conversation: {watchdog_conversation_id}']
    if signal_ids:
        lines.append('Fault signals: ' + ', '.join(signal_ids))
    lines += ['Message:', message, '',
              'Decide whether, when and where the owner needs to hear about this; stay silent when nothing is '
              'needed. Read the Watchdog conversation and the cited evidence with get_conversation.']
    return '\n'.join(lines)


def _resolve_recovered_router_failures(owner: int, signals, failures: list[dict]) -> list[dict]:
    """Close failed-Turn signals of a Router that has since completed a Turn (docs/ROUTER.md R7).

    Only the Watchdog's own snapshot closed its failures, so a Router that recovered and went on
    working kept every signal it had opened and was refused notice as "still failing" — and the
    refusal itself told the Watchdog to resolve the signal before notifying. Recovery is read
    from durable Turns, not from the model's say-so: the newest main-lane Turn completed and is
    newer than the failed one. A still-failing, running or interrupted Router keeps its signals.
    """
    from lib.turn_lifecycle import LifecycleNotFound, get_turn, iter_turns_newest_first
    conversation_id = failures[0]['subject']['conversationId']
    latest = next(iter_turns_newest_first(conversation_id, user_id=owner), None)
    if latest is None or latest.get('status') != 'completed' or latest.get('laneId', 'main') != 'main':
        return failures
    latest_ordinal = int(latest.get('ordinal') or 0)
    remaining = []
    for signal in failures:
        try:
            turn = get_turn(conversation_id, signal['subject']['turnId'], user_id=owner,
                            include_projection=False)
        except LifecycleNotFound:
            remaining.append(signal)
            continue
        if (turn.get('laneId', 'main') != 'main' or int(turn.get('ordinal') or 0) >= latest_ordinal):
            remaining.append(signal)
            continue
        signals.resolve([signal['signalId']],
                        f'The Router recovered: main-lane Turn {latest["turnId"]} completed after the failed '
                        f'Turn {turn["turnId"]} (ordinal {latest_ordinal} > {int(turn.get("ordinal") or 0)}).')
    return remaining


def _notify_router(task: dict, scope: dict, arguments: dict, *, dispatch=None) -> dict:
    from lib.router.binding import global_router
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    from .signals import FaultSignals
    arguments = validate(dict(arguments), 'watchdogNotifyRouter')
    owner = scope['user_id']
    router = global_router(owner, create=False)
    if router['availability'] != 'available':
        raise PermissionError('The owner has no available Global Router to notify')
    signals = FaultSignals(owner)
    signal_ids = list(arguments.get('signal_ids') or [])
    for signal_id in signal_ids:
        if signals.get(signal_id) is None:
            raise ValueError(f'Unknown Watchdog signal {signal_id}')
    failures = signals.open_turn_failures(router['conversationId'])
    if failures:
        failures = _resolve_recovered_router_failures(owner, signals, failures)
    if failures:
        raise PermissionError('The Global Router\u2019s own latest turn failed and that signal is open: '
                              're-check until it has recovered and resolve the signal before notifying it')
    command_id = 'router-watchdog-notice:' + hashlib.sha256(json.dumps(
        [owner, scope['conversation_id'], arguments['idempotency_key'], arguments['message'], signal_ids],
        separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    message = stamp_initiator({'role': 'user',
                               'content': render_router_notice(scope['conversation_id'], arguments['message'],
                                                               signal_ids),
                               'origin': {'initiator': INITIATOR_PEER,
                                          'sourceConversationId': scope['conversation_id']}}, INITIATOR_PEER)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    disposition = dispatch(router['conversationId'], message, {}, user_id=owner, command_id=command_id,
                           log_prefix='[RouterWatchdog]', inject_mode='steer').disposition
    if disposition not in ('started', 'steered', 'queued'):
        raise ValueError(f'The Global Router did not accept the notice ({disposition}); retry unchanged later')
    return {'router_conversation_id': router['conversationId'], 'disposition': disposition,
            'execution_started': False}


def _require_owner_request(task: dict, scope: dict) -> None:
    from lib.project_dispatch.delegation import source_for_task
    from lib.project_dispatch.repository import ProjectDispatchRepository
    try:
        source_for_task(ProjectDispatchRepository(scope['user_id'], scope['project_path']), task)
    except (PermissionError, ValueError) as error:
        raise PermissionError('Only the owner\u2019s own message in this conversation can change the '
                              f'review ({error})') from error


def execute_watchdog_tool(task: dict, scope: dict, name: str, arguments: dict) -> dict:
    from .binding import is_watchdog_scope
    if not is_watchdog_scope(scope):
        raise PermissionError('Only the owner\u2019s Watchdog can use this tool')
    owner = scope['user_id']
    if name == 'watchdog_snapshot':
        from .snapshot import health_snapshot
        arguments = validate(dict(arguments), 'watchdogSnapshot')
        resolved = _resolve_own_failures(owner, scope['conversation_id'])
        snapshot = health_snapshot(owner, after_id=arguments.get('after_id'),
                                   signals_after_id=arguments.get('signals_after_id'))
        if resolved:
            snapshot['resolved_own_failures'] = resolved
        return snapshot
    if name == 'watchdog_resolve_signals':
        from .signals import FaultSignals
        arguments = validate(dict(arguments), 'watchdogResolveSignals')
        return {'resolved': FaultSignals(owner).resolve(arguments['signal_ids'], arguments['resolution'])}
    if name == 'watchdog_schedule_recheck':
        from .review import schedule_recheck, watchdog_principal
        return schedule_recheck(owner, scope['conversation_id'], arguments,
                                principal=watchdog_principal(owner, _tenant(task)))
    if name == 'watchdog_message':
        return _message(task, scope, arguments)
    if name == 'watchdog_notify_router':
        return _notify_router(task, scope, arguments)
    if name == 'watchdog_configure_review':
        from .review import configure_review, watchdog_principal
        arguments = validate(dict(arguments), 'watchdogReviewRequest')
        _require_owner_request(task, scope)
        return configure_review(owner, arguments, principal=watchdog_principal(owner, _tenant(task)))
    raise ValueError('Unknown Watchdog tool')


__all__ = ['execute_watchdog_tool', 'render_router_notice']
