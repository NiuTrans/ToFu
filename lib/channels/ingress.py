"""Channel ingress: record once, then route (C1, C2, C3, C7, C9).

Responsibility: for each normalized event, in order: drop outbox echoes
(the account's own sends, its notification robot's posts included; an echo
of the owner's account confirms the outbox item it proves sent), keep only metadata for ignored
threads, classify by the deterministic thread mode, record the event once per
``(owner, thread, external id)`` with CAS, resolve the media of events about
to be routed (``lib.channels.media``), route newly recorded (or previously
unrouted) events, then advance the thread watermark. Routing: owner messages
in the command chat are human input to the Global Router; delivered
candidates go to the thread's subscribers (passthrough, plus a non-waking
router observation) or, without subscribers, to the Global Router; owner
context goes to subscribers only.

Entry points: ``ingest(owner, event)`` and ``ingest_batch(owner, events)``
(catch-up replays deliver one batch per target); ``apply_change(owner,
change)`` records an edit or recall on the message it changes. A routing failure raises
before the watermark moves, so the adapter's next catch-up replays the gap and
dedup turns it into duplicates, never losses.
Dependencies: ``ChannelStore``, ``policy``, ``media``, ``delivery``, ``outbox``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from lib.channels import delivery, policy
from lib.channels.media import MediaPort, resolve_event_media
from lib.channels.model import ChannelChange, ChannelEvent, ThreadKey, external_id_after
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


def ingest(owner: int, event: ChannelEvent, *, store: ChannelStore | None = None,
           media: MediaPort | None = None, advance_watermark: bool = True) -> IngestResult:
    return ingest_batch(owner, [event], store=store, media=media, advance_watermark=advance_watermark)[0]


def ingest_batch(owner: int, events: list[ChannelEvent], *,
                 store: ChannelStore | None = None,
                 media: MediaPort | None = None,
                 advance_watermark: bool = True) -> list[IngestResult]:
    """``media`` is the adapter's download port for the events' originals (C9).

    ``advance_watermark=False`` is for events that may follow an unreplayed gap
    (the adapter owes a catch-up): they are recorded and routed, but the
    watermark stays where that catch-up's replay must start.
    """
    store = store or ChannelStore(owner)
    with _owner_lock(store.owner):
        return _ingest_locked(store, events, media, advance_watermark)


def apply_change(owner: int, change: ChannelChange, *, store: ChannelStore | None = None) -> IngestResult:
    """Record an edit or recall on the message it changes; never routed (channel protocol P2).

    An edit is a revision of the recorded event, never a new message and never
    a second command (an edited owner command is not re-executed); a recall
    marks the event recalled and removes its text from Tofu's records (text
    already delivered into a conversation stays in its transcript). Ignored
    threads keep no events, so nothing there changes.
    """
    store = store or ChannelStore(owner)
    with _owner_lock(store.owner):
        account = store.account(change.thread.channel)
        if not account or not account.get('enabled'):
            return IngestResult(change.change_id, 'disabled')
        if policy.effective_mode(change.thread, store.thread(change.thread), account) == 'ignore':
            return IngestResult(change.change_id, 'ignored')
        outcome = store.record_change(change)
    if outcome == 'dropped':
        logger.info('[Channels] %s %s of %s changes no recorded message: dropped', change.thread, change.kind,
                    change.target or '(unresolved target)')
    return IngestResult(change.change_id, outcome)


def _ingest_locked(store: ChannelStore, events: list[ChannelEvent],
                   media: MediaPort | None, advance_watermark: bool) -> list[IngestResult]:
    from lib.channels import outbox
    results: list[IngestResult] = []
    routable: list[tuple[ChannelEvent, str]] = []
    delivered: dict[str, list[dict[str, Any]]] = {}
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
        echo_item = _echo_item(store, event, owner_uid, account)
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
        robot_chat = policy.robot_thread(account) == key
        disposition = policy.classify(event, mode, owner_uid, robot_chat=robot_chat)
        if disposition == 'command' and robot_chat:
            # The owner's own message, seen by this server's session, binds the robot's
            # chat to this account before its receipt goes out (C6).
            from lib.channels.dx.robot import bind_owner_message
            account = accounts[event.channel] = bind_owner_message(store, account, event)
        advance.setdefault(key, []).append(event)
        if disposition == 'drop':
            results.append(IngestResult(event.event_id, 'dropped'))
            continue
        claimed, new = store.claim_event(event, disposition)
        if not new and claimed.get('state') == 'routed':
            # Routed in an earlier session; the watermark may lag a crash.
            results.append(IngestResult(event.event_id, 'duplicate', claimed.get('disposition', '')))
            continue
        # A recorded but unrouted event is a previous session's failed route;
        # the deliveries it recorded were accepted and are never repeated.
        disposition = claimed.get('disposition') or disposition
        # Only events that are routed fetch their media, once (C9).
        routable.append((resolve_event_media(store, event, claimed, media), disposition))
        delivered[event.event_id] = list(claimed.get('deliveries') or [])
        results.append(IngestResult(event.event_id, 'recorded', disposition))
    if routable:
        _route(store, accounts, routable, threads, delivered)
        for result in results:
            result.deliveries = delivered.get(result.event_id, result.deliveries)
    for key, processed in advance.items():
        _advance_watermark(store, processed, move=advance_watermark)
    return results


def _echo_item(store: ChannelStore, event: ChannelEvent, owner_uid: str,
               account: dict[str, Any] | None) -> str | None:
    """Echoes are the account's own sends, whatever path sent them (C2/C7: never input).

    Only an identity bound to the account can echo: the owner's account, and
    in its thread the notification robot (C6), which posts under another
    sender and is matched by ``outbox.robot_echo``. For the owner's account
    the first match of the channel protocol's ``x-selfEcho`` steps wins: the
    marker names an outbox item or part of this account; the provider or
    client ID is in the echo registry; the marker carries any other value
    (only an API sets the platform's marker field, so another Tofu send path
    stamped it); the host recorded another local client sending it; this
    session sent it.
    """
    from lib.channels import outbox
    if not owner_uid:
        return None
    if event.sender_id != owner_uid:
        robot_thread = policy.robot_thread(account)
        if robot_thread and robot_thread == str(event.thread) and outbox.robot_echo(store, event):
            return outbox.ROBOT_ECHO
        return None
    if event.outbox_marker:
        # An attachment message's marker names its item and part (C10).
        item, _ = store.outbox_item(outbox.split_marker(event.outbox_marker)[0])
        if item is not None:
            return event.outbox_marker
    found = store.echo_item(event.channel, external_id=event.external_id,
                            client_id=event.client_id)
    if found:
        return found
    if event.outbox_marker:
        logger.info('[Channels] %s %s carries the Tofu marker %r of no outbox item: sent by another Tofu '
                    'path as the account, not by the owner: echo', event.thread, event.external_id,
                    event.outbox_marker)
        return '-'
    # host_sent before own_connection (both mean echo): a host record is bounded, so its IDs are
    # remembered whenever it is the evidence.
    if event.host_sent:
        # Only the engine's bounded sent-message record proves this; remember it, so
        # a later replay of the same message stays an echo after that record forgets it.
        logger.info('[Channels] %s %s was sent as the account by another engine process of this '
                    'server, not by the owner: echo', event.thread, event.external_id)
        store.remember_echo(event.channel, '-', external_id=event.external_id, client_id=event.client_id)
        return '-'
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


def _advance_watermark(store: ChannelStore, processed: list[ChannelEvent], *, move: bool) -> None:
    newest = max(processed, key=lambda item: int(item.external_id))
    title = next((e.thread_title for e in reversed(processed) if e.thread_title), '')

    def change(value):
        if move and external_id_after(newest.external_id, value.get('watermark')):
            value['watermark'] = newest.external_id
        value['lastEventAtMs'] = max(int(value.get('lastEventAtMs') or 0),
                                     max(e.sent_at_ms for e in processed))
        if title:
            value['title'] = title
        return value
    store.update_thread(newest.thread, change)


def _has_delivery(delivered: dict[str, list[dict[str, Any]]], event: ChannelEvent,
                  conversation_id: str, kind: str) -> bool:
    """A target is a conversation and a delivery kind (human, turn or observation)."""
    return any(isinstance(item, dict) and item.get('conversationId') == conversation_id
               and item.get('kind') == kind for item in delivered.get(event.event_id, ()))


def _accept(store: ChannelStore, delivered: dict[str, list[dict[str, Any]]],
            events: list[tuple[ChannelEvent, str]], conversation_id: str, kind: str,
            dispatch: str | None = None) -> None:
    """Record each accepted ``(event, target)`` before another target is dispatched."""
    for event, disposition in events:
        entry = {'conversationId': conversation_id, 'kind': kind, 'disposition': disposition}
        if dispatch is not None:
            entry['dispatch'] = dispatch
        delivered[event.event_id] = store.record_delivery(event, entry)


def _route(store: ChannelStore, accounts: dict[str, dict[str, Any] | None],
           routable: list[tuple[ChannelEvent, str]], threads: dict[str, dict[str, Any]],
           delivered: dict[str, list[dict[str, Any]]]) -> None:
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
            if not _has_delivery(delivered, event, router_id, 'human'):
                units.append(_Unit(router_id, human=True, events=[(event, disposition)]))
            continue
        subscribers = [c for c in (threads[str(event.thread)].get('subscribers') or [])
                       if c != router_id]
        if subscribers:
            for conversation_id in subscribers:
                if not _has_delivery(delivered, event, conversation_id, 'turn'):
                    unit_for(conversation_id).events.append((event, disposition))
            if not _has_delivery(delivered, event, router_id, 'observation'):
                observation = by_target.get('\0observe:' + str(event.thread))
                if observation is None:
                    observation = _Unit(router_id, human=False,
                                        observe=(str(event.thread), subscribers))
                    by_target['\0observe:' + str(event.thread)] = observation
                    units.append(observation)
                observation.events.append((event, disposition))
        elif disposition == 'candidate' and not _has_delivery(delivered, event, router_id, 'turn'):
            unit_for(router_id).events.append((event, disposition))
        # Owner context without subscribers is recorded for channel_read only.

    owner_uids = {channel: policy.owner_uid(account) for channel, account in accounts.items()}
    for unit in units:
        if unit.observe is not None:
            if delivery.observe_router(owner, router, unit.observe[0], unit.events, unit.observe[1]):
                _accept(store, delivered, unit.events, router_id, 'observation')
            continue
        if not unit.events:
            continue
        first = unit.events[0][0]
        if unit.human:
            title = threads.get(str(first.thread), {}).get('title') or first.thread_title
            outcome = delivery.deliver_command(owner, unit.conversation_id, first, title=title)
        else:
            # The Global Router reviews other people's messages for the owner (C11).
            review = (accounts.get(first.channel) or {}) if unit.conversation_id == router_id else None
            outcome = delivery.deliver(owner, unit.conversation_id, unit.events, threads=threads,
                                       owner_uid=owner_uids.get(first.channel, ''),
                                       review_account=review, store=store)
        if not outcome.accepted:
            if outcome.disposition == 'target_missing' and unit.conversation_id != router_id:
                _drop_missing_subscriber(store, unit, router, threads, owner_uids, delivered, accounts)
                continue
            raise delivery.DeliveryFailed(unit.conversation_id, outcome.disposition)
        _accept(store, delivered, unit.events, unit.conversation_id,
                'human' if unit.human else 'turn', outcome.disposition)
    for event, _ in routable:
        store.mark_routed(event)


def _drop_missing_subscriber(store, unit, router, threads, owner_uids, delivered, accounts) -> None:
    """A deleted subscriber loses its edges; its candidates return to the router."""
    logger.warning('[Channels] subscriber %s is gone; unsubscribing', unit.conversation_id)
    for thread in {str(event.thread) for event, _ in unit.events}:
        threads[thread] = store.unsubscribe(ThreadKey.parse(thread), unit.conversation_id)
    router_id = router['conversationId']
    candidates = [(e, d) for e, d in unit.events
                  if d == 'candidate' and not _has_delivery(delivered, e, router_id, 'turn')]
    if not candidates:
        return
    first = candidates[0][0]
    outcome = delivery.deliver(store.owner, router_id, candidates, threads=threads,
                               owner_uid=owner_uids.get(first.channel, ''),
                               review_account=accounts.get(first.channel) or {}, store=store)
    if not outcome.accepted:
        raise delivery.DeliveryFailed(router_id, outcome.disposition)
    _accept(store, delivered, candidates, router_id, 'turn', outcome.disposition)


__all__ = ['IngestResult', 'apply_change', 'ingest', 'ingest_batch']
