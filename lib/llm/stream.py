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

import requests

from lib.llm._sse_core import (
    SSEAccumulator,
    classify_status_error,
    prepare_request,
)
from lib.llm._transport import (
    CONNECT_TIMEOUT,
    MAX_STREAM_RETRIES,
    abortable_sleep,
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
    _RETRYABLE,
)
from lib.log import get_logger
from lib.proxy import proxies_for

logger = get_logger(__name__)


def stream_chat(body, *, on_thinking=None, on_content=None,
                on_tool_call_ready=None,
                abort_check=None, log_prefix='', api_key=None, base_url=None,
                extra_headers=None, api_protocol='openai', oauth=''):
    """Streaming chat completion with callbacks.

    Automatically retries on transient connection errors up to
    MAX_STREAM_RETRIES times.

    Returns:
        (assistant_msg, finish_reason, usage)

    Raises:
        RateLimitError, PermissionError_, AbortedError,
        ContentFilterError, PromptTooLongError, RetryableAPIError
    """
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
                oauth=oauth)
            if _limit_learned:
                if usage is None:
                    usage = {}
                usage['_model_limit_learned'] = _limit_learned
            return msg, finish_reason, usage
        except (RateLimitError, PermissionError_, AbortedError, ContentFilterError, PromptTooLongError, EndpointUnreachableError):
            # EndpointUnreachableError: the host is down — retrying it on
            # the SAME slot just burns another connect timeout. Escape to
            # the dispatch layer, which cools this slot and fails over.
            raise
        except ModelLimitError as e:
            body['max_tokens'] = e.detected_limit
            _limit_learned = {
                'model': e.model,
                'old_limit': e.requested_limit,
                'new_limit': e.detected_limit,
            }
            logger.warning('%s ⚙️ Auto-learned max_tokens for %s: %d → %d, retrying…',
                          log_prefix, e.model, e.requested_limit, e.detected_limit)
            continue
        except _RETRYABLE as e:
            last_err = e
            if attempt < MAX_STREAM_RETRIES:
                if abort_check and abort_check():
                    logger.debug('%s ✋ Abort detected before retry sleep, stopping.', log_prefix)
                    raise AbortedError('User aborted before retry')
                wait = retry_wait(attempt)
                logger.warning('%s ⚠ Transient error (attempt %d): '
                      '%s: %s — retrying in %.1fs …', log_prefix, attempt + 1, type(e).__name__, e, wait, exc_info=True)
                abortable_sleep(wait, abort_check)
            else:
                logger.error('%s ✖ All %d attempts failed.', log_prefix, 1 + MAX_STREAM_RETRIES, exc_info=True)
                raise
    raise last_err


def _stream_chat_once(body, *, on_thinking=None, on_content=None,
                      on_tool_call_ready=None,
                      abort_check=None, log_prefix='', attempt=0,
                      api_key=None, base_url=None, extra_headers=None,
                      api_protocol='openai', oauth=''):
    """Single attempt at a streaming chat completion (sync transport)."""
    plan = prepare_request(
        body, attempt=attempt, log_prefix=log_prefix,
        api_key=api_key, base_url=base_url, extra_headers=extra_headers,
        api_protocol=api_protocol, oauth=oauth)

    try:
        resp = requests.post(plan.url, headers=plan.hdrs, json=plan.body,
                             stream=True, timeout=(CONNECT_TIMEOUT, 300),
                             proxies=proxies_for(plan.url))
    except requests.exceptions.ConnectionError as e:
        # Connect-phase failure (ConnectTimeout / connection refused /
        # SYN dropped) = the endpoint is down. Convert to
        # EndpointUnreachableError so it escapes the same-key retry loop
        # and the dispatch layer fails over to a healthy slot instead of
        # burning CONNECT_TIMEOUT × MAX_STREAM_RETRIES on a dead host.
        logger.warning('%s ✖ Endpoint unreachable (connect phase) %s: %s',
                       log_prefix, plan.url, e)
        raise EndpointUnreachableError(
            'endpoint unreachable: %s' % e, base_url=plan.url) from e

    try:
        resp_trace = resp.headers.get('M-TraceId', '')
        if resp_trace and resp_trace != plan.trace_id:
            logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

        if resp.status_code != 200:
            classify_status_error(resp.status_code, resp.text, body=plan.body,
                                  log_prefix=log_prefix, raw_dumper=plan.raw_dumper)

        resp.encoding = 'utf-8'

        acc = SSEAccumulator(
            plan.body, plan.trace_id, plan.raw_dumper, plan.codex_translator,
            plan.t0, url=plan.url, log_prefix=log_prefix,
            on_thinking=on_thinking, on_content=on_content,
            on_tool_call_ready=on_tool_call_ready,
            anthropic_translator=plan.anthropic_translator)

        for line in resp.iter_lines(decode_unicode=True):
            if abort_check and abort_check():
                acc.mark_aborted()
                break
            if acc.feed_line(line):
                break

        acc.fire_final_tool_callback()
        return acc.finalize(resp_trace=resp_trace)
    finally:
        try:
            if plan.raw_dumper.enabled and plan.raw_dumper._fh is not None:
                plan.raw_dumper.finish(error=True)
        except Exception as e:
            logger.debug('%s RawSSEDumper.finish(error=True) failed: %s', log_prefix, e)
        resp.close()
