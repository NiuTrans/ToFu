"""Persistent sync WebSocket transport for Responses API turns.

The normal agent worker is synchronous and keeps one thread for the whole
tool loop.  A thread-local connection therefore gives consecutive rounds a
real persistent Responses session without crossing event-loop or tenant
boundaries.  Two routes use it: public Responses routes that opt in
(``responses.transport=websocket``), and the ChatGPT Codex subscription
backend, whose first-party client (Codex CLI) defaults to this transport.

Continuation follows Codex CLI's rule (``core/src/client.rs``
``prepare_websocket_request``): when every non-input request field is
unchanged and the new input is the previous input, then the previous
response's output, then only application-authored items, the round sends
``previous_response_id`` plus that delta.  Anything else resets the chain
with the complete input.  With ``store=false`` the server keeps only the
latest response in connection-local memory, so a refused continuation or the
60-minute connection limit reconnects and resends the complete request once;
the refusal precedes any output, so nothing is generated twice.

Fallback: if the socket cannot open before ``response.create`` is sent, the
same sealed request uses SSE and the task keeps SSE for its remaining rounds;
a socket that fails after a send is retried by the caller, also on SSE.

Entry points: :func:`stream_responses_websocket`,
:func:`release_task_websockets` (task-lane teardown).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from lib.llm._sse_core import SSEAccumulator
from lib.llm._transport import StreamProgress
from lib.llm.stream_result import ProviderStreamState
from lib.llm.llm_errors import (
    AbortedError,
    EndpointUnreachableError,
    PROVIDER_RESPONSE_ERRORS,
    RetryableAPIError,
)
from lib.log import get_logger

logger = get_logger(__name__)

#: Codex CLI's WebSocket handshake beta (openai/codex ``core/src/client.rs``).
CODEX_WEBSOCKET_BETA = 'responses_websockets=2026-02-06'
#: HTTP body/stream headers with no meaning on a WebSocket upgrade; the turn's
#: sticky-routing token travels in ``client_metadata`` on this transport.
_CODEX_HTTP_ONLY_HEADERS = frozenset({
    'accept', 'content-encoding', 'content-length', 'content-type',
    'openai-beta', 'x-codex-turn-state',
})
#: Request fields allowed to differ between a response and its continuation.
_CONTINUATION_VARIABLE_FIELDS = frozenset({
    'input', 'previous_response_id', 'stream', 'client_metadata', 'generate',
})
#: Application-authored items a continuation may append after the output.
_APPLICATION_ITEM_TYPES = frozenset({
    'function_call_output', 'custom_tool_call_output', 'tool_search_output',
})
_APPLICATION_ROLES = frozenset({'user', 'developer', 'system'})
#: Codex WebSocket side-channel frames: transport metadata, never output.
_CODEX_METADATA_EVENTS = frozenset({
    'codex.rate_limits', 'codex.response.metadata',
    'responsesapi.websocket_timing',
})
#: Server refusals that discard the connection-local chain before output.
_CHAIN_RESET_CODES = frozenset({
    'previous_response_not_found', 'websocket_connection_limit_reached',
})
_PRE_OUTPUT_EVENTS = frozenset({
    'response.created', 'response.in_progress', 'response.queued',
    'response.metadata',
})


class ResponsesWebSocketUnavailable(RuntimeError):
    """The socket could not open before ``response.create`` was sent.

    ``expected`` marks a route that has no socket path by design (desktop
    relay, or a task already moved to SSE) rather than a connect failure.
    """

    def __init__(self, message: str, *, expected: bool = False):
        super().__init__(message)
        self.expected = expected


class _ChainReset(Exception):
    """The server refused the socket chain before producing any output."""


@dataclass
class _Session:
    connection: object
    previous_response_id: str = ''
    #: Complete logical body of the response ``previous_response_id`` names.
    last_request: dict | None = None
    #: That response's authoritative ``output`` items.
    last_output: list = field(default_factory=list)
    state_key: str = ''
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


def _disabled_tasks() -> set[str]:
    """Tasks on this thread whose socket failed; they keep SSE until teardown."""
    disabled = getattr(_local, 'responses_ws_disabled', None)
    if disabled is None:
        disabled = set()
        _local.responses_ws_disabled = disabled
    return disabled


def _ws_url(http_url: str) -> str:
    parsed = urlsplit(http_url)
    scheme = 'wss' if parsed.scheme == 'https' else 'ws'
    return urlunsplit((scheme, parsed.netloc, parsed.path,
                       parsed.query, parsed.fragment))


def _is_codex(plan) -> bool:
    return getattr(plan, 'responses_profile', '') == 'codex'


def _handshake_headers(plan) -> dict:
    headers = dict(plan.hdrs)
    if not _is_codex(plan):
        return headers
    headers = {name: value for name, value in headers.items()
               if str(name).lower() not in _CODEX_HTTP_ONLY_HEADERS}
    headers['OpenAI-Beta'] = CODEX_WEBSOCKET_BETA
    return headers


def _session_key(plan, handshake: dict) -> str:
    # The handshake carries credential, account, affinity and model routing
    # identity; any change needs a new connection. Per-request trace ids do not.
    stable = sorted((str(name).lower(), str(value))
                    for name, value in handshake.items()
                    if str(name).lower() != 'm-traceid')
    material = json.dumps([plan.url, stable, plan.responses_state_key],
                          ensure_ascii=False)
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


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


def release_task_websockets(state_key: str) -> None:
    """Close this thread's sockets for a finished task and clear its latch."""
    if not state_key:
        return
    for key, session in list(_sessions().items()):
        if session.state_key == state_key:
            _close_session(key, reason='task teardown')
    _disabled_tasks().discard(state_key)


def _websocket_proxy(plan, owner_scope: str):
    """The same egress decision the HTTP transport makes, as a ``proxy`` arg.

    A desktop-relayed route has no socket path, so it keeps SSE. Subscription
    hosts use the healthiest server route; only its connect is attempted, and
    any failure leaves the full route race to the SSE transport.
    """
    try:
        from lib.desktop import egress
    except ModuleNotFoundError:
        egress = None
    if egress is not None:
        try:
            route = egress.route_request(plan.url, user_id=owner_scope)
        except Exception as exc:
            raise ResponsesWebSocketUnavailable(
                f'egress route unavailable: {type(exc).__name__}') from exc
        if route != 'direct':
            raise ResponsesWebSocketUnavailable(
                'desktop egress route', expected=True)
    from lib.proxy import proxies_for, subscription_route_candidates
    routes = subscription_route_candidates(plan.url)
    if routes:
        route = routes[0]
        if route.mode == 'direct':
            return None
        if route.mode == 'proxy':
            return route.proxy_url or True
        return True
    proxies = proxies_for(plan.url) or {}
    if proxies.get('https'):
        return proxies['https']
    if proxies.get('no_proxy') == '*':
        return None
    return True


def _open_session(plan, key: str, handshake: dict, owner_scope: str = '') -> _Session:
    try:
        from websockets.sync.client import connect
    except Exception as exc:  # pragma: no cover - dependency is installed
        raise ResponsesWebSocketUnavailable(
            f'websockets client unavailable: {exc}') from exc
    proxy = _websocket_proxy(plan, owner_scope)
    _prune_sessions()
    try:
        connection = connect(
            _ws_url(plan.url),
            additional_headers=handshake,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=None,
            max_size=None,
            proxy=proxy,
            # A socket is closed only when it is being discarded; waiting
            # for the peer's closing acknowledgement (seconds through a
            # proxy) would stall the turn that just finished.
            close_timeout=0,
        )
    except Exception as exc:
        raise ResponsesWebSocketUnavailable(str(exc)) from exc
    session = _Session(connection=connection,
                       state_key=plan.responses_state_key)
    _sessions()[key] = session
    logger.debug('[ResponsesWS] opened state=%s url=%s',
                 plan.responses_state_key[:8], _ws_url(plan.url))
    return session


def _connection_open(connection) -> bool:
    state = getattr(connection, 'state', None)
    return state is None or getattr(state, 'name', '') == 'OPEN'


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _output_signature(item) -> tuple:
    """Model-visible identity of one output item, independent of id/status.

    Tofu replays reasoning verbatim but rebuilds calls and messages from its
    canonical history, so those compare by the fields the model sees.
    """
    if not isinstance(item, dict):
        return ('invalid', _canonical(item))
    kind = item.get('type')
    if kind == 'reasoning':
        return (kind, item.get('encrypted_content'),
                _canonical(item.get('summary')))
    if kind == 'function_call':
        return (kind, item.get('call_id'), item.get('name'),
                item.get('arguments'))
    if kind == 'custom_tool_call':
        return (kind, item.get('call_id'), item.get('name'),
                item.get('input'))
    if kind == 'message':
        parts = tuple(
            (part.get('type'), part.get('text', part.get('refusal')))
            for part in item.get('content') or ()
            if isinstance(part, dict))
        return (kind, item.get('role'), item.get('phase'), parts)
    return (kind, _canonical({name: value for name, value in item.items()
                              if name not in ('id', 'status')}))


def _is_application_item(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get('type') in _APPLICATION_ITEM_TYPES:
        return True
    return (item.get('type') == 'message'
            and item.get('role') in _APPLICATION_ROLES)


def _fixed_fields(request: dict) -> dict:
    return {name: value for name, value in request.items()
            if name not in _CONTINUATION_VARIABLE_FIELDS}


def _continuation_input(session: _Session, request: dict) -> list | None:
    """Return the delta to send after ``previous_response_id``, or ``None``."""
    previous = session.last_request
    if not session.previous_response_id or not isinstance(previous, dict):
        return None
    if _fixed_fields(previous) != _fixed_fields(request):
        return None
    prior = previous.get('input')
    current = request.get('input')
    if not isinstance(prior, list) or not isinstance(current, list):
        return None
    if current[:len(prior)] != prior:
        return None
    rest = current[len(prior):]
    produced = [_output_signature(item) for item in session.last_output]
    replayed = [_output_signature(item) for item in rest[:len(produced)]]
    if replayed != produced:
        return None
    delta = rest[len(produced):]
    if not delta or not all(_is_application_item(item) for item in delta):
        return None
    return delta


def _request_payload(plan, session: _Session, *,
                     force_full: bool = False) -> tuple[dict, dict]:
    """Return ``(response.create fields, complete logical request)``."""
    from lib.llm.request import transport_body

    request = dict(transport_body(plan.body))
    request.pop('stream', None)
    request.pop('previous_response_id', None)
    payload = dict(request)
    delta = None if force_full else _continuation_input(session, request)
    if delta is None:
        payload['previous_response_id'] = None
    else:
        payload['previous_response_id'] = session.previous_response_id
        payload['input'] = delta
    if _is_codex(plan):
        from lib.oauth.codex_turn_state import TURN_STATE_HEADER, turn_state
        routing_state = turn_state(plan.responses_state_key)
        if routing_state:
            metadata = dict(payload.get('client_metadata') or {})
            metadata[TURN_STATE_HEADER] = routing_state
            payload['client_metadata'] = metadata
    return payload, request


def _chain_reset_reason(event: dict, *, continuation: bool) -> str:
    error = event.get('error')
    if not isinstance(error, dict):
        return ''
    code = error.get('code')
    if isinstance(code, str) and code in _CHAIN_RESET_CODES:
        return code
    # The ChatGPT Codex backend reports an unknown/evicted chain as
    # ``invalid_request_error`` "Invalid `previous_response_id`." without a
    # code; only a continuation can have caused it.
    text = ' '.join(str(error.get(name) or '') for name in ('param', 'message'))
    if continuation and 'previous_response_id' in text:
        return 'previous_response_rejected'
    return ''


def _observe_codex_metadata(plan, event: dict, rate_limits: list) -> None:
    kind = event.get('type')
    if kind == 'codex.rate_limits':
        rate_limits[:] = [event]
    elif kind == 'codex.response.metadata':
        headers = event.get('headers')
        if isinstance(headers, dict):
            from lib.oauth.codex_turn_state import observe_turn_state_headers
            observe_turn_state_headers(plan.responses_state_key, headers)


def stream_responses_websocket(
        plan, *, on_thinking=None, on_content=None,
        on_tool_call_ready=None, abort_check=None, log_prefix='',
        on_first_byte_wait=None, on_stream_wait=None, owner_scope=''):
    """Run one Responses turn on a task-local persistent WebSocket."""
    state_key = plan.responses_state_key
    disabled = _disabled_tasks()
    if state_key in disabled:
        raise ResponsesWebSocketUnavailable(
            'socket disabled for this task', expected=True)
    handshake = _handshake_headers(plan)
    key = _session_key(plan, handshake)
    callbacks = dict(
        on_thinking=on_thinking, on_content=on_content,
        on_tool_call_ready=on_tool_call_ready, abort_check=abort_check,
        log_prefix=log_prefix, on_first_byte_wait=on_first_byte_wait,
        on_stream_wait=on_stream_wait)
    dispatched = False
    force_full = False
    while True:
        session = _sessions().get(key)
        if session is not None and not _connection_open(session.connection):
            # Closed by the server between rounds (idle, limit, prior error):
            # nothing was sent on it, and its chain state is gone with it.
            _close_session(key, reason='closed by server')
            session = None
        if session is None:
            try:
                session = _open_session(plan, key, handshake, owner_scope)
            except ResponsesWebSocketUnavailable as exc:
                if dispatched:
                    # This plan was already dispatched once; a fresh attempt
                    # (new admission) must decide its transport again.
                    raise RetryableAPIError(
                        f'Responses WebSocket reconnect failed: {exc}') from exc
                disabled.add(state_key)
                raise
        payload, request = _request_payload(
            plan, session, force_full=force_full)
        if not dispatched:
            from lib.llm.request import mark_model_request_dispatched
            mark_model_request_dispatched(plan)
            dispatched = True
        try:
            return _exchange(plan, key, session, payload, request, **callbacks)
        except _ChainReset as reset:
            _close_session(key, reason=str(reset))
            if force_full:
                raise RetryableAPIError(
                    f'Responses WebSocket chain refused: {reset}') from None
            logger.info('%s [ResponsesWS] chain refused (%s); reconnecting '
                        'with the complete input', log_prefix, reset)
            force_full = True


def _exchange(plan, key, session, payload, request, *, on_thinking,
              on_content, on_tool_call_ready, abort_check, log_prefix,
              on_first_byte_wait, on_stream_wait):
    """Send one ``response.create`` and stream it to a verified result."""
    continuation = payload.get('previous_response_id') is not None
    codex = _is_codex(plan)
    rate_limits: list = []
    output_started = False
    try:
        # Once send() is attempted, delivery is ambiguous on failure. Never
        # replay the same response.create on SSE inside this attempt.
        frame = json.dumps({'type': 'response.create', **payload},
                           ensure_ascii=False, separators=(',', ':'))
        session.connection.send(frame)
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
            credential_present=getattr(plan, 'credential_present', False),
            progress=progress)
        idle_timeout = _tp.stream_idle_timeout_seconds()
        silence_deadline = _tp.silent_stream_deadline_seconds()
        last_beat = time.monotonic()
        while True:
            now = time.monotonic()
            if progress.transport_timed_out(idle_timeout, now):
                _close_session(key, reason='stream idle timeout')
                break
            if progress.transport_timed_out(silence_deadline, now):
                _close_session(key, reason='silent stream deadline')
                raise _tp.silent_stream_timeout_error(base_url=plan.url)
            if abort_check and abort_check():
                acc.mark_aborted()
                _close_session(key, reason='user abort')
                raise AbortedError(
                    f'User aborted while waiting on {plan.url}', url=plan.url)
            try:
                remaining = progress.transport_remaining_seconds(
                    idle_timeout, now)
                silence_left = progress.transport_remaining_seconds(
                    silence_deadline, now)
                if silence_left is not None:
                    remaining = (silence_left if remaining is None
                                 else min(remaining, silence_left))
                poll_seconds = (
                    _POLL_SECONDS if remaining is None
                    else max(0.01, min(_POLL_SECONDS, remaining)))
                message = session.connection.recv(timeout=poll_seconds)
            except TimeoutError:
                now = time.monotonic()
                if progress.transport_timed_out(idle_timeout, now):
                    _close_session(key, reason='stream idle timeout')
                    break
                if progress.transport_timed_out(silence_deadline, now):
                    _close_session(key, reason='silent stream deadline')
                    raise _tp.silent_stream_timeout_error(base_url=plan.url)
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
            event_type = event.get('type')
            if codex and event_type in _CODEX_METADATA_EVENTS:
                _observe_codex_metadata(plan, event, rate_limits)
                continue
            if event_type == 'error' and not output_started:
                reason = _chain_reset_reason(event, continuation=continuation)
                if reason:
                    raise _ChainReset(reason)
            if event_type not in _PRE_OUTPUT_EVENTS:
                output_started = True
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
            # must not keep a socket alive or advance its continuation state
            # after terminal provider evidence was missing.
            _close_session(key, reason=f'unverified {result.state.value}')
        else:
            translator = plan.wire_translator
            response_id = str(getattr(translator, 'response_id', '') or '')
            if not response_id:
                _close_session(key, reason='missing response id')
            else:
                session.previous_response_id = response_id
                session.last_request = request
                session.last_output = list(
                    getattr(translator, 'response_output', None) or [])
                session.last_used = time.monotonic()
            if (finish != 'tool_calls'
                    and not (isinstance(usage, dict)
                             and usage.get('_program_pending'))):
                _close_session(key, reason='terminal assistant response')
        if rate_limits:
            from lib.subscription_quota import (
                codex_rate_limit_event_headers, record_codex_quota)
            result = result.with_usage(record_codex_quota(
                codex_rate_limit_event_headers(rate_limits[0]),
                dict(result.usage), cache_key='oauth_codex',
                source='websocket_event'))
        return result
    except (AbortedError, ResponsesWebSocketUnavailable, _ChainReset):
        raise
    except Exception as exc:
        _close_session(key, reason='stream failure')
        if isinstance(exc, (EndpointUnreachableError, *PROVIDER_RESPONSE_ERRORS)):
            raise
        # The socket broke after a send: later rounds of this task use SSE.
        _disabled_tasks().add(plan.responses_state_key)
        raise RetryableAPIError(
            f'Responses WebSocket failed: {exc}') from exc


__all__ = [
    'CODEX_WEBSOCKET_BETA',
    'ResponsesWebSocketUnavailable',
    'release_task_websockets',
    'stream_responses_websocket',
]
