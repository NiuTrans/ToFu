"""Model-facing channel tools and their single authority boundary (C3, C5, C7).

Responsibility: decide which channel tools a conversation sees and may call,
then execute them through ``lib.channels.service`` / ``outbox``. Default deny:
every tool requires the owner's Global Router conversation, except
``channel_read`` which a subscriber of the requested thread may also use.
The owner's decisions (``channel_outbox_approve``, ``channel_configure_thread``,
``channel_subscribe``, ``channel_unsubscribe``) additionally require that the
current Turn's input is a genuine human Turn (``require_human_turn``), and a
chat approval names the draft's code in the owner's own words; channel events
never authorize them. A Turn reviewing other people's messages is confined to
the threads under review (C11, ``lib.channels.assistant``).

Entry points: ``visible_tools(owner, conversation_id)`` (catalog builder),
``execute(name, args, task)`` (handler). Schemas: ``contracts/channels_v1``.
"""
from __future__ import annotations

import json
from typing import Any

from lib.channels import outbox, service
from lib.channels.model import CHANNELS, TOOL_NAMES, TOOLS, ChannelError, ThreadKey, parse_event_id
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL_TOOLS = list(TOOLS)
CHANNEL_TOOL_NAMES = TOOL_NAMES
SUBSCRIBER_TOOLS = frozenset({'channel_read'})
# Owner decisions: who receives a thread's messages and what Tofu may send are
# the owner's to change, never a channel message's (C7, C11).
HUMAN_TURN_TOOLS = frozenset({'channel_outbox_approve', 'channel_configure_thread', 'channel_subscribe',
                              'channel_unsubscribe'})
WRITE_TOOLS = frozenset({'channel_send', 'channel_configure_thread', 'channel_subscribe',
                         'channel_unsubscribe', 'channel_outbox_approve'})


def visible_tools(owner: int, conversation_id: str) -> list[dict[str, Any]]:
    """Catalog projection of the execution boundary below (fail closed)."""
    if not owner or not conversation_id:
        return []
    try:
        store = ChannelStore(owner)
        if all(store.account(channel) is None for channel in CHANNELS):
            return []
        from lib.channels.delivery import is_global_router
        if is_global_router(owner, conversation_id):
            return list(CHANNEL_TOOLS)
        if store.subscribed_threads(conversation_id):
            return [tool for tool in CHANNEL_TOOLS if tool['function']['name'] in SUBSCRIBER_TOOLS]
    except Exception:
        logger.warning('[Channels] tool visibility lookup failed; hiding channel tools', exc_info=True)
    return []


def _identity(task: dict[str, Any]) -> tuple[int, str]:
    from lib.tasks_pkg.invocation import is_child_invocation
    from lib.tasks_pkg.manager import task_user_id
    if (is_child_invocation(task) or task.get('_parentTaskId') or task.get('_transientRuntime')
            or not task.get('convId')):
        raise ChannelError('forbidden', 'Channel tools run only in a durable top-level conversation')
    return int(task_user_id(task)), str(task['convId'])


def _require_human_turn(task: dict[str, Any], owner: int, conversation_id: str) -> None:
    """The current Turn answers genuine human input with no channel batch steered in.

    Mirrors the project-dispatch source fence: user principal, the task's own
    assistant Turn, a human parent passing ``require_human_turn``. A channel
    event batch delivered into a running Turn arrives through the steer lane,
    which records only its command ID, so those IDs are checked as well.
    """
    from lib.channels.assistant import steered_records
    from lib.channels.delivery import is_event_command
    from lib.identity import PrincipalContext
    from lib.turn_initiation import require_human_turn
    from lib.turn_lifecycle import get_turn
    refused = ChannelError('human_turn_required',
                           'Only a genuine human request can decide outbox items, configure threads or '
                           'change subscriptions; channel events never authorize this')
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    if principal.kind != 'user' or principal.require_owner() != owner or task.get('_attended') is False:
        raise refused
    turn_id = task.get('_turnId') or (task.get('config') or {}).get('_turnId')
    output = get_turn(conversation_id, turn_id, user_id=owner) if turn_id else None
    if (not isinstance(output, dict) or output.get('conversationId') != conversation_id
            or output.get('turnId') != turn_id or output.get('actor') != 'assistant'):
        raise ChannelError('human_turn_required', 'The current Turn could not be verified')
    human = get_turn(conversation_id, output.get('parentTurnId'), user_id=owner) \
        if output.get('parentTurnId') else None
    try:
        require_human_turn(human)
    except ValueError as error:
        raise refused from error
    if human.get('conversationId') != conversation_id:
        raise refused
    if any(is_event_command(record.get('commandId')) for record in steered_records(task, output)):
        raise ChannelError('human_turn_required',
                           'Channel events were merged into this Turn; ask the owner to repeat the '
                           'decision in a new message')


def _outside_review(what: str) -> ChannelError:
    return ChannelError('outside_review', f'While reviewing other people\'s messages you may {what}; '
                                          'anything else needs the owner (ask in the command chat)')


def _requested_thread(args: dict[str, Any]) -> str | None:
    if args.get('thread'):
        return str(ThreadKey.parse(args['thread']))
    if args.get('event_id'):
        return str(parse_event_id(args['event_id'])[0])
    return None


def _confine_review(name: str, args: dict[str, Any], authority, owner: int) -> None:
    """A review Turn reads and writes only the threads under review, plus the owner's command chats."""
    from lib.channels.assistant import REVIEW_TOOLS, command_chats
    if name not in REVIEW_TOOLS:
        raise _outside_review('read and draft for the threads under review and tell the owner')
    if name == 'channel_read':
        if _requested_thread(args) not in authority.threads:
            raise _outside_review('read only the threads under review')
        return
    allowed = authority.threads | set(command_chats(ChannelStore(owner)).values())
    if name == 'channel_outbox':
        thread = _requested_thread(args)
        if args.get('item_id'):
            item, _ = ChannelStore(owner).outbox_item(str(args['item_id']))
            thread = item.thread if item is not None else thread
        if thread is not None and thread not in allowed:
            raise _outside_review('read the outbox of the threads under review and the command chat')
        return
    if name == 'channel_send':
        if args.get('attachments') is not None:
            raise ChannelError('attachments_not_allowed',
                               'A reply drafted while reviewing other people\'s messages carries no files; '
                               'the owner attaches files in their own request')
        if _requested_thread(args) not in allowed:
            raise _outside_review('write only to the threads under review and the command chat')


def _authorize(name: str, args: dict[str, Any], task: dict[str, Any]):
    """Return ``(owner, conversation_id, router, authority)``; authority is None for subscribers."""
    if name not in CHANNEL_TOOL_NAMES:
        raise ChannelError('unknown_tool', name)
    owner, conversation_id = _identity(task)
    from lib.channels.delivery import is_global_router
    router = is_global_router(owner, conversation_id)
    if not router:
        if name not in SUBSCRIBER_TOOLS:
            raise ChannelError('forbidden', 'Channel tools are available only in the Global Router')
        thread = args.get('thread') or (parse_event_id(args['event_id'])[0] if args.get('event_id') else None)
        if thread is None or conversation_id not in (
                (ChannelStore(owner).thread(ThreadKey.parse(str(thread))) or {}).get('subscribers') or []):
            raise ChannelError('forbidden', 'Subscribers may read only the threads they subscribe to')
        return owner, conversation_id, router, None
    from lib.channels.assistant import turn_authority
    authority = turn_authority(task, owner, conversation_id)
    if name in HUMAN_TURN_TOOLS:
        _require_human_turn(task, owner, conversation_id)
    if authority.reviewing:
        _confine_review(name, args, authority, owner)
    if name == 'channel_outbox_approve':
        from lib.channels.assistant import require_owner_named
        require_owner_named(authority, str(args.get('item_id') or ''))
    return owner, conversation_id, router, authority


def _event_view(record: dict[str, Any]) -> dict[str, Any]:
    keys = ('eventId', 'thread', 'senderId', 'senderName', 'text', 'sentAtMs', 'messageType',
            'disposition', 'replyToExternalId', 'mentionsAll', 'mentionedIds', 'url')
    # Media records hold the outcome and attachment ref, never the provider URL (C9).
    return {key: record.get(key) for key in keys} | {'media': record.get('media') or [],
                                                     'deliveries': record.get('deliveries') or []}


def _read(owner: int, args: dict[str, Any]) -> dict[str, Any]:
    store = ChannelStore(owner)
    if args.get('event_id'):
        thread, external_id = parse_event_id(args['event_id'])
        record = store.event(thread, external_id)
        if record is None:
            raise ChannelError('not_found', 'Event is not stored (ignored threads and dropped '
                                            'messages keep no events)')
        return {'event': _event_view(record)}
    if not args.get('thread'):
        raise ChannelError('invalid_arguments', 'Provide thread or event_id')
    thread = ThreadKey.parse(args['thread'])
    events, following = store.event_page(thread, before=str(args.get('before') or ''))
    return {'thread': service.thread_view(store.thread(thread) or {'key': str(thread)},
                                          store.account(thread.channel)),
            'events': [_event_view(record) for record in events], 'next_before': following}


def execute(name: str, args: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    owner, conversation_id, _, authority = _authorize(name, args or {}, task)
    args = args or {}
    turn_id = str(task.get('_turnId') or (task.get('config') or {}).get('_turnId') or '')
    if name == 'channel_threads':
        page = service.thread_page(owner, cursor=str(args.get('cursor') or ''), mode=args.get('mode'),
                                   channel=args.get('channel') or None)
        return {'threads': page['items'], 'next_cursor': page['nextCursor']}
    if name == 'channel_read':
        return _read(owner, args)
    if name == 'channel_send':
        files = []
        if args.get('attachments') is not None:
            from lib.channels import attachments
            from lib.channels.model import CHANNEL_TRAITS
            channel = ThreadKey.parse(args.get('thread')).channel
            if not CHANNEL_TRAITS[channel].get('attachments'):
                raise ChannelError('attachments_unsupported',
                                   f'{CHANNEL_TRAITS[channel]["label"]} threads take no attachments')
            files = attachments.from_tool(task, owner, conversation_id, args['attachments'])
        item = outbox.enqueue(
            owner, args.get('thread'), args.get('text'), idempotency_key=args.get('idempotency_key'),
            reply_to=args.get('reply_to'), attachments=files,
            origin={'kind': 'tool', 'conversationId': conversation_id, 'turnId': turn_id},
            authority=authority.kind, review=authority.events if authority.reviewing else None,
            needs_approval=args.get('needs_approval') is True)
        result = {'item': service.outbox_item_view(owner, item.id),
                  'note': 'queued/draft is not proof of delivery; read channel_outbox for the outcome'}
        if files:
            from lib.channels.renditions import preview_plan
            result['previews'] = preview_plan(files)
        return result
    if name == 'channel_configure_thread':
        if 'mode' not in args and 'reply_policy' not in args:
            raise ChannelError('invalid_arguments', 'Provide mode and/or reply_policy')
        kwargs = {}
        if 'mode' in args:
            kwargs['mode'] = args['mode']
        if 'reply_policy' in args:
            kwargs['reply_policy'] = args['reply_policy']
        return {'thread': service.configure_thread(owner, args.get('thread'), **kwargs)}
    if name == 'channel_subscribe':
        return {'thread': service.subscribe(owner, args.get('thread'), args.get('conversation_id'))}
    if name == 'channel_unsubscribe':
        return {'thread': service.unsubscribe(owner, args.get('thread'), args.get('conversation_id'))}
    if name == 'channel_outbox':
        if args.get('item_id'):
            return {'item': service.outbox_item_view(owner, args['item_id'])}
        items = service.outbox_page(owner, thread=args.get('thread'))['items']
        if authority is not None and authority.reviewing:
            from lib.channels.assistant import command_chats
            allowed = authority.threads | set(command_chats(ChannelStore(owner)).values())
            items = [item for item in items if item['thread'] in allowed]
        return {'items': items}
    if name == 'channel_outbox_approve':
        return {'item': service.decide_outbox(owner, args.get('item_id'), args.get('decision'),
                                              by='owner_message', turn_id=turn_id)}
    raise ChannelError('unknown_tool', name)


def execute_json(name: str, args: dict[str, Any], task: dict[str, Any]) -> str:
    """Handler adapter: typed refusals become actionable tool results."""
    from lib.storage.errors import StorageError
    try:
        return json.dumps(execute(name, args, task), ensure_ascii=False)
    except ChannelError as error:
        return json.dumps({'error': str(error), 'code': error.code, 'executed': False},
                          ensure_ascii=False)
    except StorageError as error:
        return json.dumps({'error': 'Channel storage is unavailable; retry with the same arguments',
                           'code': getattr(error, 'code', 'storage_error'), 'executed': False},
                          ensure_ascii=False)


__all__ = ['CHANNEL_TOOLS', 'CHANNEL_TOOL_NAMES', 'HUMAN_TURN_TOOLS', 'SUBSCRIBER_TOOLS',
           'WRITE_TOOLS', 'execute', 'execute_json', 'visible_tools']
