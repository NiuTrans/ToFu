# HOT_PATH
"""Async streaming chat completion with SSE parsing.

Drop-in async replacement for stream.py. Uses httpx.AsyncClient instead
of requests.post(stream=True). All SSE parsing, error classification,
retry logic, diagnostic dumping, and tool-call accumulation are shared
with the sync transport via ``lib/llm/_sse_core.py``.

Public API:
  - async_stream_chat(body, ...) → ProviderStreamResult
"""

import asyncio
import contextlib
import time

import httpx

from lib.llm._sse_core import (
    SSEAccumulator,
    activate_native_orchestration_fallback,
    activate_native_tool_search_fallback,
    classify_status_error,
    prepare_request,
)
from lib.llm._sse_framer import SSEFramer
from lib.llm._transport import (
    MAX_STREAM_RETRIES,
    apply_model_limit_retry,
    async_abortable_sleep,
    attach_limit_learned,
    get_async_client,
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
    ModelLimitError,
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    RetryableAPIError,
    _RETRYABLE,
    repair_mojibake,
)
from lib.log import get_logger
from lib.proxy import report_outcome as _proxy_report_outcome
from lib.proxy import resolve_async_route as _resolve_async_route
from lib.subscription_quota import record_codex_quota

logger = get_logger(__name__)


async def _aiter_response_bytes(response):
    """Yield raw response bytes, with a narrow legacy-test fallback."""
    aiter_bytes = getattr(response, 'aiter_bytes', None)
    if callable(aiter_bytes):
        async for chunk in aiter_bytes():
            if chunk:
                yield bytes(chunk)
        return
    async for line in response.aiter_lines():
        if isinstance(line, str):
            line = line.encode('utf-8')
        if line:
            yield bytes(line) + b'\n\n'


def _httpx_proxy_url(url: str):
    """Proxy URL for the httpx async client — delegates to
    ``lib.proxy.async_proxy_for`` so the async transport honours env
    ``no_proxy`` exactly like the sync one (httpx alone ignores it once an
    explicit ``proxy=`` is set)."""
    return _resolve_async_route(url)[0]


def _close_abandoned_raw_dumper(plan, log_prefix: str, reason: str) -> None:
    """Close a prepared plan that will not enter the async transport."""
    try:
        if plan.raw_dumper.enabled and plan.raw_dumper._fh is not None:
            plan.raw_dumper.finish(error=True)
    except Exception as error:
        logger.debug('%s RawSSEDumper close before %s raised: %s',
                     log_prefix, reason, error)


@contextlib.asynccontextmanager
async def _open_server_stream(plan, log_prefix: str = ''):
    """Open response headers over the shared subscription route plan.

    The probe race is synchronous network I/O, so it runs in a worker.  Real
    requests remain native-httpx and sequential: only a connect-phase error
    advances to the next route; once headers arrive the response is final.
    """
    from lib.proxy import (
        report_subscription_route,
        subscription_route_candidates,
    )

    routes = await asyncio.to_thread(subscription_route_candidates, plan.url)
    if not routes:
        proxy_url, route_metadata = _resolve_async_route(plan.url)
        client = get_async_client(proxy_url)
        started = time.monotonic()
        try:
            async with client.stream(
                    'POST', plan.url, headers=plan.hdrs,
                    json=plan.body) as resp:
                if not isinstance(getattr(resp, 'extensions', None), dict):
                    resp.extensions = {}
                resp.extensions['tofu_network_route'] = route_metadata
                resp.extensions['tofu_network_latency_ms'] = (
                    time.monotonic() - started) * 1000.0
                yield resp, None
        except (httpx.ConnectTimeout, httpx.ConnectError) as error:
            _proxy_report_outcome(
                plan.url, False,
                pool_id=route_metadata.get('poolId') or '')
            try:
                error.network_route = dict(route_metadata)
                error.failure_stage = 'connect'
            except Exception as annotate_error:
                logger.debug('%s generic route annotation failed: %s',
                             log_prefix, annotate_error)
            raise
        return

    attempted = set()
    for route in routes:
        attempted.add(route.route_id)
        client = get_async_client(route.async_proxy_url())
        stream_context = client.stream(
            'POST', plan.url, headers=plan.hdrs, json=plan.body)
        started = time.monotonic()
        try:
            response = await stream_context.__aenter__()
        except (httpx.ConnectTimeout, httpx.ConnectError):
            report_subscription_route(plan.url, route, False)
            logger.info('%s connection failed via %s — trying next '
                        'subscription route', log_prefix, route.label)
            known = {item.route_id for item in routes}
            refreshed = await asyncio.to_thread(
                subscription_route_candidates, plan.url)
            for candidate in refreshed:
                if (candidate.route_id not in attempted
                        and candidate.route_id not in known):
                    routes.append(candidate)
                    known.add(candidate.route_id)
            continue
        if not isinstance(getattr(response, 'extensions', None), dict):
            response.extensions = {}
        response.extensions['tofu_network_route'] = {
            'routeId': str(route.route_id)[:160],
            'routeMode': str(route.mode)[:24],
            'decisionReason': 'subscription_route_race',
            **({'poolId': str(route.pool_id)[:160]} if route.pool_id else {}),
        }
        response.extensions['tofu_network_latency_ms'] = (
            time.monotonic() - started) * 1000.0
        try:
            yield response, route
        finally:
            await stream_context.__aexit__(None, None, None)
        return
    raise EndpointUnreachableError(
        'all server subscription routes failed during connection setup',
        base_url=plan.url) from None


async def async_stream_chat(body, *, on_thinking=None, on_content=None,
                            on_tool_call_ready=None,
                            abort_check=None, log_prefix='', api_key=None,
                            base_url=None, extra_headers=None,
                            api_protocol='openai', oauth='',
                            adapter=None,
                            on_first_byte_wait=None,
                            on_stream_wait=None) -> ProviderStreamResult:
    """Async streaming chat completion with callbacks.

    Same signature and semantics as stream_chat() but fully async.
    Uses httpx.AsyncClient for non-blocking I/O.

    Returns:
        A typed provider-stream result. Legacy callers may still unpack it as
        ``(assistant_msg, finish_reason, usage)``.
    """
    last_err = None
    _limit_learned = None
    for attempt in range(1 + MAX_STREAM_RETRIES):
        try:
            stream_result = ensure_provider_stream_result(
                await _async_stream_chat_once(
                body, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                attempt=attempt, api_key=api_key, base_url=base_url,
                extra_headers=extra_headers, api_protocol=api_protocol,
                oauth=oauth, adapter=adapter,
                on_first_byte_wait=on_first_byte_wait,
                on_stream_wait=on_stream_wait))
            usage = attach_limit_learned(stream_result.usage, _limit_learned)
            return stream_result.with_usage(usage)
        except (RateLimitError, PermissionError_, AbortedError,
                ContentFilterError, PromptTooLongError,
                EndpointUnreachableError):
            # EndpointUnreachableError escapes to the dispatch layer so a
            # dead host fails over instead of being retried on the same slot.
            # AbortedError: the user pressed Stop — never retry that.
            raise
        except ModelLimitError as e:
            logger.debug('async stream chat: ModelLimitError (%s)', e)
            _limit_learned = apply_model_limit_retry(body, e, log_prefix)
            continue
        except _RETRYABLE as e:
            last_err = e
            wait = prepare_retryable_wait(attempt, e, abort_check, log_prefix)
            await async_abortable_sleep(wait, abort_check)
    raise last_err


async def _async_stream_chat_once(body, *, on_thinking=None, on_content=None,
                                  on_tool_call_ready=None,
                                  abort_check=None, log_prefix='', attempt=0,
                                  api_key=None, base_url=None,
                                  extra_headers=None, api_protocol='openai',
                                  oauth='', adapter=None,
                                  on_first_byte_wait=None,
                                  on_stream_wait=None):
    """Single async attempt at a streaming chat completion (httpx transport)."""
    if adapter:
        # ── Subscription-adapter branch (E4) ──
        # The relay helpers are BLOCKING bridge calls — a loopback RTT to
        # the desktop agent. Running them on the Quart event loop would
        # freeze every concurrent request (design law: no blocking bridge
        # RTT on the loop), so delegate the whole attempt to the sync
        # transport in a worker thread via asyncio.to_thread. The sync
        # transport's adapter branch owns relay_stream / error mapping.
        from lib.llm.stream import _stream_chat_once as _sync_stream_once
        return await asyncio.to_thread(
            _sync_stream_once, body,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            abort_check=abort_check, log_prefix=log_prefix, attempt=attempt,
            api_key=api_key, base_url=base_url, extra_headers=extra_headers,
            api_protocol=api_protocol, oauth=oauth, adapter=adapter,
            on_first_byte_wait=on_first_byte_wait,
            on_stream_wait=on_stream_wait)
    # prepare_request is sync and CAN block for seconds: a subscription
    # OAuth slot may refresh its token inside resolve_oauth_request, and
    # under desktop-egress routing that refresh waits an agent RTT (design
    # §6.2 A2). Running it on the event loop would freeze every concurrent
    # request — move it to a worker thread.
    plan = await asyncio.to_thread(
        prepare_request,
        body, attempt=attempt, log_prefix=log_prefix,
        api_key=api_key, base_url=base_url, extra_headers=extra_headers,
        api_protocol=api_protocol, oauth=oauth)
    # Read the operator value at attempt time so tests and long-lived servers
    # can retune it without creating a second async timeout definition.
    import lib.llm._transport as _tp
    _attempt_started_at = getattr(plan, 't0', None)
    if _attempt_started_at is None:
        # Compatibility for narrow request-plan fakes and pre-t0 adapters.
        # Production RequestPlan always carries the monotonic request boundary.
        _attempt_started_at = time.monotonic()
    _progress = _tp.StreamProgress(0, started_at=_attempt_started_at)

    # Desktop-agent fallback still uses the proven sync bridge reader.  Keep
    # it off the event loop; server-side direct/proxy routes stay native async
    # below and share the same health plan as the sync transport.
    from lib.desktop import egress as _eg
    try:
        egress_route = await asyncio.to_thread(
            _eg.route_request, plan.url, user_id='')
    except _eg.EgressUnavailable as e:
        _close_abandoned_raw_dumper(plan, log_prefix, 'egress failure')
        raise EndpointUnreachableError(str(e), base_url=plan.url) from e
    if egress_route != 'direct':
        # The sync bridge attempt prepares its own plan; close this abandoned
        # async plan first so debug raw-dump file descriptors never leak.
        _close_abandoned_raw_dumper(plan, log_prefix, 'egress handoff')
        from lib.llm.stream import _stream_chat_once as _sync_stream_once
        return await asyncio.to_thread(
            _sync_stream_once, body,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            abort_check=abort_check, log_prefix=log_prefix, attempt=attempt,
            api_key=api_key, base_url=base_url, extra_headers=extra_headers,
            api_protocol=api_protocol, oauth=oauth, adapter=adapter,
            on_first_byte_wait=on_first_byte_wait,
            on_stream_wait=on_stream_wait)

    _conn_t0 = time.monotonic()
    _network_route = {
        'routeId': 'unresolved',
        'routeMode': 'unknown',
        'decisionReason': 'not_resolved',
    }
    _network_latency_ms = None
    _network_reported = False
    _active_subscription_route = None

    def _report_network_outcome(ok: bool, failure_kind='network_fail'):
        nonlocal _network_reported
        if _network_reported:
            return
        _network_reported = True
        try:
            if _active_subscription_route is not None:
                from lib.proxy import report_subscription_route
                report_subscription_route(
                    plan.url, _active_subscription_route, ok,
                    _network_latency_ms, failure_kind=failure_kind)
            elif _network_route.get('routeMode') in {
                    'direct', 'proxy', 'env'}:
                _proxy_report_outcome(
                    plan.url, ok, _network_latency_ms,
                    pool_id=_network_route.get('poolId') or '')
        except Exception as error:
            logger.debug('%s async network outcome report failed: %s',
                         log_prefix, error)

    def _annotate_network_error(error, failure_stage):
        try:
            error.network_route = dict(_network_route)
            error.failure_stage = str(failure_stage or '')[:80]
        except Exception as annotate_error:
            logger.debug('%s async network error annotation failed: %s',
                         log_prefix, annotate_error)
        return error

    try:
        async with _open_server_stream(plan, log_prefix) as (resp, route):
            _active_subscription_route = route
            route_extension = resp.extensions.get('tofu_network_route')
            if isinstance(route_extension, dict):
                _network_route = dict(route_extension)
            try:
                _network_latency_ms = float(resp.extensions.get(
                    'tofu_network_latency_ms'))
            except (TypeError, ValueError, OverflowError):
                _network_latency_ms = (
                    time.monotonic() - _conn_t0) * 1000.0
            resp_trace = resp.headers.get('M-TraceId', '')
            _progress.mark_response_headers()
            if resp_trace and resp_trace != plan.trace_id:
                logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

            if resp.status_code != 200:
                _report_network_outcome(True)
                # repair_mojibake: the toio UPSTREAM_VENDOR wrap layer double-
                # encodes CJK error text (latin-1 misdecode re-encoded as
                # UTF-8), so a correct UTF-8 decode still yields mojibake.
                err_body = repair_mojibake(
                    (await resp.aread()).decode('utf-8', errors='replace'))
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
                try:
                    classify_status_error(
                        resp.status_code, err_body, body=plan.body,
                        log_prefix=log_prefix, raw_dumper=plan.raw_dumper)
                except Exception as error:
                    _annotate_network_error(error, 'provider_response')
                    raise

            acc = SSEAccumulator(
                plan.body, plan.trace_id, plan.raw_dumper, plan.wire_translator,
                _attempt_started_at, url=plan.url, log_prefix=log_prefix,
                on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                progress=_progress)

            # ── Idle watchdog (async idiom) ──
            # Mirrors the sync StreamIdleWatchdog in lib/llm/_transport.py:
            # beat while the upstream is silent (HUD + the stuck-task
            # reaper's liveness clocks) and poll ``abort_check`` so a Stop
            # pressed during a silent stretch closes the response. There is no
            # socket read timeout. A wait with no transport activity past the
            # rolling stream-idle window is cut short; any bytes, including SSE
            # comments/keep-alives, renew it. Constants are read through the
            # module so tests/deployments can retune.
            _fb = {
                'aborted': False,
                'idle_timed_out': False,
                'done': False,
            }

            async def _idle_watchdog():
                _interval = _tp.IDLE_HEARTBEAT_S
                _idle_timeout = _tp.stream_idle_timeout_seconds()
                _beats = _interval > 0 and (
                    on_first_byte_wait is not None
                    or on_stream_wait is not None)
                if (not _beats and abort_check is None
                        and _idle_timeout <= 0):
                    return
                _last_beat = time.monotonic()
                while not _fb['done']:
                    if abort_check is not None:
                        try:
                            if abort_check():
                                _fb['aborted'] = True
                                try:
                                    await resp.aclose()
                                except Exception as _ck:
                                    logger.debug('%s watchdog aclose raised: %s',
                                                 log_prefix, _ck)
                                return
                        except Exception as _ae:
                            logger.debug('%s abort_check raised: %s', log_prefix, _ae)
                    _now = time.monotonic()
                    _idle = _progress.transport_idle_seconds(_now)
                    if _idle_timeout > 0 and _idle >= _idle_timeout:
                        _fb['idle_timed_out'] = True
                        try:
                            await resp.aclose()
                        except Exception as _ck:
                            logger.debug('%s watchdog aclose raised: %s',
                                         log_prefix, _ck)
                        return
                    if (_beats and _idle >= _interval
                            and (_now - _last_beat) >= _interval):
                        _last_beat = _now
                        if on_stream_wait is not None:
                            try:
                                on_stream_wait(_progress.wait_status(_now))
                            except Exception as _cb:
                                logger.debug('%s on_stream_wait raised: %s',
                                             log_prefix, _cb)
                        if on_first_byte_wait is not None:
                            try:
                                on_first_byte_wait(_idle)
                            except Exception as _cb:
                                logger.debug(
                                    '%s on_first_byte_wait raised: %s',
                                    log_prefix, _cb)
                    _wait = _tp.ABORT_POLL_INTERVAL if abort_check is not None else _interval
                    if _beats:
                        _wait = min(_wait, _interval)
                    if _idle_timeout > 0:
                        if _wait <= 0:
                            _wait = _idle_timeout
                        else:
                            _wait = min(_wait, _idle_timeout)
                    _remaining = _progress.transport_remaining_seconds(
                        _idle_timeout, _now)
                    if _remaining is not None:
                        _remaining = max(0.01, _remaining)
                        if _wait <= 0:
                            _wait = _remaining
                        else:
                            _wait = min(_wait, _remaining)
                    try:
                        await asyncio.sleep(max(_wait, 0.01))
                    except asyncio.CancelledError:
                        return

            _wd_task = asyncio.ensure_future(_idle_watchdog())
            stopped = False
            framer = SSEFramer()
            try:
                async for raw_chunk in _aiter_response_bytes(resp):
                    if _fb['idle_timed_out']:
                        break
                    _progress.mark_transport_bytes(len(raw_chunk))
                    if abort_check and abort_check():
                        # Abort mid-stream: break immediately. The response is
                        # left partially read, so httpx drops the connection —
                        # which is correct, an aborted stream must not be reused.
                        acc.mark_aborted()
                        break
                    if not stopped:
                        for event in framer.feed(raw_chunk):
                            if acc.feed_event(event):
                                stopped = True
                                break
                        framing_issues = framer.drain_issues()
                        if framing_issues.count:
                            acc.record_malformed_frames(
                                framing_issues.count,
                                framing_issues.diagnostics,
                            )
                        if stopped:
                            # Accumulator is done, but do NOT break: keep pulling
                            # the (now trivial) trailing lines to natural EOF so
                            # httpx returns the keep-alive connection to the pool.
                            # A partially-read response is discarded by httpx, which
                            # would defeat connection reuse across turns.
                            pass
            except BaseException as _iter_e:
                if _fb['idle_timed_out']:
                    # The watchdog closed the response after
                    # IDLE_STREAM_TIMEOUT_S of silence; fall through to
                    # finalize() so the premature-close diagnostics fire.
                    pass
                elif _fb['aborted']:
                    raise AbortedError(
                        'User aborted while waiting on %s' % plan.url) from _iter_e
                elif isinstance(_iter_e, asyncio.CancelledError):
                    # Event-loop/task cancellation is caller control flow, not
                    # evidence that the selected network path is unhealthy.
                    raise
                elif isinstance(_iter_e, (
                        RateLimitError, PermissionError_, ContentFilterError,
                        PromptTooLongError, ModelLimitError,
                        RetryableAPIError)):
                    _report_network_outcome(True)
                    _annotate_network_error(_iter_e, 'provider_stream')
                    raise
                else:
                    _report_network_outcome(False, 'midstream_io')
                    _annotate_network_error(_iter_e, 'midstream_io')
                    raise
            finally:
                _fb['done'] = True
                _wd_task.cancel()
            for event in framer.finalize():
                if not stopped and acc.feed_event(event):
                    stopped = True
            framing_issues = framer.drain_issues()
            if framing_issues.count:
                acc.record_malformed_frames(
                    framing_issues.count, framing_issues.diagnostics)
            if _fb['aborted']:
                # A close() can surface as a CLEAN end of iteration — without
                # this check an aborted attempt would finalize as a silent
                # empty/partial "success".
                raise AbortedError('User aborted while waiting on %s' % plan.url)
            acc.fire_final_tool_callback()
            stream_result = ensure_provider_stream_result(
                acc.finalize(resp_trace=resp_trace))
            msg, finish_reason, usage = stream_result
            _quota_scope = 'oauth_codex' if oauth == 'codex' else 'codex'
            usage = record_codex_quota(
                resp.headers, usage, cache_key=_quota_scope)
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
            return stream_result.with_usage(usage)

    except httpx.ConnectTimeout as e:
        # Connect-phase timeout = endpoint down → fail over (dispatch).
        inherited_route = getattr(e, 'network_route', None)
        if isinstance(inherited_route, dict):
            _network_route = dict(inherited_route)
            _network_reported = True  # _open_server_stream already reported it
        else:
            _report_network_outcome(False, 'connect')
        logger.warning('%s ✖ Endpoint unreachable (connect timeout) %s: %s',
                       log_prefix, plan.url, e)
        error = EndpointUnreachableError(
            'endpoint unreachable: %s' % e, base_url=plan.url)
        raise _annotate_network_error(error, 'connect') from e
    except httpx.ConnectError as e:
        # Connection refused / SYN dropped = endpoint down → fail over.
        inherited_route = getattr(e, 'network_route', None)
        if isinstance(inherited_route, dict):
            _network_route = dict(inherited_route)
            _network_reported = True
        else:
            _report_network_outcome(False, 'connect')
        logger.warning('%s ✖ Endpoint unreachable (connect error) %s: %s',
                       log_prefix, plan.url, e)
        error = EndpointUnreachableError(
            'endpoint unreachable: %s' % e, base_url=plan.url)
        raise _annotate_network_error(error, 'connect') from e
    except httpx.TimeoutException as e:
        # Read/write/pool timeout — server accepted but is slow.
        # Retryable on the same slot (transient), unlike connect-phase.
        _report_network_outcome(False, 'transport_timeout')
        error = RetryableAPIError(f'httpx timeout: {e}')
        raise _annotate_network_error(error, 'transport_timeout') from e
    except httpx.RemoteProtocolError as e:
        _report_network_outcome(False, 'midstream_protocol')
        error = RetryableAPIError(f'httpx protocol error: {e}')
        raise _annotate_network_error(error, 'midstream_protocol') from e
    finally:
        try:
            if plan.raw_dumper.enabled and plan.raw_dumper._fh is not None:
                plan.raw_dumper.finish(error=True)
        except Exception as e:
            logger.debug('%s RawSSEDumper.finish(error=True) failed: %s', log_prefix, e)
