"""Low-latency wakeup broker for the durable conversation change log.

The broker never carries projection authority.  Local storage ACKs and
cross-replica push frames merely wake an SSE reader, which then replays the
ordered database log.  Missing a wakeup therefore delays delivery by at most
one heartbeat probe and cannot lose state.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Mapping
import threading
from typing import Any

from lib.conversation_sync.cursor import ConversationCursorError, decode_cursor, encode_cursor
from lib.log import get_logger
from lib.observability import record_stream_admission
from lib.storage.commit_events import subscribe_committed_events
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)
_INVALIDATION_CONTRACT = "tofu.conversation-sync.invalidation/v1"
_COMMIT_CONTRACT = "storage.conversation-commit/v1"


def _scope(user_id: Any, conversation_id: str) -> tuple[str, str]:
    return str(user_id), str(conversation_id)


class ConversationWakeSubscription:
    def __init__(
        self,
        broker: "ConversationWakeBroker",
        key: tuple[str, str],
        *,
        principal_key: str,
        stream_client_id: str,
        stream_generation: int,
    ) -> None:
        self._broker = broker
        self._key = key
        self.conversation_id = key[1]
        self.principal_key = principal_key
        self.stream_client_id = stream_client_id
        self.stream_generation = stream_generation
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._state_lock = threading.Lock()
        self._closed = False
        self._close_reason = ""
        self._close_callbacks: list[Callable[[], None]] = []
        self._body_start_deadline: asyncio.TimerHandle | None = None

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    @property
    def close_reason(self) -> str:
        with self._state_lock:
            return self._close_reason

    def _wake_on_loop(self) -> None:
        if self._closed or self._queue.full():
            return
        self._queue.put_nowait(None)

    def _signal_close_on_loop(self) -> None:
        # A coalesced change wake may already occupy the one-item queue. Drain
        # it so close always interrupts the heartbeat wait immediately.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(None)

    def wake(self) -> None:
        if self._closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._wake_on_loop)
        except RuntimeError:
            self.close()

    async def wait(self, timeout_seconds: float) -> bool:
        if self.closed:
            return False
        try:
            await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
            return not self.closed
        except TimeoutError:
            return False

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        """Bind one exact resource release, even across a close/bind race."""
        run_now = False
        with self._state_lock:
            if self._closed:
                run_now = True
            else:
                self._close_callbacks.append(callback)
        if run_now:
            callback()

    def arm_body_start_deadline(self, timeout_seconds: float) -> None:
        """Reclaim admission if ASGI never begins consuming the response."""
        handle = self._loop.call_later(
            max(0.01, float(timeout_seconds)),
            self.close,
            "body_start_timeout",
        )
        previous: asyncio.TimerHandle | None = None
        cancel_new = False
        with self._state_lock:
            if self._closed:
                cancel_new = True
            else:
                previous = self._body_start_deadline
                self._body_start_deadline = handle
        if previous is not None:
            previous.cancel()
        if cancel_new:
            handle.cancel()

    def mark_body_started(self) -> None:
        with self._state_lock:
            handle = self._body_start_deadline
            self._body_start_deadline = None
        if handle is not None:
            handle.cancel()

    def close(self, reason: str = "closed") -> bool:
        callbacks: tuple[Callable[[], None], ...]
        body_start_deadline: asyncio.TimerHandle | None
        with self._state_lock:
            if self._closed:
                return False
            self._closed = True
            self._close_reason = str(reason or "closed")
            callbacks = tuple(self._close_callbacks)
            self._close_callbacks.clear()
            body_start_deadline = self._body_start_deadline
            self._body_start_deadline = None
        if body_start_deadline is not None:
            body_start_deadline.cancel()
        self._broker._unsubscribe(self._key, self)
        try:
            self._loop.call_soon_threadsafe(self._signal_close_on_loop)
        except RuntimeError:
            pass
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.warning(
                    "Conversation stream close callback failed: %s", exc,
                    exc_info=True,
                )
        return True


class ConversationWakeBroker:
    def __init__(self, *, owner_history_capacity: int | None = None) -> None:
        self._lock = threading.RLock()
        self._subscriptions: dict[
            tuple[str, str], set[ConversationWakeSubscription]
        ] = defaultdict(set)
        self._principal_subscriptions: dict[
            str, OrderedDict[ConversationWakeSubscription, None]
        ] = defaultdict(OrderedDict)
        self._stream_owners: dict[
            tuple[str, str, str], ConversationWakeSubscription
        ] = {}
        self._owner_generations: OrderedDict[tuple[str, str, str], int] = (
            OrderedDict()
        )
        self._owner_history_capacity = (
            max(1, int(owner_history_capacity))
            if owner_history_capacity is not None
            else max(
                128,
                resolve_resource_budget(
                    "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY", maximum=8192),
            )
        )
        self._latest_sequence: dict[tuple[str, str], int] = {}

    @staticmethod
    def _owner_key(
        principal_key: str, conversation_id: str, stream_client_id: str,
    ) -> tuple[str, str, str] | None:
        if not stream_client_id:
            return None
        return principal_key, str(conversation_id), stream_client_id

    def _trim_owner_history_locked(self) -> None:
        while len(self._owner_generations) > self._owner_history_capacity:
            removable = next(
                (
                    owner_key
                    for owner_key in self._owner_generations
                    if owner_key not in self._stream_owners
                ),
                None,
            )
            if removable is None:
                return
            self._owner_generations.pop(removable, None)

    def subscribe(
        self,
        user_id: Any,
        conversation_id: str,
        *,
        principal_key: str = "",
        stream_client_id: str = "",
        stream_generation: int = 0,
    ) -> ConversationWakeSubscription:
        key = _scope(user_id, conversation_id)
        principal = principal_key or f"owner:{key[0]}"
        subscription = ConversationWakeSubscription(
            self,
            key,
            principal_key=principal,
            stream_client_id=stream_client_id,
            stream_generation=stream_generation,
        )
        owner_key = self._owner_key(
            principal, conversation_id, stream_client_id)
        previous: ConversationWakeSubscription | None = None
        stale = False
        with self._lock:
            if owner_key is not None:
                latest_generation = self._owner_generations.get(owner_key)
                if (latest_generation is not None
                        and stream_generation < latest_generation):
                    stale = True
                else:
                    self._owner_generations[owner_key] = stream_generation
                    self._owner_generations.move_to_end(owner_key)
                    previous = self._stream_owners.get(owner_key)
                    self._stream_owners[owner_key] = subscription
                    self._trim_owner_history_locked()
            if not stale:
                self._subscriptions[key].add(subscription)
                self._principal_subscriptions[principal][subscription] = None
        if stale:
            record_stream_admission("conversation-sync", "stale")
            subscription.close("stale_generation")
        elif previous is not None:
            record_stream_admission("conversation-sync", "superseded")
            previous.close("superseded")
        return subscription

    def _unsubscribe(
        self, key: tuple[str, str], subscription: ConversationWakeSubscription
    ) -> None:
        with self._lock:
            subscribers = self._subscriptions.get(key)
            if subscribers:
                subscribers.discard(subscription)
                if not subscribers:
                    self._subscriptions.pop(key, None)
                    self._latest_sequence.pop(key, None)
            principal_subscribers = self._principal_subscriptions.get(
                subscription.principal_key)
            if principal_subscribers is not None:
                principal_subscribers.pop(subscription, None)
                if not principal_subscribers:
                    self._principal_subscriptions.pop(
                        subscription.principal_key, None)
            owner_key = self._owner_key(
                subscription.principal_key,
                key[1],
                subscription.stream_client_id,
            )
            if (owner_key is not None
                    and self._stream_owners.get(owner_key) is subscription):
                self._stream_owners.pop(owner_key, None)
            self._trim_owner_history_locked()

    def evict_oldest(
        self,
        principal_key: str,
        *,
        exclude: ConversationWakeSubscription | None = None,
    ) -> ConversationWakeSubscription | None:
        """Close one oldest local stream so a current browser can make progress."""
        with self._lock:
            candidates = self._principal_subscriptions.get(principal_key)
            victim = next(
                (
                    subscription
                    for subscription in candidates or ()
                    if subscription is not exclude and not subscription.closed
                ),
                None,
            )
            if victim is not None:
                owner_key = self._owner_key(
                    victim.principal_key,
                    victim.conversation_id,
                    victim.stream_client_id,
                )
                if owner_key is not None:
                    # Closing an established 200 SSE makes native EventSource
                    # reconnect with the same URL. Fence that exact generation
                    # so capacity eviction cannot become a hot eviction loop;
                    # the coordinator's next explicit recovery uses a newer
                    # generation and remains eligible.
                    self._owner_generations[owner_key] = max(
                        self._owner_generations.get(owner_key, 0),
                        victim.stream_generation + 1,
                    )
                    self._owner_generations.move_to_end(owner_key)
                    self._trim_owner_history_locked()
        if victim is None:
            return None
        record_stream_admission("conversation-sync", "evicted")
        victim.close("capacity_evicted")
        return victim

    def snapshot(self) -> dict[str, int]:
        """Return bounded, identity-free registry evidence for diagnostics."""
        with self._lock:
            subscriptions = tuple(
                subscription
                for scoped in self._subscriptions.values()
                for subscription in scoped
            )
            return {
                "active": len(subscriptions),
                "scopes": len(self._subscriptions),
                "principals": len(self._principal_subscriptions),
                "owned": sum(
                    1 for subscription in subscriptions
                    if subscription.stream_client_id
                ),
                "legacy": sum(
                    1 for subscription in subscriptions
                    if not subscription.stream_client_id
                ),
                "ownerHistory": len(self._owner_generations),
                "ownerHistoryCapacity": self._owner_history_capacity,
            }

    def wake(self, user_id: Any, conversation_id: str, sequence: int | None) -> None:
        key = _scope(user_id, conversation_id)
        with self._lock:
            subscribers = tuple(self._subscriptions.get(key, ()))
            if not subscribers:
                # A later subscriber performs a durable replay probe before
                # waiting, so retaining sequence hints for inactive
                # conversations would be pure unbounded process memory.
                return
            if sequence is not None:
                previous = self._latest_sequence.get(key, -1)
                if sequence <= previous:
                    return
                self._latest_sequence[key] = sequence
        for subscription in subscribers:
            subscription.wake()


broker = ConversationWakeBroker()


def _publish_invalidation(user_id: Any, event: Mapping[str, Any]) -> None:
    conversation_id = str(event.get("conversationId") or "")
    sequence = int(event.get("syncSeq") or 0)
    if not conversation_id or sequence <= 0:
        return
    broker.wake(user_id, conversation_id, sequence)
    try:
        from lib.agent_core.push import push_event

        push_event("notify", conversation_id, {
            "contract": _INVALIDATION_CONTRACT,
            "type": "conversation.invalidated",
            "conversationId": conversation_id,
            "cursorHint": encode_cursor(conversation_id, user_id, sequence),
            "userId": user_id,
        }, user_id=user_id)
    except Exception as exc:
        # Durable heartbeat probes still converge if the optional wake bus is
        # unavailable; log the transport fault without changing command truth.
        logger.warning(
            "Conversation invalidation publish failed conv=%s: %s",
            conversation_id[:12],
            exc,
        )


def _on_committed_events(carriers: tuple[Mapping[str, Any], ...]) -> None:
    newest: dict[tuple[str, str], tuple[Any, Mapping[str, Any]]] = {}
    for carrier in carriers:
        if carrier.get("contract") != _COMMIT_CONTRACT:
            continue
        event = carrier.get("event")
        user_id = carrier.get("userId")
        if not isinstance(event, Mapping) or user_id is None:
            continue
        conversation_id = str(event.get("conversationId") or "")
        key = _scope(user_id, conversation_id)
        previous = newest.get(key)
        if previous is None or int(event.get("syncSeq") or 0) > int(
            previous[1].get("syncSeq") or 0
        ):
            newest[key] = (user_id, event)
    for user_id, event in newest.values():
        _publish_invalidation(user_id, event)


def observe_delivery_frame(frame: Mapping[str, Any]) -> None:
    """Wake this replica for a validated cross-replica invalidation hint."""
    if (frame.get("channel") != "notify"
            or frame.get("contract") != _INVALIDATION_CONTRACT
            or frame.get("type") != "conversation.invalidated"):
        return
    conversation_id = str(frame.get("conversationId") or "")
    user_id = frame.get("userId")
    cursor = frame.get("cursorHint")
    if not conversation_id or user_id is None or not isinstance(cursor, str):
        return
    try:
        sequence = decode_cursor(conversation_id, user_id, cursor)
    except ConversationCursorError:
        logger.warning("Ignored malformed conversation invalidation cursor")
        return
    broker.wake(user_id, conversation_id, sequence)


_unsubscribe_storage = subscribe_committed_events(_on_committed_events)

# Route import happens during application assembly, after the global push hub
# exists.  Delivery listeners run on every replica, including frames received
# through Redis, so one committed write wakes all locally-held SSE streams.
from lib.agent_core.push import hub as _push_hub  # noqa: E402

_push_hub.add_delivery_listener(observe_delivery_frame)


__all__ = ["ConversationWakeBroker", "ConversationWakeSubscription", "broker"]
