# HOT_PATH
"""Async streaming chat completion with SSE parsing.

Drop-in async replacement for stream.py. Uses httpx.AsyncClient instead
of requests.post(stream=True). All SSE parsing, error classification,
retry logic, diagnostic dumping, and tool-call accumulation are shared
with the sync transport via ``lib/llm/_sse_core.py``.

Public API:
  - async_stream_chat(body, ...) → (assistant_msg, finish_reason, usage)
"""

import asyncio
import contextlib
import time

import httpx

from lib.llm._sse_core import (
    SSEAccumulator,
    activate_native_tool_search_fallback,
    classify_status_error,
    prepare_request,
)
from lib.llm._transport import (
    MAX_STREAM_RETRIES,
    apply_model_limit_retry,
    async_abortable_sleep,
    attach_limit_learned,
    get_async_client,
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
    repair_mojibake,
)
from lib.log import get_logger
from lib.proxy import async_proxy_for as _async_proxy_for
from lib.proxy import report_outcome as _proxy_report_outcome
from lib.subscription_quota import record_codex_quota

logger = get_logger(__name__)


def _httpx_proxy_url(url: str):
    """Proxy URL for the httpx async client — delegates to
    ``lib.proxy.async_proxy_for`` so the async transport honours env
    ``no_proxy`` exactly like the sync one (httpx alone ignores it once an
    explicit ``proxy=`` is set)."""
    return _async_proxy_for(url)


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
        client = get_async_client(_httpx_proxy_url(plan.url))
        async with client.stream(
                'POST', plan.url, headers=plan.hdrs, json=plan.body) as resp:
            yield resp, None
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
        report_subscription_route(
            plan.url, route, True,
            (time.monotonic() - started) * 1000.0)
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
                            adapter=None, on_first_byte_wait=None):
    """Async streaming chat completion with callbacks.

    Same signature and semantics as stream_chat() but fully async.
    Uses httpx.AsyncClient for non-blocking I/O.

    Returns:
        (assistant_msg, finish_reason, usage)
    """
    last_err = None
    _limit_learned = None
    for attempt in range(1 + MAX_STREAM_RETRIES):
        try:
            msg, finish_reason, usage = await _async_stream_chat_once(
                body, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                attempt=attempt, api_key=api_key, base_url=base_url,
                extra_headers=extra_headers, api_protocol=api_protocol,
                oauth=oauth, adapter=adapter, on_first_byte_wait=on_first_byte_wait)
            usage = attach_limit_learned(usage, _limit_learned)
            return msg, finish_reason, usage
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
                                  oauth='', adapter=None, on_first_byte_wait=None):
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
            on_first_byte_wait=on_first_byte_wait)
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
            on_first_byte_wait=on_first_byte_wait)

    _conn_t0 = time.monotonic()
    try:
        async with _open_server_stream(plan, log_prefix) as (resp, route):
            if route is None:
                _proxy_report_outcome(
                    plan.url, True,
                    (time.monotonic() - _conn_t0) * 1000.0)
            resp_trace = resp.headers.get('M-TraceId', '')
            if resp_trace and resp_trace != plan.trace_id:
                logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

            if resp.status_code != 200:
                # repair_mojibake: the toio UPSTREAM_VENDOR wrap layer double-
                # encodes CJK error text (latin-1 misdecode re-encoded as
                # UTF-8), so a correct UTF-8 decode still yields mojibake.
                err_body = repair_mojibake(
                    (await resp.aread()).decode('utf-8', errors='replace'))
                if activate_native_tool_search_fallback(
                        resp.status_code, err_body, plan=plan,
                        canonical_body=body):
                    raise RetryableAPIError(
                        'native Tool Search rejected; retrying locally',
                        status_code=resp.status_code)
                classify_status_error(resp.status_code, err_body, body=plan.body,
                                      log_prefix=log_prefix, raw_dumper=plan.raw_dumper)

            acc = SSEAccumulator(
                plan.body, plan.trace_id, plan.raw_dumper, plan.wire_translator,
                plan.t0, url=plan.url, log_prefix=log_prefix,
                on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready)

            # ── Idle watchdog (async idiom) ──
            # Mirrors the sync StreamIdleWatchdog in lib/llm/_transport.py:
            # beat while the upstream is silent (HUD + the stuck-task
            # reaper's liveness clocks) and poll ``abort_check`` so a Stop
            # pressed during a silent stretch closes the response. There is
            # NO time-based kill and no read timeout — a wait is not a
            # failure. The idle clock RESETS on each line rather than
            # disarming, so a mid-stream stall keeps beating and stays
            # abortable. Constants are read through the module so tests /
            # deployments can retune.
            import lib.llm._transport as _tp
            _fb = {'last': time.monotonic(), 'aborted': False, 'done': False}

            async def _idle_watchdog():
                _interval = _tp.IDLE_HEARTBEAT_S
                _beats = _interval > 0 and on_first_byte_wait is not None
                if not _beats and abort_check is None:
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
                    _idle = _now - _fb['last']
                    if (_beats and _idle >= _interval
                            and (_now - _last_beat) >= _interval):
                        _last_beat = _now
                        try:
                            on_first_byte_wait(_idle)
                        except Exception as _cb:
                            logger.debug('%s on_first_byte_wait raised: %s',
                                         log_prefix, _cb)
                    _wait = _tp.ABORT_POLL_INTERVAL if abort_check is not None else _interval
                    if _beats:
                        _wait = min(_wait, _interval)
                    try:
                        await asyncio.sleep(max(_wait, 0.01))
                    except asyncio.CancelledError:
                        return

            _wd_task = asyncio.ensure_future(_idle_watchdog())
            stopped = False
            try:
                async for raw_line in resp.aiter_lines():
                    # Any line — even a blank keep-alive — proves the upstream
                    # is alive: reset the idle clock (NOT a disarm; see above).
                    _fb['last'] = time.monotonic()
                    if abort_check and abort_check():
                        # Abort mid-stream: break immediately. The response is
                        # left partially read, so httpx drops the connection —
                        # which is correct, an aborted stream must not be reused.
                        acc.mark_aborted()
                        break
                    if not stopped:
                        if acc.feed_line(raw_line):
                            # Accumulator is done, but do NOT break: keep pulling
                            # the (now trivial) trailing lines to natural EOF so
                            # httpx returns the keep-alive connection to the pool.
                            # A partially-read response is discarded by httpx, which
                            # would defeat connection reuse across turns.
                            stopped = True
            except BaseException as _iter_e:
                if _fb['aborted']:
                    raise AbortedError(
                        'User aborted while waiting on %s' % plan.url) from _iter_e
                raise
            finally:
                _fb['done'] = True
                _wd_task.cancel()
            if _fb['aborted']:
                # A close() can surface as a CLEAN end of iteration — without
                # this check an aborted attempt would finalize as a silent
                # empty/partial "success".
                raise AbortedError('User aborted while waiting on %s' % plan.url)

            acc.fire_final_tool_callback()
            msg, finish_reason, usage = acc.finalize(resp_trace=resp_trace)
            _quota_scope = 'oauth_codex' if oauth == 'codex' else 'codex'
            usage = record_codex_quota(
                resp.headers, usage, cache_key=_quota_scope)
            return msg, finish_reason, usage

    except httpx.ConnectTimeout as e:
        # Connect-phase timeout = endpoint down → fail over (dispatch).
        _proxy_report_outcome(plan.url, False)
        logger.warning('%s ✖ Endpoint unreachable (connect timeout) %s: %s',
                       log_prefix, plan.url, e)
        raise EndpointUnreachableError(
            'endpoint unreachable: %s' % e, base_url=plan.url) from e
    except httpx.ConnectError as e:
        # Connection refused / SYN dropped = endpoint down → fail over.
        _proxy_report_outcome(plan.url, False)
        logger.warning('%s ✖ Endpoint unreachable (connect error) %s: %s',
                       log_prefix, plan.url, e)
        raise EndpointUnreachableError(
            'endpoint unreachable: %s' % e, base_url=plan.url) from e
    except httpx.TimeoutException as e:
        # Read/write/pool timeout — server accepted but is slow.
        # Retryable on the same slot (transient), unlike connect-phase.
        raise RetryableAPIError(f'httpx timeout: {e}') from e
    except httpx.RemoteProtocolError as e:
        raise RetryableAPIError(f'httpx protocol error: {e}') from e
    finally:
        try:
            if plan.raw_dumper.enabled and plan.raw_dumper._fh is not None:
                plan.raw_dumper.finish(error=True)
        except Exception as e:
            logger.debug('%s RawSSEDumper.finish(error=True) failed: %s', log_prefix, e)
