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
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from weakref import WeakSet

from lib.log import get_logger

from lib.agent_core.push_frames import PushFrameStore

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _QueuedPushEvent:
    store: PushFrameStore
    prepared: object

    @property
    def retained_bytes(self) -> int:
        if not self.prepared.done() or self.prepared.cancelled():
            return 0
        if self.prepared.exception() is not None:
            return 0
        return self.prepared.result().length

    async def decode(self, *, encoded: bool = False) -> dict | str:
        stored = await asyncio.shield(asyncio.wrap_future(self.prepared))
        return await asyncio.shield(asyncio.wrap_future(self.store.read(stored, encoded=encoded)))


class PushHub:
    """Central hub for server-push connections.

    Thread-safe: backend tasks (running in thread pool) can call
    push_event() from any thread. The hub schedules delivery onto
    the asyncio event loop.
    """

    def __init__(self):
        self._clients: WeakSet = WeakSet()
        self._subscriptions: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        self._listeners: list = []  # in-process observers; see add_listener
        # Replica-local observers for frames *received* from the fan-out bus.
        # Unlike publication listeners, these run on every replica and let a
        # durable domain stream use push solely as a low-latency wakeup hint.
        self._delivery_listeners: list = []
        self._lock = threading.Lock()
        self._frame_store = PushFrameStore()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._draining = False
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
        with self._lock:
            self._draining = False
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
        """Stop transport and join owned frame workers after socket shutdown."""
        self.close_clients_for_shutdown()
        bus = self._bus
        if bus is not None:
            try:
                bus.stop()
            except Exception as e:
                logger.warning('[Push] bus stop failed: %s', e)
        self._bus_started = False
        self._frame_store.close()
        self._frame_store = PushFrameStore()

    def close_clients_for_shutdown(self) -> int:
        """Wake every live WebSocket before Hypercorn drains connections.

        Called on the serving loop after its shutdown trigger fires.  The
        lifespan shutdown hook runs only *after* Hypercorn's connection drain,
        so stopping clients there can leave an open socket holding up restart.
        Registration is fenced atomically with the snapshot.
        """
        with self._lock:
            self._draining = True
            clients = tuple(self._clients)
        closed = 0
        for client in clients:
            try:
                client.disconnect()
                closed += 1
            except Exception:
                logger.exception('[Push] Client disconnect failed during shutdown')
        return closed

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
            'connection_admission': 'demand',
            'event_queue_items': sum(
                client._queue.qsize() for client in clients
                if hasattr(client, '_queue')),
            'event_retained_bytes': sum(
                int(getattr(client, 'event_retained_bytes', 0) or 0)
                for client in clients),
            **self._frame_store.snapshot(),
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
            if self._draining:
                return False
            self._clients.add(client)
            total = len(self._clients)
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
            from lib.runtime.runtime_state_store import get_store
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
            from lib.runtime.runtime_state_store import get_store
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
            from lib.runtime.runtime_state_store import get_store
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

        Explicit channel, task_id and user_id own routing. A producer taskId
        that differs from the subscription key becomes executionTaskId, taking
        precedence over a conflicting payload executionTaskId. Normalize a
        copy only: task sinks/replay retain the producer's execution identity.
        Publication listeners keep their payload-only API, but any supplied
        routing fields agree with the bus envelope.
        """
        owner_user_id = _require_owner_user_id(user_id)
        payload = {**payload, '_ownerUserId': owner_user_id}
        producer_task_id = payload.get('taskId')
        if producer_task_id is not None and producer_task_id != task_id:
            payload['executionTaskId'] = producer_task_id
        if 'taskId' in payload:
            payload['taskId'] = task_id
        if 'channel' in payload:
            payload['channel'] = channel
        frame = {**payload, 'channel': channel, 'taskId': task_id}
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                # Observers cannot rewrite identity for later observers or
                # for the bus publication (including owner authorization).
                fn(channel, task_id, dict(payload))
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
            if self._draining:
                targets.clear()
            targets = {
                client
                for client in targets
                if client.user_id == owner_user_id
            }
        for fn in delivery_listeners:
            try:
                # Delivery observers see the same canonical identity, without
                # being able to rewrite the queued frame or the next observer.
                fn(dict(frame))
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
        prepared = None
        if any(callable(getattr(client, 'enqueue_prepared', None)) for client in targets):
            prepared = _QueuedPushEvent(self._frame_store, self._frame_store.prepare(frame))
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._deliver, targets, frame, prepared)
        else:
            self._deliver(targets, frame, prepared)

    def _deliver(self, targets: set, frame: dict, prepared=None):
        for client in targets:
            enqueue_prepared = getattr(client, 'enqueue_prepared', None)
            if prepared is not None and callable(enqueue_prepared):
                enqueue_prepared(prepared)
            else:
                # Plugin/test clients retain their enqueue(frame) protocol.
                client.enqueue(frame)

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
        # Transport frames are reconstructible; a stalled socket that fills
        # this probe-derived backlog is disconnected so it reconciles from
        # durable state instead of accumulating unbounded memory.
        from lib.agent_core.push_policy import resolve_push_event_queue_capacity
        self._queue: asyncio.Queue = asyncio.Queue(
            maxsize=resolve_push_event_queue_capacity())
        self._active_event = None
        self._frame_store = None
        # Liveness and correlated responses jump event data through the sole
        # sender. Preserve every correlated pong instead of evicting at 64.
        self._ctl: deque = deque()
        self._rpc: deque = deque()
        self._ctl_waiter: asyncio.Future | None = None
        self._connected = True
        self.user_id = _require_owner_user_id(user_id)
        self.req_id: str = str(req_id or '')

    @property
    def event_retained_bytes(self) -> int:
        events = list(self._queue._queue)
        if self._active_event is not None:
            events.append(self._active_event)
        return sum(event.retained_bytes for event in events
                   if isinstance(event, _QueuedPushEvent))

    def enqueue_prepared(self, event: _QueuedPushEvent) -> None:
        if not self._connected:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                '[Push] Client event backlog full (%d); disconnecting stalled '
                'socket for durable reconciliation (rid=%s)',
                self._queue.maxsize, self.req_id or '?')
            self.disconnect()

    def enqueue(self, frame: dict, *, retained_bytes: int | None = None):
        if not self._connected:
            return
        if self._frame_store is None:
            self._frame_store = PushFrameStore()
        self.enqueue_prepared(_QueuedPushEvent(
            self._frame_store, self._frame_store.prepare(frame)))

    def enqueue_sized(self, frame: dict, retained_bytes: int) -> None:
        # Older adapters may report size; actual encoded bytes own accounting.
        self.enqueue(frame)

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
        """Reliably enqueue one correlated response until delivery or disconnect.

        LOOP-THREAD ONLY. Returning ``False`` tells the RPC session that this
        socket has actually disconnected. Count backlog is retained until
        delivery; disconnect wakes the sender and clears the owned frames.
        """
        if not self._connected:
            return False
        self._rpc.append(frame)
        waiter = self._ctl_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
        return True

    async def drain(self, *, encoded: bool = False) -> dict | str | None:
        """Drain FIFO events without blocking pongs/RPC on encoding or disk.

        Waiting cancellation does not cancel a shared publication Future. The
        active range remains owned until delivery or actual disconnect; every
        temporary asyncio waiter is cancelled and joined before returning.
        """
        while self._connected:
            if self._ctl:
                return self._ctl.popleft()
            if self._rpc:
                return self._rpc.popleft()
            waiter = asyncio.get_running_loop().create_future()
            self._ctl_waiter = waiter
            reading = self._active_event is not None
            operation = asyncio.create_task(
                self._active_event.decode(encoded=encoded) if reading else self._queue.get())
            try:
                done, _ = await asyncio.wait(
                    (operation, waiter), timeout=30,
                    return_when=asyncio.FIRST_COMPLETED)
                if operation in done:
                    result = operation.result()
                    if reading:
                        self._active_event = None
                        return result if self._connected else None
                    # A simultaneous control wake cannot discard a dequeued
                    # event. Own it before any control priority check.
                    self._active_event = result
                elif not done:
                    return {'channel': 'system', 'type': 'ping'}
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('[Push] frame encoding/read failed (client=%s)', self.req_id or '?')
                self.disconnect()
                return None
            finally:
                self._ctl_waiter = None
                if (not reading and self._connected and self._active_event is None
                        and operation.done() and not operation.cancelled()
                        and operation.exception() is None):
                    self._active_event = operation.result()
                if not operation.done():
                    operation.cancel()
                try:
                    await operation
                except (asyncio.CancelledError, Exception) as diagnostic_error:
                    logger.debug('drain failure handled: %s', type(diagnostic_error).__name__)
                    pass
                if not waiter.done():
                    waiter.cancel()
        return None

    def disconnect(self):
        self._connected = False
        self._active_event = None
        while not self._queue.empty():
            self._queue.get_nowait()
        self._ctl.clear()
        self._rpc.clear()
        if self._frame_store is not None:
            self._frame_store.close(wait=False)
        waiter = self._ctl_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def aclose(self) -> None:
        self.disconnect()
        if self._frame_store is not None:
            joining = asyncio.create_task(asyncio.to_thread(self._frame_store.close))
            cancelled = False
            while not joining.done():
                try:
                    await asyncio.shield(joining)
                except asyncio.CancelledError:
                    cancelled = True
            joining.result()
            if cancelled:
                raise asyncio.CancelledError



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
