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
from collections import defaultdict, deque
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

    def deliver_to_socket(self, req_id: str, frame: dict) -> bool:
        """Deliver a frame to ONE socket, identified by its ``req_id``.

        The repair channel for the sync-drift probe (pt_cadaa70ffa6b468d). The
        probe knows exactly WHICH client is stalled — it is the one that just
        POSTed a frozen digest — so the correction must reach that socket and
        no other.

        WHY NOT ``broadcast`` / ``push_event``: both fan out to every
        subscriber. A conv_state_snapshot is per-tenant scoped and rev-gated,
        so a stray copy is harmless in itself, but fanning a repair to N tabs
        because ONE is stalled turns a targeted correction into fleet-wide
        traffic on a 60s cadence — and it would mask the very condition being
        repaired, since a healthy tab absorbing the frame looks identical to a
        stalled tab being fixed.

        Returns True iff a matching LOCAL socket was found and enqueued.
        Cross-replica is deliberately NOT attempted: the digest POST is an HTTP
        request that, in a sticky-session deployment, lands on the replica
        holding that socket; and when it does not, returning False lets the
        caller log an honest "could not reach" rather than silently doing
        nothing. Enqueue only — ``_sender`` stays the sole writer of the
        WebSocket.
        """
        if not req_id:
            return False
        with self._lock:
            targets = [c for c in self._clients
                       if getattr(c, 'req_id', '') == req_id]
        if not targets:
            return False
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._deliver, set(targets), frame)
        else:
            for client in targets:
                client.enqueue(frame)
        return True

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
    """Represents a single WebSocket connection to the push channel.

    ``user_id`` is the resolved owner of the WebSocket (from
    ``AuthContext.user_id`` at handshake — see routes/push.py::push_ws).
    Stashed for the connection lifetime so every subsequent frame handler
    can consult it without re-doing auth. Empty string means "no
    resolved user" — the single-user / personal-install / pre-auth
    default, and also what open-mode requests without a bearer token
    produce (see :func:`lib.api_keys.local_admin_context`). Downstream
    readers (``build_conv_state_snapshot``, ``snapshot_running_by_conv``)
    treat empty as "unscoped, all-registry".

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

    def __init__(self, user_id: str = '', req_id: str = ''):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        # ── Control lane (pt_afbaf3d7) ──────────────────────────────────
        # Pongs (and future control frames) must JUMP the data backlog: under
        # event-loop congestion (e.g. a 176 MB HTTP response being serialized
        # on the same loop) a pong queued behind MBs of event frames arrives
        # past the client's 8s watchdog, which then force-closes a HEALTHY
        # socket into the reconnect→refetch→stall loop. Bounded — pongs are
        # redundant by design, so silently discarding the oldest past the cap
        # is the correct degradation.
        self._ctl: deque = deque(maxlen=64)
        self._ctl_waiter: asyncio.Future | None = None
        self._connected = True
        self.user_id: str = str(user_id or '')
        self.req_id: str = str(req_id or '')

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

    async def drain(self) -> dict | None:
        """Wait for and return the next frame, or None if disconnected.

        The control lane is checked FIRST: a pending pong goes out before any
        queued data frame. A data frame already dequeued by the await below
        when a control frame lands is returned first (one-frame delay — the
        ``_sender`` loop calls drain again immediately and picks up the
        control frame next).
        """
        if not self._connected:
            return None
        while True:
            if self._ctl:
                return self._ctl.popleft()
            get_fut = asyncio.ensure_future(self._queue.get())
            waiter = asyncio.get_running_loop().create_future()
            self._ctl_waiter = waiter
            try:
                done, _pending = await asyncio.wait(
                    (get_fut, waiter), timeout=30,
                    return_when=asyncio.FIRST_COMPLETED)
            except Exception as e:
                get_fut.cancel()
                logger.debug('[Push] drain failed (signaling disconnect): %s', e)
                return None
            finally:
                self._ctl_waiter = None
            if not done:
                get_fut.cancel()
                return {'channel': 'system', 'type': 'ping'}
            if get_fut in done:
                return get_fut.result()
            get_fut.cancel()
            # Only the control waiter fired — loop back; the ctl check at the
            # top returns the control frame.

    def disconnect(self):
        self._connected = False


# Singleton hub
hub = PushHub()


def build_conv_state_snapshot(user_id='') -> dict:
    """Build the ``conv_state_snapshot`` frame for a client that just
    subscribed to ``notify:*``.

    ``user_id`` is required (empty string = pre-auth / single-user
    default) and is BOTH the scope key for the registry projection AND
    the value stamped into the outbound frame's ``userId`` field so the
    client's cross-user gate (``_frameIsOurs``) accepts it. Historically
    this was hardcoded to ``1`` — that latent multi-tenant leak is what
    pt_ab42421158214591 filed and this commit closes.

    Content sourced from ONE call to
    ``lib.tasks_pkg.manager._registry.snapshot_running_by_conv`` — the SSOT
    for "which convs have live tasks" (carrier / aborted / empty-convId
    filter shared with the notify_conv_changed seam so client sidebar and
    connect-snapshot cannot disagree by construction). The registry read is
    scoped by ``user_id`` so a snapshot built for user B never leaks user
    A's tasks (multi-tenant SSOT invariant).

    Each conv's entry independently carries a fresh
    ``[monotonic_ns, replica_id]`` rev tuple (owner mandate: per-conv rev,
    NOT a single frame-wide rev — a stale ``conv_changed`` for one conv
    must not be able to override the snapshot's state for OTHER convs).

    Best-effort: a registry snapshot failure returns an empty ``convs``
    dict — the client still receives the frame and treats it as "no live
    tasks", which is the safe default (a false negative extinguishes a
    busy dot; a real notify frame within seconds re-lights it).
    """
    try:
        # Late import — the manager package is not always importable at
        # push module load time (e.g. tests that only exercise the hub).
        from lib.tasks_pkg.manager._registry import snapshot_running_by_conv
        # pt_ab42421158214591: pass user_id through so the projection is
        # scoped to this connection's owner. Cast to str: the registry
        # stores AuthContext.user_id (a str), while some legacy callers
        # still pass DEFAULT_USER_ID=1 (int) — str() coerces both to a
        # comparable form; empty string ('' == '' == unscoped) is the
        # explicit "no filter" signal.
        raw = snapshot_running_by_conv(user_id=str(user_id or ''))
    except Exception as _e:
        logger.debug('[Push] snapshot_running_by_conv failed (%s); '
                     'sending empty snapshot', _e)
        raw = {}
    try:
        from lib.agent_core.rev_clock import _running_task_ids_rev
    except Exception as _ie:
        logger.debug('[Push] _running_task_ids_rev import failed (%s); '
                     'sending snapshot without per-conv rev', _ie)
        _running_task_ids_rev = lambda: [0, '']  # noqa: E731
    convs: dict[str, dict] = {}
    for conv_id, tids in raw.items():
        convs[conv_id] = {
            'runningTaskIds': list(tids),
            'runningTaskIdsRev': _running_task_ids_rev(),
        }
    return {
        'channel': 'notify',
        'taskId': '*',
        'type': 'conv_state_snapshot',
        'userId': user_id,
        'convs': convs,
        # ── pt_781ae072d6ee4e84: frame-level rev for the CLEAR branch ──
        # A conv ABSENT from this snapshot must have its local busy state
        # extinguished, and the client must advance that conv's rev so a
        # reordered older notify cannot un-clear the dot. The client used to
        # SYNTHESIZE that value from its own wall clock — a different clock
        # domain from the server's rev, which poisoned the strict-greater gate
        # and left the conv permanently deaf on both transports.
        #
        # Shipping the rev HERE is what makes client-side minting unnecessary
        # (and therefore forbiddable): the clear is stamped with an
        # authoritative value on the same timeline as every other rev, so
        # "cleared" and "busy" are totally ordered against each other.
        # Minted AFTER the per-conv revs above so it dominates them — the
        # snapshot is by construction the newest view of the registry.
        'rev': _running_task_ids_rev(),
    }


def push_event(channel: str, task_id: str, payload: dict):
    """Convenience function — push an event via the global hub."""
    hub.push_event(channel, task_id, payload)


def broadcast(channel: str, payload: dict):
    """Convenience function — broadcast to all clients."""
    hub.broadcast(channel, payload)
