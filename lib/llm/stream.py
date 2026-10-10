# HOT_PATH
"""Streaming chat completion with SSE parsing (sync transport).

Public API:
  - stream_chat(body, ...) → ProviderStreamResult

The SSE parsing / error classification / tool-call accumulation / anomaly
diagnostics live in ``lib/llm/_sse_core.py`` and are shared with the async
transport (``lib/llm/astream.py``). This module is the thin ``requests``
shell: it opens the stream, supplies bytes through ``SSEByteStream``, and keeps
its retry/backoff wrapper.
"""

import json
import os
import time

import requests

from lib.llm._sse_core import (
    SSEAccumulator,
    activate_native_orchestration_fallback,
    activate_native_tool_search_fallback,
    activate_subscription_transient_retry,
    classify_status_error,
    prepare_request,
)
from lib.llm.request import transport_body_arguments, transport_payload
from lib.llm._sse_stream import SSEByteStream
from lib.llm._transport import (
    CONNECT_TIMEOUT,
    MAX_STREAM_RETRIES,
    StreamIdleWatchdog,
    abortable_sleep,
    apply_model_limit_retry,
    attach_limit_learned,
    get_sync_session,
    prepare_retryable_wait,
)
from lib.llm.stream_result import (
    ProviderStreamResult,
    ProviderStreamState,
    ensure_provider_stream_result,
)
from lib.llm_errors import (
    AbortedError,
    ContentFilterError,
    EndpointUnreachableError,
    LocalRequestPreparationError,
    ModelLimitError,
    PermissionError_,
    PromptTooLongError,
    PROVIDER_RESPONSE_ERRORS,
    RateLimitError,
    RequestTransportError,
    RetryableAPIError,
    _RETRYABLE,
)
from lib.log import get_logger
from lib.proxy import (
    describe_route,
    proxies_for,
    report_outcome as _proxy_report_outcome,
)
from lib.subscription_quota import USAGE_QUOTA_KEY, record_codex_quota

logger = get_logger(__name__)


def _iter_response_bytes(response):
    """Yield available decoded bytes, never wait to fill a read buffer.

    requests.iter_content(64KiB) uses urllib3.read(amt), which fills amt on
    non-chunked HTTP bodies. Even a flushed heartbeat then stays invisible to
    the watchdog until EOF. read1 keeps decompression but returns available
    data. Preserve requests' exception vocabulary for the existing retries.
    """
    from urllib3.response import HTTPResponse
    from urllib3.exceptions import (
        DecodeError, ProtocolError, ReadTimeoutError, SSLError,
    )

    raw = getattr(response, 'raw', None)
    if isinstance(raw, HTTPResponse) and callable(getattr(raw, 'read1', None)):
        try:
            while True:
                chunk = raw.read1(64 << 10, decode_content=True)
                if not chunk:
                    return
                yield chunk
        except ProtocolError as error:
            raise requests.exceptions.ChunkedEncodingError(error) from error
        except DecodeError as error:
            raise requests.exceptions.ContentDecodingError(error) from error
        except ReadTimeoutError as error:
            raise requests.exceptions.ConnectionError(error) from error
        except SSLError as error:
            raise requests.exceptions.SSLError(error) from error
    iter_content = getattr(response, 'iter_content', None)
    if callable(iter_content):
        # Old urllib3 without read1 needs a one-byte read to avoid buffering.
        # Desktop relay adapters already deliver available bounded chunks.
        for chunk in iter_content(chunk_size=1 if isinstance(raw, HTTPResponse)
                                  else 64 << 10):
            if chunk:
                yield bytes(chunk)
        return
    # Older adapters/fakes exposed only the former line-oriented surface.
    # Treat each non-empty yielded line as a complete event so they can migrate
    # without reintroducing line parsing into production transports.
    for line in response.iter_lines(decode_unicode=False):
        if isinstance(line, str):
            line = line.encode('utf-8')
        if line:
            yield bytes(line) + b'\n\n'


def stream_chat(body, *, on_thinking=None, on_content=None,
                on_tool_call_ready=None,
                abort_check=None, log_prefix='', api_key=None, base_url=None,
                extra_headers=None, api_protocol='openai', oauth='',
                adapter=None, on_attempt_restart=None,
                on_first_byte_wait=None,
                on_stream_wait=None,
                owner_user_id=None) -> ProviderStreamResult:
    """Streaming chat completion with callbacks.

    Automatically retries on transient connection errors up to
    MAX_STREAM_RETRIES times.

    ``on_attempt_restart`` (optional): fired with ``reason=<str>`` whenever an
    in-flight attempt is being DISCARDED and the request is about to restart
    from scratch. Any content/thinking already delivered via on_content /
    on_thinking during that attempt will be re-streamed — the callee must drop
    its partial accumulation (e.g. truncate back to the per-round base) so the
    re-streamed text does not stack on the abandoned attempt's tail.

    ``on_stream_wait`` (optional): receives the typed current-attempt progress
    status every ``IDLE_HEARTBEAT_S`` during semantic silence or unfinished
    tool-argument generation, even while transport bytes arrive. The
    status distinguishes response headers, wire traffic, complete SSE events,
    and semantic progress without borrowing retry-attempt counters.

    ``on_first_byte_wait`` is the legacy duration-only heartbeat. Despite its
    historical name it reports current transport silence both before the first
    byte and during a mid-stream stall. New callers should use
    ``on_stream_wait``.

    Returns:
        A typed provider-stream result. Legacy callers may still unpack it as
        ``(assistant_msg, finish_reason, usage)``.

    Raises:
        RateLimitError, PermissionError_, AbortedError,
        ContentFilterError, PromptTooLongError, RetryableAPIError
    """
    def _fire_attempt_restart(reason: str) -> None:
        if on_attempt_restart is None:
            return
        try:
            on_attempt_restart(reason=reason)
        except Exception as _oar_e:
            logger.debug('%s on_attempt_restart raised: %s', log_prefix, _oar_e)

    last_err = None
    _limit_learned = None
    for attempt in range(1 + MAX_STREAM_RETRIES):
        try:
            stream_result = ensure_provider_stream_result(_stream_chat_once(
                body, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                attempt=attempt, api_key=api_key, base_url=base_url,
                extra_headers=extra_headers, api_protocol=api_protocol,
                oauth=oauth, adapter=adapter,
                on_first_byte_wait=on_first_byte_wait,
                on_stream_wait=on_stream_wait,
                owner_user_id=owner_user_id))
            usage = attach_limit_learned(stream_result.usage, _limit_learned)
            return stream_result.with_usage(usage)
        except (RateLimitError, PermissionError_, AbortedError, ContentFilterError, PromptTooLongError, EndpointUnreachableError):
            # EndpointUnreachableError: the host is down — retrying it on
            # the SAME slot just burns another connect timeout. Escape to
            # the dispatch layer, which cools this slot and fails over.
            # AbortedError: the user pressed Stop — never retry that.
            raise
        except ModelLimitError as e:
            _limit_learned = apply_model_limit_retry(body, e, log_prefix)
            # The clamped retry restarts from scratch — anything streamed so
            # far belongs to a discarded attempt.
            _fire_attempt_restart('model-limit clamp retry')
            continue
        except _RETRYABLE as e:
            last_err = e
            if attempt < MAX_STREAM_RETRIES:
                # Another attempt WILL run from scratch — the partial stream
                # from this attempt is being abandoned.
                _fire_attempt_restart('transport retry: %s' % e.__class__.__name__)
            wait = prepare_retryable_wait(attempt, e, abort_check, log_prefix)
            abortable_sleep(wait, abort_check)
    raise last_err


def _stream_chat_once(body, **kwargs):
    """Settle every physical provider attempt through its required budget port."""
    from lib.llm.request import ModelRequestBudgetScope
    with ModelRequestBudgetScope(body) as scope:
        result = ensure_provider_stream_result(_stream_chat_once_impl(scope.body, **kwargs))
        scope.usage = result.usage
        return result


def _stream_chat_once_impl(body, *, on_thinking=None, on_content=None,
                      on_tool_call_ready=None,
                      abort_check=None, log_prefix='', attempt=0,
                      api_key=None, base_url=None, extra_headers=None,
                      api_protocol='openai', oauth='', adapter=None,
                      on_first_byte_wait=None, on_stream_wait=None,
                      owner_user_id=None):
    """Single attempt at a streaming chat completion (sync transport)."""
    from lib.llm._transport import transport_owner_scope
    _owner_scope = transport_owner_scope(owner_user_id)
    try:
        plan = prepare_request(
            body, attempt=attempt, log_prefix=log_prefix,
            api_key=api_key, base_url=base_url, extra_headers=extra_headers,
            api_protocol=api_protocol, oauth=oauth,
            owner_user_id=_owner_scope)
    except (LocalRequestPreparationError, AbortedError):
        raise
    except Exception as error:
        raise LocalRequestPreparationError(
            f'local request preparation failed: {error}',
            stage='wire_projection',
        ) from error

    _network_route = {
        'routeId': 'unresolved',
        'routeMode': 'unknown',
        'decisionReason': 'not_resolved',
    }
    _subscription_route = None
    _network_latency_ms = None
    _network_reported = False

    def _set_network_route(metadata, *, subscription_route=None,
                           latency_ms=None):
        nonlocal _network_route, _subscription_route, _network_latency_ms
        _network_route = dict(metadata or _network_route)
        _subscription_route = subscription_route
        if latency_ms is not None:
            _network_latency_ms = max(0.0, float(latency_ms))

    def _report_network_outcome(ok: bool, failure_kind='network_fail'):
        """Feed the complete stream outcome to the exact route once."""
        nonlocal _network_reported
        if _network_reported:
            return
        _network_reported = True
        try:
            if _subscription_route is not None:
                from lib.proxy import report_subscription_route
                report_subscription_route(
                    plan.url, _subscription_route, ok, _network_latency_ms,
                    failure_kind=failure_kind)
            elif _network_route.get('routeMode') in {
                    'direct', 'proxy', 'env'}:
                _proxy_report_outcome(
                    plan.url, ok, _network_latency_ms,
                    pool_id=_network_route.get('poolId') or '')
        except Exception as error:
            logger.debug('%s network outcome report failed: %s',
                         log_prefix, error)

    def _annotate_network_error(error, failure_stage):
        """Carry safe route evidence through dispatch exception wrapping."""
        try:
            error.network_route = dict(_network_route)
            error.failure_stage = str(failure_stage or '')[:80]
        except Exception as annotate_error:
            logger.debug('%s network error annotation failed: %s',
                         log_prefix, annotate_error)
        return error

    if plan.responses_transport == 'websocket':
        from lib.llm.responses_websocket import (
            ResponsesWebSocketUnavailable,
            stream_responses_websocket,
        )
        try:
            websocket_result = stream_responses_websocket(
                plan, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                on_first_byte_wait=on_first_byte_wait,
                on_stream_wait=on_stream_wait)
            if plan.raw_archive_capture is not None:
                plan.raw_archive_capture.append_response(
                    json.dumps(websocket_result.message,
                               ensure_ascii=False).encode('utf-8'))
                plan.raw_archive_capture.commit(
                    response_complete=True, status_code=200)
            return websocket_result
        except ResponsesWebSocketUnavailable as exc:
            # The socket failed before response.create was sent, so the same
            # translated request can safely use the proven SSE transport.
            logger.warning('%s [ResponsesWS] unavailable before send (%s); '
                           'falling back to SSE', log_prefix, exc)

    # ``prepare_request`` already opened the RawSSEDumper fd (when enabled), so
    # a single outer try/finally must guard EVERY exit path — including the
    # connect-phase re-raise below, which used to escape before the dumper was
    # closed and leaked the fd once per retry against a down endpoint.
    from lib.llm.request import dispatch_model_request
    resp = None
    # ── Idle watchdog ──
    # Three jobs: beat while the upstream is silent (HUD + the reaper's
    # liveness clocks), poll ``abort_check`` so a Stop pressed during a
    # silent stretch actually lands, and cut a genuinely SILENT stream short
    # after the rolling transport-idle window. Any bytes — including SSE
    # comments/keep-alives — renew the window, matching native Codex. The
    # in-loop abort check below only runs when a byte arrives, so without the
    # poll a zero-byte hang would ignore Stop entirely. Constants are read
    # through the module at call time so tests/deployments can retune.
    import lib.llm._transport as _tp
    _resp_holder = {}
    _progress = _tp.StreamProgress(0, started_at=plan.t0)
    _watchdog = StreamIdleWatchdog(
        heartbeat_interval=_tp.IDLE_HEARTBEAT_S,
        on_beat=on_first_byte_wait,
        on_progress=on_stream_wait,
        progress=_progress,
        abort_check=abort_check,
        on_abort=lambda: _tp.interrupt_response_read(_resp_holder.get('resp')),
        idle_timeout=_tp.stream_idle_timeout_seconds(),
        on_idle_timeout=lambda: _tp.interrupt_response_read(_resp_holder.get('resp')))
    _watchdog.start()
    try:
        _conn_t0 = time.monotonic()
        try:
            # ── Desktop-egress branch (S3): when the server's own egress to
            # this host is geo-blocked / dead, open the stream through the
            # user's desktop agent instead. Probe is per-host cached (300s).
            try:
                from lib.desktop import egress as _eg
            except ModuleNotFoundError:
                # Headless wheels exclude lib.storage (a server-persistence
                # concern), which lib.desktop.egress imports at module top.
                # Without it no desktop relay can exist, so every route
                # resolves to direct.
                _eg = None
            if adapter and _eg is not None:
                # ── Subscription-adapter branch (E4): the provider IS a
                # CLIProxyAPI sidecar on the user's desktop agent; its
                # base_url is loopback-ON-THE-AGENT, which the server can
                # NEVER reach directly. Ride the bridge loopback relay — no
                # route_request, no direct fallback. Error mapping mirrors
                # the egress branch (EgressUnavailable → unreachable).
                from urllib.parse import urlparse as _urlparse
                from lib.desktop import adapter as _ad
                _pu = _urlparse(plan.url)
                _relay_path = _pu.path + (('?' + _pu.query) if _pu.query else '')
                _set_network_route({
                    'routeId': 'desktop:adapter',
                    'routeMode': 'desktop',
                    'decisionReason': 'subscription_adapter',
                })
                try:
                    resp = dispatch_model_request(plan, _ad.relay_stream,
                        adapter.get('agent_id', ''),
                        int(adapter.get('port') or 0),
                        _relay_path, headers=plan.hdrs,
                        body=transport_payload(plan.body),
                        user_id=_owner_scope, log_prefix=log_prefix)
                except _eg.EgressUnavailable as e:
                    _report_network_outcome(False, 'connect')
                    error = EndpointUnreachableError(
                        str(e), base_url=plan.url)
                    raise _annotate_network_error(error, 'connect') from e
                _resp_holder['resp'] = resp
                if _watchdog.aborted:
                    raise AbortedError(
                        'User aborted while awaiting response headers',
                        url=plan.url)
                _network_latency_ms = (
                    time.monotonic() - _conn_t0) * 1000.0
            else:
                if _eg is None:
                    _egress_route = 'direct'
                else:
                    try:
                        _egress_route = _eg.route_request(
                            plan.url, user_id=_owner_scope)
                    except _eg.EgressUnavailable as e:
                        raise EndpointUnreachableError(str(e), base_url=plan.url) from e
                if _egress_route != 'direct':
                    _set_network_route({
                        'routeId': 'desktop:egress',
                        'routeMode': 'desktop',
                        'decisionReason': 'desktop_egress',
                    })
                    try:
                        resp = dispatch_model_request(plan, _eg.open_stream,
                            plan.url, method='POST', headers=plan.hdrs,
                            body=transport_payload(plan.body),
                            agent_id=_egress_route, user_id=_owner_scope,
                            log_prefix=log_prefix, abort_check=abort_check)
                    except _eg.EgressUnavailable as e:
                        raise EndpointUnreachableError(str(e), base_url=plan.url) from e
                else:
                    from lib.proxy import (
                        report_subscription_route,
                        subscription_route_candidates,
                    )
                    _server_routes = subscription_route_candidates(plan.url)
                    if _server_routes:
                        _attempted_routes = set()
                        for _server_route in _server_routes:
                            _attempted_routes.add(_server_route.route_id)
                            _route_t0 = time.monotonic()
                            _set_network_route({
                                'routeId': str(_server_route.route_id)[:160],
                                'routeMode': str(_server_route.mode)[:24],
                                'decisionReason': 'subscription_route_race',
                                **({'poolId': str(_server_route.pool_id)[:160]}
                                   if _server_route.pool_id else {}),
                            }, subscription_route=_server_route)
                            try:
                                resp = _tp.post_headers_abortable(
                                    lambda: dispatch_model_request(plan, get_sync_session().post,
                                        plan.url, headers=plan.hdrs,
                                        **transport_body_arguments(plan.body, transport='requests'), stream=True,
                                        timeout=(CONNECT_TIMEOUT, None),
                                        proxies=_server_route.requests_proxies(),
                                        allow_redirects=False),
                                    is_aborted=lambda: _watchdog.aborted)
                            except requests.exceptions.ConnectionError as e:
                                from lib.subscription_routes import (
                                    is_safe_connect_failure,
                                )
                                if not is_safe_connect_failure(e):
                                    report_subscription_route(
                                        plan.url, _server_route, False)
                                    raise EndpointUnreachableError(
                                        'subscription route disconnected '
                                        'before response headers; request was '
                                        'not replayed because delivery is '
                                        'ambiguous (%s)'
                                        % type(e).__name__,
                                        base_url=plan.url) from e
                                report_subscription_route(
                                    plan.url, _server_route, False)
                                logger.info(
                                    '%s connection failed via %s — trying '
                                    'next subscription route',
                                    log_prefix, _server_route.label)
                                _known_routes = {
                                    item.route_id for item in _server_routes}
                                for _candidate in subscription_route_candidates(
                                        plan.url):
                                    if (_candidate.route_id
                                            not in _attempted_routes
                                            and _candidate.route_id
                                            not in _known_routes):
                                        _server_routes.append(_candidate)
                                        _known_routes.add(_candidate.route_id)
                                continue
                            _network_latency_ms = (
                                time.monotonic() - _route_t0) * 1000.0
                            break
                        else:
                            raise EndpointUnreachableError(
                                'all server subscription routes failed during '
                                'connection setup',
                                base_url=plan.url) from None
                    else:
                        _generic_proxies = proxies_for(plan.url)
                        _set_network_route(
                            describe_route(
                                plan.url, proxies=_generic_proxies))
                        resp = _tp.post_headers_abortable(
                            lambda: dispatch_model_request(plan, get_sync_session().post,
                                plan.url, headers=plan.hdrs, **transport_body_arguments(plan.body, transport='requests'),
                                stream=True, timeout=(CONNECT_TIMEOUT, None),
                                proxies=_generic_proxies,
                                allow_redirects=False),
                            is_aborted=lambda: _watchdog.aborted)
                _resp_holder['resp'] = resp
                if _watchdog.aborted:
                    # Stop landed in the narrow window between header receipt
                    # and this check; the header wait itself is covered by
                    # post_headers_abortable above.
                    raise AbortedError(
                        'User aborted while awaiting response headers',
                        url=plan.url)
                if _network_latency_ms is None:
                    _network_latency_ms = (
                        time.monotonic() - _conn_t0) * 1000.0
        except AbortedError:
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                RequestTransportError) as e:
            error = _tp.classify_request_transport_error(
                e, base_url=plan.url, write_timeout=_tp.WRITE_TIMEOUT)
            stage = getattr(error, 'failure_stage', 'connect')
            _report_network_outcome(False, stage)
            logger.warning('%s Transport failed during %s: %s',
                           log_prefix, stage, error)
            if error is e:
                raise _annotate_network_error(error, stage)
            raise _annotate_network_error(error, stage) from e

        resp_trace = resp.headers.get('M-TraceId', '')
        _watchdog.notify_response_headers()
        if resp_trace and resp_trace != plan.trace_id:
            logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

        if resp.status_code != 200:
            # A complete application-level HTTP response proves the selected
            # network route. Provider status classification is separate from
            # route health.
            _report_network_outcome(True)
            from lib.llm.provider_error_body import read_error_body, ERROR_BODY_TIMEOUT_S
            _watchdog.arm_read_deadline(
                ERROR_BODY_TIMEOUT_S, lambda: _tp.interrupt_response_read(resp))
            error_read_options = dict(
                abort_check=abort_check,
                expired_check=lambda: _watchdog.read_deadline_expired)
            # Keep desktop route failures distinct; both adapters consume only
            # a bounded prefix rather than retaining/draining the entire body.
            if hasattr(resp, 'read_all_text'):
                from lib.desktop.egress import EgressUnavailable as _EU
                try:
                    captured_error = read_error_body(resp, **error_read_options)
                except _EU as e:
                    raise EndpointUnreachableError(str(e), base_url=plan.url) from e
            else:
                chunks = (_iter_response_bytes(resp)
                          if callable(getattr(resp, 'iter_content', None)) else None)
                captured_error = read_error_body(resp, chunks=chunks, **error_read_options)
            err_body = captured_error.text(resp)
            if plan.raw_archive_capture is not None:
                plan.raw_archive_capture.append_response(captured_error.data)
                plan.raw_archive_capture.commit(
                    response_complete=captured_error.complete, status_code=resp.status_code)
            if activate_native_tool_search_fallback(
                    resp.status_code, err_body, plan=plan,
                    canonical_body=body):
                error = RetryableAPIError(
                    'native Tool Search rejected; retrying locally',
                    status_code=resp.status_code)
                raise _annotate_network_error(
                    error, 'provider_response')
            if activate_native_orchestration_fallback(
                    resp.status_code, err_body, plan=plan,
                    canonical_body=body):
                error = RetryableAPIError(
                    'native orchestration rejected; retrying locally',
                    status_code=resp.status_code)
                raise _annotate_network_error(
                    error, 'provider_response')
            if activate_subscription_transient_retry(
                    resp.status_code, oauth=oauth, canonical_body=body):
                error = RetryableAPIError(
                    'subscription upstream transient 404; retrying',
                    status_code=resp.status_code)
                raise _annotate_network_error(
                    error, 'provider_response')
            try:
                classify_status_error(
                    resp.status_code, err_body, body=plan.body,
                    log_prefix=log_prefix, raw_dumper=plan.raw_dumper,
                    credential_present=plan.credential_present,
                    route_limit_key=plan.route_output_limit_key)
            except Exception as error:
                _annotate_network_error(error, 'provider_response')
                raise

        resp.encoding = 'utf-8'

        acc = SSEAccumulator(
            plan.body, plan.trace_id, plan.raw_dumper, plan.wire_translator,
            plan.t0, url=plan.url, log_prefix=log_prefix,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            route_output_limit_key=plan.route_output_limit_key,
            credential_present=getattr(plan, 'credential_present', False),
            progress=_progress)

        byte_stream = SSEByteStream(acc)
        _dbg_sse_fp = None
        if os.environ.get('TOFU_DEBUG_RAW_SSE') == '1' and 'mcli.internal.example.com' in str(plan.url):
            try:
                _dbg_dir = os.path.join('logs', 'debug_sse')
                os.makedirs(_dbg_dir, exist_ok=True)
                _dbg_sse_fp = open(os.path.join(_dbg_dir, f'{plan.trace_id}.sse'), 'wb')
            except Exception as _dbg_open_e:
                logger.debug('%s debug SSE capture open failed: %s',
                             log_prefix, _dbg_open_e)
                _dbg_sse_fp = None
        try:
            for raw_chunk in _iter_response_bytes(resp):
                if _dbg_sse_fp is not None:
                    try:
                        _dbg_sse_fp.write(raw_chunk)
                    except Exception as _dbg_write_e:
                        # Debug capture is best-effort; never break the stream.
                        logger.debug('%s debug SSE capture write failed: %s',
                                     log_prefix, _dbg_write_e)
                if plan.raw_archive_capture is not None:
                    plan.raw_archive_capture.append_response(raw_chunk)
                if _watchdog.idle_timed_out:
                    # The watchdog closed the socket after
                    # IDLE_STREAM_TIMEOUT_S of silence. Fall through to
                    # finalize() so the premature-close diagnostics fire.
                    break
                _watchdog.notify_transport_bytes(len(raw_chunk))
                if abort_check and abort_check():
                    acc.mark_aborted()
                    break
                if byte_stream.feed(raw_chunk):
                    break
        except Exception as _iter_e:
            # An I/O exception bypasses accumulator.finalize(). Preserve the
            # failed physical attempt's counts before a retry replaces them.
            from dataclasses import asdict
            _interrupted_evidence = asdict(_progress.evidence())
            logger.warning(
                '%s Provider stream interrupted trace=%s error=%s evidence=%s',
                log_prefix, plan.trace_id, type(_iter_e).__name__,
                json.dumps(_interrupted_evidence, sort_keys=True),
                extra={'tofu_event_name': 'llm.stream_interrupted',
                       'tofu_event_fields': {
                           'trace_id': plan.trace_id,
                           'error_kind': type(_iter_e).__name__,
                           'stream_evidence': _interrupted_evidence,
                       }})
            if _watchdog.idle_timed_out:
                # Closing the response from the watchdog thread usually
                # surfaces as an I/O error here; treat it as a premature close
                # (finalize will flag _missing_done) rather than re-raising.
                pass
            elif _watchdog.aborted:
                raise AbortedError(
                    'User aborted while waiting on %s' % plan.url,
                    url=plan.url) from _iter_e
            elif isinstance(_iter_e, PROVIDER_RESPONSE_ERRORS):
                # A typed SSE application error proves the route delivered a
                # valid provider frame. It belongs to dispatch/provider health,
                # not network path health.
                _report_network_outcome(True)
                _annotate_network_error(_iter_e, 'provider_stream')
                raise
            else:
                from lib.desktop.egress import EgressUnavailable as _EU
                if isinstance(_iter_e, _EU):
                    # Agent died / stream vanished mid-flight — fail over
                    # (provider-down semantics), never a silent partial success.
                    _report_network_outcome(False, 'midstream_disconnect')
                    error = EndpointUnreachableError(
                        str(_iter_e), base_url=plan.url)
                    raise _annotate_network_error(
                        error, 'midstream_disconnect') from _iter_e
                _report_network_outcome(False, 'midstream_io')
                _annotate_network_error(_iter_e, 'midstream_io')
                raise
        if _dbg_sse_fp is not None:
            try:
                _dbg_sse_fp.close()
            except Exception as _dbg_close_e:
                logger.debug('%s debug SSE capture close failed: %s',
                             log_prefix, _dbg_close_e)
        byte_stream.finalize(interrupted=acc.aborted_by_client or _watchdog.aborted)
        if _watchdog.aborted:
            # A close() can surface as a CLEAN end of iteration on some
            # urllib3 versions — without this check an aborted attempt would
            # finalize as a silent empty/partial "success".
            raise AbortedError(
                'User aborted while waiting on %s' % plan.url, url=plan.url)
        acc.fire_final_tool_callback()
        stream_result = ensure_provider_stream_result(
            acc.finalize(resp_trace=resp_trace))
        msg, finish_reason, usage = stream_result
        # ChatGPT-backed Codex reports the subscription allowance in response
        # headers, separately from the token usage carried by the SSE body.
        # Preserve both facts on the same per-round usage record.
        _quota_scope = ('oauth_codex' if oauth == 'codex' else
                        ('adapter:' + str(adapter.get('agent_id') or '')
                         if isinstance(adapter, dict) and adapter else 'codex'))
        usage = record_codex_quota(
            resp.headers, usage, cache_key=_quota_scope)
        if (isinstance(adapter, dict) and adapter
                and isinstance(usage, dict) and USAGE_QUOTA_KEY in usage):
            from lib.desktop import adapter as _adapter_service
            _adapter_service.note_adapter_quota_observed(
                adapter.get('agent_id'), user_id=_owner_scope)
        usage['_network_route'] = dict(_network_route)
        stream_state = stream_result.state
        if stream_state is ProviderStreamState.NO_ACTIONABLE_OUTPUT:
            usage['_failure_stage'] = 'no_actionable_output'
            _report_network_outcome(True)
        elif stream_state is ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT:
            usage['_failure_stage'] = 'semantic_progress_timeout'
            _report_network_outcome(True)
        elif stream_state is ProviderStreamState.MALFORMED_STREAM:
            usage['_failure_stage'] = 'stream_decode'
            _report_network_outcome(True)
        elif stream_state is ProviderStreamState.PREMATURE_CLOSE:
            usage['_failure_stage'] = 'midstream_close'
            _report_network_outcome(False, 'midstream_close')
        elif stream_state is ProviderStreamState.CLIENT_ABORTED:
            # Caller cancellation is neither route success nor route failure.
            usage.pop('_failure_stage', None)
        elif stream_state in {
                ProviderStreamState.EMPTY_RESPONSE,
                ProviderStreamState.TOOL_PAYLOAD_MISSING,
        }:
            usage['_failure_stage'] = 'provider_stream_invalid'
            _report_network_outcome(True)
        elif stream_state is not ProviderStreamState.PROVIDER_FINISHED:
            usage['_failure_stage'] = 'provider_stream_invalid'
            _report_network_outcome(False, 'provider_stream_invalid')
        else:
            usage.pop('_failure_stage', None)
            _report_network_outcome(True)
        if plan.raw_archive_capture is not None:
            plan.raw_archive_capture.commit(
                response_complete=True, status_code=200)
        return stream_result.with_usage(usage)
    finally:
        _watchdog.cancel()
        if plan.raw_archive_capture is not None:
            plan.raw_archive_capture.commit(
                response_complete=False,
                status_code=(getattr(resp, 'status_code', None)
                             if resp is not None else None),
            )
        try:
            if plan.raw_dumper.enabled and plan.raw_dumper._fh is not None:
                plan.raw_dumper.finish(error=True)
        except Exception as e:
            logger.debug('%s RawSSEDumper.finish(error=True) failed: %s', log_prefix, e)
        if resp is not None:
            resp.close()
