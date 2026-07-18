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
# per attempt. The READ timeout stays large (300s) — generation is slow.
# Override per-deployment with TOFU_LLM_CONNECT_TIMEOUT.
try:
    CONNECT_TIMEOUT = float(os.environ.get('TOFU_LLM_CONNECT_TIMEOUT', '10'))
    if CONNECT_TIMEOUT <= 0:
        CONNECT_TIMEOUT = 10.0
except (ValueError, TypeError) as e:
    logger.debug('[Transport] TOFU_LLM_CONNECT_TIMEOUT parse failed, using default: %s', e)
    CONNECT_TIMEOUT = 10.0

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
                timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=300,
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
