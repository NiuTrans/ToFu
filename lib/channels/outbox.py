"""Durable channel outbox (C5): policy at enqueue, ordered per-thread execution.

Responsibility: record every outbound message before any network effect;
decide its policy once at enqueue from the thread's reply policy and the
request's authority (command chat → send; elsewhere only the owner's own
request or a review's auto-reply to a reviewed message sends, other items are
drafts announced in the command chat, ``never`` refuses; C5, C11); audit and
record the owner's decisions on items for other people; label agent text in
third-party threads with ``[Tofu 代发]``; send
per thread in queue order (threads independent; drafts and failures never
block); reconcile an unknown result against recent thread history by the
item's marker or client ID (a notification-robot item by its posted texts)
before any resend; keep the provider message ID for echo suppression. An
item with attachments (C10) is sent as parts, each with its own state; an
accepted file counts as sent only once the thread shows it.

Entry points: ``enqueue``, ``approve``, ``discard``, ``execute``,
``confirm_echo``, ``record_robot_texts``, ``robot_echo``,
``register_executor``/``kick``. The sender port is the
running adapter of the item's channel (``send``, ``history_since``); each
adapter executes only its own channel's items. This module performs no I/O of
its own besides the store.
"""
from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from typing import Any, Protocol

from lib.channels.model import (
    CHANNEL_TRAITS, DISCLOSURE_LABEL, DX_ATTACHMENTS, OUTBOX_AUTHORITIES, ChannelError, ChannelEvent, OutboxItem,
    ThreadKey, parse_event_id,
)
from lib.channels.policy import effective_reply_policy, is_command_chat, owner_uid
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
    'attachment_rejected',
})


# An echo that confirms no item: a send of the bridge's own connection without
# a recorded item, or a notification-robot send (C6), whose item settles by
# the robot API's own answer.
ROBOT_ECHO = '-'


class OutboxSender(Protocol):
    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]: ...

    def history_since(self, thread: ThreadKey, since_ms: int) -> list[ChannelEvent]: ...

    # Channels whose trait ``attachments`` is true (C10): one file as one message,
    # and the thread's messages from a provider message ID on.
    def send_attachment(self, thread: ThreadKey, path: str, *, kind: str,
                        marker: str) -> dict[str, Any]: ...

    def history_from(self, thread: ThreadKey, external_id: str) -> list[ChannelEvent]: ...


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


def _admission(store: ChannelStore, key: ThreadKey, account: dict[str, Any], *, authority: str,
               review: frozenset[str] | None, reply_to: str | None, needs_approval: bool) -> tuple[str, str]:
    """``(state, decided by)`` of a new item (C5, C11); ``decided by`` is '' for a draft.

    The command chat always sends. Elsewhere the thread's reply policy and the
    request's authority decide: only the owner's own request or an auto-reply
    to a reviewed message of that thread skips the owner's approval.
    """
    if is_command_chat(key, account):
        return 'queued', 'command_chat'
    policy = effective_reply_policy(store.thread(key))
    if policy == 'never':
        raise ChannelError('reply_policy_never', f'Thread {key} has reply policy never; nothing was recorded')
    if authority == 'third_party':
        review = review or frozenset()
        if str(key) not in {str(parse_event_id(event_id)[0]) for event_id in review}:
            raise ChannelError('outside_review', f'Thread {key} is not under review; while reviewing other '
                                                 'people\'s messages you may write only to the threads under '
                                                 'review and the command chat. Nothing was recorded')
        if policy == 'notify':
            raise ChannelError('reply_policy_notify', f'Thread {key} is notify-only: tell the owner in the '
                                                      'command chat instead; nothing was recorded')
        if policy == 'auto' and not needs_approval and reply_to in review:
            return 'queued', 'auto_reply'
        return 'draft', ''
    if authority == 'owner' and policy == 'auto' and not needs_approval:
        return 'queued', 'owner_request'
    return 'draft', ''


def enqueue(owner: int, thread: str, text: str, *, idempotency_key: str,
            origin: dict[str, str], reply_to: str | None = None,
            attachments: list[dict[str, Any]] | None = None, authority: str = 'owner',
            review: frozenset[str] | None = None, needs_approval: bool = False,
            store: ChannelStore | None = None) -> OutboxItem:
    """Record one outbound message; ``attachments`` are admitted records (``lib.channels.attachments``).

    ``authority`` is the request's authority (C11, ``lib.channels.assistant``):
    model tools pass their Turn's; owner-side code paths (command-chat
    receipts and answers) keep ``owner``. ``review`` is the event scope of a
    review of other people's messages, ``needs_approval`` keeps an auto-reply
    thread's message a draft. A new draft is announced in the command chat.
    """
    if authority not in OUTBOX_AUTHORITIES:
        raise ValueError(f'unknown outbox authority {authority!r}')
    store = store or ChannelStore(owner)
    key = ThreadKey.parse(thread)
    attachments = [dict(record) for record in attachments or ()]
    if attachments and not CHANNEL_TRAITS[key.channel].get('attachments'):
        raise ChannelError('attachments_unsupported',
                           f'{CHANNEL_TRAITS[key.channel]["label"]} threads take no attachments')
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
    state, decided_by = _admission(store, key, account, authority=authority, review=review,
                                   reply_to=reply_to or None, needs_approval=needs_approval)
    created = now_ms()
    queue_seq = store.next_queue_seq(key) if state == 'queued' else 0
    item = OutboxItem(
        id=ChannelStore.outbox_item_id(store.owner, str(key), idempotency_key),
        thread=str(key), text=text if command else f'{DISCLOSURE_LABEL} {text}',
        idempotency_key=idempotency_key, state=state, origin=dict(origin),
        created_at_ms=created, updated_at_ms=created,
        queued_at_ms=created if state == 'queued' else 0, queue_seq=queue_seq,
        reply_to=reply_to or None, extra={'authoredText': text, 'replyToExternalId': reply_external,
                                          'authority': authority})
    if decided_by:
        item.extra['decision'] = {'kind': 'queued', 'by': decided_by, 'atMs': created,
                                  'turnId': str(origin.get('turnId') or '')}
    if attachments:
        item.extra['attachments'] = attachments
    # Everything for other people is audited; the command chat is the owner's own.
    stored, new = store.create_outbox_item(item, audit=not command)
    from lib.channels.attachments import identities
    if not new and (stored.extra.get('authoredText') != text or stored.reply_to != (reply_to or None)
                    or identities(stored.extra.get('attachments') or []) != identities(attachments)):
        raise ChannelError('idempotency_conflict',
                           'This idempotency_key was already used with different content')
    if stored.state == 'queued':
        kick(store.owner, key.channel)
    elif stored.state == 'draft':
        _announce_draft(store, stored)
    return stored


def _announce_draft(store: ChannelStore, item: OutboxItem) -> None:
    """Tell the owner in a command chat that a draft awaits them, once per draft (C11).

    The notice is not translated (origin ``draft_notice``): it quotes the exact
    text that would be sent. It goes to the draft's own platform's command
    chat, else 大象's; without a live one the draft waits in Settings only.
    """
    from lib.channels.assistant import draft_notice_text, notice_command_chat
    command_chat = notice_command_chat(store, ThreadKey.parse(item.thread).channel)
    if not command_chat:
        return
    title = (store.thread(ThreadKey.parse(item.thread)) or {}).get('title') or ''
    try:
        enqueue(store.owner, command_chat, draft_notice_text(item, title),
                idempotency_key='draft-notice:' + item.id, store=store, authority='nonhuman',
                origin={'kind': 'draft_notice', 'conversationId': str(item.origin.get('conversationId') or ''),
                        'turnId': str(item.origin.get('turnId') or '')})
    except ChannelError as error:
        logger.warning('[Channels] draft notice not enqueued item=%s: %s', item.id, error.code)


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


def _decision(kind: str, by: str, turn_id: str) -> dict[str, Any]:
    return {'kind': kind, 'by': by, 'atMs': now_ms(), 'turnId': str(turn_id or '')}


def approve(owner: int, item_id: str, *, by: str = 'owner_settings', turn_id: str = '',
            store: ChannelStore | None = None) -> OutboxItem:
    """The owner's approval of a draft; ``by``/``turn_id`` record where they gave it (audit)."""
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
        item.extra['decision'] = _decision('approved', by, turn_id)
        return item
    item = _transition(store, item_id, change)
    kick(store.owner, ThreadKey.parse(item.thread).channel)
    return item


def discard(owner: int, item_id: str, *, by: str = 'owner_settings', turn_id: str = '',
            store: ChannelStore | None = None) -> OutboxItem:
    store = store or ChannelStore(owner)

    def change(item):
        if item.state == 'discarded':
            return None
        if item.state not in ('draft', 'queued', 'failed'):
            raise ChannelError('invalid_state',
                               f'Item in state {item.state} may already be sent; it cannot be discarded')
        item.state = 'discarded'
        item.extra['decision'] = _decision('discarded', by, turn_id)
        return item
    return _transition(store, item_id, change)


def confirm_echo(store: ChannelStore, item_id: str, *, external_id: str, client_id: str) -> None:
    """An echo of the owner's own send proves an in-flight item was delivered."""
    if not item_id or item_id == '-':
        return
    if _confirm_part(store, item_id, external_id=external_id, client_id=client_id):
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


def record_robot_texts(store: ChannelStore, item_id: str, texts) -> None:
    """Note the exact texts a robot send of an in-flight item posts, before it posts them.

    The robot's messages reach the owner's connection from another sender and
    carry no marker, so echo suppression and reconciliation match these
    texts. An empty ``texts`` clears them (the item went out as the owner).
    """
    texts = [str(text) for text in texts]

    def change(item):
        if item.state != 'sending':
            return None
        if texts:
            item.extra['robotTexts'] = texts
        else:
            item.extra.pop('robotTexts', None)
        return item
    _transition(store, item_id, change)


def robot_echo(store: ChannelStore, event: ChannelEvent) -> bool:
    """Whether ``event`` is one of the notification robot's own posts in its thread.

    A message ID the robot API returned proves it; while an item is active, so
    does its exact posted text (the push can arrive before the API answers).
    """
    if store.echo_item(event.channel, external_id=event.external_id) is not None:
        return True
    return any(event.text in (item.extra.get('robotTexts') or ()) for item in store.active_outbox(event.thread))


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
    if item.extra.get('attachments'):
        return _send_parts(store, item, sender)

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
    if item.extra.get('attachments'):
        return _reconcile_parts(store, item, sender)
    thread = ThreadKey.parse(item.thread)
    found = None
    history = sender.history_since(thread, item.attempt_at_ms)
    for event in history:
        if event.outbox_marker == item.id or (item.client_id and event.client_id == item.client_id):
            found = event
            break
    robot_texts = item.extra.get('robotTexts') or []
    if found is None and robot_texts:
        # A robot item is sent when every part it posts is in the history (C6).
        owner = owner_uid(store.account(thread.channel))
        posted = {event.text: event for event in history if event.sender_id != owner}
        if all(text in posted for text in robot_texts):
            found = posted[robot_texts[-1]]
            for text in robot_texts:
                store.remember_echo(thread.channel, ROBOT_ECHO, external_id=posted[text].external_id,
                                    client_id=None)

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


# ── attachments (C10): one item, several messages ────────────────────────────
#
# An item with attachments is a sequence of parts: its text (``_TEXT``) and
# one message per attachment, in ``extra['partOrder']``. Each part keeps its
# own state, so a resumed item never sends a confirmed part again. The text is
# sent when the provider accepts it, as for any item. An attachment is sent
# only once the thread shows it (its echo or the thread history): 大象 can
# accept a file message and still not deliver it, so an accepted part waits
# (``acked``) and is failed, never resent, when the history already holds a
# later message but not it. A part whose send outcome is unknown is looked up
# by its marker and sent again when absent, like an unknown text item.

_TEXT = -1
_PART_SEPARATOR = DX_ATTACHMENTS['partMarker'].split('{item}', 1)[1].split('{part}', 1)[0]
_NOT_SENT = 'not_sent'
_NOT_DELIVERED = 'not_delivered'
_AWAITING = 'awaiting_confirmation'


def part_marker(item_id: str, index: int) -> str:
    """The outbox marker of attachment ``index`` of item ``item_id`` (``x-dxAttachments`` partMarker)."""
    return DX_ATTACHMENTS['partMarker'].format(item=item_id, part=index)


def split_marker(marker: str) -> tuple[str, int | None]:
    """``(item ID, attachment index)``; the index is None for an item's own (text) marker."""
    item_id, separator, part = str(marker or '').partition(_PART_SEPARATOR)
    return item_id, (int(part) if separator and part.isdigit() else None)


def _parts(item: OutboxItem) -> list[dict[str, Any]]:
    return [item.extra.get('textPart') or {'state': 'pending'}, *(item.extra.get('attachments') or ())]


def _unsent(part: dict[str, Any]) -> bool:
    """Not accepted by the provider yet: never attempted, or its outcome is unknown."""
    return part.get('state') == 'pending' and not part.get('acked')


def _blocks(item: OutboxItem) -> bool:
    """Whether an in-flight item holds back later items of its thread.

    Only a part that may still be sent again can reorder the thread; parts
    the provider accepted are never resent, so an item that only awaits their
    confirmation lets the thread move on.
    """
    return not item.extra.get('attachments') or any(_unsent(part) for part in _parts(item))


def _outcome(item: OutboxItem) -> tuple[str, str | None]:
    """Item state and error derived from its parts."""
    parts = _parts(item)
    unsent = [part for part in parts if _unsent(part)]
    if unsent:
        return 'unknown', str(unsent[0].get('error') or 'unknown')
    if any(part.get('state') == 'pending' for part in parts):
        return 'unknown', _AWAITING
    text = parts[0]
    if text.get('state') == 'failed':
        return 'failed', str(text.get('error') or 'failed')
    if any(part.get('state') == 'failed' for part in parts[1:]):
        return 'failed', 'attachment_failed'
    return 'sent', None


def _settle(store: ChannelStore, item_id: str) -> OutboxItem:
    def change(current):
        if current.state not in ('sending', 'unknown'):
            return None
        state, error = _outcome(current)
        if (state, error) == (current.state, current.error):
            return None
        current.state, current.error = state, error
        return current
    item = _transition(store, item_id, change)
    if item.state == 'failed' or (item.state == 'unknown' and _blocks(item)):
        # Failed and unknown sends become Router Watchdog signals (docs/ROUTER.md R7);
        # an item that only awaits confirmation of accepted parts is not a fault yet.
        from lib.router.watchdog.signals import report_outbox_fault
        report_outbox_fault(store.owner, item)
    return item


def _confirm_part(store: ChannelStore, marker: str, *, external_id: str, client_id: str) -> bool:
    """An echo of one part of an item with attachments; False for any other item."""
    item_id, index = split_marker(marker)
    item, _ = store.outbox_item(item_id)
    if item is None or not item.extra.get('attachments'):
        return False

    def change(current):
        if current.state not in ('sending', 'unknown'):
            return None
        if index is None:
            part = dict(current.extra.get('textPart') or {'state': 'pending'})
        elif index < len(current.extra['attachments']):
            part = dict(current.extra['attachments'][index])
        else:
            return None
        if part.get('state') != 'pending':
            return None
        part.update(state='sent', externalId=external_id or part.get('externalId'),
                    clientId=part.get('clientId') or client_id or None, error=None)
        if index is None:
            current.extra['textPart'] = part
            current.external_id = current.external_id or external_id or None
        else:
            current.extra['attachments'][index] = part
        return current
    _transition(store, item_id, change)
    store.remember_echo(ThreadKey.parse(item.thread).channel, marker, external_id=external_id,
                        client_id=client_id)
    if item.state == 'unknown':
        settled = _settle(store, item_id)
        if settled.state != 'unknown' or not _blocks(settled):
            kick(store.owner, ThreadKey.parse(item.thread).channel)
    return True


def _materialize(store: ChannelStore, record: dict[str, Any], directory: str) -> str:
    """The original's verified bytes as a private file named like the attachment."""
    from lib.channels.attachments import read_original
    name = str(record['name'])
    if os.path.basename(name) != name or name in ('', '.', '..'):
        raise ChannelError('attachment_unavailable', f'{name!r} is not a file name')
    data = read_original(record, owner=store.owner)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(data)
    return path


def _digest(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare(store: ChannelStore, item: OutboxItem, scratch: str) -> tuple[OutboxItem, dict[int, str]]:
    """Write the files this attempt sends into ``scratch``; plan the parts on the first attempt.

    The first attempt makes the preview copies (``lib.channels.renditions``),
    places each after its original, fixes the part order and notes every
    preview copy that could not be made; later attempts make the copies again
    only for preview parts still to send.
    """
    from lib.channels.attachments import SIZE_BELOW_BYTES, limits_text
    from lib.channels.renditions import RENDITION_SOURCES, render
    first = 'partOrder' not in item.extra
    records = [dict(record) for record in item.extra['attachments']]
    pending_copies = {record['renditionOf'] for record in records
                      if record.get('renditionOf') is not None and _unsent(record)}
    files: dict[int, str] = {}
    previews = {}
    # Changes this attempt makes to existing records, applied to the stored
    # records at the end: echoes of earlier attempts may confirm parts meanwhile.
    updates: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if record.get('renditionOf') is not None:
            continue
        wants_copies = (first and os.path.splitext(record['name'])[1].lower().lstrip('.') in RENDITION_SOURCES
                        ) or index in pending_copies
        if not (_unsent(record) or wants_copies):
            continue
        try:
            files[index] = _materialize(store, record, os.path.join(scratch, str(index)))
        except ChannelError as error:
            if _unsent(record):
                updates[index] = {'state': 'failed', 'error': error.code}
                record.update(updates[index])
            logger.warning('[Channels] attachment %s of item %s unavailable: %s', index, item.id, error.code)
            continue
        if wants_copies:
            previews[index] = render(record, files[index], os.path.join(scratch, f'{index}-previews'))
    notes: list[str] = []
    if first:
        planned, placed = [], {}
        for index, record in enumerate(records):
            placed[len(planned)] = files.get(index)
            original = len(planned)
            planned.append(record)
            preview = previews.get(index)
            if preview is None:
                continue
            if not preview.files:
                notes.append(f'（{record["name"]} 未附预览副本：{preview.reason}。）')
            for copy in preview.files:
                size = os.path.getsize(copy['path'])
                if size >= SIZE_BELOW_BYTES:
                    notes.append(f'（{copy["name"]} 未发送：{limits_text()}。）')
                    continue
                placed[len(planned)] = copy['path']
                planned.append({'name': copy['name'], 'type': copy['type'], 'size': size,
                                'sha256': _digest(copy['path']), 'kind': copy['kind'], 'renditionOf': original,
                                'source': {'rendition': record['name']}, 'state': 'pending', 'error': None})
        records, files = planned, {index: path for index, path in placed.items() if path}
        indices = list(range(len(records)))
        from lib.channels.policy import robot_thread
        if robot_thread(store.account(ThreadKey.parse(item.thread).channel)) == item.thread:
            order = indices + [_TEXT]  # the robot's text notifies: it comes once the files are there
        else:
            order = [_TEXT] + indices  # the text, with any disclosure label, introduces the files
    else:
        for index, record in enumerate(records):
            source = record.get('renditionOf')
            if source is None or not _unsent(record):
                continue
            copy = next((copy for copy in (previews.get(source).files if previews.get(source) else ())
                         if copy['name'] == record['name']), None)
            if copy is None:
                updates[index] = {'state': 'failed', 'error': 'preview_unavailable'}
            else:
                files[index] = copy['path']
                updates[index] = {'size': os.path.getsize(copy['path']), 'sha256': _digest(copy['path'])}

    def change(current):
        if current.state != 'sending':
            return None
        if first:
            if 'partOrder' in current.extra:
                return None
            # Nothing of this item was sent before its plan, so no echo can race it.
            current.extra['attachments'] = records
            current.extra['partOrder'] = order
            current.extra['attachmentNotes'] = notes
            return current
        stored = current.extra['attachments']
        changed = False
        for index, fields in updates.items():
            if _unsent(stored[index]):
                stored[index] = {**stored[index], **fields}
                changed = True
        return current if changed else None
    return _transition(store, item.id, change), files


def _with_notes(store: ChannelStore, item: OutboxItem, text: str) -> str:
    """The delivery text followed, once and durably, by the item's attachment notes."""
    notes = item.extra.get('attachmentNotes') or []
    if not notes or item.extra.get('attachmentNotesAdded'):
        return text

    def change(current):
        if current.state != 'sending' or current.extra.get('attachmentNotesAdded'):
            return None
        current.text = text.rstrip() + '\n\n' + '\n'.join(notes)
        current.extra['attachmentNotesAdded'] = True
        return current
    return _transition(store, item.id, change).text


def _attempt(call: Callable[[], dict[str, Any]], item_id: str) -> dict[str, Any]:
    try:
        return call()
    except Exception as error:
        logger.warning('[Channels] outbox send outcome unknown item=%s: %s', item_id, type(error).__name__)
        return {'ok': False, 'error': {'code': 'unknown', 'message': type(error).__name__}}


def _record_result(store: ChannelStore, item_id: str, index: int, result: dict[str, Any]) -> OutboxItem:
    """Note one part's send result; an echo that already confirmed the part wins."""
    code = str((result.get('error') or {}).get('code') or '')
    external_id = str(result.get('externalId') or '') or None
    client_id = str(result.get('clientId') or '') or None

    def change(current):
        if current.state != 'sending':
            return None
        part = dict(current.extra.get('textPart') or {'state': 'pending'}) if index == _TEXT \
            else dict(current.extra['attachments'][index])
        if part.get('state') != 'pending' or part.get('acked'):
            return None
        if result.get('ok'):
            # The text is sent once accepted; an attachment once the thread shows it.
            part.update(externalId=external_id, clientId=client_id, error=None)
            part.update({'state': 'sent'} if index == _TEXT else {'acked': True})
        elif code in DEFINITE_SEND_FAILURES:
            part.update(state='failed', error=code)
        else:
            part.update(attempted=True, clientId=client_id or part.get('clientId'), error=code or 'unknown')
        if index == _TEXT:
            current.extra['textPart'] = part
            current.client_id = client_id or current.client_id
            if part['state'] == 'sent':
                current.external_id = external_id
        else:
            current.extra['attachments'][index] = part
        return current
    item = _transition(store, item_id, change)
    if result.get('ok'):
        marker = item_id if index == _TEXT else part_marker(item_id, index)
        store.remember_echo(ThreadKey.parse(item.thread).channel, marker, external_id=external_id,
                            client_id=client_id)
    return item


def _abandon(store: ChannelStore, item_id: str) -> OutboxItem:
    """The text was refused: attachments it should have introduced are not sent."""
    def change(current):
        if current.state != 'sending':
            return None
        current.extra['attachments'] = [{**record, 'state': 'failed', 'error': _NOT_SENT}
                                        if _unsent(record) else record
                                        for record in current.extra['attachments']]
        return current
    return _transition(store, item_id, change)


def _history(sender: OutboxSender, item: OutboxItem) -> tuple[list[ChannelEvent], int | None]:
    """The thread history that can confirm an item's parts, and the first message ID it surely covers.

    Accepted parts carry 大象's own message IDs, so their window is read by ID
    (``history_from``), immune to clock skew, and can prove a part missing.
    Otherwise the attempt time bounds it, as for a text item.
    """
    thread = ThreadKey.parse(item.thread)
    accepted = [int(record['externalId']) for record in item.extra['attachments']
                if record.get('acked') and record.get('state') == 'pending'
                and str(record.get('externalId') or '').isdigit()]
    if accepted:
        return sender.history_from(thread, str(min(accepted))), min(accepted)
    return sender.history_since(thread, item.attempt_at_ms), None


def _apply_history(store: ChannelStore, item_id: str, history: list[ChannelEvent],
                   floor: int | None) -> OutboxItem:
    """Mark the parts the thread history shows; fail accepted parts it proves missing.

    ``history`` holds every message of the thread from ID ``floor`` on (when
    not None), so an accepted part with an ID from ``floor`` on that is absent
    while a later message is present was not delivered.
    """
    if not history:
        return store.outbox_item(item_id)[0]
    thread = ThreadKey.parse(store.outbox_item(item_id)[0].thread)
    owner = owner_uid(store.account(thread.channel))
    newest = max(int(event.external_id) for event in history)
    found: dict[int, ChannelEvent] = {}

    def match(index: int, part: dict[str, Any]) -> ChannelEvent | None:
        marker = item_id if index == _TEXT else part_marker(item_id, index)
        for event in history:
            if (event.outbox_marker == marker
                    or (part.get('clientId') and event.client_id == part['clientId'])
                    or (part.get('externalId') and event.external_id == part['externalId'])):
                return event
        return None

    def change(current):
        if current.state not in ('sending', 'unknown'):
            return None
        changed = False
        text = dict(current.extra.get('textPart') or {'state': 'pending'})
        if text.get('state') == 'pending':
            event = match(_TEXT, text)
            robot_texts = current.extra.get('robotTexts') or []
            posted = {e.text: e for e in history if e.sender_id != owner}
            if event is None and robot_texts and all(t in posted for t in robot_texts):
                event = posted[robot_texts[-1]]  # a robot text is sent when all its parts are there (C6)
                for posted_text in robot_texts:
                    store.remember_echo(thread.channel, ROBOT_ECHO, external_id=posted[posted_text].external_id,
                                        client_id=None)
            if event is not None:
                text.update(state='sent', externalId=event.external_id,
                            clientId=text.get('clientId') or event.client_id or None, error=None)
                current.extra['textPart'] = text
                current.external_id = current.external_id or event.external_id
                found[_TEXT] = event
                changed = True
        for index, record in enumerate(current.extra['attachments']):
            if record.get('state') != 'pending':
                continue
            event = match(index, record)
            if event is not None:
                current.extra['attachments'][index] = {
                    **record, 'state': 'sent', 'externalId': event.external_id,
                    'clientId': record.get('clientId') or event.client_id or None, 'error': None}
                found[index] = event
                changed = True
            elif (record.get('acked') and floor is not None and str(record.get('externalId') or '').isdigit()
                  and floor <= int(record['externalId']) < newest):
                # Accepted, yet a later message is in the history and this one is not.
                current.extra['attachments'][index] = {**record, 'state': 'failed', 'error': _NOT_DELIVERED}
                changed = True
        return current if changed else None
    item = _transition(store, item_id, change)
    for index, event in found.items():
        store.remember_echo(thread.channel, item_id if index == _TEXT else part_marker(item_id, index),
                            external_id=event.external_id, client_id=event.client_id or None)
    return item


def _send_parts(store: ChannelStore, item: OutboxItem, sender: OutboxSender) -> OutboxItem:
    """Send an item's parts in order, each at most once unless its outcome is unknown."""
    def claim(current):
        if current.state != 'queued':
            return None
        current.state, current.attempt_at_ms = 'sending', now_ms()
        return current
    item = _transition(store, item.id, claim)
    if item.state != 'sending':
        return item
    thread = ThreadKey.parse(item.thread)
    with tempfile.TemporaryDirectory(prefix='tofu-outbox-') as scratch:
        item, files = _prepare(store, item, scratch)
        text = _with_notes(store, item, _delivery_text(store, item))
        for index in item.extra['partOrder']:
            item, _ = store.outbox_item(item.id)
            if item.state != 'sending':
                return item
            if index == _TEXT:
                if not _unsent(item.extra.get('textPart') or {'state': 'pending'}):
                    continue
                result = _attempt(lambda: sender.send(
                    thread, text, marker=item.id,
                    reply_to_external_id=item.extra.get('replyToExternalId')), item.id)
                item = _record_result(store, item.id, _TEXT, result)
                part = item.extra.get('textPart') or {}
                if part.get('state') == 'failed':
                    item = _abandon(store, item.id)
                    break
                if part.get('state') != 'sent':
                    break  # unknown: reconcile before anything else of this item
                continue
            record = item.extra['attachments'][index]
            if not _unsent(record) or index not in files:
                continue
            result = _attempt(lambda: sender.send_attachment(
                thread, files[index], kind=record['kind'], marker=part_marker(item.id, index)), item.id)
            item = _record_result(store, item.id, index, result)
            if _unsent(item.extra['attachments'][index]):
                break  # unknown: reconcile before anything else of this item
        else:
            if any(record.get('acked') and record.get('state') == 'pending'
                   for record in item.extra['attachments']):
                try:
                    item = _apply_history(store, item.id, *_history(sender, item))
                except Exception as error:
                    logger.warning('[Channels] attachment confirmation deferred item=%s: %s', item.id,
                                   type(error).__name__)
    return _settle(store, item.id)


def _reconcile_parts(store: ChannelStore, item: OutboxItem, sender: OutboxSender) -> OutboxItem:
    """Find an in-flight item's parts in the thread history; send again only what is absent."""
    item = _apply_history(store, item.id, *_history(sender, item))

    def change(current):
        if current.state not in ('sending', 'unknown') or not _blocks(current):
            return None
        current.state, current.queued_at_ms = 'queued', current.queued_at_ms or now_ms()
        return current
    item = _transition(store, item.id, change)
    return item if item.state == 'queued' else _settle(store, item.id)


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
            if item.state in ('sending', 'unknown') and _blocks(item):
                break  # this thread waits for reconciliation; other threads continue
    return sent


__all__ = ['DEFINITE_SEND_FAILURES', 'OutboxSender', 'ROBOT_ECHO', 'approve', 'confirm_echo', 'discard',
           'enqueue', 'execute', 'kick', 'part_marker', 'record_robot_texts', 'register_executor',
           'robot_echo', 'split_marker']
