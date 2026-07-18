"""lib/mcp/client/_errors.py — connect-failure diagnostics + error type.

``MCPConnectError`` plus the low-level helpers that classify exceptions
(transport-dead vs call-timeout) and capture a subprocess's stderr tail for
user-facing diagnosis. Pure/leaf module — no other client submodule imports.
"""

from __future__ import annotations

import asyncio

from lib.log import get_logger

logger = get_logger(__name__)


# How many bytes of child-process stderr to keep for failure diagnosis.
# Just enough to surface a Python traceback or a "module not found" line
# without flooding the UI / log file.
_MCP_STDERR_TAIL_BYTES = 8192


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Return the deepest non-group leaf exception in a (possibly nested)
    ``BaseExceptionGroup`` chain.

    anyio / mcp wrap the real failure in 2-3 levels of
    ``ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)``,
    which is useless to show users. We walk ``.exceptions`` to find the
    first concrete leaf and return it. If no leaf is found (degenerate
    case), the original exception is returned unchanged.
    """
    try:
        Group = BaseExceptionGroup  # type: ignore[name-defined]
    except NameError:  # pragma: no cover — Python < 3.11
        return exc

    seen: set[int] = set()
    cur: BaseException | None = exc
    while isinstance(cur, Group):
        if id(cur) in seen:
            break
        seen.add(id(cur))
        subs = list(getattr(cur, 'exceptions', ()) or ())
        if not subs:
            break
        # Prefer the first non-group sub; otherwise descend into the first group.
        leaf = next((s for s in subs if not isinstance(s, Group)), None)
        cur = leaf if leaf is not None else subs[0]
    return cur if cur is not None else exc


def _is_transport_dead_error(exc: BaseException) -> bool:
    """Heuristically decide whether ``exc`` means the MCP transport is gone.

    Used by the reactive reconnect path: a tool call that fails because the
    underlying stdio pipe / SSE stream died (subprocess crashed, peer closed,
    broken pipe) is retryable after a fresh reconnect, whereas a genuine
    tool-level error (bad args, server-side exception) is not — reconnecting
    would just loop. We match on the concrete leaf exception type/text.

    Conservative by design: when unsure we return False, so we never mask a
    real tool error behind an endless reconnect cycle.
    """
    leaf = _unwrap_exception_group(exc)
    # anyio stream-lifecycle errors are the canonical "pipe is gone" signals.
    name = type(leaf).__name__
    if name in (
        'ClosedResourceError', 'BrokenResourceError', 'EndOfStream',
        'BrokenPipeError', 'ConnectionResetError', 'ConnectionError',
    ):
        return True
    text = (str(leaf) or '').lower()
    needles = (
        'connection closed', 'broken pipe', 'closed resource',
        'connection reset', 'end of stream', 'transport closed',
        'session is closed', 'peer closed',
    )
    return any(n in text for n in needles)


def _is_call_timeout_error(exc: BaseException) -> bool:
    """True when ``exc`` is a tool-call timeout (transport read-timeout or the
    outer thread-future timeout), as opposed to a tool-level / transport-dead
    error. Used to drive the call-level degraded-health gate.

    Matches:
      - ``concurrent.futures.TimeoutError`` / ``asyncio.TimeoutError`` /
        builtin ``TimeoutError`` raised by the outer ``future.result(...)``;
      - mcp's ``McpError`` whose text reports "Timed out while waiting for
        response" (the read_timeout_seconds path).
    """
    leaf = _unwrap_exception_group(exc)
    if isinstance(leaf, (TimeoutError, asyncio.TimeoutError)):
        return True
    if type(leaf).__name__ in ('TimeoutError', 'McpError'):
        text = (str(leaf) or '').lower()
        if 'timed out while waiting' in text or 'timeout' in text:
            return True
    return False


def _read_stderr_tail(f, max_bytes: int = _MCP_STDERR_TAIL_BYTES) -> str:
    """Read the last ``max_bytes`` bytes of a binary tempfile, decoded as UTF-8.

    Returns '' on any failure — diagnostics must never raise.
    """
    if f is None:
        return ''
    try:
        f.flush()
    except OSError as e:
        logger.debug('[MCP] stderr tempfile flush failed: %s', e)
    try:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    except OSError as e:
        logger.debug('[MCP] Could not read stderr tail: %s', e)
        return ''
    if not data:
        return ''
    # `errors='replace'` makes decode infallible for utf-8, but keep a
    # narrow guard for the rare LookupError if stdlib codec is missing.
    try:
        text = data.decode('utf-8', errors='replace')
    except (UnicodeDecodeError, LookupError) as e:
        logger.debug('[MCP] stderr tail decode failed: %s', e)
        return ''
    # Trim partial leading line if we sliced mid-line.
    if size > max_bytes and '\n' in text:
        text = text.split('\n', 1)[1]
    return text.strip()


class MCPConnectError(RuntimeError):
    """Connection failure for a single MCP server with a user-facing message
    that includes the actionable root cause and the child process's stderr
    tail (when stdio transport).

    ``str(e)`` yields the formatted user-facing message; ``.cause`` holds
    the original (unwrapped) leaf exception, ``.stderr_tail`` holds the
    captured stderr (may be empty).
    """

    def __init__(self, server_name: str, cause: BaseException, stderr_tail: str = ''):
        self.server_name = server_name
        self.cause = cause
        self.stderr_tail = stderr_tail or ''
        super().__init__(self._format())

    def _format(self) -> str:
        cause_msg = (str(self.cause) or type(self.cause).__name__).strip()
        # MCP's "Connection closed" is too terse — clarify it.
        if cause_msg == 'Connection closed':
            cause_msg = (
                'Connection closed by server during initialize '
                '(the launcher started but exited before completing the MCP handshake)'
            )
        msg = f'MCP server {self.server_name!r}: {cause_msg}'
        if self.stderr_tail:
            tail = self.stderr_tail
            # Cap at 1500 chars when surfaced to the UI — full tail is in logs.
            if len(tail) > 1500:
                tail = '…' + tail[-1500:]
            msg += f'\n\nServer stderr (tail):\n{tail}'
        return msg
