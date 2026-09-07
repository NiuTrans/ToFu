"""lib/agent_core/push_bus.py — Cross-replica fan-out transport for PushHub.

Epic B (). See the ratified design in
``docs/ENTERPRISE_READINESS_AUDIT.md`` §3 (relay), §4 (Redis substrate) and
§3.1 (uniform bus-only delivery).

**The bug this fixes.** ``PushHub`` fan-out is process-local: a frame
published on the replica that owns a task never reaches a subscriber whose
``/api/push`` WebSocket lives on a DIFFERENT replica — it is silently dropped.
This module is the transport that carries a published frame to every replica,
each of which then re-delivers to ITS OWN local subscribers.

Two backends, selected by the SAME env as the runtime-state store
(``TOFU_RUNTIME_STATE_BACKEND``, the ratified single substrate):

  * ``InProcPushBus`` (default, ``inproc``): publish == deliver locally, exactly
    as today. Single process, no cross-replica, BYTE-IDENTICAL to the previous
    behaviour.
  * ``RedisPushBus`` (``redis``): publish == ``PUBLISH`` to a shared topic; a
    per-replica subscriber loop receives every published frame (including the
    publisher's own) and hands it to the local-delivery callback → UNIFORM
    bus-only delivery (design §3.1, one code path). Fail-OPEN: if Redis is
    unreachable, ``publish`` degrades to direct local delivery + a loud log, so
    a single-replica deployment keeps working and a multi-replica one degrades
    to "same-replica only" (today's behaviour), never a crash.

``redis`` is an OPTIONAL dependency — the import is guarded and this backend is
only built under the flag; the ``inproc`` default never imports it.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

_TOPIC = 'tofu:push:fanout'


class InProcPushBus:
    """Single-process bus: publish delivers locally. Byte-identical to the
    pre-Epic-B path."""

    def __init__(self, deliver_fn, topic=_TOPIC):
        self._deliver = deliver_fn  # callable(frame) → enqueue to local subs
        self._topic = topic

    def start(self) -> None:  # no subscriber loop needed
        pass

    def stop(self) -> None:
        pass

    def publish(self, frame: dict) -> None:
        self._deliver(frame)


class RedisPushBus:
    """Redis pub/sub bus: publish → PUBLISH; a subscriber loop re-delivers
    every received frame to THIS replica's local subscribers.

    ``client`` may be injected (tests); otherwise a lazy guarded connect.
    """

    _RETRY_MIN = 0.5
    _RETRY_MAX = 30.0

    def __init__(self, deliver_fn, client=None, topic=_TOPIC,
                 client_factory=None):
        self._deliver = deliver_fn
        self._client = client
        self._client_factory = client_factory
        self._topic = topic
        self._available = client is not None
        self._subscriber_available = False
        self._lock = threading.Lock()
        self._thread = None
        self._pubsub = None
        self._stop = threading.Event()
        self._next_retry_at = 0.0
        self._retry_delay = self._RETRY_MIN
        self._last_error = ''
        self._last_warn_at = 0.0

    def _new_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        import redis  # optional dependency — guarded by the caller
        url = os.environ.get('TOFU_REDIS_URL') or 'redis://127.0.0.1:6379/0'
        return redis.Redis.from_url(
            url, socket_connect_timeout=1.0, socket_timeout=2.0,
            health_check_interval=15, decode_responses=True)

    @staticmethod
    def _close_client(client) -> None:
        if client is None:
            return
        try:
            close = getattr(client, 'close', None)
            if callable(close):
                close()
            else:
                pool = getattr(client, 'connection_pool', None)
                if pool is not None:
                    pool.disconnect()
        except Exception as e:
            logger.debug('[PushBus] redis client close failed: %s', e)

    def _mark_failed(self, error, operation: str, *, client=None) -> None:
        """Invalidate the failed connection and arm an automatic retry."""
        with self._lock:
            old = None
            if client is None or self._client is client:
                old = self._client
                self._client = None
            elif client is not None:
                # A subscriber may fail on a stale client just after a
                # publisher installed a replacement. Close only the stale
                # argument; never tear down the newer shared client.
                old = client
            self._available = False
            self._subscriber_available = False
            now = time.monotonic()
            delay = self._retry_delay * random.uniform(0.8, 1.2)
            self._next_retry_at = now + max(0.05, delay)
            self._retry_delay = min(
                self._RETRY_MAX,
                max(self._RETRY_MIN, self._retry_delay * 2.0))
            self._last_error = '%s: %s' % (operation, error)
            should_warn = now - self._last_warn_at >= 30.0
            if should_warn:
                self._last_warn_at = now
        self._close_client(old)
        if should_warn:
            logger.warning(
                '[PushBus] redis %s failed (%s) — LOCAL-ONLY while degraded; '
                'automatic reconnect in %.1fs', operation, error,
                max(0.0, self._next_retry_at - time.monotonic()))

    def _redis(self):
        if self._client is not None:
            return self._client
        if self._stop.is_set() or time.monotonic() < self._next_retry_at:
            return None
        with self._lock:
            if self._client is not None:
                return self._client
            if self._stop.is_set() or time.monotonic() < self._next_retry_at:
                return None
            client = None
            try:
                client = self._new_client()
                client.ping()
                self._client = client
                recovered = bool(self._last_error)
                self._available = True
                self._next_retry_at = 0.0
                self._retry_delay = self._RETRY_MIN
                self._last_error = ''
                logger.info('[PushBus] redis fan-out %s',
                            'reconnected' if recovered else 'connected')
                return self._client
            except Exception as e:
                # We already hold _lock, so update the backoff inline.
                now = time.monotonic()
                delay = self._retry_delay * random.uniform(0.8, 1.2)
                self._next_retry_at = now + max(0.05, delay)
                self._retry_delay = min(
                    self._RETRY_MAX,
                    max(self._RETRY_MIN, self._retry_delay * 2.0))
                self._available = False
                self._subscriber_available = False
                self._last_error = 'connect: %s' % e
                if now - self._last_warn_at >= 30.0:
                    self._last_warn_at = now
                    logger.warning(
                        '[PushBus] redis unavailable (%s) — LOCAL-ONLY while '
                        'degraded; automatic reconnect in %.1fs', e,
                        max(0.0, self._next_retry_at - now))
                self._close_client(client)
                return None

    def on_message(self, raw) -> None:
        """Handle one frame received from the bus → deliver to local subs.

        Public so tests (and the fake broker) can drive delivery synchronously
        without a live pubsub thread.
        """
        try:
            frame = raw if isinstance(raw, dict) else json.loads(raw)
        except (TypeError, ValueError) as e:
            logger.warning('[PushBus] dropping unparseable bus frame: %s', e)
            return
        try:
            self._deliver(frame)
        except Exception as e:
            logger.warning('[PushBus] local delivery of bus frame failed: %s', e)

    def start(self) -> None:
        """Start an idempotent reconnecting subscriber supervisor.

        The supervisor starts even when the first Redis dial fails.  It keeps
        retrying with jittered exponential backoff, recreates Pub/Sub after a
        socket drop, and re-subscribes before marking fan-out healthy again.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._subscriber_loop, name='tofu-pushbus', daemon=True)
            thread = self._thread
        thread.start()
        logger.info('[PushBus] subscriber supervisor started topic=%s', self._topic)

    def _subscriber_loop(self) -> None:
        while not self._stop.is_set():
            client = self._redis()
            if client is None:
                with self._lock:
                    wait_for = max(0.05, self._next_retry_at - time.monotonic())
                self._stop.wait(min(self._RETRY_MAX, wait_for))
                continue

            pubsub = None
            try:
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self._topic)
                with self._lock:
                    self._pubsub = pubsub
                    self._subscriber_available = True
                    self._available = True
                    self._retry_delay = self._RETRY_MIN
                logger.info('[PushBus] subscribed topic=%s', self._topic)

                while not self._stop.is_set():
                    # redis-py get_message is interruptible by close() and lets
                    # stop/reconnect checks run even during a quiet topic.
                    msg = pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get('type') == 'message':
                        self.on_message(msg.get('data'))
            except Exception as e:
                if not self._stop.is_set():
                    self._mark_failed(e, 'subscribe/listen', client=client)
            finally:
                with self._lock:
                    if self._pubsub is pubsub:
                        self._pubsub = None
                    self._subscriber_available = False
                try:
                    if pubsub is not None:
                        pubsub.close()
                except Exception as e:
                    logger.debug('[PushBus] pubsub close failed: %s', e)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            pubsub = self._pubsub
            thread = self._thread
            client = self._client
        try:
            if pubsub is not None:
                pubsub.close()
        except Exception as e:
            logger.debug('[PushBus] pubsub close failed: %s', e)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._thread = None
            self._pubsub = None
            if self._client is client:
                self._client = None
            self._available = False
            self._subscriber_available = False
        self._close_client(client)

    def publish(self, frame: dict) -> None:
        r = self._redis()
        if r is None:
            # Fail-open: no bus → deliver locally so a single-replica install
            # (or a degraded fleet) still works.
            self._deliver(frame)
            return
        try:
            r.publish(self._topic, json.dumps(frame))
        except Exception as e:
            self._mark_failed(e, 'publish', client=r)
            self._deliver(frame)

    def health(self) -> dict:
        """Non-blocking state for Prometheus/support diagnostics."""
        with self._lock:
            return {
                'backend': 'redis',
                'publisher_available': bool(
                    self._client is not None and self._available),
                'subscriber_available': self._subscriber_available,
                'reconnect_in_s': max(
                    0.0, self._next_retry_at - time.monotonic()),
                'last_error': self._last_error,
            }


def make_push_bus(deliver_fn, *, client=None, topic=_TOPIC):
    """Build the push bus for the active backend (``TOFU_RUNTIME_STATE_BACKEND``).

    ``inproc`` (default) → :class:`InProcPushBus`; ``redis`` →
    :class:`RedisPushBus`. ``client`` injects a redis client (tests).
    """
    backend = (os.environ.get('TOFU_RUNTIME_STATE_BACKEND') or 'inproc').strip().lower()
    if backend == 'redis':
        return RedisPushBus(deliver_fn, client=client, topic=topic)
    return InProcPushBus(deliver_fn, topic=topic)


__all__ = ['InProcPushBus', 'RedisPushBus', 'make_push_bus']
