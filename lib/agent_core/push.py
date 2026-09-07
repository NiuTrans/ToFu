"""Unified server-push channel.

Single global WebSocket per client (``/api/push``) that multiplexes all
real-time backend events. The hub is the single fan-out point used by
``TaskRuntime`` and any server-side observer (e.g. webhooks).

Architecture:
  - One WebSocket per browser tab — backed by ``PushClient``.
  - Backend pushes JSON frames tagged with ``{channel, taskId, ...payload}``.
  - Clients subscribe with ``{action: 'subscribe', channel, taskId}``;
    ``taskId='*'`` means "every task on this channel".
  - In addition to per-client subscriptions, in-process observers can
    register a callback via ``hub.add_listener(fn)`` — each event is
    invoked with ``(channel, task_id, payload)``. Used by the webhooks
    delivery worker to fan events out to external HTTP subscribers.

Channels in use:
  - ``paper``      — report generation events (progress, section, done)
  - ``translate``  — translation status (running, done, error)
  - ``notify``     — server notifications (config change, health, etc.)
  - ``timer``      — timer-list invalidations (created/progress/terminal)
  - ``oauth``      — owner-scoped passive account-state completion receipts
  - ``chat``       — chat task lifecycle for headless API/webhook clients.
                     Native conversation state uses Conversation Sync v3;
                     task-event SSE replay uses ``/api/v1/tasks/<task_id>/stream``.
"""

import asyncio
import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from weakref import WeakSet

from lib.agent_core.push_policy import resolve_push_budget
from lib.log import get_logger

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - exported minimal installations
    _orjson = None

logger = get_logger(__name__)


# One slow WebSocket must not retain a permanent 1,000-frame hot queue while
# every publisher keeps paying fan-out, queue churn, and warning costs.  A
# short burst remains lossy-but-connected (the historical drop-oldest
# contract); sustained loss disconnects the socket so its declared reconnect
# reconciliation can run.  These are code-owned resource safety ceilings, not
# deployment tuning knobs.
_PUSH_BUDGET = resolve_push_budget()
_EVENT_QUEUE_CAPACITY = _PUSH_BUDGET.event_queue_capacity
_EVENT_QUEUE_BYTE_CAPACITY = _PUSH_BUDGET.event_queue_byte_capacity
_EVENT_MAX_BYTES = _PUSH_BUDGET.event_max_bytes
_EVENT_OVERFLOW_GRACE_SECONDS = 15.0
_EVENT_OVERFLOW_MIN_DROPS = 256
_EVENT_OVERFLOW_RESET_SECONDS = 5.0


def _serialized_frame_bytes(frame: dict) -> int:
    """Return compact JSON bytes, or an over-limit sentinel on bad payloads."""
    try:
        if _orjson is not None:
            return len(_orjson.dumps(frame))
        return len(json.dumps(
            frame, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    except (TypeError, ValueError, OverflowError):
        # An unencodable frame cannot be delivered by WebSocket either. Treat
        # it as oversized so the client reconnects through durable authority.
        return _EVENT_MAX_BYTES + 1


@dataclass(frozen=True, slots=True)
class _QueuedPushEvent:
    """Keep byte accounting inseparable from the queued frame."""

    frame: dict
    retained_bytes: int


class PushHub:
    """Central hub for server-push connections.

    Thread-safe: backend tasks (running in thread pool) can call
    push_event() from any thread. The hub schedules delivery onto
    the asyncio event loop.
    """

    def __init__(
            self, *, client_capacity: int | None = None,
            owner_client_capacity: int | None = None):
        self._clients: WeakSet = WeakSet()
        resolved_client_capacity = (
            _PUSH_BUDGET.client_capacity
            if client_capacity is None else client_capacity)
        resolved_owner_capacity = (
            _PUSH_BUDGET.owner_client_capacity
            if owner_client_capacity is None else owner_client_capacity)
        self._client_capacity = max(1, int(resolved_client_capacity))
        self._owner_client_capacity = max(
            1, min(self._client_capacity, int(resolved_owner_capacity)))
        self._subscriptions: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        self._listeners: list = []  # in-process observers; see add_listener
        # Replica-local observers for frames *received* from the fan-out bus.
        # Unlike publication listeners, these run on every replica and let a
        # durable domain stream use push solely as a low-latency wakeup hint.
        self._delivery_listeners: list = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Cross-replica fan-out transport (Epic B). Under the default
        # ``inproc`` backend this is a pass-through that delivers locally
        # (byte-identical to the pre-Epic-B path); under ``redis`` a publish
        # goes to a shared topic and every replica's subscriber loop
        # re-delivers to ITS OWN local clients. Built lazily so the backend
        # env is read once, at first use / set_loop.
        self._bus = None
        self._bus_started = False

    def _get_bus(self):
        if self._bus is None:
            from lib.agent_core.push_bus import make_push_bus
            self._bus = make_push_bus(self._deliver_frame)
        return self._bus

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        # Start the cross-replica subscriber loop now that a loop exists.
        # No-op for the inproc bus; connect + subscribe for redis.
        if not self._bus_started:
            try:
                self._get_bus().start()
                self._bus_started = True
            except Exception as e:
                logger.warning('[Push] bus start failed (%s) - local-only', e)

    def clear_loop(self, expected: asyncio.AbstractEventLoop | None = None) -> bool:
        """Drop a closing serving loop without starting or stopping the bus."""
        if expected is not None and self._loop is not expected:
            return False
        self._loop = None
        return True

    def stop(self) -> None:
        """Stop the fan-out transport during server shutdown (idempotent)."""
        bus = self._bus
        if bus is None:
            return
        try:
            bus.stop()
        except Exception as e:
            logger.warning('[Push] bus stop failed: %s', e)
        finally:
            self._bus_started = False

    def bus_health(self) -> dict:
        """Return a non-blocking transport snapshot for metrics/support data."""
        bus = self._bus
        if bus is None:
            result = {'backend': 'not_started', 'publisher_available': True,
                      'subscriber_available': True, 'reconnect_in_s': 0.0}
        else:
            health = getattr(bus, 'health', None)
            result = (health() if callable(health) else {
                'backend': 'inproc', 'publisher_available': True,
                'subscriber_available': True, 'reconnect_in_s': 0.0})
        with self._lock:
            clients = tuple(self._clients)
        result.update({
            'local_clients': len(clients),
            'client_capacity': self._client_capacity,
            'owner_client_capacity': self._owner_client_capacity,
            'event_queue_items': sum(
                client._queue.qsize() for client in clients
                if hasattr(client, '_queue')),
            'event_retained_bytes': sum(
                int(getattr(client, 'event_retained_bytes', 0) or 0)
                for client in clients),
            'event_queue_byte_capacity_per_client':
                _EVENT_QUEUE_BYTE_CAPACITY,
            'event_max_bytes': _EVENT_MAX_BYTES,
        })
        return result

    # ── In-process observers ───────────────────────────────────
    # Listeners receive every event the hub processes. Used by the
    # webhooks worker to deliver events to external HTTP subscribers
    # without monkey-patching ``push_event``.

    def add_listener(self, fn) -> None:
        """Register a callback ``fn(channel, task_id, payload)``.

        Idempotent: registering the same callable twice has no effect.
        Listener exceptions are caught and logged so a misbehaving
        observer can never break the per-client fan-out.
        """
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        with self._lock:
            try:
                self._listeners.remove(fn)
            except ValueError as _e_audit:
                logger.debug('[push] remove_listener caught %s: %s', type(_e_audit).__name__, _e_audit)
                pass

    def add_delivery_listener(self, fn) -> None:
        """Observe bus-delivered frames on this replica (idempotent)."""
        with self._lock:
            if fn not in self._delivery_listeners:
                self._delivery_listeners.append(fn)

    def register(self, client: 'PushClient') -> bool:
        with self._lock:
            if client in self._clients:
                return True
            existing_clients = tuple(self._clients)
            owner_clients = sum(
                existing.user_id == client.user_id
                for existing in existing_clients)
            if (len(existing_clients) >= self._client_capacity
                    or owner_clients >= self._owner_client_capacity):
                total = len(existing_clients)
                accepted = False
            else:
                self._clients.add(client)
                total = len(self._clients)
                accepted = True
        if not accepted:
            logger.warning(
                '[Push] Client capacity rejected (total=%d/%d owner=%s '
                'owner_clients=%d/%d)',
                total, self._client_capacity, client.user_id,
                owner_clients, self._owner_client_capacity)
            return False
        logger.debug('[Push] Client registered (total=%d)', total)
        return True

    def unregister(self, client: 'PushClient'):
        emptied = []
        with self._lock:
            self._clients.discard(client)
            # Iterate snapshots because empty task/channel buckets are removed
            # in place. Keeping one empty Set for every task ever viewed made
            # the process-lifetime subscription registry grow monotonically.
            for channel, channel_subs in list(self._subscriptions.items()):
                for task_id, task_clients in list(channel_subs.items()):
                    if client in task_clients:
                        task_clients.discard(client)
                        if not task_clients:
                            emptied.append((channel, task_id))
                            channel_subs.pop(task_id, None)
                if not channel_subs:
                    self._subscriptions.pop(channel, None)
        # Release the registry lease for any (channel, task) this replica no
        # longer has a local subscriber for (release-on-last-unsubscribe).
        for channel, task_id in emptied:
            self._deregister_subscription(channel, task_id)
        logger.debug('[Push] Client unregistered (total=%d)', len(self._clients))

    # ── Cross-replica subscription registry (Epic B design B.5.1) ──
    # A lease per (channel, task_id) marking that THIS replica has >=1
    # subscriber, keyed sub:{channel}:{task_id}:{replica}. Refreshed by the
    # bus/keepalive; reclaimed by TTL if this replica crashes. Used to reason
    # about "which replicas have a watcher" without cross-replica RPC. The
    # actual delivery does NOT depend on it (the bus broadcasts to all
    # replicas which filter locally) - it is the design's liveness registry.
    _SUB_KIND = 'sub'
    _SUB_TTL = 90.0  # design B.5.4: 90s lease, refreshed by the 30s heartbeat

    def _replica_id(self) -> str:
        import os
        rid = getattr(self, '_rid', None)
        if rid is None:
            rid = os.environ.get('TOFU_REPLICA_ID') or ('%d' % os.getpid())
            self._rid = rid
        return rid

    def _sub_key(self, channel: str, task_id: str) -> str:
        return '%s:%s:%s' % (channel, task_id, self._replica_id())

    def _register_subscription(self, channel: str, task_id: str) -> None:
        try:
            from lib.runtime_state_store import get_store
            get_store().acquire_lease(self._SUB_KIND,
                                      self._sub_key(channel, task_id),
                                      self._SUB_TTL)
        except Exception as e:
            logger.debug('[Push] subscription registry acquire failed: %s', e)

    def _deregister_subscription(self, channel: str, task_id: str) -> None:
        """Drop the registry lease for (channel, task_id) on THIS replica.

        Called only when the LAST local subscriber for that key departs, so a
        second still-connected client on the same replica keeps the lease. TTL
        remains the crash-only backstop; this is the normal eager release."""
        try:
            from lib.runtime_state_store import get_store
            get_store().release_lease(self._SUB_KIND,
                                      self._sub_key(channel, task_id))
        except Exception as e:
            logger.debug('[Push] subscription registry release failed: %s', e)

    def refresh_subscriptions(self) -> None:
        """Heartbeat: re-arm the registry lease for every (channel, task_id)
        this replica currently has a local subscriber for, so a LIVING
        subscriber's registry entry never expires under the 90s TTL. Driven by
        the /api/push ping loop (~30s), mirroring the SSE slot's refresh.
        Design B.5.2 (refresh at ttl/3). No-op when there are no subscriptions."""
        with self._lock:
            live = [(channel, task_id)
                    for channel, channel_subs in self._subscriptions.items()
                    for task_id, task_clients in channel_subs.items()
                    if task_clients]
        if not live:
            return
        try:
            from lib.runtime_state_store import get_store
            store = get_store()
            for channel, task_id in live:
                store.refresh_lease(self._SUB_KIND,
                                    self._sub_key(channel, task_id),
                                    self._SUB_TTL)
        except Exception as e:
            logger.debug('[Push] subscription registry refresh failed: %s', e)

    def subscribe(self, client: 'PushClient', channel: str, task_id: str = '*'):
        with self._lock:
            self._subscriptions[channel][task_id].add(client)
        self._register_subscription(channel, task_id)

    def unsubscribe(self, client: 'PushClient', channel: str, task_id: str = '*'):
        with self._lock:
            # ``defaultdict`` indexing an unknown unsubscribe used to CREATE a
            # permanent empty tombstone. Read with .get(), and eagerly prune
            # both levels when the last real subscriber leaves.
            channel_subs = self._subscriptions.get(channel)
            task_clients = channel_subs.get(task_id) if channel_subs else None
            if task_clients is None:
                emptied = False
            else:
                task_clients.discard(client)
                emptied = not task_clients
                if emptied:
                    channel_subs.pop(task_id, None)
                    if not channel_subs:
                        self._subscriptions.pop(channel, None)
        if emptied:
            self._deregister_subscription(channel, task_id)

    def push_event(
        self,
        channel: str,
        task_id: str,
        payload: dict,
        *,
        user_id: int | str,
    ):
        """Publish an event to every subscriber of this channel+task across
        the FLEET, and fire in-process listeners exactly once.

        Thread-safe. The frame is PUBLISHED to the cross-replica bus; each
        replica's subscriber loop then re-delivers to ITS OWN local clients
        via ``_deliver_frame`` (uniform bus-only delivery, design B.3.1).
        Under the default inproc bus, publish == local delivery (byte-identical
        to the pre-Epic-B path). Webhook/in-process listeners run HERE, on the
        publishing replica, so they fire once fleet-wide, not once per replica.
        """
        owner_user_id = _require_owner_user_id(user_id)
        frame = {
            'channel': channel,
            'taskId': task_id,
            **payload,
            '_ownerUserId': owner_user_id,
        }
        payload = {**payload, '_ownerUserId': owner_user_id}
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(channel, task_id, payload)
            except Exception as e:
                logger.warning('[Push] listener %r failed: %s', fn, e)
        try:
            self._get_bus().publish(frame)
        except Exception as e:
            logger.warning('[Push] bus publish failed (%s) - delivering local', e)
            self._deliver_frame(frame)

    def _deliver_frame(self, frame: dict):
        """Deliver a frame (received from the bus, or local) to THIS replica's
        matching local subscribers. Runs on every replica's subscriber loop.

        The channel+taskId subscription lookup selects local targets, then the
        mandatory owner marker narrows delivery to that owner's sockets.
        Frames without an owner are rejected before observers or clients can
        see them; the bus is never an authorization boundary.
        """
        channel = frame.get('channel', '')
        task_id = frame.get('taskId', '*')
        owner_user_id = str(frame.get('_ownerUserId') or '').strip()
        if not owner_user_id:
            logger.error(
                '[Push] rejected ownerless frame channel=%s task=%s type=%s',
                channel, task_id, frame.get('type'))
            return
        with self._lock:
            delivery_listeners = tuple(self._delivery_listeners)
            targets = set()
            channel_subscriptions = self._subscriptions.get(channel)
            if channel_subscriptions:
                targets.update(channel_subscriptions.get(task_id, set()))
                targets.update(channel_subscriptions.get('*', set()))
            targets = {
                client
                for client in targets
                if client.user_id == owner_user_id
            }
        for fn in delivery_listeners:
            try:
                fn(frame)
            except Exception as e:
                logger.warning('[Push] delivery listener %r failed: %s', fn, e)
        # Never ship internal routing markers to the client.
        if '_ownerUserId' in frame:
            frame = {
                key: value
                for key, value in frame.items()
                if key != '_ownerUserId'
            }
        if not targets:
            logger.debug('[Push] no local subscriber for channel=%s task=%s type=%s',
                         channel, task_id, frame.get('type'))
            return
        retained_bytes = _serialized_frame_bytes(frame)
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(
                self._deliver, targets, frame, retained_bytes)
        else:
            for client in targets:
                self._enqueue_target(client, frame, retained_bytes)

    @staticmethod
    def _enqueue_target(client, frame: dict, retained_bytes: int) -> None:
        """Use byte-aware delivery when a client advertises that capability."""
        enqueue_sized = getattr(client, 'enqueue_sized', None)
        if callable(enqueue_sized):
            enqueue_sized(frame, retained_bytes)
            return
        # Keep PushHub's historical duck-typed client protocol for plugin and
        # transport test clients that implement only ``enqueue(frame)``.
        client.enqueue(frame)

    def _deliver(self, targets: set, frame: dict,
                 retained_bytes: int | None = None):
        for client in targets:
            if retained_bytes is None:
                client.enqueue(frame)
            else:
                self._enqueue_target(client, frame, retained_bytes)

    @property
    def client_count(self) -> int:
        return len(self._clients)


class PushClient:
    """Represents a single WebSocket connection to the push channel.

    ``user_id`` is the resolved owner of the WebSocket (from
    ``AuthContext.owner_user_id`` at handshake — see routes/push.py::push_ws).
    Stashed for the connection lifetime so every subsequent frame handler
    can consult it without re-doing auth. Ownerless sockets are invalid: the
    handshake maps personal mode to its declared owner and multi-user mode
    rejects an unbound principal.

    ``req_id`` is this socket's correlation id, resolved at handshake from
    the client's ``_rid`` query param (see routes/push.py::push_ws). It is
    carried HERE, next to ``user_id``, for the same reason: the frame
    handlers that log a socket's activity are module-level functions and
    cannot see the handler coroutine's locals, so a per-connection field is
    the only way for ``[Push] Client abort``-style lines to name the socket
    they belong to. Without it those lines are unjoinable — the id would
    cover only connect/disconnect, which are the two lines that least need
    it.
    """

    def __init__(self, *, user_id: int | str, req_id: str = ''):
        self._queue: asyncio.Queue = asyncio.Queue(
            maxsize=_EVENT_QUEUE_CAPACITY)
        self._event_retained_bytes = 0
        self._event_queue_byte_capacity = _EVENT_QUEUE_BYTE_CAPACITY
        self._event_max_bytes = _EVENT_MAX_BYTES
        # ── Control lane () ──────────────────────────────────
        # Pongs (and future control frames) must JUMP the data backlog: under
        # event-loop congestion (e.g. a 176 MB HTTP response being serialized
        # on the same loop) a pong queued behind MBs of event frames arrives
        # past the client's 8s watchdog, which then force-closes a HEALTHY
        # socket into the reconnect→refetch→stall loop. Bounded — pongs are
        # redundant by design, so silently discarding the oldest past the cap
        # is the correct degradation.
        self._ctl: deque = deque(maxlen=64)
        # Correlated RPC results are neither lossy event data nor redundant
        # liveness frames.  They get a separate reliable lane: saturation
        # disconnects the slow client so every pending caller fails visibly
        # and can use its declared HTTP fallback. Silent oldest-frame eviction
        # would strand one Promise forever.
        self._rpc: deque = deque()
        self._rpc_capacity = 64
        self._ctl_waiter: asyncio.Future | None = None
        self._connected = True
        self.user_id = _require_owner_user_id(user_id)
        self.req_id: str = str(req_id or '')
        self._event_overflow_started_at: float | None = None
        self._event_overflow_last_at: float | None = None
        self._event_overflow_drops = 0
        self._event_overflow_clock = time.monotonic
        self._event_overflow_grace_seconds = _EVENT_OVERFLOW_GRACE_SECONDS
        self._event_overflow_min_drops = _EVENT_OVERFLOW_MIN_DROPS
        self._event_overflow_reset_seconds = _EVENT_OVERFLOW_RESET_SECONDS

    def _reset_event_overflow(self) -> None:
        self._event_overflow_started_at = None
        self._event_overflow_last_at = None
        self._event_overflow_drops = 0

    def _record_event_overflow(
            self, frame: dict, *, incoming_bytes: int) -> bool:
        """Return False after sustained loss makes reconnect safer than churn."""
        now = self._event_overflow_clock()
        last_at = self._event_overflow_last_at
        new_episode = (
            self._event_overflow_started_at is None
            or last_at is None
            or now - last_at >= self._event_overflow_reset_seconds
        )
        if new_episode:
            self._event_overflow_started_at = now
            self._event_overflow_drops = 0
            logger.warning(
                '[Push] Client event queue saturated; dropping oldest frames '
                'during bounded grace (client=%s channel=%s task=%s type=%s '
                'capacity=%d retained_bytes=%d byte_capacity=%d '
                'incoming_bytes=%d)',
                self.req_id or '?', str(frame.get('channel') or '?')[:48],
                str(frame.get('taskId') or '?')[:12],
                str(frame.get('type') or '?')[:48], self._queue.maxsize,
                self._event_retained_bytes,
                self._event_queue_byte_capacity, incoming_bytes,
        )
        self._event_overflow_last_at = now
        self._event_overflow_drops += 1
        started_at = self._event_overflow_started_at
        elapsed = now - started_at if started_at is not None else 0.0
        if (
            self._event_overflow_drops >= self._event_overflow_min_drops
            and elapsed >= self._event_overflow_grace_seconds
        ):
            logger.warning(
                '[Push] Client event queue remained saturated for %.1fs '
                '(%d dropped); disconnecting slow client for reconnect '
                'reconciliation (client=%s channel=%s task=%s type=%s)',
                elapsed, self._event_overflow_drops, self.req_id or '?',
                str(frame.get('channel') or '?')[:48],
                str(frame.get('taskId') or '?')[:12],
                str(frame.get('type') or '?')[:48],
            )
            self.disconnect()
            return False
        return True

    def _release_oldest_event(self) -> bool:
        try:
            queued = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        size = (queued.retained_bytes
                if isinstance(queued, _QueuedPushEvent) else 0)
        self._event_retained_bytes = max(
            0, self._event_retained_bytes - size)
        return True

    def _release_dequeued_event_size(self, queued: object) -> None:
        size = (queued.retained_bytes
                if isinstance(queued, _QueuedPushEvent) else 0)
        self._event_retained_bytes = max(
            0, self._event_retained_bytes - size)

    @property
    def event_retained_bytes(self) -> int:
        return self._event_retained_bytes

    def enqueue(self, frame: dict, *, retained_bytes: int | None = None):
        if not self._connected:
            return
        incoming_bytes = (
            _serialized_frame_bytes(frame)
            if retained_bytes is None else max(1, int(retained_bytes)))
        if incoming_bytes > self._event_max_bytes:
            logger.warning(
                '[Push] Client event frame oversized or unencodable; '
                'disconnecting for durable reconciliation '
                '(client=%s channel=%s task=%s type=%s incoming_bytes=%d '
                'max_bytes=%d)',
                self.req_id or '?', str(frame.get('channel') or '?')[:48],
                str(frame.get('taskId') or '?')[:12],
                str(frame.get('type') or '?')[:48], incoming_bytes,
                self._event_max_bytes)
            self.disconnect()
            return
        saturated = (
            self._queue.full()
            or self._event_retained_bytes + incoming_bytes
            > self._event_queue_byte_capacity)
        if saturated:
            if not self._record_event_overflow(
                    frame, incoming_bytes=incoming_bytes):
                return
            while (self._queue.full()
                   or self._event_retained_bytes + incoming_bytes
                   > self._event_queue_byte_capacity):
                if not self._release_oldest_event():
                    break
            if (self._queue.full()
                    or self._event_retained_bytes + incoming_bytes
                    > self._event_queue_byte_capacity):
                # The remaining frame may already belong to the active sender
                # Future and cannot be evicted from the queue. Drop this new
                # lossy event rather than exceeding the byte envelope.
                return
        try:
            self._queue.put_nowait(_QueuedPushEvent(frame, incoming_bytes))
            self._event_retained_bytes += incoming_bytes
        except asyncio.QueueFull:
            logger.debug('[Push] bounded event enqueue raced with saturation')

    def enqueue_sized(self, frame: dict, retained_bytes: int) -> None:
        """Byte-aware PushHub capability; external clients may omit it."""
        self.enqueue(frame, retained_bytes=retained_bytes)

    def enqueue_control(self, frame: dict):
        """Enqueue a control frame (pong) that jumps the data backlog.

        LOOP-THREAD ONLY: called from the ``_receiver`` coroutine in
        routes/push.py. Wakes a ``drain()`` that is currently sleeping on an
        empty data queue, so an idle socket's pong is answered promptly too.
        """
        if not self._connected:
            return
        self._ctl.append(frame)
        waiter = self._ctl_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def enqueue_rpc(self, frame: dict) -> bool:
        """Reliably enqueue one correlated response or close on saturation.

        LOOP-THREAD ONLY. Returning ``False`` tells the RPC session that this
        socket can no longer promise delivery; ``disconnect`` wakes the sole
        sender so the socket lifecycle closes promptly.
        """
        if not self._connected:
            return False
        if len(self._rpc) >= self._rpc_capacity:
            logger.warning(
                '[Push] RPC response queue full — disconnecting slow client')
            self.disconnect()
            return False
        self._rpc.append(frame)
        waiter = self._ctl_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
        return True

    async def drain(self) -> dict | None:
        """Wait for and return the next frame, or None if disconnected.

        The liveness lane is checked first, then reliable RPC responses, then
        lossy event data. A data frame already dequeued by the await below
        when a control frame lands is returned first (one-frame delay — the
        ``_sender`` loop calls drain again immediately and picks up the
        control frame next).
        """
        while True:
            if not self._connected:
                return None
            if self._ctl:
                return self._ctl.popleft()
            if self._rpc:
                return self._rpc.popleft()
            get_fut = asyncio.ensure_future(self._queue.get())
            waiter = asyncio.get_running_loop().create_future()
            self._ctl_waiter = waiter
            try:
                done, _pending = await asyncio.wait(
                    (get_fut, waiter), timeout=30,
                    return_when=asyncio.FIRST_COMPLETED)
            except Exception as e:
                logger.debug('[Push] drain failed (signaling disconnect): %s', e)
                return None
            finally:
                self._ctl_waiter = None
                # ``asyncio.Queue.get()`` owns an internal waiter. Merely
                # calling cancel() and returning leaves that waiter pending
                # until a later loop turn; if the WebSocket loop is shutting
                # down at the same time it is destroyed alive (and repeated
                # reconnects accumulate orphan tasks). Always cancel *and
                # drain* it before this coroutine can exit, including when
                # drain() itself is cancelled by the duplex-socket joiner.
                if not get_fut.done():
                    get_fut.cancel()
                    try:
                        await get_fut
                    except asyncio.CancelledError:
                        pass
            if not done:
                return {'channel': 'system', 'type': 'ping'}
            if get_fut in done:
                queued = get_fut.result()
                self._release_dequeued_event_size(queued)
                frame = (queued.frame
                         if isinstance(queued, _QueuedPushEvent) else queued)
                # A half-drained data lane has genuinely recovered.  A later
                # burst receives a fresh grace window instead of inheriting an
                # old episode's elapsed time/drop count.
                if self._queue.qsize() <= self._queue.maxsize // 2:
                    self._reset_event_overflow()
                return frame
            # Only the control waiter fired — loop back; the ctl check at the
            # top returns the control frame.

    def disconnect(self):
        self._connected = False
        while self._release_oldest_event():
            pass
        self._event_retained_bytes = 0
        self._ctl.clear()
        self._rpc.clear()
        waiter = self._ctl_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)


# Singleton hub
hub = PushHub()


def _require_owner_user_id(user_id: int | str) -> str:
    """Return one explicit owner identifier or reject the publication.

    Owner identity is routing authority, not optional metadata. Keeping this
    validator at both the publisher and socket boundaries prevents a future
    caller from reviving fleet-wide delivery by omission.
    """
    if isinstance(user_id, bool) or user_id is None:
        raise ValueError('push user_id is required')
    value = str(user_id).strip()
    if not value or (isinstance(user_id, int) and user_id < 1):
        raise ValueError('push user_id must identify a positive owner')
    return value


def push_event(
    channel: str,
    task_id: str,
    payload: dict,
    *,
    user_id: int | str,
):
    """Convenience function — push an event via the global hub."""
    hub.push_event(channel, task_id, payload, user_id=user_id)
