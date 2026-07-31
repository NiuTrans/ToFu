# HOT_PATH
"""Shared transport layer: retry config, HTTP helpers, sleep utilities."""

import asyncio
import os
import random
import threading
import time
import weakref

import httpx
import requests

import lib as _lib
from lib.llm_errors import AbortedError
from lib.log import get_logger

logger = get_logger(__name__)

# ── Connect-phase timeout (seconds) ──
# How long to wait for the TCP/TLS handshake to the model endpoint before
# declaring it unreachable. Kept short (default 10s) so a dead self-hosted
# box fails over to a healthy slot fast instead of burning a full minute
# per attempt.
#
# This is the ONLY timeout left on an LLM request, and deliberately so: it
# bounds "the box never answered the SYN", i.e. a crash, not a wait. Once
# the handshake succeeds there is NO read timeout at all — generation may
# take as long as it takes, and a user who does not want to wait presses
# Stop (honored by StreamIdleWatchdog's abort poll below).
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
# closes the response, which is what makes "no timeouts, I will stop it
# myself" actually true rather than a promise the transport cannot keep.
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
#      TOFU_STUCK_TASK_MAX_SILENT_SECS (30 min). With no read timeout left
#      to interrupt a long silence, this beat is the ONLY thing keeping
#      those clocks fresh — remove it and the reaper becomes the new
#      30-minute timeout, killing exactly the long waits we just made
#      legal. Aliveness is proven by beating, never by not-timing-out.
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


class StreamIdleWatchdog:
    """Watches one HTTP attempt while it is idle — without bounding it.

    ``start()`` arms two independent schedules:

      * **heartbeat** — ``on_beat(idle_seconds)`` once the attempt has been
        silent for ``heartbeat_interval``, and every interval thereafter
        while it stays silent.
      * **abort poll** — ``abort_check()`` every ``ABORT_POLL_INTERVAL``;
        the first True latches ``aborted`` and fires ``on_abort()`` (the
        transport supplies a closure that closes the response, unblocking
        the read).

    ``notify_activity()`` resets the idle clock but does NOT disarm: a
    stream that delivers a byte and then goes quiet again resumes beating,
    and abort stays pollable for the whole attempt. This is the difference
    that matters now that no read timeout exists — a mid-stream stall is
    just as unbounded as a pre-first-byte one, so both need the beat and
    both need the abort poll.

    There is deliberately NO time-based kill. A wait is not a failure.

    Callback exceptions are swallowed + debug-logged: a HUD-side bug must
    never take the request watchdog down with it.

    Known boundary: an abort that fires before the response object exists
    (the pre-headers wait) can only latch ``aborted`` — there is no socket
    handle to close yet. The transport checks the flag once ``post()``
    returns and raises ``AbortedError`` then.
    """

    def __init__(self, *, heartbeat_interval=0, on_beat=None,
                 abort_check=None, on_abort=None):
        self._interval = float(heartbeat_interval or 0)
        self._on_beat = on_beat
        self._abort_check = abort_check
        self._on_abort = on_abort
        self._done = threading.Event()
        self._aborted = False
        self._lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._thread = None

    @property
    def aborted(self):
        return self._aborted

    def _beats_on(self):
        return self._interval > 0 and self._on_beat is not None

    def start(self):
        if not self._beats_on() and self._abort_check is None:
            return  # nothing to watch
        self._thread = threading.Thread(
            target=self._run, name='stream-idle-watchdog', daemon=True)
        self._thread.start()

    def notify_activity(self):
        """Record upstream activity — resets the idle clock, keeps watching."""
        with self._lock:
            self._last_activity = time.monotonic()

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
            with self._lock:
                idle = now - self._last_activity
            if (self._beats_on() and idle >= self._interval
                    and (now - last_beat) >= self._interval):
                last_beat = now
                try:
                    self._on_beat(idle)
                except Exception as e:
                    logger.debug('[Watchdog] on_beat raised: %s', e)
            wait = ABORT_POLL_INTERVAL if self._abort_check is not None else self._interval
            if self._beats_on():
                wait = min(wait, self._interval)
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
                # read=None: no read timeout. A slow generation is not a
                # failure; a Stop is honored by StreamIdleWatchdog's abort
                # poll instead. write/pool stay bounded — neither is a wait
                # for the model (write = uploading our own request body,
                # pool = queueing for a free connection), and an unbounded
                # pool wait would deadlock silently rather than wait.
                timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=None,
                                      write=60, pool=60),
                follow_redirects=True,
            )
            by_proxy[proxy_url] = client
            logger.debug('[Transport] Created shared httpx.AsyncClient proxy=%s',
                         proxy_url or '(direct)')
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
