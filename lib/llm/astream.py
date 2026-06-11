# HOT_PATH
"""Async streaming chat completion with SSE parsing.

Drop-in async replacement for stream.py. Uses httpx.AsyncClient instead
of requests.post(stream=True). All SSE parsing, error classification,
retry logic, diagnostic dumping, and tool-call accumulation are shared
with the sync transport via ``lib/llm/_sse_core.py``.

Public API:
  - async_stream_chat(body, ...) → (assistant_msg, finish_reason, usage)
"""

import os

import httpx

from lib.llm._sse_core import (
    SSEAccumulator,
    classify_status_error,
    prepare_request,
)
from lib.llm._transport import (
    CONNECT_TIMEOUT,
    MAX_STREAM_RETRIES,
    async_abortable_sleep,
    retry_wait,
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
)
from lib.log import get_logger
from lib.proxy import proxies_for

logger = get_logger(__name__)


def _httpx_proxy_url(url: str):
    """Convert requests-style proxies_for() result to httpx proxy URL."""
    pf = proxies_for(url)
    if pf:
        return None
    return (os.environ.get('https_proxy')
            or os.environ.get('HTTPS_PROXY')
            or os.environ.get('http_proxy')
            or os.environ.get('HTTP_PROXY')
            or None)


async def async_stream_chat(body, *, on_thinking=None, on_content=None,
                            on_tool_call_ready=None,
                            abort_check=None, log_prefix='', api_key=None,
                            base_url=None, extra_headers=None,
                            api_protocol='openai'):
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
                extra_headers=extra_headers, api_protocol=api_protocol)
            if _limit_learned:
                if usage is None:
                    usage = {}
                usage['_model_limit_learned'] = _limit_learned
            return msg, finish_reason, usage
        except (RateLimitError, PermissionError_, AbortedError,
                ContentFilterError, PromptTooLongError,
                EndpointUnreachableError):
            # EndpointUnreachableError escapes to the dispatch layer so a
            # dead host fails over instead of being retried on the same slot.
            raise
        except ModelLimitError as e:
            body['max_tokens'] = e.detected_limit
            _limit_learned = {
                'model': e.model,
                'old_limit': e.requested_limit,
                'new_limit': e.detected_limit,
            }
            logger.warning('%s ⚙️ Auto-learned max_tokens for %s: %d → %d, retrying…',
                           log_prefix, e.model, e.requested_limit,
                           e.detected_limit)
            continue
        except _RETRYABLE as e:
            last_err = e
            if attempt < MAX_STREAM_RETRIES:
                if abort_check and abort_check():
                    logger.debug('%s ✋ Abort detected before retry sleep.',
                                 log_prefix)
                    raise AbortedError('User aborted before retry')
                wait = retry_wait(attempt)
                logger.warning(
                    '%s ⚠ Transient error (attempt %d): %s: %s — '
                    'retrying in %.1fs …', log_prefix, attempt + 1,
                    type(e).__name__, e, wait, exc_info=True)
                await async_abortable_sleep(wait, abort_check)
            else:
                logger.error('%s ✖ All %d attempts failed.', log_prefix,
                             1 + MAX_STREAM_RETRIES, exc_info=True)
                raise
    raise last_err


async def _async_stream_chat_once(body, *, on_thinking=None, on_content=None,
                                  on_tool_call_ready=None,
                                  abort_check=None, log_prefix='', attempt=0,
                                  api_key=None, base_url=None,
                                  extra_headers=None, api_protocol='openai'):
    """Single async attempt at a streaming chat completion (httpx transport)."""
    plan = prepare_request(
        body, attempt=attempt, log_prefix=log_prefix,
        api_key=api_key, base_url=base_url, extra_headers=extra_headers,
        api_protocol=api_protocol)

    proxy_url = _httpx_proxy_url(plan.url)

    async with httpx.AsyncClient(
        proxy=proxy_url,
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=300, write=60, pool=60),
        follow_redirects=True,
    ) as client:
        try:
            async with client.stream(
                'POST', plan.url, headers=plan.hdrs, json=plan.body,
            ) as resp:
                resp_trace = resp.headers.get('M-TraceId', '')
                if resp_trace and resp_trace != plan.trace_id:
                    logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

                if resp.status_code != 200:
                    err_body = (await resp.aread()).decode('utf-8', errors='replace')
                    classify_status_error(resp.status_code, err_body, body=plan.body,
                                          log_prefix=log_prefix, raw_dumper=plan.raw_dumper)

                acc = SSEAccumulator(
                    plan.body, plan.trace_id, plan.raw_dumper, plan.codex_translator,
                    plan.t0, url=plan.url, log_prefix=log_prefix,
                    on_thinking=on_thinking, on_content=on_content,
                    on_tool_call_ready=on_tool_call_ready,
                    anthropic_translator=plan.anthropic_translator)

                async for raw_line in resp.aiter_lines():
                    if abort_check and abort_check():
                        acc.mark_aborted()
                        break
                    if acc.feed_line(raw_line):
                        break

                acc.fire_final_tool_callback()
                return acc.finalize(resp_trace=resp_trace)

        except httpx.ConnectTimeout as e:
            # Connect-phase timeout = endpoint down → fail over (dispatch).
            logger.warning('%s ✖ Endpoint unreachable (connect timeout) %s: %s',
                           log_prefix, plan.url, e)
            raise EndpointUnreachableError(
                'endpoint unreachable: %s' % e, base_url=plan.url) from e
        except httpx.ConnectError as e:
            # Connection refused / SYN dropped = endpoint down → fail over.
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
