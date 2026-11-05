"""Channel ingress: record once, then route (C1, C2, C3, C7).

Responsibility: for each normalized event, in order: drop outbox echoes
(confirming the outbox item they prove sent), keep only metadata for ignored
threads, classify by the deterministic thread mode, record the event once per
``(owner, thread, external id)`` with CAS, route newly recorded (or previously
unrouted) events, then advance the thread watermark. Routing: owner messages
in the command chat are human input to the Global Router; delivered
candidates go to the thread's subscribers (passthrough, plus a non-waking
router observation) or, without subscribers, to the Global Router; owner
context goes to subscribers only.

Entry points: ``ingest(owner, event)`` and ``ingest_batch(owner, events)``
(catch-up replays deliver one batch per target). A routing failure raises
before the watermark moves, so the adapter's next session replays the gap and
dedup turns it into duplicates, never losses.
Dependencies: ``ChannelStore``, ``policy``, ``delivery``, ``outbox``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from lib.channels import delivery, policy
from lib.channels.model import ChannelEvent, ThreadKey, external_id_after
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

# TODO(enterprise): ingest is serialized per owner inside one process; several
# scheduler replicas would need a per-account lease before running bridges.
_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _owner_lock(owner: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(int(owner), threading.Lock())


@dataclass
class IngestResult:
    """Outcome per event: recorded, duplicate, echo, ignored, dropped or disabled."""

    event_id: str
    outcome: str
    disposition: str = ''
    deliveries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Unit:
    conversation_id: str
    human: bool
    events: list[tuple[ChannelEvent, str]] = field(default_factory=list)
    observe: tuple[str, list[str]] | None = None


def ingest(owner: int, event: ChannelEvent, *, store: ChannelStore | None = None) -> IngestResult:
    return ingest_batch(owner, [event], store=store)[0]


def ingest_batch(owner: int, events: list[ChannelEvent], *,
                 store: ChannelStore | None = None) -> list[IngestResult]:
    store = store or ChannelStore(owner)
    with _owner_lock(store.owner):
        return _ingest_locked(store, events)


def _ingest_locked(store: ChannelStore, events: list[ChannelEvent]) -> list[IngestResult]:
    from lib.channels import outbox
    results: list[IngestResult] = []
    routable: list[tuple[ChannelEvent, str]] = []
    threads: dict[str, dict[str, Any]] = {}
    advance: dict[str, list[ChannelEvent]] = {}
    accounts: dict[str, dict[str, Any] | None] = {}
    for event in events:
        if event.channel not in accounts:
            accounts[event.channel] = store.account(event.channel)
        account = accounts[event.channel]
        if not account or not account.get('enabled'):
            results.append(IngestResult(event.event_id, 'disabled'))
            continue
        owner_uid = policy.owner_uid(account)
        echo_item = _echo_item(store, event, owner_uid)
        if echo_item:
            outbox.confirm_echo(store, echo_item, external_id=event.external_id,
                                client_id=event.client_id)
            results.append(IngestResult(event.event_id, 'echo'))
            continue
        key = str(event.thread)
        if key not in threads:
            threads[key] = store.thread(event.thread) or {}
        record = threads[key]
        mode = policy.effective_mode(event.thread, record, account)
        record['_effectiveMode'] = mode
        record['_effectiveReplyPolicy'] = policy.effective_reply_policy(record)
        if mode == 'ignore':
            _touch_ignored(store, event, record)
            results.append(IngestResult(event.event_id, 'ignored'))
            continue
        # No watermark short-circuit: a push can arrive after a newer one of
        # the same thread, and only the CAS record proves it was seen.
        disposition = policy.classify(event, mode, owner_uid)
        advance.setdefault(key, []).append(event)
        if disposition == 'drop':
            results.append(IngestResult(event.event_id, 'dropped'))
            continue
        claimed, new = store.claim_event(event, disposition)
        if not new and claimed.get('state') == 'routed':
            # Routed in an earlier session; the watermark may lag a crash.
            results.append(IngestResult(event.event_id, 'duplicate', claimed.get('disposition', '')))
            continue
        # A recorded but unrouted event is a previous session's failed route.
        disposition = claimed.get('disposition') or disposition
        routable.append((event, disposition))
        results.append(IngestResult(event.event_id, 'recorded', disposition))
    if routable:
        deliveries = _route(store, accounts, routable, threads)
        for result in results:
            result.deliveries = deliveries.get(result.event_id, result.deliveries)
    for key, processed in advance.items():
        _advance_watermark(store, processed)
    return results


def _echo_item(store: ChannelStore, event: ChannelEvent, owner_uid: str) -> str | None:
    """Outbox echoes are the owner's own account sending (C2/C7: never input)."""
    if not owner_uid or event.sender_id != owner_uid:
        return None
    if event.outbox_marker:
        item, _ = store.outbox_item(event.outbox_marker)
        if item is not None:
            return item.id
    found = store.echo_item(event.channel, external_id=event.external_id,
                            client_id=event.client_id)
    if found:
        return found
    if event.own_connection:
        return '-'
    return None


def _touch_ignored(store: ChannelStore, event: ChannelEvent, record: dict[str, Any]) -> None:
    """Ignored threads keep metadata only, written when it actually changes."""
    title = event.thread_title or record.get('title') or ''
    if record.get('key') and record.get('title') == title:
        return

    def change(value):
        value['title'] = title
        value['lastEventAtMs'] = max(int(value.get('lastEventAtMs') or 0), event.sent_at_ms)
        return value
    record.update(store.update_thread(event.thread, change))


def _advance_watermark(store: ChannelStore, processed: list[ChannelEvent]) -> None:
    newest = max(processed, key=lambda item: int(item.external_id))
    title = next((e.thread_title for e in reversed(processed) if e.thread_title), '')

    def change(value):
        if external_id_after(newest.external_id, value.get('watermark')):
            value['watermark'] = newest.external_id
        value['lastEventAtMs'] = max(int(value.get('lastEventAtMs') or 0),
                                     max(e.sent_at_ms for e in processed))
        if title:
            value['title'] = title
        return value
    store.update_thread(newest.thread, change)


def _route(store: ChannelStore, accounts: dict[str, dict[str, Any] | None],
           routable: list[tuple[ChannelEvent, str]],
           threads: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    owner = store.owner
    router = delivery.global_router_binding(owner, create=True)
    router_id = router['conversationId']
    units: list[_Unit] = []
    by_target: dict[str, _Unit] = {}

    def unit_for(conversation_id: str) -> _Unit:
        if conversation_id not in by_target:
            by_target[conversation_id] = _Unit(conversation_id, human=False)
            units.append(by_target[conversation_id])
        return by_target[conversation_id]

    for event, disposition in routable:
        if disposition == 'command':
            units.append(_Unit(router_id, human=True, events=[(event, disposition)]))
            continue
        subscribers = [c for c in (threads[str(event.thread)].get('subscribers') or [])
                       if c != router_id]
        if subscribers:
            for conversation_id in subscribers:
                unit_for(conversation_id).events.append((event, disposition))
            observation = by_target.get('\0observe:' + str(event.thread))
            if observation is None:
                observation = _Unit(router_id, human=False, observe=(str(event.thread), subscribers))
                by_target['\0observe:' + str(event.thread)] = observation
                units.append(observation)
            observation.events.append((event, disposition))
        elif disposition == 'candidate':
            unit_for(router_id).events.append((event, disposition))
        # Owner context without subscribers is recorded for channel_read only.

    deliveries: dict[str, list[dict[str, Any]]] = {}
    owner_uids = {channel: policy.owner_uid(account) for channel, account in accounts.items()}
    for unit in units:
        if unit.observe is not None:
            if delivery.observe_router(owner, router, unit.observe[0], unit.events, unit.observe[1]):
                for event, disposition in unit.events:
                    deliveries.setdefault(event.event_id, []).append(
                        {'conversationId': router_id, 'kind': 'observation',
                         'disposition': disposition})
            continue
        if not unit.events:
            continue
        first = unit.events[0][0]
        if unit.human:
            title = threads.get(str(first.thread), {}).get('title') or first.thread_title
            outcome = delivery.deliver_command(owner, unit.conversation_id, first, title=title)
        else:
            outcome = delivery.deliver(owner, unit.conversation_id, unit.events, threads=threads,
                                       owner_uid=owner_uids.get(first.channel, ''))
        if not outcome.accepted:
            if outcome.disposition == 'target_missing' and unit.conversation_id != router_id:
                _drop_missing_subscriber(store, unit, router, threads, owner_uids, deliveries)
                continue
            raise delivery.DeliveryFailed(unit.conversation_id, outcome.disposition)
        for event, disposition in unit.events:
            deliveries.setdefault(event.event_id, []).append(
                {'conversationId': unit.conversation_id, 'kind': 'human' if unit.human else 'turn',
                 'disposition': disposition, 'dispatch': outcome.disposition})
    for event, _ in routable:
        store.mark_routed(event, deliveries.get(event.event_id, []))
    return deliveries


def _drop_missing_subscriber(store, unit, router, threads, owner_uids, deliveries) -> None:
    """A deleted subscriber loses its edges; its candidates return to the router."""
    logger.warning('[Channels] subscriber %s is gone; unsubscribing', unit.conversation_id)
    for thread in {str(event.thread) for event, _ in unit.events}:
        threads[thread] = store.unsubscribe(ThreadKey.parse(thread), unit.conversation_id)
    candidates = [(e, d) for e, d in unit.events if d == 'candidate']
    if not candidates:
        return
    first = candidates[0][0]
    outcome = delivery.deliver(store.owner, router['conversationId'], candidates, threads=threads,
                               owner_uid=owner_uids.get(first.channel, ''))
    if not outcome.accepted:
        raise delivery.DeliveryFailed(router['conversationId'], outcome.disposition)
    for event, disposition in candidates:
        deliveries.setdefault(event.event_id, []).append(
            {'conversationId': router['conversationId'], 'kind': 'turn',
             'disposition': disposition, 'dispatch': outcome.disposition})


__all__ = ['IngestResult', 'ingest', 'ingest_batch']
