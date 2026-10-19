"""Await shared subscription-route recovery before sending a model request.

Sync/async transports call resolve_egress_route / async_resolve_egress_route.
Route health and probes remain in subscription_routes via desktop.egress;
transport cancellation, timeout and heartbeat semantics remain in _transport.
No credentials, route-health mutations, retries of sent requests or workers.
"""
from __future__ import annotations

import asyncio
import time
import threading

from lib.llm.llm_errors import AbortedError, RequestTransportError
from lib.log import get_logger

logger = get_logger(__name__)


class _RecoveryWait:
    def __init__(self, url, abort_check, on_wait, timeout_s):
        from lib.llm._transport import stream_idle_timeout_seconds
        self.url = url
        self.abort_check = abort_check
        self.on_wait = on_wait
        self.timeout_s = (stream_idle_timeout_seconds() if timeout_s is None
                          else max(0.0, float(timeout_s)))
        self.started = time.monotonic()
        self.waited = False

    def check(self):
        if self.abort_check and self.abort_check():
            raise AbortedError('Aborted while waiting for network recovery')
        elapsed = time.monotonic() - self.started
        if self.timeout_s > 0 and elapsed >= self.timeout_s:
            raise RequestTransportError(
                failure_stage='connect', reason='timeout',
                timeout_seconds=self.timeout_s, base_url=self.url)
        return elapsed

    def recovered(self):
        if self.waited and self.on_wait is not None:
            from lib.llm._transport import StreamWaitStatus
            try:
                self.on_wait(StreamWaitStatus(
                    kind='network_recovered', request_elapsed_s=time.monotonic() - self.started,
                    transport_idle_s=0, semantic_idle_s=0, response_headers_seen=False,
                    transport_byte_count=0, sse_event_count=0, reasoning_chars=0,
                    content_chars=0, tool_call_count=0))
            except Exception as exc:
                logger.debug('Network recovery observer failed: %s', type(exc).__name__)

    def delay(self, error):
        from lib.llm._transport import StreamWaitStatus
        elapsed = self.check()
        self.waited = True
        if self.on_wait is not None:
            status = StreamWaitStatus(
                kind='network_recovery', request_elapsed_s=elapsed,
                transport_idle_s=0, semantic_idle_s=0,
                response_headers_seen=False, transport_byte_count=0,
                sse_event_count=0, reasoning_chars=0, content_chars=0,
                tool_call_count=0)
            try:
                self.on_wait(status)
            except Exception as exc:
                logger.debug('Network recovery observer failed: %s', type(exc).__name__)
        # Recheck cached shared health at most once a second: a sibling's
        # successful request or a newly online desktop can recover earlier.
        delay = min(1.0, max(0.05, error.retry_after_s))
        if self.timeout_s > 0:
            delay = min(delay, max(0.0, self.timeout_s - elapsed))
        return delay


def resolve_egress_route(egress, url, *, user_id, abort_check=None,
                         on_wait=None, timeout_s=None):
    from lib.llm._transport import abortable_sleep
    wait = _RecoveryWait(url, abort_check, on_wait, timeout_s)
    while True:
        wait.check()
        try:
            route = egress.route_request(url, user_id=user_id, cancelled=abort_check)
            wait.check()
            wait.recovered()
            return route
        except egress.EgressRecoveryPending as error:
            abortable_sleep(wait.delay(error), abort_check)


async def async_resolve_egress_route(egress, url, *, user_id, abort_check=None,
                                   on_wait=None, timeout_s=None):
    from lib.llm._transport import async_abortable_sleep
    wait = _RecoveryWait(url, abort_check, on_wait, timeout_s)
    while True:
        wait.check()
        try:
            stop = threading.Event()
            def cancelled():
                return stop.is_set() or bool(abort_check and abort_check())
            probe = asyncio.create_task(asyncio.to_thread(
                egress.route_request, url, user_id=user_id, cancelled=cancelled))
            try:
                route = await asyncio.shield(probe)
            except asyncio.CancelledError:
                # Join the existing probe-wait adapter before releasing the
                # prepared request. The shared probe itself remains reusable
                # by sibling requests; only this caller's wait is cancelled.
                stop.set()
                while not probe.done():
                    try:
                        await asyncio.shield(probe)
                    except asyncio.CancelledError:
                        continue
                    except Exception as diagnostic_error:
                        logger.debug('async_resolve_egress_route failure handled: %s', type(diagnostic_error).__name__)
                        break
                if not probe.cancelled():
                    probe.exception()  # retrieve a cancellation-induced route error
                raise
            wait.check()
            wait.recovered()
            return route
        except egress.EgressRecoveryPending as error:
            await async_abortable_sleep(wait.delay(error), abort_check)
