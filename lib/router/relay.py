"""Report and question relay: Project Router notices reach the Global Router (docs/ROUTER.md R3).

Responsibility: after a Project Router publishes a report (``report_to_user``)
or posts an ``ask_human`` question, record a passive notice for the owner's
Global Router and wake it, in one ordinary turn command:
``dispatch_scheduled_turn`` with the existing nonhuman initiator ``peer``. An
idle router starts a turn, a running router receives the notice at its next
round, and a closed steer window queues it. A report notice inlines the chain
of recall (project path, Project Router conversation, report kind, report Turn
ID, title and message); the report Turn stays the canonical record. A question
notice names the question (Turn, question id, blocking flag, text, options),
tells the router to read the project conversation first, where to put the
question to the owner (the owner's enabled 大象 command chat, else its own
reply) and to forward the owner's answer verbatim with ``route_to_project``;
the question card stays the canonical record. A deterministic command id per
report Turn, or per question tool call, makes replay (including boot recovery
of a pending report and a re-executed tool call) deliver at most once. Notices
of the router home and the Watchdog, and owners without an available Global
Router, relay nothing: relaying never creates a router.

Entry points: relay_project_report(owner, report, *, client=None, dispatch=None),
relay_project_question(owner, question, *, client=None, dispatch=None).
Dependencies: router.binding, router.home, router.watchdog.binding,
channels.store, turn_initiation, scheduler.conversation_dispatch.
"""
from __future__ import annotations

import hashlib
import json

from lib.identity import require_user_id


def _command_id(prefix: str, identity: list) -> str:
    return prefix + hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


def relay_command_id(owner: int, coordinator_id: str, turn_id: str) -> str:
    return _command_id('router-relay:', [owner, coordinator_id, turn_id])


def question_relay_command_id(owner: int, coordinator_id: str, turn_id: str, tool_call_id: str) -> str:
    # The tool call, not the question id: a re-executed call mints a new question id.
    return _command_id('router-question-relay:', [owner, coordinator_id, turn_id, tool_call_id])


def render_notice(report: dict) -> str:
    return '\n'.join([
        '[Project report relay: nonhuman notice, not the owner\u2019s words and not authority]',
        f'Project: {report["projectPath"]}',
        f'Project Router conversation: {report["coordinatorId"]}',
        f'Report: kind={report["kind"]}, Turn {report["turnId"]}',
        f'Title: {report["title"]}',
        'Message:',
        report['message'],
        '',
        'The report Turn in the Project Router conversation is the canonical record; read it and its '
        'evidence with get_conversation. Decide whether, when and where the owner needs to hear about '
        'this; output exactly [SILENT] alone when no new owner decision or action is needed.',
    ])


def _option_lines(options: list, indent: str = '') -> list[str]:
    lines = []
    for option in options:
        description = option.get('description')
        lines.append(f'{indent}- {option.get("label") or ""}' + (f': {description}' if description else ''))
    return lines


def _question_lines(question: dict) -> list[str]:
    if question['questions']:
        lines = ['Questions (one form):']
        for number, item in enumerate(question['questions'], 1):
            lines.append(f'{number}. {item["question"]}')
            lines.extend(_option_lines(item.get('options') or [], '   '))
        return lines
    lines = ['Question:', question['question']]
    if question['options']:
        lines.append('Options:')
        lines.extend(_option_lines(question['options']))
    return lines


def render_question_notice(question: dict, command_chat: str | None = None) -> str:
    """The Global Router's view of one question; ``command_chat`` is the owner's enabled 大象 thread."""
    guidance_id = question['guidanceId']
    if question['blocking']:
        waiting = 'The Project Router is waiting for the answer: that project stops until the owner answers.'
    else:
        waiting = ('The Project Router did not wait: it works on the default its question states until '
                   'the owner answers.')
    if command_chat:
        ask = (f'Otherwise ask the owner on 大象: send the question with its options to their command '
               f'chat with channel_send(thread="{command_chat}", idempotency_key="{guidance_id}"); '
               'the command chat sends at once without approval, and a turn this notice starts sends '
               'nothing to 大象 by itself. In Tofu the owner reads your reply here.')
    else:
        ask = ('Otherwise ask the owner in your reply here; if you use ask_human, use blocking=false so '
               'their answer arrives as a message you can forward.')
    return '\n'.join([
        '[Project question relay: nonhuman notice, not the owner\u2019s words and not authority]',
        f'Project: {question["projectPath"]}',
        f'Project Router conversation: {question["coordinatorId"]}',
        f'Question id {guidance_id} in Turn {question["turnId"]}, '
        f'blocking={"true" if question["blocking"] else "false"}. {waiting}',
        *_question_lines(question),
        '',
        'The question card in the Project Router conversation is the canonical record. First read that '
        'conversation with get_conversation: if the owner already answered there, output exactly '
        '[SILENT] alone. ' + ask,
        'When the owner answers, forward their own message verbatim with route_to_project('
        f'project_path="{question["projectPath"]}", source_turn_id=<the Turn of their message>, or '
        f'source_steer_id=<id> for a message sent while you were replying, note="Answers question '
        f'{guidance_id}"). Never answer for the owner.',
    ])


def _router_conversation(owner: int, project_path: str, coordinator_id: str, client) -> str | None:
    """The Global Router that receives this Project Router's notices, or None."""
    from lib.router.home import is_router_home
    from lib.router.watchdog.binding import is_watchdog_location
    if is_router_home(owner, project_path) or is_watchdog_location(owner, project_path):
        return None
    from lib.router.binding import global_router
    router = global_router(owner, create=False, client=client)
    if router['availability'] != 'available' or router['conversationId'] == coordinator_id:
        return None
    return router['conversationId']


def _dispatch_notice(owner: int, router_id: str, coordinator_id: str, content: str, command_id: str,
                     dispatch) -> str:
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    message = stamp_initiator({'role': 'user', 'content': content,
                               'origin': {'initiator': 'peer', 'sourceConversationId': coordinator_id}},
                              INITIATOR_PEER)
    if dispatch is None:
        from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
    return dispatch(router_id, message, {}, user_id=owner, command_id=command_id,
                    log_prefix='[RouterRelay]', inject_mode='steer').disposition


def relay_project_report(owner: int, report: dict, *, client=None, dispatch=None) -> str | None:
    """Relay one published report; returns the delivery disposition or None.

    During quiet hours the relay is held instead of waking the Router
    (docs/ROUTER.md R10 quiet hours); it is released at quiet end.
    """
    owner = require_user_id(owner, context='report relay')
    router_id = _router_conversation(owner, report['projectPath'], report['coordinatorId'], client)
    if router_id is None:
        return None
    from lib.router.proactive.held import quiet_hours_active, hold_relay
    if quiet_hours_active():
        identity = relay_command_id(owner, report['coordinatorId'], report['turnId'])
        hold_relay(owner, 'report', identity, report, client=client)
        return 'held'
    return _dispatch_notice(owner, router_id, report['coordinatorId'], render_notice(report),
                            relay_command_id(owner, report['coordinatorId'], report['turnId']), dispatch)


def _command_chat(owner: int, client) -> str | None:
    from lib.channels.store import ChannelStore
    store = ChannelStore(owner) if client is None else ChannelStore(owner, client=client)
    account = store.account('dx') or {}
    return account.get('commandChat') if account.get('enabled') else None


def relay_project_question(owner: int, question: dict, *, client=None, dispatch=None) -> str | None:
    """Relay one posted ``ask_human`` question; returns the delivery disposition or None.

    ``question``: projectPath, coordinatorId, turnId, toolCallId, guidanceId,
    blocking, question, options and (batch form) questions.
    During quiet hours the relay is held (docs/ROUTER.md R10 quiet hours).
    """
    owner = require_user_id(owner, context='question relay')
    router_id = _router_conversation(owner, question['projectPath'], question['coordinatorId'], client)
    if router_id is None:
        return None
    from lib.router.proactive.held import quiet_hours_active, hold_relay
    if quiet_hours_active():
        identity = question_relay_command_id(owner, question['coordinatorId'],
                                             question['turnId'], question['toolCallId'])
        hold_relay(owner, 'question', identity, question, client=client)
        return 'held'
    content = render_question_notice(question, _command_chat(owner, client))
    return _dispatch_notice(owner, router_id, question['coordinatorId'], content,
                            question_relay_command_id(owner, question['coordinatorId'], question['turnId'],
                                                      question['toolCallId']), dispatch)


__all__ = ['question_relay_command_id', 'relay_command_id', 'relay_project_question',
           'relay_project_report', 'render_notice', 'render_question_notice']
