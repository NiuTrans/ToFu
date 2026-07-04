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
  - ``chat``       — chat task lifecycle (kept as a future hook;
                     browser sessions still receive chat via SSE
                     ``/api/chat/stream/<task_id>`` for Last-Event-ID
                     resume support, but headless API/webhook clients
                     can subscribe here).
"""

import asyncio
import threading
from collections import defaultdict
from weakref import WeakSet

from lib.log import get_logger

logger = get_logger(__name__)


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

    def register(self, client: 'PushClient'):
        with self._lock:
            self._clients.add(client)
        logger.debug('[Push] Client registered (total=%d)', len(self._clients))

    def unregister(self, client: 'PushClient'):
        emptied = []
        with self._lock:
            self._clients.discard(client)
            for channel, channel_subs in self._subscriptions.items():
                for task_id, task_clients in channel_subs.items():
                    if client in task_clients:
                        task_clients.discard(client)
                        if not task_clients:
                            emptied.append((channel, task_id))
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
            self._subscriptions[channel][task_id].discard(client)
            emptied = not self._subscriptions[channel][task_id]
        if emptied:
            self._deregister_subscription(channel, task_id)

    def push_event(self, channel: str, task_id: str, payload: dict):
        """Publish an event to every subscriber of this channel+task across
        the FLEET, and fire in-process listeners exactly once.

        Thread-safe. The frame is PUBLISHED to the cross-replica bus; each
        replica's subscriber loop then re-delivers to ITS OWN local clients
        via ``_deliver_frame`` (uniform bus-only delivery, design B.3.1).
        Under the default inproc bus, publish == local delivery (byte-identical
        to the pre-Epic-B path). Webhook/in-process listeners run HERE, on the
        publishing replica, so they fire once fleet-wide, not once per replica.
        """
        frame = {'channel': channel, 'taskId': task_id, **payload}
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

        A broadcast frame carries ``_bcast=True`` and goes to all local
        clients; otherwise the channel+taskId subscription lookup selects the
        local targets (same semantics as before, now applied per-replica).
        """
        channel = frame.get('channel', '')
        task_id = frame.get('taskId', '*')
        with self._lock:
            if frame.get('_bcast'):
                targets = set(self._clients)
            else:
                targets = set()
                targets.update(self._subscriptions[channel].get(task_id, set()))
                targets.update(self._subscriptions[channel].get('*', set()))
        # Never ship the internal routing marker to the client.
        if '_bcast' in frame:
            frame = {k: v for k, v in frame.items() if k != '_bcast'}
        if not targets:
            logger.debug('[Push] no local subscriber for channel=%s task=%s type=%s',
                         channel, task_id, frame.get('type'))
            return
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._deliver, targets, frame)
        else:
            for client in targets:
                client.enqueue(frame)

    def _deliver(self, targets: set, frame: dict):
        for client in targets:
            client.enqueue(frame)

    def broadcast(self, channel: str, payload: dict):
        """Broadcast to ALL connected clients across the fleet.

        Published to the bus with a ``_bcast`` marker so every replica delivers
        to all of ITS local clients; the marker is stripped before enqueue.
        """
        frame = {'channel': channel, 'taskId': '*', '_bcast': True, **payload}
        try:
            self._get_bus().publish(frame)
        except Exception as e:
            logger.warning('[Push] bus broadcast failed (%s) - delivering local', e)
            self._deliver_frame(frame)

    @property
    def client_count(self) -> int:
        return len(self._clients)


class PushClient:
    """Represents a single WebSocket connection to the push channel."""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._connected = True

    def enqueue(self, frame: dict):
        if not self._connected:
            return
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning('[Push] Client queue full — dropping oldest frame')
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
            except Exception as e:
                logger.debug('[Push] drop-and-replace failed (frame lost): %s', e)

    async def drain(self) -> dict | None:
        """Wait for and return the next frame, or None if disconnected."""
        if not self._connected:
            return None
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=30)
        except asyncio.TimeoutError:
            return {'channel': 'system', 'type': 'ping'}
        except Exception as e:
            logger.debug('[Push] drain failed (signaling disconnect): %s', e)
            return None

    def disconnect(self):
        self._connected = False


# Singleton hub
hub = PushHub()


def push_event(channel: str, task_id: str, payload: dict):
    """Convenience function — push an event via the global hub."""
    hub.push_event(channel, task_id, payload)


def broadcast(channel: str, payload: dict):
    """Convenience function — broadcast to all clients."""
    hub.broadcast(channel, payload)
