# HOT_PATH
"""Shared transport layer: retry config, HTTP helpers, sleep utilities."""

import asyncio
from dataclasses import dataclass
import math
import os
import random
import threading
import time
import weakref
from concurrent.futures import Future

import httpx
import requests

import lib as _lib
from lib.llm.stream_result import ProviderStreamEvidence
from lib.llm_errors import AbortedError
from lib.log import get_logger

logger = get_logger(__name__)


def transport_owner_scope(owner_user_id: object | None) -> str:
    """Normalize an optional application owner for desktop bridge transport.

    ``''`` means this model call has no owner and therefore may use only
    server-side network routes. Any supplied identity must be a positive
    repository owner; account subjects and booleans never become bridge scope.
    """
    if owner_user_id is None:
        return ''
    from lib.identity import require_user_id

    return str(require_user_id(
        owner_user_id, context='LLM desktop egress owner'))


def _bounded_env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning('[Transport] invalid %s=%r; using %s',
                       name, os.environ.get(name), default)
        value = int(default)
    return max(minimum, min(maximum, value))


# Personal-server defaults: allow a burst of parallel agents without retaining
# one idle socket per completed branch forever.  requests/httpx only discover a
# peer-closed keep-alive when the pool is used again; a large idle pool can
# therefore sit in CLOSE_WAIT between turns. Four warm sockets preserve
# sequential/short-burst reuse, while the active connection ceiling remains 32.
LLM_MAX_CONNECTIONS = _bounded_env_int(
    'TOFU_LLM_MAX_CONNECTIONS', 32, 4, 256)
LLM_MAX_KEEPALIVE_CONNECTIONS = min(
    LLM_MAX_CONNECTIONS,
    _bounded_env_int('TOFU_LLM_MAX_KEEPALIVE_CONNECTIONS', 4, 1, 64),
)
LLM_KEEPALIVE_EXPIRY_S = _bounded_env_int(
    'TOFU_LLM_KEEPALIVE_EXPIRY_SECS', 15, 1, 300)

# ── Connect-phase timeout (seconds) ──
# How long to wait for the TCP/TLS handshake to the model endpoint before
# declaring it unreachable. Kept short (default 10s) so a dead self-hosted
# box fails over to a healthy slot fast instead of burning a full minute
# per attempt.
#
# This is the socket-connect bound: it covers "the box never answered the
# SYN", not model generation. Once the handshake succeeds there is no socket
# read timeout; the watchdog separately bounds silence and the period before
# first stream activity, then allows an active generation to run without a
# request-wide wall-clock deadline.
# Override per-deployment with TOFU_LLM_CONNECT_TIMEOUT.
try:
    CONNECT_TIMEOUT = float(os.environ.get('TOFU_LLM_CONNECT_TIMEOUT', '10'))
    if CONNECT_TIMEOUT <= 0:
        CONNECT_TIMEOUT = 10.0
except (ValueError, TypeError) as e:
    logger.debug('[Transport] TOFU_LLM_CONNECT_TIMEOUT parse failed, using default: %s', e)
    CONNECT_TIMEOUT = 10.0

# ── Abort poll interval (seconds) ──
# A blocked socket read sits OUTSIDE the SSE line loop, so the in-loop
# ``abort_check`` cannot observe a Stop pressed while the upstream is
# silent. StreamIdleWatchdog polls the same predicate on this cadence and
# closes the response, preserving Stop control independently of socket reads
# and the stream-idle timeout.
# 0.5s: a Stop feels instant to a human, and the poll is one flag read.
try:
    ABORT_POLL_INTERVAL = float(
        os.environ.get('TOFU_LLM_ABORT_POLL_INTERVAL', '0.5'))
    if ABORT_POLL_INTERVAL <= 0:
        ABORT_POLL_INTERVAL = 0.5
except (ValueError, TypeError) as e:
    logger.debug('[Transport] TOFU_LLM_ABORT_POLL_INTERVAL parse failed, using default: %s', e)
    ABORT_POLL_INTERVAL = 0.5

# ── Idle heartbeat (seconds) ──
# Fires ``on_beat(idle_seconds)`` whenever the attempt has produced nothing
# for this long — before the first byte AND during any mid-stream silence.
#
# Load-bearing, not cosmetic. Two consumers depend on it:
#   1. the HUD, which shows a live "still waiting, here is what the slot
#      pool knows" phase instead of a frozen spinner;
#   2. the stuck-task reaper (lib/tasks_pkg/manager/_maintenance.py), which
#      force-fails a task once BOTH ``_t_last_event`` and
#      ``_dispatch_heartbeat`` have been silent past
#      TOFU_STUCK_TASK_MAX_SILENT_SECS (30 min). With no socket read timeout,
#      the beat keeps those clocks fresh throughout the configured stream-idle
#      window (including deployments that disable or extend it). Aliveness is
#      proven by beating, never inferred from a blocked read.
# 20s is well inside the reaper window while adding only a handful of
# transient phase events. 0 disables beats.
# Override with TOFU_LLM_IDLE_HEARTBEAT_S.
try:
    IDLE_HEARTBEAT_S = float(
        os.environ.get('TOFU_LLM_IDLE_HEARTBEAT_S', '20'))
    if IDLE_HEARTBEAT_S < 0:
        IDLE_HEARTBEAT_S = 20.0
except (ValueError, TypeError) as e:
    logger.debug('[Transport] TOFU_LLM_IDLE_HEARTBEAT_S parse failed, using default: %s', e)
    IDLE_HEARTBEAT_S = 20.0


# ── Rolling stream-activity idle timeout (seconds) ──
# Match native Codex's stream contract: each received transport event renews a
# 300-second idle window. SSE comments/keep-alives and WebSocket messages count
# as activity even when they carry no reasoning, content, or tool delta. The
# bound is not a total request deadline; an active stream may run indefinitely.
# A genuinely silent stream is closed and enters the existing transport-
# interruption diagnostics/retry path. The socket itself keeps read=None so
# user Stop remains independently enforced by the abort poll.
_STREAM_IDLE_TIMEOUT_ENV = 'TOFU_LLM_IDLE_STREAM_TIMEOUT_S'
_DEPRECATED_STREAM_IDLE_TIMEOUT_ENVS = (
    'TOFU_LLM_SEMANTIC_IDLE_TIMEOUT_S',
    'TOFU_LLM_NO_ACTIONABLE_TIMEOUT_S',
)


def _stream_idle_timeout_from_environment() -> float:
    configured = os.environ.get(_STREAM_IDLE_TIMEOUT_ENV)
    configured_by = _STREAM_IDLE_TIMEOUT_ENV
    if configured is None:
        for alias in _DEPRECATED_STREAM_IDLE_TIMEOUT_ENVS:
            configured = os.environ.get(alias)
            if configured is not None:
                configured_by = alias
                logger.warning(
                    '[Transport] %s is deprecated; use %s. The value now '
                    'bounds transport inactivity, and keep-alives renew it.',
                    alias,
                    _STREAM_IDLE_TIMEOUT_ENV,
                )
                break
    if configured is None:
        configured = '300'
    try:
        value = float(configured)
    except (TypeError, ValueError, OverflowError) as error:
        logger.warning(
            '[Transport] invalid %s=%r; using 300s: %s',
            configured_by, configured, error,
        )
        return 300.0
    if not math.isfinite(value):
        logger.warning(
            '[Transport] non-finite %s=%r; using 300s',
            configured_by, configured,
        )
        return 300.0
    if value <= 0:
        return 0.0
    # Preserve the existing operator-safety floor: a typo must not turn into a
    # near-instant, repeatedly billed reconnect loop.
    return max(30.0, value)


IDLE_STREAM_TIMEOUT_S = _stream_idle_timeout_from_environment()


def stream_idle_timeout_seconds() -> float:
    """Return the live transport-idle window (tests may retune the symbol)."""
    try:
        value = float(IDLE_STREAM_TIMEOUT_S)
    except (TypeError, ValueError, OverflowError):
        return 300.0
    return max(0.0, value) if math.isfinite(value) else 300.0


# Read compatibility for plugins and stored-attempt adapters. Production
# transports no longer arm a semantic-progress deadline; old environment names
# are consumed above only as deprecated aliases for the transport-idle window.
SEMANTIC_IDLE_TIMEOUT_S = 0.0
NO_ACTIONABLE_OUTPUT_TIMEOUT_S = 0.0


@dataclass(frozen=True, slots=True)
class StreamWaitStatus:
    """Current-attempt status delivered to the waiting HUD."""

    kind: str
    request_elapsed_s: float
    transport_idle_s: float
    semantic_idle_s: float
    response_headers_seen: bool
    transport_byte_count: int
    sse_event_count: int
    reasoning_chars: int
    content_chars: int
    tool_call_count: int


class StreamProgress:
    """Single monotonic progress state machine for one provider attempt."""

    _MAX_DIAGNOSTICS = 4
    _MAX_DIAGNOSTIC_CHARS = 240

    def __init__(self, timeout_s=0, *, monotonic=None, started_at=None):
        self._timeout_s = max(0.0, float(timeout_s or 0))
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        origin = (self._monotonic() if started_at is None
                  else float(started_at))
        self._request_started_at = origin
        self._last_transport_activity_at = origin
        self._last_semantic_progress_at = origin
        self._response_headers_seen = False
        self._transport_byte_count = 0
        self._sse_event_count = 0
        self._reasoning_chars = 0
        self._reasoning_chunks = 0
        self._content_chars = 0
        self._content_chunks = 0
        self._tool_call_count = 0
        self._tool_argument_chars = 0
        self._tool_argument_chunks = 0
        self._provider_finish_seen = False
        self._done_seen = False
        self._client_aborted = False
        self._semantic_timeout = False
        self._malformed_frame_count = 0
        self._diagnostics: list[str] = []

    def _now(self, value=None) -> float:
        return self._monotonic() if value is None else float(value)

    def mark_response_headers(self, now=None) -> None:
        current = self._now(now)
        with self._lock:
            self._response_headers_seen = True
            self._last_transport_activity_at = current

    def mark_transport_activity(self, now=None) -> None:
        """Renew the raw stream clock without inventing a byte count."""
        current = self._now(now)
        with self._lock:
            self._last_transport_activity_at = current

    def mark_transport_bytes(self, byte_count: int, now=None) -> None:
        count = max(0, int(byte_count or 0))
        if count == 0:
            return
        current = self._now(now)
        with self._lock:
            self._transport_byte_count += count
            self._last_transport_activity_at = current

    def mark_sse_event(self, now=None) -> None:
        current = self._now(now)
        with self._lock:
            self._sse_event_count += 1
            self._last_transport_activity_at = current

    def mark_reasoning(self, text: object, now=None) -> bool:
        if not isinstance(text, str) or not text.strip():
            return False
        current = self._now(now)
        with self._lock:
            self._reasoning_chars += len(text)
            self._reasoning_chunks += 1
            self._last_semantic_progress_at = current
        return True

    def mark_content(self, text: object, now=None) -> bool:
        if not isinstance(text, str) or not text.strip():
            return False
        current = self._now(now)
        with self._lock:
            self._content_chars += len(text)
            self._content_chunks += 1
            self._last_semantic_progress_at = current
        return True

    def mark_tool_delta(
            self, *, recognized: bool = False, argument_delta: object = '',
            now=None) -> bool:
        argument = argument_delta if isinstance(argument_delta, str) else ''
        if not recognized and not argument:
            return False
        current = self._now(now)
        with self._lock:
            if recognized:
                self._tool_call_count += 1
            if argument:
                self._tool_argument_chars += len(argument)
                self._tool_argument_chunks += 1
            self._last_semantic_progress_at = current
        return True

    def mark_provider_finish(self, now=None) -> None:
        with self._lock:
            self._provider_finish_seen = True

    def mark_done(self, now=None) -> None:
        current = self._now(now)
        with self._lock:
            self._done_seen = True
            self._last_transport_activity_at = current

    def mark_client_aborted(self) -> None:
        with self._lock:
            self._client_aborted = True

    def mark_semantic_timeout(self) -> None:
        with self._lock:
            self._semantic_timeout = True

    def mark_malformed(
            self, count: int = 1, diagnostics=()) -> None:
        issue_count = max(0, int(count or 0))
        with self._lock:
            self._malformed_frame_count += issue_count
            for raw in diagnostics or ():
                if len(self._diagnostics) >= self._MAX_DIAGNOSTICS:
                    break
                diagnostic = ' '.join(str(raw or '').split())[
                    :self._MAX_DIAGNOSTIC_CHARS]
                if diagnostic:
                    self._diagnostics.append(diagnostic)

    def timed_out(self, now=None) -> bool:
        current = self._now(now)
        with self._lock:
            if (self._timeout_s <= 0 or self._provider_finish_seen
                    or self._client_aborted):
                return False
            return current - self._last_semantic_progress_at >= self._timeout_s

    def remaining_seconds(self, now=None):
        current = self._now(now)
        with self._lock:
            if (self._timeout_s <= 0 or self._provider_finish_seen
                    or self._client_aborted):
                return None
            return max(
                0.0,
                self._timeout_s
                - (current - self._last_semantic_progress_at),
            )

    def transport_idle_seconds(self, now=None) -> float:
        current = self._now(now)
        with self._lock:
            return max(0.0, current - self._last_transport_activity_at)

    def transport_timed_out(self, timeout_s, now=None) -> bool:
        try:
            timeout = max(0.0, float(timeout_s or 0))
        except (TypeError, ValueError, OverflowError):
            return False
        if timeout <= 0:
            return False
        return self.transport_idle_seconds(now) >= timeout

    def transport_remaining_seconds(self, timeout_s, now=None):
        try:
            timeout = max(0.0, float(timeout_s or 0))
        except (TypeError, ValueError, OverflowError):
            return None
        if timeout <= 0:
            return None
        return max(0.0, timeout - self.transport_idle_seconds(now))

    def wait_status(self, now=None) -> StreamWaitStatus:
        current = self._now(now)
        with self._lock:
            has_semantic_progress = bool(
                self._reasoning_chunks or self._content_chunks
                or self._tool_call_count or self._tool_argument_chunks)
            if has_semantic_progress:
                kind = 'stream_stalled'
            elif self._response_headers_seen:
                kind = 'waiting_event'
            else:
                kind = 'waiting_headers'
            return StreamWaitStatus(
                kind=kind,
                request_elapsed_s=max(
                    0.0, current - self._request_started_at),
                transport_idle_s=max(
                    0.0, current - self._last_transport_activity_at),
                semantic_idle_s=max(
                    0.0, current - self._last_semantic_progress_at),
                response_headers_seen=self._response_headers_seen,
                transport_byte_count=self._transport_byte_count,
                sse_event_count=self._sse_event_count,
                reasoning_chars=self._reasoning_chars,
                content_chars=self._content_chars,
                tool_call_count=self._tool_call_count,
            )

    def evidence(self, now=None) -> ProviderStreamEvidence:
        current = self._now(now)
        with self._lock:
            return ProviderStreamEvidence(
                request_elapsed_ms=round(max(
                    0.0, current - self._request_started_at) * 1000),
                response_headers_seen=self._response_headers_seen,
                transport_byte_count=self._transport_byte_count,
                sse_event_count=self._sse_event_count,
                reasoning_chars=self._reasoning_chars,
                reasoning_chunks=self._reasoning_chunks,
                content_chars=self._content_chars,
                content_chunks=self._content_chunks,
                tool_call_count=self._tool_call_count,
                tool_argument_chars=self._tool_argument_chars,
                tool_argument_chunks=self._tool_argument_chunks,
                provider_finish_seen=self._provider_finish_seen,
                done_seen=self._done_seen,
                malformed_frame_count=self._malformed_frame_count,
                semantic_progress_timeout=self._semantic_timeout,
                semantic_idle_timeout_ms=round(self._timeout_s * 1000),
                client_aborted=self._client_aborted,
                last_semantic_progress_age_ms=round(max(
                    0.0, current - self._last_semantic_progress_at) * 1000),
                last_transport_activity_age_ms=round(max(
                    0.0, current - self._last_transport_activity_at) * 1000),
                diagnostics=tuple(self._diagnostics),
            )

    def snapshot(self, now=None) -> dict:
        """Compatibility dictionary for existing bounded diagnostics."""
        evidence = self.evidence(now)
        return {
            'timeout_s': self._timeout_s,
            'request_elapsed_s': evidence.request_elapsed_ms / 1000,
            'last_progress_age_s': (
                evidence.last_semantic_progress_age_ms / 1000),
            'reasoning_chars': evidence.reasoning_chars,
            'reasoning_chunks': evidence.reasoning_chunks,
            'content_chars': evidence.content_chars,
            'content_chunks': evidence.content_chunks,
            'tool_calls': evidence.tool_call_count,
            'actionable_output_seen': bool(
                evidence.content_chunks or evidence.tool_call_count
                or evidence.tool_argument_chunks),
        }

    # Transitional callback names used by provider accumulators.
    notify_reasoning_progress = mark_reasoning

    def notify_actionable_output(self) -> None:
        """Renew the rolling window at a legacy callback seam."""
        current = self._now()
        with self._lock:
            self._last_semantic_progress_at = current


# Import compatibility for plugins. Production transports instantiate
# ``StreamProgress`` directly.
SemanticStallClock = StreamProgress


class StreamIdleWatchdog:
    """Watches one HTTP attempt while it is idle.

    ``start()`` arms three independent schedules:

      * **heartbeat** — ``on_beat(idle_seconds)`` once the attempt has been
        silent for ``heartbeat_interval``, and every interval thereafter
        while it stays silent.
      * **abort poll** — ``abort_check()`` every ``ABORT_POLL_INTERVAL``;
        the first True latches ``aborted`` and fires ``on_abort()`` (the
        transport supplies a closure that closes the response, unblocking
        the read).
      * **idle timeout** — when ``idle_timeout`` is positive and the attempt
        has been silent that long, latch ``idle_timed_out`` and fire
        ``on_idle_timeout()`` (the transport closes the response). Every raw
        event, including an SSE keep-alive, renews this rolling window.

    ``notify_activity()`` resets the idle clock but does NOT disarm: a
    stream that delivers a byte and then goes quiet again resumes beating,
    and abort stays pollable for the whole attempt. This is the difference
    that matters now that no read timeout exists — a mid-stream stall is
    just as unbounded as a pre-first-byte one, so both need the beat and
    both need the abort poll.

    Callback exceptions are swallowed + debug-logged: a HUD-side bug must
    never take the request watchdog down with it.

    Known boundary: an abort that fires before the response object exists
    (the pre-headers wait) can only latch ``aborted`` — there is no socket
    handle to close yet. The transport checks the flag once ``post()``
    returns and raises ``AbortedError`` then.
    """

    def __init__(self, *, heartbeat_interval=0, on_beat=None,
                 on_progress=None, progress=None,
                 abort_check=None, on_abort=None, idle_timeout=0,
                 on_idle_timeout=None, actionable_timeout=0,
                 on_actionable_timeout=None):
        self._interval = float(heartbeat_interval or 0)
        self._on_beat = on_beat
        self._on_progress = on_progress
        self._abort_check = abort_check
        self._on_abort = on_abort
        self._idle_timeout = float(idle_timeout or 0)
        self._on_idle_timeout = on_idle_timeout
        # Accepted for plugin compatibility only. Semantic inactivity remains
        # observable in ``progress`` but no longer arms a termination schedule.
        self._done = threading.Event()
        self._aborted = False
        self._idle_timed_out = False
        self._actionable_timed_out = False
        self._started_at = time.monotonic()
        self.progress = progress or StreamProgress(
            0, started_at=self._started_at)
        self._thread = None

    @property
    def aborted(self):
        return self._aborted

    @property
    def idle_timed_out(self):
        return self._idle_timed_out

    @property
    def actionable_timed_out(self):
        return self._actionable_timed_out

    def _beats_on(self):
        return self._interval > 0 and (
            self._on_beat is not None or self._on_progress is not None)

    def start(self):
        if (not self._beats_on() and self._abort_check is None
                and self._idle_timeout <= 0):
            return  # nothing to watch
        self._thread = threading.Thread(
            target=self._run, name='stream-idle-watchdog', daemon=True)
        self._thread.start()

    def notify_activity(self):
        """Record upstream activity — resets the idle clock, keeps watching."""
        self.progress.mark_transport_activity()

    def notify_response_headers(self):
        self.notify_activity()
        self.progress.mark_response_headers()

    def notify_transport_bytes(self, byte_count):
        self.notify_activity()
        self.progress.mark_transport_bytes(byte_count)

    def notify_reasoning_progress(self, text) -> bool:
        """Renew the rolling semantic clock for non-blank reasoning."""
        return self.progress.mark_reasoning(text)

    def notify_actionable_output(self):
        """Renew the rolling semantic deadline at a legacy callback seam."""
        self.progress.notify_actionable_output()

    def cancel(self):
        self._done.set()

    def _run(self):
        last_beat = time.monotonic()
        while not self._done.is_set():
            if self._abort_check is not None:
                try:
                    if self._abort_check():
                        self._aborted = True
                        if self._on_abort:
                            try:
                                self._on_abort()
                            except Exception as e:
                                logger.debug('[Watchdog] on_abort raised: %s', e)
                        return
                except Exception as e:
                    logger.debug('[Watchdog] abort_check raised: %s', e)
            now = time.monotonic()
            idle = self.progress.transport_idle_seconds(now)
            if self._idle_timeout > 0 and idle >= self._idle_timeout:
                self._idle_timed_out = True
                if self._on_idle_timeout is not None:
                    try:
                        self._on_idle_timeout()
                    except Exception as e:
                        logger.debug('[Watchdog] on_idle_timeout raised: %s', e)
                return
            if (self._beats_on() and idle >= self._interval
                    and (now - last_beat) >= self._interval):
                last_beat = now
                if self._on_progress is not None:
                    try:
                        self._on_progress(self.progress.wait_status(now))
                    except Exception as e:
                        logger.debug('[Watchdog] on_progress raised: %s', e)
                if self._on_beat is not None:
                    try:
                        self._on_beat(idle)
                    except Exception as e:
                        logger.debug('[Watchdog] on_beat raised: %s', e)
            wait = ABORT_POLL_INTERVAL if self._abort_check is not None else self._interval
            if self._beats_on():
                wait = min(wait, self._interval)
            if self._idle_timeout > 0:
                if wait <= 0:
                    wait = self._idle_timeout
                else:
                    wait = min(wait, self._idle_timeout)
            idle_remaining = self.progress.transport_remaining_seconds(
                self._idle_timeout, now)
            if idle_remaining is not None:
                idle_remaining = max(0.01, idle_remaining)
                if wait <= 0:
                    wait = idle_remaining
                else:
                    wait = min(wait, idle_remaining)
            if self._done.wait(max(wait, 0.01)):
                return

# ── Retry config for transient API errors (streaming & non-streaming) ──
MAX_STREAM_RETRIES = 4          # retry up to 4 times (5 attempts total)
RETRY_BACKOFF_BASE = 3          # base backoff in seconds (exponential: 3, 6, 12, 24)
RETRY_BACKOFF_MAX = 30          # cap backoff at 30s
RETRY_JITTER = 1.0              # random ±1s jitter


def retry_wait(attempt: int) -> float:
    """Exponential backoff with jitter: base 3s, 6s, 12s, 24s (capped at 30s) ±1s jitter."""
    base = min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_MAX)
    return base + random.uniform(-RETRY_JITTER, RETRY_JITTER)


def abortable_sleep(seconds: float, abort_check=None, interval: float = 0.5):
    """Sleep for `seconds` but check abort_check every `interval`.
    Raises AbortedError if abort is detected during the sleep."""
    if not abort_check:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if abort_check():
            raise AbortedError('User aborted during retry backoff')
        remaining = deadline - time.monotonic()
        time.sleep(min(interval, max(0, remaining)))



async def async_abortable_sleep(seconds: float, abort_check=None, interval: float = 0.5):
    """Async version of abortable_sleep."""
    if not abort_check:
        await asyncio.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if abort_check():
            raise AbortedError('User aborted during retry backoff')
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(interval, max(0, remaining)))


def post_headers_abortable(post_fn, *, is_aborted, poll_interval=None):
    """Run a blocking stream-open ``post_fn`` on a daemon thread while polling
    ``is_aborted``, so a Stop pressed during the response-header wait lands
    within one poll interval instead of whenever the upstream first speaks.

    ``post_fn`` blocks until response headers arrive; before that point no
    response handle exists, so the idle watchdog's ``resp.close()`` cannot
    reach the socket (the read sits inside ``session.post``). On abort the
    in-flight request cannot be cancelled through the requests API either:
    the orphaned thread keeps the socket until the upstream answers or its
    own timeout fires, then closes it. The abandon is deliberate — user Stop
    responsiveness outranks one leaked half-open connection bounded by the
    upstream's own timeout.
    """
    outcome: Future = Future()
    abandoned = threading.Event()

    def _run_post():
        resp = None
        try:
            resp = post_fn()
            outcome.set_result(resp)
        except BaseException as error:  # re-raised in the caller thread below
            outcome.set_exception(error)
        finally:
            if abandoned.is_set() and resp is not None:
                try:
                    resp.close()
                except Exception as close_error:
                    # Cleanup must not replace the already-delivered user
                    # abort, but a failed close can retain a socket until
                    # the peer timeout and therefore needs bounded evidence.
                    logger.warning(
                        '[Transport] abandoned response close failed: %s',
                        type(close_error).__name__,
                    )

    worker = threading.Thread(
        target=_run_post, name='llm-post-headers', daemon=True)
    worker.start()
    while True:
        worker.join(poll_interval or ABORT_POLL_INTERVAL)
        if not worker.is_alive():
            break
        if is_aborted is not None and is_aborted():
            abandoned.set()
            raise AbortedError(
                'User aborted while awaiting response headers')
    return outcome.result()

def attach_limit_learned(usage, limit_learned):
    """Attach an auto-learned model-limit marker to a usage dict.

    Shared by the sync + async stream retry loops so the ``usage`` shape stays
    identical across transports. Returns the (possibly newly-created) usage
    dict; a no-op returning ``usage`` unchanged when ``limit_learned`` is falsy.
    """
    if not limit_learned:
        return usage
    if usage is None:
        usage = {}
    usage['_model_limit_learned'] = limit_learned
    return usage


def apply_model_limit_retry(body, err, log_prefix=''):
    """Handle a ``ModelLimitError`` in a stream retry loop.

    Clamps ``body['max_tokens']`` to the endpoint-detected limit and returns the
    ``_limit_learned`` marker dict (attached to ``usage`` on the eventual
    success via :func:`attach_limit_learned`). Shared by both transports.
    """
    body['max_tokens'] = err.detected_limit
    logger.warning('%s ⚙️ Auto-learned max_tokens for %s: %d → %d, retrying…',
                   log_prefix, err.model, err.requested_limit, err.detected_limit)
    return {
        'model': err.model,
        'old_limit': err.requested_limit,
        'new_limit': err.detected_limit,
    }


def prepare_retryable_wait(attempt, err, abort_check, log_prefix=''):
    """Shared decision for a ``_RETRYABLE`` error in the stream retry loop.

    On a NON-final attempt: honor abort (raise ``AbortedError``), compute the
    backoff wait, log the transient-error warning, and RETURN the wait. The
    caller performs the actual sleep in its own sync/async idiom
    (``abortable_sleep`` / ``async_abortable_sleep`` bound in the caller's
    module) so the transport-level monkeypatch seam the tests rely on stays
    intact. On the FINAL attempt: log the exhaustion error and re-raise ``err``.

    Returns:
        float: the number of seconds the caller should sleep before retrying.

    Raises:
        AbortedError: abort was requested before the retry sleep.
        The original ``err``: no attempts remain.
    """
    if attempt < MAX_STREAM_RETRIES:
        if abort_check and abort_check():
            logger.debug('%s ✋ Abort detected before retry sleep, stopping.', log_prefix)
            raise AbortedError('User aborted before retry')
        wait = retry_wait(attempt)
        # §2.2 retry-loop row: each attempt = WARNING *without* exc_info —
        # error.log captures WARNING+, so a traceback here spams the error
        # log with self-healing noise (the next attempt usually succeeds).
        # Only the final-exhaustion ERROR below keeps exc_info.
        logger.warning('%s ⚠ Transient error (attempt %d): %s: %s — retrying in %.1fs …',
                       log_prefix, attempt + 1, type(err).__name__, err, wait)
        return wait
    logger.error('%s ✖ All %d attempts failed.', log_prefix,
                 1 + MAX_STREAM_RETRIES, exc_info=True)
    raise err


def headers():
    """Build default request headers with current API key."""
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {_lib.LLM_API_KEY}',
    }


def chat_url():
    """Build chat completions URL from current config."""
    return f'{_lib.LLM_BASE_URL}/chat/completions'


# ═══════════════════════════════════════════════════════
#  Connection pooling — reuse TCP/TLS across turns
# ═══════════════════════════════════════════════════════
# A fresh ``requests.post`` / ``httpx.AsyncClient`` per turn pays a full
# TCP+TLS handshake (~50–300ms WAN) on the critical path of EVERY turn,
# independent of conversation length. Reusing a keep-alive connection pool
# removes that fixed latency. Proxy *resolution* still happens per call
# (cheap urlparse + dict lookup in ``proxies_for``), so a runtime Settings
# proxy change still applies — only the expensive client object is cached.

# ── Sync: one process-wide Session (pools connections keyed by host+proxy) ──
_sync_session: "requests.Session | None" = None
_sync_session_lock = threading.Lock()


def get_sync_session() -> "requests.Session":
    """Return a process-wide ``requests.Session`` with a keep-alive pool.

    ``requests.Session`` pools connections per (host, proxy), so the
    per-request ``proxies=proxies_for(url)`` kwarg is preserved unchanged —
    the Session merely reuses an already-open connection when the same
    endpoint is hit again on a later turn.
    """
    global _sync_session
    if _sync_session is None:
        with _sync_session_lock:
            if _sync_session is None:
                _sync_session = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=LLM_MAX_KEEPALIVE_CONNECTIONS,
                    pool_maxsize=LLM_MAX_KEEPALIVE_CONNECTIONS,
                    max_retries=0,
                    # Never turn the retained-idle cap into an active-request
                    # semaphore. Parallel agents may exceed four connections;
                    # the surplus sockets simply are not kept after use.
                    pool_block=False,
                )
                _sync_session.mount('http://', adapter)
                _sync_session.mount('https://', adapter)
                logger.debug('[Transport] Created shared requests.Session')
    return _sync_session


# ── Async: one AsyncClient per (event-loop, resolved-proxy) ──
# httpx binds ``proxy=`` at construction, so a single client cannot serve
# URLs that resolve to different proxies (e.g. localhost-bypass vs remote).
# We therefore cache one client per resolved proxy value. Clients are also
# keyed by their owning event loop via a WeakKeyDictionary: an ``AsyncClient``
# is bound to the loop it was created on, and a stale client from a
# closed-and-GC'd loop (common in tests) must never be handed back. In
# production there is one long-lived loop → exactly one client per proxy.
_async_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_async_clients_lock = threading.Lock()


def get_async_client(proxy_url) -> "httpx.AsyncClient":
    """Return a keep-alive ``httpx.AsyncClient`` for *proxy_url* on this loop.

    Args:
        proxy_url: the resolved proxy URL (or ``None`` for a direct
            connection). Different values get different pooled clients
            because httpx fixes the proxy at construction time.

    The client is reused across turns on the same event loop, so the
    TCP/TLS handshake is amortised instead of paid per turn.
    """
    loop = asyncio.get_event_loop()
    with _async_clients_lock:
        by_proxy = _async_clients.get(loop)
        if by_proxy is None:
            by_proxy = {}
            _async_clients[loop] = by_proxy
        client = by_proxy.get(proxy_url)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                proxy=proxy_url,
                # ``proxy=None`` means an EXPLICIT direct route.  httpx would
                # otherwise re-read HTTP(S)_PROXY from the environment and
                # silently turn that direct candidate back into the env route.
                # async_proxy_for/subscription_routes already resolve the env
                # proxy to an explicit URL when it is wanted.
                trust_env=False,
                # read=None: no read timeout. A slow generation is not a
                # failure; a Stop is honored by StreamIdleWatchdog's abort
                # poll instead. write/pool stay bounded — neither is a wait
                # for the model (write = uploading our own request body,
                # pool = queueing for a free connection), and an unbounded
                # pool wait would deadlock silently rather than wait.
                timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=None,
                                      write=60, pool=60),
                limits=httpx.Limits(
                    max_connections=LLM_MAX_CONNECTIONS,
                    max_keepalive_connections=LLM_MAX_KEEPALIVE_CONNECTIONS,
                    keepalive_expiry=LLM_KEEPALIVE_EXPIRY_S,
                ),
                # Completion endpoints are exact POST targets. Following a
                # provider-controlled 30x can bypass the base_url egress check
                # and can rewrite POST to GET, so fail visibly instead.
                follow_redirects=False,
            )
            by_proxy[proxy_url] = client
            # proxy_url may contain vault-injected userinfo. Never put it in a
            # log record, including DEBUG/error aggregation sinks.
            logger.debug('[Transport] Created shared httpx.AsyncClient route=%s',
                         'configured-proxy' if proxy_url else 'direct')
        return client


def reset_pools_for_test():
    """Drop pooled sync/async clients — test-only helper.

    Lets a test assert a fresh pool and avoids leaking a client bound to a
    test event loop across tests. Async clients are best-effort closed.
    """
    global _sync_session
    with _sync_session_lock:
        if _sync_session is not None:
            try:
                _sync_session.close()
            except Exception as e:
                logger.debug('[Transport] sync session close failed: %s', e)
        _sync_session = None
    with _async_clients_lock:
        for by_proxy in list(_async_clients.values()):
            for client in list(by_proxy.values()):
                try:
                    if not client.is_closed:
                        # aclose() is async; drop the ref and let GC/atexit
                        # reclaim. Best-effort sync close of transport.
                        client._transport = None  # noqa: SLF001
                except Exception as e:
                    logger.debug('[Transport] async client drop failed: %s', e)
        _async_clients.clear()
