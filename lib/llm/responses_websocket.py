"""Persistent sync WebSocket transport for public Responses API turns.

The normal agent worker is synchronous and keeps one thread for the whole
tool loop.  A thread-local connection therefore gives consecutive rounds a
real persistent Responses session without crossing event-loop or tenant
boundaries.  It is opt-in (``responses.transport=websocket``) and falls back
to SSE only when the WebSocket could not be opened before a request was sent.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from lib.llm._sse_core import SSEAccumulator
from lib.llm._transport import StreamProgress
from lib.llm.stream_result import ProviderStreamState
from lib.llm_errors import (
    AbortedError,
    EndpointUnreachableError,
    RetryableAPIError,
)
from lib.log import get_logger

logger = get_logger(__name__)


class ResponsesWebSocketUnavailable(RuntimeError):
    """The socket could not open before ``response.create`` was sent."""


@dataclass
class _Session:
    connection: object
    previous_response_id: str = ''
    seen_external: Counter = field(default_factory=Counter)
    last_used: float = field(default_factory=time.monotonic)


_local = threading.local()
_POLL_SECONDS = 0.25
_MAX_THREAD_SESSIONS = 8


def _sessions() -> dict[str, _Session]:
    sessions = getattr(_local, 'responses_ws_sessions', None)
    if sessions is None:
        sessions = {}
        _local.responses_ws_sessions = sessions
    return sessions


def _ws_url(http_url: str) -> str:
    parsed = urlsplit(http_url)
    scheme = 'wss' if parsed.scheme == 'https' else 'ws'
    return urlunsplit((scheme, parsed.netloc, parsed.path,
                       parsed.query, parsed.fragment))


def _session_key(plan) -> str:
    auth = str(plan.hdrs.get('Authorization') or '')
    digest = hashlib.sha256(
        f'{plan.url}\0{auth}\0{plan.responses_state_key}'.encode('utf-8')
    ).hexdigest()
    return digest


def _external_key(item: dict) -> str:
    """Identity for app-authored incremental input already sent on a socket."""
    item_type = item.get('type')
    if item_type == 'function_call_output':
        return 'function:' + str(item.get('call_id') or '')
    if item_type == 'tool_search_output':
        return 'tool-search:' + str(
            item.get('tool_search_call_id') or item.get('call_id') or '')
    if item_type == 'message' and item.get('role') in ('developer', 'user'):
        return 'message:' + json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return ''


def _external_counts(items) -> Counter:
    counts = Counter()
    for item in items or ():
        if isinstance(item, dict):
            key = _external_key(item)
            if key:
                counts[key] += 1
    return counts


def _incremental_input(session: _Session, full_input) -> list[dict]:
    """Return only app-authored items not present on the prior response state."""
    current_seen = Counter()
    delta: list[dict] = []
    for item in full_input or ():
        if not isinstance(item, dict):
            continue
        key = _external_key(item)
        if not key:
            continue
        current_seen[key] += 1
        if current_seen[key] > session.seen_external.get(key, 0):
            delta.append(item)
    return delta


def _close_session(key: str, *, reason: str = '') -> None:
    session = _sessions().pop(key, None)
    if session is None:
        return
    try:
        session.connection.close()
    except Exception as exc:
        logger.debug('[ResponsesWS] close failed (%s): %s', reason, exc)


def _prune_sessions() -> None:
    sessions = _sessions()
    while len(sessions) >= _MAX_THREAD_SESSIONS:
        oldest = min(sessions, key=lambda key: sessions[key].last_used)
        _close_session(oldest, reason='thread-local LRU')


def _open_session(plan, key: str) -> _Session:
    try:
        from websockets.sync.client import connect
    except Exception as exc:  # pragma: no cover - dependency is installed
        raise ResponsesWebSocketUnavailable(
            f'websockets client unavailable: {exc}') from exc
    _prune_sessions()
    try:
        connection = connect(
            _ws_url(plan.url),
            additional_headers=plan.hdrs,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=None,
            max_size=None,
        )
    except Exception as exc:
        raise ResponsesWebSocketUnavailable(str(exc)) from exc
    session = _Session(connection=connection)
    _sessions()[key] = session
    logger.debug('[ResponsesWS] opened state=%s url=%s',
                 plan.responses_state_key[:8], _ws_url(plan.url))
    return session


def _request_payload(plan, session: _Session) -> tuple[dict, Counter]:
    full_input = list(plan.body.get('input') or [])
    counts = _external_counts(full_input)
    if not session.previous_response_id:
        response = dict(plan.body)
        # Streaming is inherent in WebSocket mode.
        response.pop('stream', None)
        return response, counts
    response = {
        'model': plan.body.get('model', ''),
        'previous_response_id': session.previous_response_id,
        'input': _incremental_input(session, full_input),
        'store': False,
    }
    return response, counts


def stream_responses_websocket(
        plan, *, on_thinking=None, on_content=None,
        on_tool_call_ready=None, abort_check=None, log_prefix='',
        on_first_byte_wait=None, on_stream_wait=None):
    """Run one Responses turn on a task-local persistent WebSocket."""
    key = _session_key(plan)
    session = _sessions().get(key)
    if session is None:
        session = _open_session(plan, key)
    response, current_counts = _request_payload(plan, session)
    envelope = {'type': 'response.create', 'response': response}
    send_attempted = False
    try:
        # Once send() is attempted, delivery is ambiguous on failure. Never
        # replay the same response.create on SSE inside this attempt.
        send_attempted = True
        session.connection.send(json.dumps(
            envelope, ensure_ascii=False, separators=(',', ':')))
        session.last_used = time.monotonic()
        import lib.llm._transport as _tp
        progress = StreamProgress(0, started_at=plan.t0)
        progress.mark_response_headers()
        acc = SSEAccumulator(
            plan.body, plan.trace_id, plan.raw_dumper, plan.wire_translator,
            plan.t0, url=plan.url, log_prefix=log_prefix,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            route_output_limit_key=plan.route_output_limit_key,
            progress=progress)
        idle_timeout = _tp.stream_idle_timeout_seconds()
        last_beat = time.monotonic()
        while True:
            now = time.monotonic()
            if progress.transport_timed_out(idle_timeout, now):
                _close_session(key, reason='stream idle timeout')
                break
            if abort_check and abort_check():
                acc.mark_aborted()
                _close_session(key, reason='user abort')
                raise AbortedError(
                    f'User aborted while waiting on {plan.url}', url=plan.url)
            try:
                remaining = progress.transport_remaining_seconds(
                    idle_timeout, now)
                poll_seconds = (
                    _POLL_SECONDS if remaining is None
                    else max(0.01, min(_POLL_SECONDS, remaining)))
                message = session.connection.recv(timeout=poll_seconds)
            except TimeoutError:
                now = time.monotonic()
                if progress.transport_timed_out(idle_timeout, now):
                    _close_session(key, reason='stream idle timeout')
                    break
                heartbeat = _tp.IDLE_HEARTBEAT_S
                beat_due = heartbeat > 0 and now - last_beat >= heartbeat
                if beat_due:
                    last_beat = now
                if on_first_byte_wait is not None and beat_due:
                    try:
                        on_first_byte_wait(
                            progress.transport_idle_seconds(now))
                    except Exception as exc:
                        logger.debug('%s ResponsesWS wait callback failed: %s',
                                     log_prefix, exc)
                if on_stream_wait is not None and beat_due:
                    try:
                        on_stream_wait(progress.wait_status(now))
                    except Exception as exc:
                        logger.debug('%s ResponsesWS progress callback failed: %s',
                                     log_prefix, exc)
                continue
            if message is None:
                raise RetryableAPIError('Responses WebSocket closed')
            if isinstance(message, bytes):
                progress.mark_transport_bytes(len(message))
                try:
                    message = message.decode('utf-8', errors='strict')
                except UnicodeDecodeError as exc:
                    acc.record_malformed_frames(
                        1,
                        (f'invalid_utf8: websocket event '
                         f'error={type(exc).__name__}',),
                    )
                    _close_session(key, reason='invalid UTF-8')
                    break
            else:
                progress.mark_transport_bytes(
                    len(str(message).encode('utf-8')))
            try:
                event = json.loads(message)
            except (TypeError, json.JSONDecodeError) as exc:
                acc.record_malformed_frames(
                    1,
                    (f'invalid_json: websocket event '
                     f'error={type(exc).__name__}',),
                )
                _close_session(key, reason='invalid JSON')
                break
            if not isinstance(event, dict):
                acc.record_malformed_frames(
                    1,
                    ('invalid_shape: websocket event must be an object',),
                )
                _close_session(key, reason='invalid event shape')
                break
            if event.get('type') == 'error':
                event = {'type': 'response.error',
                         'error': event.get('error') or event}
                message = json.dumps(event, ensure_ascii=False)
            if acc.feed_payload(message, count_event=True):
                break

        acc.fire_final_tool_callback()
        result = acc.finalize()
        if result.state is ProviderStreamState.PREMATURE_CLOSE:
            usage = dict(result.usage)
            usage['_failure_stage'] = 'midstream_close'
            result = result.with_usage(usage)
        _msg, finish, usage = result
        if not result.is_verified_complete:
            # An interrupted/malformed response is not a state checkpoint.
            # In particular, a compatibility finish_reason of ``tool_calls``
            # must not keep a socket alive or advance its incremental-input
            # ledger after terminal provider evidence was missing.
            _close_session(key, reason=f'unverified {result.state.value}')
        else:
            translator = plan.wire_translator
            response_id = str(getattr(translator, 'response_id', '') or '')
            if not response_id:
                _close_session(key, reason='missing response id')
            else:
                session.previous_response_id = response_id
                session.seen_external = current_counts
                session.last_used = time.monotonic()
            if (finish != 'tool_calls'
                    and not (isinstance(usage, dict)
                             and usage.get('_program_pending'))):
                _close_session(key, reason='terminal assistant response')
        return result
    except (AbortedError, ResponsesWebSocketUnavailable):
        raise
    except Exception as exc:
        _close_session(key, reason='stream failure')
        if not send_attempted:
            raise ResponsesWebSocketUnavailable(str(exc)) from exc
        if isinstance(exc, (EndpointUnreachableError, RetryableAPIError)):
            raise
        raise RetryableAPIError(
            f'Responses WebSocket failed: {exc}') from exc


__all__ = [
    'ResponsesWebSocketUnavailable',
    'stream_responses_websocket',
]
