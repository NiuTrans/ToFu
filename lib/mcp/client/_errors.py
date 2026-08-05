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

#: Cache for :func:`_mcp_error_types` — None = not yet resolved, () = resolved
#: but nothing found (the name fallback then carries classification).
_MCP_ERROR_TYPES: tuple[type, ...] | None = None


def _safe_text(exc: BaseException) -> str:
    """``str(exc)`` with a guard — classifiers must never raise.

    v2 ``MCPError.__str__`` dereferences ``self.error``; a malformed or
    wrongly-initialised subclass instance raises AttributeError from inside
    ``str()``, which would crash the classification path and mask the real
    error. Fall back to the constructor args, where the message lives for a
    normally-raised exception.
    """
    try:
        return str(exc) or ''
    except Exception as e:
        # str(exc) itself raised (e.g. a malformed MCPError subclass whose
        # __str__ dereferences a missing .error) — fall back to the args;
        # classification diagnostics must never raise, but never silently.
        logger.debug('[MCP] str(exc) raised during error classification, '
                     'falling back to exc.args: %s', e)
        return ' '.join(str(a) for a in getattr(exc, 'args', ()))


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
    text = _safe_text(leaf).lower()
    needles = (
        'connection closed', 'broken pipe', 'closed resource',
        'connection reset', 'end of stream', 'transport closed',
        'session is closed', 'peer closed',
    )
    return any(n in text for n in needles)


def _mcp_error_types() -> tuple[type, ...]:
    """Resolve the SDK's protocol-error class(es) for ``isinstance`` checks.

    WHY THIS IS NOT A NAME COMPARISON
    ---------------------------------
    This used to be ``type(leaf).__name__ in ('TimeoutError', 'McpError')``.
    That judgement is anchored to a string the UPSTREAM SDK owns: mcp 2.x
    renamed the class ``McpError`` → ``MCPError`` (acronym consistency), and a
    name comparison does not fail loudly on a rename — it simply stops matching
    forever. What it gates is the call-level degraded-health circuit
    (``MCP_DEGRADED_TIMEOUT_STREAK``), so a silent miss means Tofu goes back to
    paying a FULL timeout on every call to a stalled server, with nothing in the
    log to say the gate died.

    Resolved once at import and cached. The tuple may be empty (SDK missing or
    restructured); callers therefore keep a name-based fallback covering BOTH
    spellings, so classification degrades rather than disappearing.
    """
    global _MCP_ERROR_TYPES
    if _MCP_ERROR_TYPES is not None:
        return _MCP_ERROR_TYPES
    found: list[type] = []
    try:
        from mcp.shared import exceptions as _exc
        for attr in ('McpError', 'MCPError'):
            cls = getattr(_exc, attr, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                found.append(cls)
    except Exception as e:
        # Never let diagnostics break the caller: an SDK that moved this module
        # leaves us on the name fallback below, which is exactly the degraded
        # (not broken) behaviour we want.
        logger.debug('[MCP] could not resolve SDK error class for isinstance '
                     'check, falling back to name match: %s', e)
    _MCP_ERROR_TYPES = tuple(dict.fromkeys(found))
    if not _MCP_ERROR_TYPES:
        logger.warning('[MCP] SDK protocol-error class not resolvable '
                       '(neither McpError nor MCPError) — call-timeout '
                       'classification is running on the name fallback')
    return _MCP_ERROR_TYPES


def _is_call_timeout_error(exc: BaseException) -> bool:
    """True when ``exc`` is a tool-call timeout (transport read-timeout or the
    outer thread-future timeout), as opposed to a tool-level / transport-dead
    error. Used to drive the call-level degraded-health gate.

    Matches:
      - ``concurrent.futures.TimeoutError`` / ``asyncio.TimeoutError`` /
        builtin ``TimeoutError`` raised by the outer ``future.result(...)``;
      - the SDK's protocol error (``McpError`` in v1, ``MCPError`` in v2)
        whose text reports "Timed out while waiting for response" (the
        ``read_timeout_seconds`` path), matched by ``isinstance`` against the
        real class rather than by its name — see :func:`_mcp_error_types`.
    """
    leaf = _unwrap_exception_group(exc)
    if isinstance(leaf, (TimeoutError, asyncio.TimeoutError)):
        return True
    sdk_types = _mcp_error_types()
    # Name fallback keeps BOTH spellings so a resolution failure still
    # classifies; 'TimeoutError' stays because a subclass may not be caught by
    # the isinstance above when it comes from a different module object.
    is_protocol_err = (isinstance(leaf, sdk_types) if sdk_types else False)
    if is_protocol_err or type(leaf).__name__ in ('TimeoutError', 'McpError', 'MCPError'):
        text = _safe_text(leaf).lower()
        if 'timed out while waiting' in text or 'timeout' in text:
            return True
    return False


#: JSON-RPC 2.0 "Method not found". Owned by the JSON-RPC spec (not by the MCP
#: SDK and not by any server), which is why it is safe to hard-code: unlike a
#: class name or an English message, this number cannot be renamed upstream.
#: ``mcp.types.METHOD_NOT_FOUND`` carries the same value in BOTH SDK majors
#: (measured: v1 1.27.2 and v2 2.0.0 both define it as -32601); we prefer the
#: SDK constant when importable and fall back to the literal.
_JSONRPC_METHOD_NOT_FOUND = -32601


def _jsonrpc_error_code(exc: BaseException) -> int | None:
    """Return the JSON-RPC error code carried by ``exc``, or None.

    Both SDK majors expose the code the same way despite differing
    constructors (measured: v1 ``McpError(ErrorData)`` and v2
    ``MCPError(code, message)`` both end up with ``.error.code``), so reading
    ``.error.code`` spans the pin boundary without a version test.
    """
    leaf = _unwrap_exception_group(exc)
    code = getattr(getattr(leaf, 'error', None), 'code', None)
    return code if isinstance(code, int) else None


def _is_peer_answered_error(exc: BaseException) -> bool:
    """True when ``exc`` is a protocol-level error RESPONSE from the peer.

    THE DISTINCTION THIS ENCODES
    ----------------------------
    A JSON-RPC error response is *proof of liveness*, not evidence of death:
    the request reached the server, the server parsed it, and the reply
    travelled back. "The peer answered" and "the peer answered successfully"
    are different questions, and conflating them is what makes a health probe
    reconnect a perfectly healthy transport.

    This became load-bearing with protocol revision 2026-07-28, which REMOVED
    ``ping`` from the schema entirely (measured: 0 occurrences in the published
    schema.ts, versus ``logging/setLevel`` which merely carries an
    ``@deprecated`` tag). A conforming server answers a ping with
    ``-32601 Method not found`` while remaining perfectly usable — measured
    end-to-end against a real 2.0.0 server: the very next ``tools/list`` on the
    same session succeeds.

    Deliberately narrow: only genuine protocol-error responses count. A
    timeout, a dead pipe, or a transport-layer failure is NOT an answer, so
    those keep their existing "reconnect" verdict.
    """
    leaf = _unwrap_exception_group(exc)
    # A timeout is the absence of an answer — never treat it as liveness.
    if _is_call_timeout_error(leaf) or _is_transport_dead_error(leaf):
        return False
    if _jsonrpc_error_code(leaf) is not None:
        return True
    # No structured code (SDK restructured / wrapped): fall back to the SDK's
    # protocol-error class. Still strictly narrower than "any exception".
    sdk_types = _mcp_error_types()
    if sdk_types and isinstance(leaf, sdk_types):
        return True
    return type(leaf).__name__ in ('McpError', 'MCPError')


def _is_method_not_found(exc: BaseException) -> bool:
    """True when the peer answered with JSON-RPC ``-32601 Method not found``.

    Used to detect that a health-probe RPC is not implemented by this peer so
    the caller can fall back to one that is, instead of declaring the server
    dead. Anchored on the numeric code (see ``_JSONRPC_METHOD_NOT_FOUND``);
    the English message is only consulted when no structured code survived
    the SDK's wrapping.
    """
    code = _jsonrpc_error_code(exc)
    if code is not None:
        return code == _JSONRPC_METHOD_NOT_FOUND
    leaf = _unwrap_exception_group(exc)
    if not _is_peer_answered_error(leaf):
        return False
    return 'method not found' in _safe_text(leaf).lower()


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
        # Scrub credentials LAST, over the whole assembled message. A remote
        # transport failure arrives as an httpx error whose text embeds the
        # RESOLVED request URL (``... for url '…/mcp?key=<real key>'``), and a
        # subprocess's stderr can echo its own argv/env — so both halves are
        # credential-bearing. This is the third credential exit after the two
        # config-returning endpoints, and the most durable one: an API response
        # is transient, a log line persists.
        from lib.mcp.transport import scrub_text
        return scrub_text(msg)
