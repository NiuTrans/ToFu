# HOT_PATH
"""Shared transport layer: retry config, HTTP helpers, sleep utilities."""

import asyncio
import random
import time

import lib as _lib
from lib.llm_errors import AbortedError
from lib.log import get_logger

logger = get_logger(__name__)

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
