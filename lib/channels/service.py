"""Channel application service: owner-facing views and commands.

Responsibility: the one owner-scoped command/query surface shared by the HTTP
routes and the model tools — account configuration (enable resolves the
owner identity with ``whoami``), command-chat selection or creation, thread
listing and configuration, subscriptions, outbox listing and decisions.
Handlers stay stateless; state lives in ``ChannelStore``.

Entry points: ``account_view``, ``update_account``, ``create_command_chat``,
``thread_page``, ``thread_view``, ``configure_thread``, ``subscribe``,
``unsubscribe``, ``outbox_page``, ``decide_outbox``. Dependencies:
``ChannelStore``, ``policy``, ``outbox``, ``delivery`` (router lookup) and the
dx adapter's one-shot launcher commands.
"""
from __future__ import annotations

from typing import Any

from lib.channels import outbox, policy
from lib.channels.model import (
    CHANNELS, CONFIGURABLE_MODES, REPLY_POLICIES, ChannelError, OutboxItem, ThreadKey, session_owns_status,
)
from lib.channels.store import ChannelStore, now_ms

CHANNEL = 'dx'
COMMAND_CHAT_TITLE = 'Tofu Router'
_UNSET = object()


def _store(owner: int, store: ChannelStore | None) -> ChannelStore:
    return store or ChannelStore(owner)


def account_view(owner: int, *, store: ChannelStore | None = None) -> dict[str, Any]:
    """Contract ``ChannelAccountView``."""
    from lib.channels.delivery import RouterUnavailable, global_router_binding
    from lib.channels.dx.adapter import launch_spec
    store = _store(owner, store)
    account = store.account(CHANNEL) or {}
    status = dict(account.get('status') or {})
    try:
        router_id = global_router_binding(store.owner, create=False)['conversationId']
    except RouterUnavailable:
        router_id = None
    return {
        'channel': CHANNEL,
        'enabled': bool(account.get('enabled')),
        'identity': account.get('identity') or None,
        'commandChat': account.get('commandChat') or None,
        'status': {'state': status.get('state') or 'disabled', 'detail': status.get('detail') or '',
                   'updatedAtMs': int(status.get('updatedAtMs') or 0),
                   'connectedAtMs': status.get('connectedAtMs')},
        'skillInstalled': launch_spec(store.owner).installed,
        'routerConversationId': router_id,
    }


def update_account(owner: int, update: dict[str, Any], *, store: ChannelStore | None = None,
                   identity_resolver=None) -> dict[str, Any]:
    """Apply ``ChannelAccountUpdate``; enabling resolves the owner identity first."""
    store = _store(owner, store)
    unknown = set(update) - {'enabled', 'commandChat'}
    if unknown:
        raise ChannelError('invalid_update', f'Unknown fields: {sorted(unknown)}')
    command_chat = _UNSET
    if 'commandChat' in update:
        value = update['commandChat']
        if value is not None and ThreadKey.parse(value).channel != CHANNEL:
            raise ChannelError('invalid_update', 'The command chat must be a 大象 thread')
        command_chat = None if value is None else str(ThreadKey.parse(value))
    enabled = update.get('enabled')
    if enabled is not None and not isinstance(enabled, bool):
        raise ChannelError('invalid_update', 'enabled must be a boolean')
    identity = None
    if enabled:
        if identity_resolver is None:
            from lib.channels.dx.adapter import resolve_identity as identity_resolver
        try:
            identity = identity_resolver(store.owner)
        except ChannelError as error:
            if error.code == 'needs_login':
                store.set_status(CHANNEL, 'needs_login', str(error))
            raise

    def change(account):
        if enabled is True:
            keep_status = session_owns_status(account)
            account['enabled'] = True
            account['identity'] = identity
            if account.get('frontierAtMs') is None:
                # Ingress starts at enable time; earlier history is not replayed.
                account['frontierAtMs'] = now_ms()
            if not keep_status:
                account['status'] = {'state': 'starting', 'detail': '', 'updatedAtMs': now_ms(),
                                     'connectedAtMs': None}
        elif enabled is False:
            account['enabled'] = False
            account['status'] = {'state': 'disabled', 'detail': '', 'updatedAtMs': now_ms(),
                                 'connectedAtMs': None}
        if command_chat is not _UNSET:
            account['commandChat'] = command_chat
        return account
    store.update_account(CHANNEL, change)
    if command_chat not in (_UNSET, None):
        _ensure_thread(store, ThreadKey.parse(command_chat))
    from lib.channels.dx.supervisor import notify_account_changed
    notify_account_changed(store.owner)
    return account_view(store.owner, store=store)


def _ensure_thread(store: ChannelStore, thread: ThreadKey, title: str = '') -> None:
    store.update_thread(thread, lambda value: value if value.get('title') or not title
                        else {**value, 'title': title})


def create_command_chat(owner: int, *, store: ChannelStore | None = None,
                        group_creator=None) -> dict[str, Any]:
    """Create the single-member ``Tofu Router`` group on explicit owner request.

    Idempotent per account: an existing command chat is returned unchanged; a
    creation claim stops a concurrent request from creating a second group.
    """
    store = _store(owner, store)
    account = store.account(CHANNEL) or {}
    if account.get('commandChat'):
        return account_view(store.owner, store=store)
    identity = account.get('identity') or {}
    if not account.get('enabled') or not identity.get('uid'):
        raise ChannelError('account_unavailable', 'Enable the 大象 account before creating a command chat')
    claimed = {'value': False}

    def claim(value):
        if value.get('commandChat') or (value.get('commandChatCreation') or {}).get('state') == 'creating':
            return value
        claimed['value'] = True
        return {**value, 'commandChatCreation': {'state': 'creating', 'startedAtMs': now_ms()}}
    store.update_account(CHANNEL, claim)
    if not claimed['value']:
        raise ChannelError('creation_in_progress', 'A Tofu Router group creation is already in progress')
    if group_creator is None:
        from lib.channels.dx.adapter import create_command_group as group_creator
    try:
        gid = group_creator(store.owner, name=COMMAND_CHAT_TITLE, member_uid=identity['uid'])
    except BaseException:
        store.update_account(CHANNEL, lambda value: {**value, 'commandChatCreation': None})
        raise
    thread = ThreadKey(CHANNEL, 'gid', gid)
    store.update_account(CHANNEL, lambda value: {**value, 'commandChat': str(thread),
                                                 'commandChatCreation': {'state': 'created', 'gid': gid}})
    _ensure_thread(store, thread, COMMAND_CHAT_TITLE)
    return account_view(store.owner, store=store)


def thread_view(record: dict[str, Any], account: dict[str, Any] | None) -> dict[str, Any]:
    """Contract ``ChannelThreadView``."""
    thread = ThreadKey.parse(record['key'])
    return {
        'key': str(thread), 'kind': thread.kind, 'title': record.get('title') or '',
        'mode': policy.effective_mode(thread, record, account),
        'modeOverride': record.get('mode') or None,
        'replyPolicy': policy.effective_reply_policy(record),
        'replyPolicyOverride': record.get('replyPolicy') or None,
        'isCommandChat': policy.is_command_chat(thread, account),
        'subscribers': list(record.get('subscribers') or []),
        'lastEventAtMs': record.get('lastEventAtMs'),
        'watermark': record.get('watermark'),
    }


def thread_page(owner: int, *, cursor: str = '', mode: str | None = None,
                channel: str | None = None, store: ChannelStore | None = None) -> dict[str, Any]:
    """One page of threads, optionally of one ``channel`` (each viewed with its own account)."""
    store = _store(owner, store)
    if channel is not None and channel not in CHANNELS:
        raise ChannelError('invalid_channel', f'channel must be one of {list(CHANNELS)}')
    records, following = store.thread_page(after=cursor, channel=channel)
    accounts: dict[str, dict[str, Any] | None] = {}
    items = []
    for record in records:
        name = ThreadKey.parse(record['key']).channel
        if name not in accounts:
            accounts[name] = store.account(name)
        items.append(thread_view(record, accounts[name]))
    if mode:
        items = [item for item in items if item['mode'] == mode]
    return {'items': items, 'nextCursor': following}


def configure_thread(owner: int, thread: str, *, mode: Any = _UNSET, reply_policy: Any = _UNSET,
                     store: ChannelStore | None = None) -> dict[str, Any]:
    """Owner decision on one thread's mode / reply policy (``None`` = default)."""
    store = _store(owner, store)
    key = ThreadKey.parse(thread)
    if mode is not _UNSET and mode is not None and mode not in CONFIGURABLE_MODES:
        raise ChannelError('invalid_mode', f'mode must be one of {list(CONFIGURABLE_MODES)}')
    if reply_policy is not _UNSET and reply_policy is not None and reply_policy not in REPLY_POLICIES:
        raise ChannelError('invalid_reply_policy', f'reply_policy must be one of {list(REPLY_POLICIES)}')
    account = store.account(key.channel)

    def change(value):
        before = policy.effective_mode(key, value, account)
        if mode is not _UNSET:
            value['mode'] = mode
        if reply_policy is not _UNSET:
            value['replyPolicy'] = reply_policy
        after = policy.effective_mode(key, value, account)
        if before == 'ignore' and after != 'ignore':
            # A newly watched thread starts now; its ignored past is not replayed.
            value['watermark'] = None
            value['sinceMs'] = now_ms()
        if after != before:
            # Adapters that read threads directly in watch (GitHub, C8) start
            # there instead of replaying what the previous mode left unread.
            value['modeChangedAtMs'] = now_ms()
        return value
    return thread_view(store.update_thread(key, change), account)


def subscribe(owner: int, thread: str, conversation_id: str, *,
              store: ChannelStore | None = None) -> dict[str, Any]:
    store = _store(owner, store)
    key = ThreadKey.parse(thread)
    _require_owned_conversation(store.owner, conversation_id)
    return thread_view(store.subscribe(key, conversation_id), store.account(key.channel))


def unsubscribe(owner: int, thread: str, conversation_id: str, *,
                store: ChannelStore | None = None) -> dict[str, Any]:
    store = _store(owner, store)
    key = ThreadKey.parse(thread)
    return thread_view(store.unsubscribe(key, conversation_id), store.account(key.channel))


def _require_owned_conversation(owner: int, conversation_id: str) -> None:
    from lib.conversations.repository import ConversationRepository
    if not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 256:
        raise ChannelError('invalid_conversation', 'A conversation ID is required')
    snapshot = ConversationRepository().get(conversation_id, user_id=owner, include_messages=False)
    settings = (snapshot.metadata.get('settings') if snapshot else None) or {}
    if snapshot is None or settings.get('archived') or settings.get('readOnly'):
        raise ChannelError('conversation_unavailable', 'Subscriber must be an active conversation of this owner')


def _titles(store: ChannelStore, items: list[OutboxItem]) -> dict[str, str]:
    titles = {}
    for item in items:
        if item.thread not in titles:
            titles[item.thread] = (store.thread(ThreadKey.parse(item.thread)) or {}).get('title') or ''
    return titles


def outbox_page(owner: int, *, thread: str | None = None, channel: str | None = None,
                store: ChannelStore | None = None) -> dict[str, Any]:
    """Active outbox items, optionally of one ``thread`` or one ``channel``."""
    store = _store(owner, store)
    if channel is not None and channel not in CHANNELS:
        raise ChannelError('invalid_channel', f'channel must be one of {list(CHANNELS)}')
    items = store.active_outbox(str(ThreadKey.parse(thread)) if thread else None, channel=channel)
    titles = _titles(store, items)
    return {'items': [item.view(titles[item.thread]) for item in items]}


def outbox_item_view(owner: int, item_id: str, *, store: ChannelStore | None = None) -> dict[str, Any]:
    store = _store(owner, store)
    item, _ = store.outbox_item(item_id)
    if item is None:
        raise ChannelError('not_found', 'Outbox item not found')
    return item.view(_titles(store, [item])[item.thread])


def decide_outbox(owner: int, item_id: str, decision: str, *,
                  store: ChannelStore | None = None) -> dict[str, Any]:
    store = _store(owner, store)
    if decision == 'approve':
        item = outbox.approve(store.owner, item_id, store=store)
    elif decision == 'discard':
        item = outbox.discard(store.owner, item_id, store=store)
    else:
        raise ChannelError('invalid_decision', 'decision must be approve or discard')
    return item.view(_titles(store, [item])[item.thread])


__all__ = ['COMMAND_CHAT_TITLE', 'account_view', 'configure_thread', 'create_command_chat',
           'decide_outbox', 'outbox_item_view', 'outbox_page', 'subscribe', 'thread_page',
           'thread_view', 'unsubscribe', 'update_account']
