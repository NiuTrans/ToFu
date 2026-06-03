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

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

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
        with self._lock:
            self._clients.discard(client)
            for channel_subs in self._subscriptions.values():
                for task_clients in channel_subs.values():
                    task_clients.discard(client)
        logger.debug('[Push] Client unregistered (total=%d)', len(self._clients))

    def subscribe(self, client: 'PushClient', channel: str, task_id: str = '*'):
        with self._lock:
            self._subscriptions[channel][task_id].add(client)

    def unsubscribe(self, client: 'PushClient', channel: str, task_id: str = '*'):
        with self._lock:
            self._subscriptions[channel][task_id].discard(client)

    def push_event(self, channel: str, task_id: str, payload: dict):
        """Push an event to all clients subscribed to this channel+task,
        and to every registered in-process listener.

        Thread-safe — can be called from any thread (orchestrator,
        background workers). Listener exceptions are isolated.
        """
        frame = {'channel': channel, 'taskId': task_id, **payload}
        with self._lock:
            targets = set()
            targets.update(self._subscriptions[channel].get(task_id, set()))
            targets.update(self._subscriptions[channel].get('*', set()))
            listeners = list(self._listeners)

        for fn in listeners:
            try:
                fn(channel, task_id, payload)
            except Exception as e:
                logger.warning('[Push] listener %r failed: %s', fn, e)

        if not targets:
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
        """Broadcast to ALL connected clients (for server notifications)."""
        frame = {'channel': channel, 'taskId': '*', **payload}
        with self._lock:
            targets = set(self._clients)
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._deliver, targets, frame)
        else:
            for client in targets:
                client.enqueue(frame)

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
