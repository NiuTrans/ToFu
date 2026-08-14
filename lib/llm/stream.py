# HOT_PATH
"""Streaming chat completion with SSE parsing (sync transport).

Public API:
  - stream_chat(body, ...) → (assistant_msg, finish_reason, usage)

The SSE parsing / error classification / tool-call accumulation / anomaly
diagnostics live in ``lib/llm/_sse_core.py`` and are shared with the async
transport (``lib/llm/astream.py``). This module is the thin ``requests``
shell: it opens the stream, feeds lines to the core, and keeps the
retry/backoff wrapper.
"""

import json
import time

import requests

from lib.llm._sse_core import (
    SSEAccumulator,
    activate_native_tool_search_fallback,
    classify_status_error,
    prepare_request,
)
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
from lib.llm_errors import (
    AbortedError,
    ContentFilterError,
    EndpointUnreachableError,
    ModelLimitError,
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    RetryableAPIError,
    _RETRYABLE,
    decode_error_body,
)
from lib.log import get_logger
from lib.proxy import proxies_for, report_outcome as _proxy_report_outcome
from lib.subscription_quota import record_codex_quota

logger = get_logger(__name__)


def stream_chat(body, *, on_thinking=None, on_content=None,
                on_tool_call_ready=None,
                abort_check=None, log_prefix='', api_key=None, base_url=None,
                extra_headers=None, api_protocol='openai', oauth='',
                adapter=None, on_attempt_restart=None, on_first_byte_wait=None):
    """Streaming chat completion with callbacks.

    Automatically retries on transient connection errors up to
    MAX_STREAM_RETRIES times.

    ``on_attempt_restart`` (optional): fired with ``reason=<str>`` whenever an
    in-flight attempt is being DISCARDED and the request is about to restart
    from scratch. Any content/thinking already delivered via on_content /
    on_thinking during that attempt will be re-streamed — the callee must drop
    its partial accumulation (e.g. truncate back to the per-round base) so the
    re-streamed text does not stack on the abandoned attempt's tail.

    ``on_first_byte_wait`` (optional): fired with the current IDLE duration
    (seconds since the last byte, or since request send when none has
    arrived) every IDLE_HEARTBEAT_S while the attempt is silent — both
    before the first SSE byte and during any mid-stream stall. See
    lib/llm/_transport.StreamIdleWatchdog. There is no read timeout, so
    this beat is the only liveness signal a long silence produces; the
    stuck-task reaper depends on it (docstring there).

    Returns:
        (assistant_msg, finish_reason, usage)

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
            msg, finish_reason, usage = _stream_chat_once(
                body, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                attempt=attempt, api_key=api_key, base_url=base_url,
                extra_headers=extra_headers, api_protocol=api_protocol,
                oauth=oauth, adapter=adapter, on_first_byte_wait=on_first_byte_wait)
            usage = attach_limit_learned(usage, _limit_learned)
            return msg, finish_reason, usage
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


def _stream_chat_once(body, *, on_thinking=None, on_content=None,
                      on_tool_call_ready=None,
                      abort_check=None, log_prefix='', attempt=0,
                      api_key=None, base_url=None, extra_headers=None,
                      api_protocol='openai', oauth='', adapter=None,
                      on_first_byte_wait=None):
    """Single attempt at a streaming chat completion (sync transport)."""
    plan = prepare_request(
        body, attempt=attempt, log_prefix=log_prefix,
        api_key=api_key, base_url=base_url, extra_headers=extra_headers,
        api_protocol=api_protocol, oauth=oauth)

    if plan.responses_transport == 'websocket':
        from lib.llm.responses_websocket import (
            ResponsesWebSocketUnavailable,
            stream_responses_websocket,
        )
        try:
            return stream_responses_websocket(
                plan, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                on_first_byte_wait=on_first_byte_wait)
        except ResponsesWebSocketUnavailable as exc:
            # The socket failed before response.create was sent, so the same
            # translated request can safely use the proven SSE transport.
            logger.warning('%s [ResponsesWS] unavailable before send (%s); '
                           'falling back to SSE', log_prefix, exc)

    # ``prepare_request`` already opened the RawSSEDumper fd (when enabled), so
    # a single outer try/finally must guard EVERY exit path — including the
    # connect-phase re-raise below, which used to escape before the dumper was
    # closed and leaked the fd once per retry against a down endpoint.
    resp = None
    # ── Idle watchdog ──
    # Two jobs, neither of which bounds the request: beat while the
    # upstream is silent (HUD + the reaper's liveness clocks), and poll
    # ``abort_check`` so a Stop pressed during a silent stretch actually
    # lands. The in-loop abort check below only runs when a line arrives,
    # so without this poll a zero-byte hang would ignore Stop entirely —
    # and with no read timeout left, nothing else would ever end it.
    # Constants are read through the module at call time so tests /
    # deployments can retune without a re-import.
    import lib.llm._transport as _tp
    _resp_holder = {}
    _watchdog = StreamIdleWatchdog(
        heartbeat_interval=_tp.IDLE_HEARTBEAT_S,
        on_beat=on_first_byte_wait,
        abort_check=abort_check,
        on_abort=lambda: (_resp_holder.get('resp') and
                          _resp_holder['resp'].close()))
    _watchdog.start()
    try:
        _conn_t0 = time.monotonic()
        try:
            # ── Desktop-egress branch (S3): when the server's own egress to
            # this host is geo-blocked / dead, open the stream through the
            # user's desktop agent instead. Probe is per-host cached (300s).
            from lib.desktop import egress as _eg
            if adapter:
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
                try:
                    resp = _ad.relay_stream(
                        adapter.get('agent_id', ''),
                        int(adapter.get('port') or 0),
                        _relay_path, headers=plan.hdrs,
                        body=json.dumps(plan.body).encode(),
                        log_prefix=log_prefix)
                except _eg.EgressUnavailable as e:
                    _proxy_report_outcome(plan.url, False)
                    raise EndpointUnreachableError(
                        str(e), base_url=plan.url) from e
                _resp_holder['resp'] = resp
                if _watchdog.aborted:
                    raise AbortedError('User aborted while awaiting response headers')
                _proxy_report_outcome(
                    plan.url, True, (time.monotonic() - _conn_t0) * 1000.0)
            else:
                try:
                    _egress_route = _eg.route_request(plan.url, user_id='')
                except _eg.EgressUnavailable as e:
                    raise EndpointUnreachableError(str(e), base_url=plan.url) from e
                if _egress_route != 'direct':
                    try:
                        resp = _eg.open_stream(
                            plan.url, method='POST', headers=plan.hdrs,
                            body=json.dumps(plan.body).encode(),
                            agent_id=_egress_route, log_prefix=log_prefix)
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
                            try:
                                resp = get_sync_session().post(
                                    plan.url, headers=plan.hdrs,
                                    json=plan.body, stream=True,
                                    timeout=(CONNECT_TIMEOUT, None),
                                    proxies=_server_route.requests_proxies(),
                                    allow_redirects=False)
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
                            report_subscription_route(
                                plan.url, _server_route, True,
                                (time.monotonic() - _route_t0) * 1000.0)
                            break
                        else:
                            raise EndpointUnreachableError(
                                'all server subscription routes failed during '
                                'connection setup',
                                base_url=plan.url) from None
                    else:
                        resp = get_sync_session().post(
                            plan.url, headers=plan.hdrs, json=plan.body,
                            stream=True, timeout=(CONNECT_TIMEOUT, None),
                            proxies=proxies_for(plan.url),
                            allow_redirects=False)
                _resp_holder['resp'] = resp
                if _watchdog.aborted:
                    # Stop landed while we were blocked pre-headers — the flag
                    # is all we get (no socket handle to close retroactively).
                    raise AbortedError('User aborted while awaiting response headers')
                # Server-route attempts report their concrete route above;
                # desktop-agent success belongs to agent health, not a stale
                # server proxy choice. Only generic non-subscription direct
                # traffic still feeds the legacy netpath scorer here.
                if _egress_route == 'direct' and not _server_routes:
                    _proxy_report_outcome(
                        plan.url, True,
                        (time.monotonic() - _conn_t0) * 1000.0)
        except AbortedError:
            raise
        except requests.exceptions.ConnectionError as e:
            _proxy_report_outcome(plan.url, False)
            # Connect-phase failure (ConnectTimeout / connection refused /
            # SYN dropped) = the endpoint is down. Convert to
            # EndpointUnreachableError so it escapes the same-key retry loop
            # and the dispatch layer fails over to a healthy slot instead of
            # burning CONNECT_TIMEOUT × MAX_STREAM_RETRIES on a dead host.
            logger.warning('%s ✖ Endpoint unreachable (connect phase) %s: %s',
                           log_prefix, plan.url, e)
            raise EndpointUnreachableError(
                'endpoint unreachable: %s' % e, base_url=plan.url) from e

        resp_trace = resp.headers.get('M-TraceId', '')
        if resp_trace and resp_trace != plan.trace_id:
            logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

        if resp.status_code != 200:
            # decode_error_body, NOT resp.text: requests falls back to
            # ISO-8859-1 for text/* without charset, garbling UTF-8 CJK
            # gateway error pages into mojibake (toio 400 incident 2026-07-25).
            # Egress readers have no .content — drain their text instead.
            if hasattr(resp, 'read_all_text'):
                from lib.desktop.egress import EgressUnavailable as _EU
                try:
                    err_body = resp.read_all_text()
                except _EU as e:
                    raise EndpointUnreachableError(str(e), base_url=plan.url) from e
            else:
                err_body = decode_error_body(resp)
            if activate_native_tool_search_fallback(
                    resp.status_code, err_body, plan=plan,
                    canonical_body=body):
                raise RetryableAPIError(
                    'native Tool Search rejected; retrying locally',
                    status_code=resp.status_code)
            classify_status_error(resp.status_code, err_body,
                                  body=plan.body,
                                  log_prefix=log_prefix, raw_dumper=plan.raw_dumper)

        resp.encoding = 'utf-8'

        acc = SSEAccumulator(
            plan.body, plan.trace_id, plan.raw_dumper, plan.wire_translator,
            plan.t0, url=plan.url, log_prefix=log_prefix,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready)

        try:
            for line in resp.iter_lines(decode_unicode=True):
                # Any line — even a blank keep-alive — proves the upstream is
                # alive: reset the idle clock. Deliberately NOT a disarm:
                # with no read timeout, a stream that goes quiet again after
                # its first byte is just as unbounded as one that never
                # started, so beats must resume and abort must stay pollable.
                _watchdog.notify_activity()
                if abort_check and abort_check():
                    acc.mark_aborted()
                    break
                if acc.feed_line(line):
                    break
        except Exception as _iter_e:
            if _watchdog.aborted:
                raise AbortedError(
                    'User aborted while waiting on %s' % plan.url) from _iter_e
            from lib.desktop.egress import EgressUnavailable as _EU
            if isinstance(_iter_e, _EU):
                # Agent died / stream vanished mid-flight — fail over
                # (provider-down semantics), never a silent partial success.
                raise EndpointUnreachableError(
                    str(_iter_e), base_url=plan.url) from _iter_e
            raise
        if _watchdog.aborted:
            # A close() can surface as a CLEAN end of iteration on some
            # urllib3 versions — without this check an aborted attempt would
            # finalize as a silent empty/partial "success".
            raise AbortedError('User aborted while waiting on %s' % plan.url)

        acc.fire_final_tool_callback()
        msg, finish_reason, usage = acc.finalize(resp_trace=resp_trace)
        # ChatGPT-backed Codex reports the subscription allowance in response
        # headers, separately from the token usage carried by the SSE body.
        # Preserve both facts on the same per-round usage record.
        _quota_scope = ('oauth_codex' if oauth == 'codex' else
                        ('adapter:' + str(adapter.get('agent_id') or '')
                         if isinstance(adapter, dict) and adapter else 'codex'))
        usage = record_codex_quota(
            resp.headers, usage, cache_key=_quota_scope)
        return msg, finish_reason, usage
    finally:
        _watchdog.cancel()
        try:
            if plan.raw_dumper.enabled and plan.raw_dumper._fh is not None:
                plan.raw_dumper.finish(error=True)
        except Exception as e:
            logger.debug('%s RawSSEDumper.finish(error=True) failed: %s', log_prefix, e)
        if resp is not None:
            resp.close()
