"""lib/agent_core/push_bus.py — Cross-replica fan-out transport for PushHub.

Epic B (board `pt_823ff5a3bf004c40`). See the ratified design in
``docs/EPIC_B_PUSH_FANOUT_DESIGN.md`` §3 (relay), §4 (Redis substrate) and
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
import threading

from lib.env_compat import getenv_compat
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

    def __init__(self, deliver_fn, client=None, topic=_TOPIC):
        self._deliver = deliver_fn
        self._client = client
        self._topic = topic
        self._available = True
        self._lock = threading.Lock()
        self._thread = None
        self._pubsub = None
        self._stop = threading.Event()

    def _redis(self):
        if not self._available:
            return None
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                import redis  # optional dependency — guarded
                url = getenv_compat('TOFU_REDIS_URL') or 'redis://127.0.0.1:6379/0'
                client = redis.Redis.from_url(
                    url, socket_connect_timeout=1.0, socket_timeout=1.0,
                    decode_responses=True)
                client.ping()
                self._client = client
                logger.info('[PushBus] redis fan-out connected (%s)', url)
                return self._client
            except Exception as e:
                self._available = False
                logger.warning(
                    '[PushBus] redis unavailable (%s) — fan-out degrades to '
                    'LOCAL-ONLY delivery (same-replica clients only) until '
                    'restart', e)
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
        """Start the background subscriber loop (idempotent). No-op if Redis
        is unavailable — publish() then fails open to local delivery."""
        r = self._redis()
        if r is None or self._thread is not None:
            return
        try:
            self._pubsub = r.pubsub(ignore_subscribe_messages=True)
            self._pubsub.subscribe(self._topic)
        except Exception as e:
            logger.warning('[PushBus] subscribe failed (%s) — local-only', e)
            self._available = False
            return

        def _loop():
            for msg in self._pubsub.listen():
                if self._stop.is_set():
                    break
                if msg and msg.get('type') == 'message':
                    self.on_message(msg.get('data'))

        self._thread = threading.Thread(
            target=_loop, name='tofu-pushbus', daemon=True)
        self._thread.start()
        logger.info('[PushBus] subscriber loop started on topic=%s', self._topic)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._pubsub is not None:
                self._pubsub.close()
        except Exception as e:
            logger.debug('[PushBus] pubsub close failed: %s', e)

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
            logger.warning('[PushBus] publish failed (%s) — local-only '
                           'delivery this frame', e)
            self._deliver(frame)


def make_push_bus(deliver_fn, *, client=None, topic=_TOPIC):
    """Build the push bus for the active backend (``TOFU_RUNTIME_STATE_BACKEND``).

    ``inproc`` (default) → :class:`InProcPushBus`; ``redis`` →
    :class:`RedisPushBus`. ``client`` injects a redis client (tests).
    """
    backend = (getenv_compat('TOFU_RUNTIME_STATE_BACKEND') or 'inproc').strip().lower()
    if backend == 'redis':
        return RedisPushBus(deliver_fn, client=client, topic=topic)
    return InProcPushBus(deliver_fn, topic=topic)


__all__ = ['InProcPushBus', 'RedisPushBus', 'make_push_bus']
