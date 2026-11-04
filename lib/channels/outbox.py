"""Durable channel outbox (C5): policy at enqueue, ordered per-thread execution.

Responsibility: record every outbound message before any network effect;
decide its policy once at enqueue (command chat → send; other threads by reply
policy: ``auto`` sends, ``draft`` waits for owner approval, ``never``
refuses); label agent text in third-party threads with ``[Tofu 代发]``; send
per thread in queue order (threads independent; drafts and failures never
block); reconcile an unknown result against recent thread history by the
item's marker or client ID before any resend; keep the provider message ID
for echo suppression.

Entry points: ``enqueue``, ``approve``, ``discard``, ``execute``,
``confirm_echo``, ``register_executor``/``kick``. The sender port is the
running adapter of the item's channel (``send``, ``history_since``); each
adapter executes only its own channel's items. This module performs no I/O of
its own besides the store.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol

from lib.channels.model import (
    CHANNEL_TRAITS, DISCLOSURE_LABEL, ChannelError, ChannelEvent, OutboxItem, ThreadKey, parse_event_id,
)
from lib.channels.policy import effective_reply_policy, is_command_chat
from lib.channels.store import ChannelStore, is_conflict, now_ms
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

# Provider results that prove nothing was sent; every other failure is unknown.
# GitHub (C8) adds refused credentials and permission or lock refusals; a rate
# limit stays unknown, so the item is reconciled and resent after the limit.
DEFINITE_SEND_FAILURES = frozenset({
    'invalid_input', 'unsupported_target', 'reply_target_not_found',
    'reply_target_lookup_failed', 'sdk_not_found', 'auth_failed', 'forbidden',
})


class OutboxSender(Protocol):
    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]: ...

    def history_since(self, thread: ThreadKey, since_ms: int) -> list[ChannelEvent]: ...


_EXECUTORS: dict[tuple[int, str], Callable[[], None]] = {}
_EXECUTORS_GUARD = threading.Lock()


def register_executor(owner: int, wake: Callable[[], None] | None, *, channel: str) -> None:
    """The running ``channel`` adapter of ``owner`` registers its wake callback (or clears it)."""
    with _EXECUTORS_GUARD:
        if wake is None:
            _EXECUTORS.pop((int(owner), channel), None)
        else:
            _EXECUTORS[(int(owner), channel)] = wake


def kick(owner: int, channel: str | None = None) -> None:
    """Wake this process's executor of ``channel`` (every channel when None).

    Other processes pick work up on their tick.
    """
    with _EXECUTORS_GUARD:
        wakes = [wake for (key_owner, key_channel), wake in _EXECUTORS.items()
                 if key_owner == int(owner) and channel in (None, key_channel)]
    for wake in wakes:
        wake()


def enqueue(owner: int, thread: str, text: str, *, idempotency_key: str,
            origin: dict[str, str], reply_to: str | None = None,
            store: ChannelStore | None = None) -> OutboxItem:
    store = store or ChannelStore(owner)
    key = ThreadKey.parse(thread)
    if not isinstance(text, str) or not text.strip():
        raise ChannelError('invalid_text', 'Text must be non-empty')
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
        raise ChannelError('invalid_idempotency_key', 'An idempotency key of 1..200 characters is required')
    account = store.account(key.channel)
    if not account or not account.get('enabled') or not account.get('identity'):
        raise ChannelError('account_unavailable', 'The channel account is not enabled')
    reply_external = None
    if reply_to:
        if not CHANNEL_TRAITS[key.channel]['quoteReply']:
            raise ChannelError('reply_to_unsupported',
                               f'{CHANNEL_TRAITS[key.channel]["label"]} threads have no quote replies; '
                               'omit reply_to')
        reply_thread, reply_external = parse_event_id(reply_to)
        if reply_thread != key:
            raise ChannelError('invalid_reply_to', 'reply_to must be an event of the same thread')
    command = is_command_chat(key, account)
    if command:
        state = 'queued'
    else:
        policy = effective_reply_policy(store.thread(key))
        if policy == 'never':
            raise ChannelError('reply_policy_never',
                               f'Thread {key} has reply policy never; nothing was recorded')
        state = 'queued' if policy == 'auto' else 'draft'
    created = now_ms()
    queue_seq = store.next_queue_seq(key) if state == 'queued' else 0
    item = OutboxItem(
        id=ChannelStore.outbox_item_id(store.owner, str(key), idempotency_key),
        thread=str(key), text=text if command else f'{DISCLOSURE_LABEL} {text}',
        idempotency_key=idempotency_key, state=state, origin=dict(origin),
        created_at_ms=created, updated_at_ms=created,
        queued_at_ms=created if state == 'queued' else 0, queue_seq=queue_seq,
        reply_to=reply_to or None, extra={'authoredText': text, 'replyToExternalId': reply_external})
    stored, new = store.create_outbox_item(item)
    if not new and (stored.extra.get('authoredText') != text or stored.reply_to != (reply_to or None)):
        raise ChannelError('idempotency_conflict',
                           'This idempotency_key was already used with different content')
    if stored.state == 'queued':
        kick(store.owner, key.channel)
    return stored


def _transition(store: ChannelStore, item_id: str,
                change: Callable[[OutboxItem], OutboxItem | None]) -> OutboxItem:
    while True:
        item, version = store.outbox_item(item_id)
        if item is None:
            raise ChannelError('not_found', 'Outbox item not found')
        updated = change(item)
        if updated is None:
            return item
        try:
            store.save_outbox_item(updated, version)
            return updated
        except StorageError as error:
            if not is_conflict(error):
                raise


def approve(owner: int, item_id: str, *, store: ChannelStore | None = None) -> OutboxItem:
    store = store or ChannelStore(owner)
    position = {}

    def change(item):
        if item.state != 'draft':
            if item.state in ('queued', 'sending', 'unknown', 'sent'):
                return None
            raise ChannelError('invalid_state', f'Only drafts can be approved (state {item.state})')
        if item.thread not in position:
            # Approval places the draft at the end of its thread's queue.
            position[item.thread] = store.next_queue_seq(ThreadKey.parse(item.thread))
        item.state, item.queued_at_ms, item.error = 'queued', now_ms(), None
        item.queue_seq = position[item.thread]
        return item
    item = _transition(store, item_id, change)
    kick(store.owner, ThreadKey.parse(item.thread).channel)
    return item


def discard(owner: int, item_id: str, *, store: ChannelStore | None = None) -> OutboxItem:
    store = store or ChannelStore(owner)

    def change(item):
        if item.state == 'discarded':
            return None
        if item.state not in ('draft', 'queued', 'failed'):
            raise ChannelError('invalid_state',
                               f'Item in state {item.state} may already be sent; it cannot be discarded')
        item.state = 'discarded'
        return item
    return _transition(store, item_id, change)


def confirm_echo(store: ChannelStore, item_id: str, *, external_id: str, client_id: str) -> None:
    """An echo of the owner's own send proves an in-flight item was delivered."""
    if not item_id or item_id == '-':
        return

    def change(item):
        if item.state not in ('sending', 'unknown'):
            return None
        item.state, item.external_id, item.error = 'sent', external_id, None
        item.client_id = item.client_id or client_id or None
        return item
    try:
        item = _transition(store, item_id, change)
    except ChannelError:
        return
    if item.state == 'sent':
        store.remember_echo(item.thread.split(':', 1)[0], item.id, external_id=item.external_id,
                            client_id=item.client_id)


def _delivery_text(store: ChannelStore, item: OutboxItem) -> str:
    """Translate once durably; reconciliation always retries the same provider bytes."""
    if item.origin.get('kind') not in ('router_receipt', 'router_content', 'router_progress',
                                       'router_reply', 'router_status'):
        return item.text
    if item.extra.get('deliveryTextReady'):
        return item.text
    from lib.conv_config import resolve_translate_target
    from lib.conversations.repository import ConversationRepository
    from lib.translate.display_texts import translate_display_texts
    try:
        snapshot = ConversationRepository().get(
            item.origin.get('conversationId', ''), user_id=store.owner, include_messages=False)
        settings = (snapshot.metadata.get('settings') if snapshot else None) or {}
        target = resolve_translate_target(settings)
        authored = str(item.extra.get('authoredText') or item.text)
        translated = translate_display_texts(
            [authored], target, log_tag='channel-outbox:' + item.id).get(authored)
        if not translated:
            translated = item.text
        if item.extra.get('replyToExternalId') is None and not item.text.startswith(authored):
            translated = item.text.replace(authored, translated, 1)
        def change(current):
            if current.state != 'sending':
                return None
            current.text = translated
            current.extra['deliveryTextReady'] = True
            return current
        return _transition(store, item.id, change).text
    except Exception:
        logger.warning('[Channels] router text translation failed item=%s; sending original',
                       item.id, exc_info=True)
        return item.text


def _send(store: ChannelStore, item: OutboxItem, sender: OutboxSender) -> OutboxItem:
    def claim(current):
        if current.state != 'queued':
            return None
        current.state, current.attempt_at_ms = 'sending', now_ms()
        return current
    item = _transition(store, item.id, claim)
    if item.state != 'sending':
        return item
    try:
        result = sender.send(ThreadKey.parse(item.thread), _delivery_text(store, item), marker=item.id,
                             reply_to_external_id=item.extra.get('replyToExternalId'))
    except Exception as error:
        logger.warning('[Channels] outbox send outcome unknown item=%s: %s', item.id,
                       type(error).__name__)
        result = {'ok': False, 'error': {'code': 'unknown', 'message': type(error).__name__}}
    code = str((result.get('error') or {}).get('code') or '')

    def settle(current):
        if current.state != 'sending':
            return None
        current.client_id = str(result.get('clientId') or '') or current.client_id
        if result.get('ok'):
            current.state, current.error = 'sent', None
            current.external_id = str(result.get('externalId') or '') or None
        elif code in DEFINITE_SEND_FAILURES:
            current.state, current.error = 'failed', code
        else:
            current.state, current.error = 'unknown', code or 'unknown'
        return current
    item = _transition(store, item.id, settle)
    if item.state == 'sent':
        store.remember_echo(ThreadKey.parse(item.thread).channel, item.id,
                            external_id=item.external_id, client_id=item.client_id)
    else:
        # Failed and unknown sends become Router Watchdog signals (docs/ROUTER.md R7).
        from lib.router.watchdog.signals import report_outbox_fault
        report_outbox_fault(store.owner, item)
    return item


def _reconcile(store: ChannelStore, item: OutboxItem, sender: OutboxSender) -> OutboxItem:
    """Search thread history since the attempt for the item's marker or client ID."""
    thread = ThreadKey.parse(item.thread)
    found = None
    for event in sender.history_since(thread, item.attempt_at_ms):
        if event.outbox_marker == item.id or (item.client_id and event.client_id == item.client_id):
            found = event
            break

    def change(current):
        if current.state not in ('sending', 'unknown'):
            return None
        if found is not None:
            current.state, current.external_id, current.error = 'sent', found.external_id, None
            current.client_id = current.client_id or found.client_id or None
        else:
            current.state, current.queued_at_ms = 'queued', current.queued_at_ms or now_ms()
        return current
    item = _transition(store, item.id, change)
    if item.state == 'sent':
        store.remember_echo(thread.channel, item.id, external_id=item.external_id,
                            client_id=item.client_id)
    return item


def execute(owner: int, sender: OutboxSender, *, channel: str,
            store: ChannelStore | None = None) -> int:
    """One executor pass over ``channel``'s items; returns how many reached ``sent``."""
    store = store or ChannelStore(owner)
    by_thread: dict[str, list[OutboxItem]] = {}
    for item in store.active_outbox(channel=channel):
        by_thread.setdefault(item.thread, []).append(item)
    sent = 0
    for thread, items in by_thread.items():
        items.sort(key=lambda value: (value.queue_seq, value.id))
        for item in items:
            if item.state in ('draft', 'failed'):
                continue
            if item.state in ('sending', 'unknown'):
                item = _reconcile(store, item, sender)
                if item.state == 'sent':
                    sent += 1
                    continue
            if item.state == 'queued':
                item = _send(store, item, sender)
            if item.state == 'sent':
                sent += 1
                continue
            if item.state in ('sending', 'unknown'):
                break  # this thread waits for reconciliation; other threads continue
    return sent


__all__ = ['DEFINITE_SEND_FAILURES', 'OutboxSender', 'approve', 'confirm_echo', 'discard',
           'enqueue', 'execute', 'kick', 'register_executor']
