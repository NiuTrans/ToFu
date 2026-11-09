"""Owner-scoped durable channel state over the portable record store.

Responsibility: accounts, threads (mode, reply policy, subscribers, ingress
watermark), recorded events, subscription edges and the outbox, all in the
``channels.v1`` record namespace. Every key starts with ``u<owner>/`` so the
SQL authority (global keys) and TofuDB (session-owner keys) both stay
owner-scoped. Durable user state: no TTL, no pruning; ignored threads keep
metadata only.

Entry point: ``ChannelStore(owner)``. Dependencies: ``lib.storage`` record
operations (``record.get/put/list/delete`` with CAS on ``expected_version``).
Keys hold only lowercase ASCII (digits, hex, fixed words and lowercase GitHub
``owner/repo#number`` thread keys), because SQLite's prefix ``LIKE`` is
case-insensitive.
"""
from __future__ import annotations

import copy
import hashlib
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from lib.channels.model import (
    LIMITS, NAMESPACE, ChannelChange, ChannelError, ChannelEvent, OutboxItem, ThreadKey, order_key,
)
from lib.identity import require_user_id
from lib.agent_core.storage_errors import StorageError

# Storage authority page limit for record.list (contract of record.list).
_RECORD_LIST_MAX = 1000
# ASCII sentinel above every digit/hex key character, for newest-first pages.
_KEY_END = '~'


def now_ms() -> int:
    return int(time.time() * 1000)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


def is_conflict(error: BaseException) -> bool:
    return isinstance(error, StorageError) and error.code == 'database_conflict'


class ChannelStore:
    """Repository for one owner's channel state; stateless between calls."""

    def __init__(self, owner: int, *, client: Callable[..., Any] | None = None):
        self.owner = require_user_id(owner, context='channel store owner')
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self._client = client
        self._root = f'u{self.owner}/'

    # ── record primitives ─────────────────────────────────────────────
    def _get(self, key: str) -> tuple[dict[str, Any] | None, int]:
        row = self._client().query('record.get', {'namespace': NAMESPACE, 'key': key})
        if not isinstance(row, dict):
            return None, 0
        value = row.get('value')
        return (copy.deepcopy(value) if isinstance(value, dict) else None), int(row.get('version') or 0)

    def _put(self, key: str, value: dict[str, Any], expected_version: int) -> int:
        receipt = self._client(write=True).command(
            'record.put', {'namespace': NAMESPACE, 'key': key, 'value': value,
                           'expected_version': expected_version},
            'channels:' + uuid.uuid4().hex)
        return int((receipt or {}).get('version') or expected_version + 1)

    def _delete(self, key: str, expected_version: int) -> None:
        self._client(write=True).command(
            'record.delete', {'namespace': NAMESPACE, 'key': key,
                              'expected_version': expected_version},
            'channels:' + uuid.uuid4().hex)

    def _list(self, prefix: str, *, after: str = '', before: str = '',
              limit: int = _RECORD_LIST_MAX, include_value: bool = True) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {'namespace': NAMESPACE, 'prefix': prefix, 'limit': limit,
                                   'include_value': include_value}
        if after:
            payload['after_key'] = after
        if before:
            payload['before_key'] = before
        rows = self._client().query('record.list', payload)
        return [row for row in rows or [] if isinstance(row, dict)]

    def _scan(self, prefix: str) -> Iterator[dict[str, Any]]:
        """Every row under ``prefix`` in key order, paging at the authority limit."""
        after = ''
        while True:
            rows = self._list(prefix, after=after)
            yield from rows
            if len(rows) < _RECORD_LIST_MAX:
                return
            after = rows[-1]['key']

    def _update(self, key: str, change: Callable[[dict[str, Any] | None], dict[str, Any] | None]
                ) -> dict[str, Any] | None:
        """Read-modify-CAS until one write wins; a conflict means another writer progressed.

        ``change`` returns the new value, or the unchanged input to skip the write.
        """
        while True:
            current, version = self._get(key)
            value = change(copy.deepcopy(current))
            if value is None or value == current:
                return current
            try:
                self._put(key, value, version)
                return value
            except StorageError as error:
                if not is_conflict(error):
                    raise

    # ── keys ──────────────────────────────────────────────────────────
    def _account_key(self, channel: str) -> str:
        return f'{self._root}account/{channel}'

    def _thread_key(self, thread: ThreadKey | str) -> str:
        return f'{self._root}thread/{thread}'

    def _event_prefix(self, thread: ThreadKey | str) -> str:
        return f'{self._root}event/{thread}/'

    def _event_key(self, thread: ThreadKey | str, external_id: str) -> str:
        return self._event_prefix(thread) + order_key(external_id)

    def _subscription_prefix(self, conversation_id: str) -> str:
        return f'{self._root}subscription/{_digest(conversation_id)}/'

    def _outbox_key(self, item_id: str) -> str:
        return f'{self._root}outbox/{item_id}'

    def _active_prefix(self, thread: ThreadKey | str | None = None, channel: str | None = None) -> str:
        if thread is not None:
            return f'{self._root}outbox-active/{thread}/'
        return f'{self._root}outbox-active/' + (f'{channel}:' if channel else '')

    def _echo_key(self, channel: str, kind: str, value: str) -> str:
        return f'{self._root}echo/{channel}/{kind}/{_digest(value.lower())}'

    def _audit_prefix(self, channel: str) -> str:
        return f'{self._root}audit/{channel}/'

    def _audit_key(self, item: OutboxItem) -> str:
        return self._audit_prefix(ThreadKey.parse(item.thread).channel) + f'{item.created_at_ms:020d}-{item.id}'

    def _batch_key(self, command_id: str) -> str:
        return f'{self._root}batch/{_digest(command_id)}'

    # ── account ───────────────────────────────────────────────────────
    def account(self, channel: str) -> dict[str, Any] | None:
        return self._get(self._account_key(channel))[0]

    def accounts(self) -> list[dict[str, Any]]:
        """Every channel account of this owner, in one list query."""
        return [row['value'] for row in self._list(f'{self._root}account/') if isinstance(row.get('value'), dict)]

    def update_account(self, channel: str,
                       change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        def apply(current):
            base = current or {'channel': channel, 'enabled': False, 'identity': None,
                               'commandChat': None, 'frontierAtMs': None,
                               'status': {'state': 'disabled', 'detail': '',
                                          'updatedAtMs': now_ms(), 'connectedAtMs': None}}
            return change(base)
        return self._update(self._account_key(channel), apply)

    def set_status(self, channel: str, state: str, detail: str = '', *,
                   connected_at_ms: int | None = None,
                   when: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any]:
        """Record a status; ``when`` (atomic with the write) may refuse a stale writer."""
        def change(account):
            if when is not None and not when(account):
                return account
            status = dict(account.get('status') or {})
            if (status.get('state') == state and status.get('detail') == detail
                    and connected_at_ms is None):
                return account  # unchanged: keep the time this state began
            status.update(state=state, detail=detail, updatedAtMs=now_ms())
            if connected_at_ms is not None:
                status['connectedAtMs'] = connected_at_ms
            status.setdefault('connectedAtMs', None)
            account['status'] = status
            return account
        return self.update_account(channel, change)

    # ── threads ───────────────────────────────────────────────────────
    def thread(self, thread: ThreadKey) -> dict[str, Any] | None:
        return self._get(self._thread_key(thread))[0]

    def update_thread(self, thread: ThreadKey,
                      change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        def apply(current):
            base = current or {'key': str(thread), 'kind': thread.kind, 'title': '',
                               'mode': None, 'replyPolicy': None, 'subscribers': [],
                               'watermark': None, 'sinceMs': None, 'lastEventAtMs': None}
            return change(base)
        return self._update(self._thread_key(thread), apply)

    def thread_page(self, *, after: str = '', channel: str | None = None
                    ) -> tuple[list[dict[str, Any]], str | None]:
        """Threads in key order, optionally of one channel; ``after`` is the previous cursor."""
        prefix = f'{self._root}thread/'
        scope = prefix + (f'{channel}:' if channel else '')
        limit = LIMITS['threadPageItems']
        start = prefix + after if after else ''
        if start and not start.startswith(scope):
            raise ChannelError('invalid_cursor', 'The cursor belongs to another channel')
        rows = self._list(scope, after=start, limit=limit)
        items = [row['value'] for row in rows if isinstance(row.get('value'), dict)]
        cursor = rows[-1]['key'][len(prefix):] if len(rows) == limit else None
        return items, cursor

    def threads(self, channel: str) -> Iterator[dict[str, Any]]:
        """Every thread record of ``channel`` (adapters scan their watched threads)."""
        for row in self._scan(f'{self._root}thread/{channel}:'):
            if isinstance(row.get('value'), dict):
                yield row['value']

    # ── events ────────────────────────────────────────────────────────
    def event(self, thread: ThreadKey, external_id: str) -> dict[str, Any] | None:
        return self._get(self._event_key(thread, external_id))[0]

    def claim_event(self, event: ChannelEvent, disposition: str) -> tuple[dict[str, Any], bool]:
        """Record an event once (CAS create). Returns ``(record, newly_recorded)``.

        An existing record is a duplicate (replay, reconnect, at-least-once
        delivery); its ``state`` says whether routing completed and, while
        ``pending``, its ``deliveries`` name the targets that already accepted it.
        """
        key = self._event_key(event.thread, event.external_id)
        record = {**event.to_record(), 'disposition': disposition, 'state': 'pending',
                  'deliveries': [], 'recordedAtMs': now_ms()}
        try:
            self._put(key, record, 0)
            return record, True
        except StorageError as error:
            if not is_conflict(error):
                raise
        existing, _ = self._get(key)
        if existing is None:
            raise StorageError('database_conflict', 'Channel event claim changed concurrently')
        return existing, False

    def record_event_media(self, event: ChannelEvent, media: list[dict[str, Any]]) -> None:
        """Record the resolved media of a claimed event (C9) before it is routed."""
        key = self._event_key(event.thread, event.external_id)

        def change(current):
            if current is None:
                raise ChannelError('event_missing', 'Recorded channel event disappeared')
            current['media'] = media
            return current
        self._update(key, change)

    def record_delivery(self, event: ChannelEvent, delivery: dict[str, Any]) -> list[dict[str, Any]]:
        """Append one accepted ``(event, target)`` delivery; returns the event's deliveries.

        Written as soon as the target accepts, before any other target is
        dispatched, so a re-route after a later failure skips it.
        """
        key = self._event_key(event.thread, event.external_id)

        def change(current):
            if current is None:
                raise ChannelError('event_missing', 'Recorded channel event disappeared')
            current['deliveries'] = [*(current.get('deliveries') or []), delivery]
            return current
        return list(self._update(key, change)['deliveries'])

    def mark_routed(self, event: ChannelEvent) -> None:
        """Routing of the event finished; each accepted delivery is already recorded."""
        key = self._event_key(event.thread, event.external_id)

        def change(current):
            if current is None:
                raise ChannelError('event_missing', 'Recorded channel event disappeared')
            current['state'] = 'routed'
            return current
        self._update(key, change)

    def record_change(self, change: ChannelChange) -> str:
        """Apply an edit or recall to its recorded message; returns the ingest outcome.

        ``recorded``: an edit appended as a revision (the original text stays,
        never a new message), or a recall that marked the message recalled and
        removed its text, revision texts and voice transcripts. ``duplicate``:
        this change was applied before. ``dropped``: nothing recorded to change
        (a message the policy dropped or Tofu never saw, an unresolvable
        target), or an edit of a recalled message.
        """
        if not change.target:
            return 'dropped'
        outcome: list[str] = []

        def apply(current):
            if current is None:
                outcome.append('dropped')
                return None
            revisions = list(current.get('revisions') or [])
            if change.kind == 'recall':
                if current.get('recalled'):
                    outcome.append('duplicate')
                    return current
                current.update(recalled=True, recalledAtMs=change.sent_at_ms or now_ms(),
                               recallId=change.change_id, text='', revisions=[])
                current['media'] = [{**item, 'transcript': ''} for item in current.get('media') or []]
            elif current.get('recalled'):
                outcome.append('dropped')
                return current
            elif any(revision.get('id') == change.change_id for revision in revisions):
                outcome.append('duplicate')
                return current
            else:
                current['revisions'] = [*revisions, {
                    'id': change.change_id, 'text': change.text, 'senderId': change.sender_id,
                    'atMs': change.sent_at_ms or None, 'recordedAtMs': now_ms()}]
            outcome.append('recorded')
            return current
        self._update(self._event_key(change.thread, change.target), apply)
        return outcome[-1]

    def provider_message_alias(self, channel: str, provider_id: str) -> str | None:
        """The external ID ``provider_message_id`` minted for an opaque provider message ID, if any."""
        current, _ = self._get(f'{self._root}msgid/{channel}/{_digest(provider_id)}')
        return str(current['externalId']) if current else None

    def provider_message_id(self, channel: str, provider_id: str, mint: Callable[[], str]) -> str:
        """The external ID of an opaque provider message ID, minted once (CAS create).

        For a provider whose message IDs carry no time (企业微信, C12): ``mint``
        makes a time-ordered external ID at first sight and a redelivery maps
        to the same one, so the event record still decides duplicates (C1).
        """
        key = f'{self._root}msgid/{channel}/{_digest(provider_id)}'
        current, version = self._get(key)
        if current is None:
            minted = mint()
            try:
                self._put(key, {'externalId': minted}, version)
                return minted
            except StorageError as error:
                if not is_conflict(error):
                    raise
            current, _ = self._get(key)
            if current is None:
                raise StorageError('database_conflict', 'Provider message alias changed concurrently')
        return str(current['externalId'])

    def event_page(self, thread: ThreadKey, *, before: str = '') -> tuple[list[dict[str, Any]], str | None]:
        """Newest-first page; ``before`` is the previous page's cursor."""
        prefix = self._event_prefix(thread)
        limit = LIMITS['readPageEvents']
        boundary = prefix + (before if before else _KEY_END)
        rows = self._list(prefix, before=boundary, limit=limit)
        items = [row['value'] for row in rows if isinstance(row.get('value'), dict)]
        cursor = rows[-1]['key'][len(prefix):] if len(rows) == limit else None
        return items, cursor

    # ── subscriptions ─────────────────────────────────────────────────
    def subscribe(self, thread: ThreadKey, conversation_id: str) -> dict[str, Any]:
        record = self.update_thread(thread, lambda value: value if conversation_id in value['subscribers']
                                    else {**value, 'subscribers': sorted({*value['subscribers'], conversation_id})})
        key = self._subscription_prefix(conversation_id) + str(thread)
        current, version = self._get(key)
        if current is None:
            try:
                self._put(key, {'conversationId': conversation_id, 'thread': str(thread),
                                'since': now_ms()}, version)
            except StorageError as error:
                if not is_conflict(error):
                    raise
        return record

    def unsubscribe(self, thread: ThreadKey, conversation_id: str) -> dict[str, Any]:
        record = self.update_thread(thread, lambda value: {
            **value, 'subscribers': [c for c in value['subscribers'] if c != conversation_id]})
        key = self._subscription_prefix(conversation_id) + str(thread)
        current, version = self._get(key)
        if current is not None:
            try:
                self._delete(key, version)
            except StorageError as error:
                if not is_conflict(error):
                    raise
        return record

    def subscribed_threads(self, conversation_id: str) -> list[str]:
        """Reverse edges for one conversation, verified against the thread record."""
        threads = []
        for row in self._scan(self._subscription_prefix(conversation_id)):
            value = row.get('value') or {}
            if value.get('conversationId') != conversation_id:
                continue
            try:
                thread = ThreadKey.parse(value.get('thread'))
            except ChannelError:
                continue
            if conversation_id in ((self.thread(thread) or {}).get('subscribers') or []):
                threads.append(str(thread))
        return threads

    # ── outbox ────────────────────────────────────────────────────────
    @staticmethod
    def outbox_item_id(owner: int, thread: str, idempotency_key: str) -> str:
        return 'ob' + _digest(f'{owner}\n{thread}\n{idempotency_key}')

    def outbox_item(self, item_id: str) -> tuple[OutboxItem | None, int]:
        value, version = self._get(self._outbox_key(item_id))
        return (OutboxItem.from_record(value) if value else None), version

    def create_outbox_item(self, item: OutboxItem, *, audit: bool = False) -> tuple[OutboxItem, bool]:
        """CAS-create; an existing item with the same identity is returned unchanged.

        ``audit`` lists the item in the owner's audit of messages recorded on
        their behalf for other people (C11), in creation order.
        """
        try:
            self._put(self._outbox_key(item.id), item.to_record(), 0)
        except StorageError as error:
            if not is_conflict(error):
                raise
            existing, _ = self.outbox_item(item.id)
            if existing is None:
                raise
            if existing.state not in ('sent', 'discarded'):
                # Heal an item whose index write was lost after its create.
                self._index_active(existing)
            if audit:
                self._index_audit(existing)
            return existing, False
        self._index_active(item)
        if audit:
            self._index_audit(item)
        return item, True

    def _index_audit(self, item: OutboxItem) -> None:
        key = self._audit_key(item)
        current, version = self._get(key)
        if current is None:
            try:
                self._put(key, {'itemId': item.id}, version)
            except StorageError as error:
                if not is_conflict(error):
                    raise

    def audit_page(self, channel: str, *, before: str = '') -> tuple[list[OutboxItem], str | None]:
        """Newest-first page of the audited items of ``channel``; ``before`` is the previous cursor."""
        prefix = self._audit_prefix(channel)
        limit = LIMITS['readPageEvents']
        rows = self._list(prefix, before=prefix + (before if before else _KEY_END), limit=limit)
        items = []
        for row in rows:
            item, _ = self.outbox_item(str((row.get('value') or {}).get('itemId') or ''))
            if item is not None:
                items.append(item)
        cursor = rows[-1]['key'][len(prefix):] if len(rows) == limit else None
        return items, cursor

    # ── delivered batches ─────────────────────────────────────────────
    def record_batch(self, command_id: str, conversation_id: str, event_ids: list[str]) -> None:
        """Remember which events one delivery command carried (C11 review scope).

        A batch steered into a running Turn leaves only its command ID there.
        """
        key = self._batch_key(command_id)
        current, version = self._get(key)
        if current is None:
            try:
                self._put(key, {'commandId': command_id, 'conversationId': conversation_id,
                                'eventIds': list(event_ids)}, version)
            except StorageError as error:
                if not is_conflict(error):
                    raise

    def batch(self, command_id: str) -> dict[str, Any] | None:
        record = self._get(self._batch_key(command_id))[0]
        return record if record and record.get('commandId') == command_id else None

    def save_outbox_item(self, item: OutboxItem, expected_version: int) -> int:
        """CAS write of a state transition; keeps the active index in step."""
        item.updated_at_ms = now_ms()
        version = self._put(self._outbox_key(item.id), item.to_record(), expected_version)
        if item.state in ('sent', 'discarded'):
            self._unindex_active(item)
        else:
            self._index_active(item)
        return version

    def next_queue_seq(self, thread: ThreadKey) -> int:
        """Allocate the thread's next outbox queue position (exact, CAS-ordered)."""
        record = self.update_thread(thread, lambda value: {
            **value, 'outboxSeq': int(value.get('outboxSeq') or 0) + 1})
        return int(record['outboxSeq'])

    def _active_key(self, item: OutboxItem) -> str:
        return self._active_prefix(item.thread) + f'{item.queue_seq:020d}-{item.id}'

    def _index_active(self, item: OutboxItem) -> None:
        # Approval re-queues at approval time; drop any earlier position first.
        for row in self._scan(self._active_prefix(item.thread)):
            if (row.get('value') or {}).get('itemId') == item.id and row['key'] != self._active_key(item):
                self._delete_quietly(row['key'], int(row.get('version') or 0))
        key = self._active_key(item)
        current, version = self._get(key)
        if current is None:
            try:
                self._put(key, {'itemId': item.id}, version)
            except StorageError as error:
                if not is_conflict(error):
                    raise

    def _unindex_active(self, item: OutboxItem) -> None:
        for row in self._scan(self._active_prefix(item.thread)):
            if (row.get('value') or {}).get('itemId') == item.id:
                self._delete_quietly(row['key'], int(row.get('version') or 0))

    def _delete_quietly(self, key: str, version: int) -> None:
        try:
            self._delete(key, version)
        except StorageError as error:
            # A concurrent writer already removed or rewrote this index row.
            if not is_conflict(error):
                raise

    def active_outbox(self, thread: ThreadKey | str | None = None, *,
                      channel: str | None = None) -> list[OutboxItem]:
        """Active items in per-thread queue order (thread key, then queue position)."""
        items, seen = [], set()
        for row in self._scan(self._active_prefix(thread, channel)):
            item_id = (row.get('value') or {}).get('itemId')
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            item, _ = self.outbox_item(item_id)
            if item is not None and item.state not in ('sent', 'discarded'):
                items.append(item)
        return items

    # ── echo suppression ──────────────────────────────────────────────
    def remember_echo(self, channel: str, item_id: str, *, external_id: str | None,
                      client_id: str | None) -> None:
        for kind, value in (('mid', external_id), ('uuid', client_id)):
            if not value:
                continue
            key = self._echo_key(channel, kind, value)
            current, version = self._get(key)
            if current is None:
                try:
                    self._put(key, {'itemId': item_id, 'value': value}, version)
                except StorageError as error:
                    if not is_conflict(error):
                        raise

    def echo_item(self, channel: str, *, external_id: str = '', client_id: str = '') -> str | None:
        for kind, value in (('mid', external_id), ('uuid', client_id)):
            if not value:
                continue
            record, _ = self._get(self._echo_key(channel, kind, value))
            if record and record.get('value', '').lower() == value.lower():
                return str(record.get('itemId') or '')
        return None


__all__ = ['ChannelStore', 'is_conflict', 'now_ms']
