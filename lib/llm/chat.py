# HOT_PATH
"""Non-streaming chat completion.

Public API:
  - chat(messages, model=None, ...) → (content_text, usage_dict)
"""

import json
import re
import time
import uuid


import lib as _lib
from lib.llm._transport import (
    MAX_STREAM_RETRIES,
    chat_url,
    headers,
    retry_wait,
)
from lib.llm.body import build_body
from lib.llm.cache import add_cache_breakpoints
from lib.llm_errors import (
    ContentFilterError,
    InvalidImageError,
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    StreamOnlyError,
    _RETRYABLE,
    _classify_http_error,
)
from lib.log import get_logger
from lib.model_info import (
    _learn_model_limit,
    _parse_token_limit_from_error,
    is_claude,
)
from lib.http_client import http_post

logger = get_logger(__name__)


def chat(messages, model=None, *, max_tokens=4096, temperature=0,
         thinking_enabled=False, preset='low', effort=None, extra=None,
         timeout=120, log_prefix='', api_key=None, base_url=None,
         extra_headers=None, max_retries=None, _limit_retry=False,
         thinking_format='', provider_id='', api_protocol='openai'):
    """Non-streaming chat completion.

    Args:
        api_key:      optional API key override (from dispatch slot).
        base_url:     optional base URL override.
        extra_headers: optional dict of additional headers.
        max_retries:  override retry count (default: MAX_STREAM_RETRIES).

    Returns:
        (content_text: str, usage_dict: dict)

    Raises:
        RateLimitError, PermissionError_, ContentFilterError,
        RetryableAPIError, PromptTooLongError, Exception
    """
    model = model or _lib.LLM_MODEL
    _anthropic = (api_protocol == 'anthropic')
    if _anthropic:
        from lib.llm.anthropic_outbound import anthropic_messages_url
        url = anthropic_messages_url(base_url)
    else:
        url = f'{base_url.rstrip("/")}/chat/completions' if base_url else chat_url()

    body = build_body(
        model, messages,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking_enabled=thinking_enabled,
        preset=effort or preset,
        stream=False,
        extra=extra,
        thinking_format=thinking_format,
        provider_id=provider_id,
    )

    # Cache breakpoints + extended-TTL beta header
    _task_id_for_latch = body.get('_task_id', '')
    add_cache_breakpoints(body, log_prefix)
    body.pop('_task_id', None)

    if is_claude(body.get('model', '')):
        if _task_id_for_latch:
            from lib.tasks_pkg.cache_tracking import latch_extended_ttl
            _use_ext_ttl = latch_extended_ttl(_task_id_for_latch)
        else:
            _use_ext_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
        if _use_ext_ttl:
            if extra_headers is None:
                extra_headers = {}
            else:
                extra_headers = dict(extra_headers)
            _existing_beta = extra_headers.get('anthropic-beta', '')
            _ttl_beta = 'extended-cache-ttl-2025-04-11'
            if _ttl_beta not in _existing_beta:
                if _existing_beta:
                    extra_headers['anthropic-beta'] = f'{_existing_beta},{_ttl_beta}'
                else:
                    extra_headers['anthropic-beta'] = _ttl_beta

    if _anthropic:
        from lib.llm.anthropic_outbound import openai_body_to_anthropic
        body = openai_body_to_anthropic(body)

    if log_prefix:
        logger.debug('%s POST %s model=%s msgs=%d', log_prefix, url, model, len(messages))

    retries = MAX_STREAM_RETRIES if max_retries is None else max_retries
    resp = None
    resp_trace = ''
    trace_id = ''
    for attempt in range(1 + retries):
        try:
            trace_id = uuid.uuid4().hex
            if _anthropic:
                from lib.llm.anthropic_outbound import anthropic_headers
                hdrs = anthropic_headers(api_key, extra_headers)
            else:
                hdrs = headers()
                if api_key:
                    hdrs['Authorization'] = f'Bearer {api_key}'
                if extra_headers:
                    hdrs.update(extra_headers)
            hdrs['M-TraceId'] = trace_id
            if log_prefix:
                logger.debug('%s M-TraceId=%s', log_prefix, trace_id)
            resp = http_post(url, headers=hdrs, json=body,
                                 timeout=(30, timeout))
            resp_trace = resp.headers.get('M-TraceId', '')
            if resp_trace and resp_trace != trace_id:
                logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)
            if resp.status_code != 200:
                err_msg = f'API HTTP {resp.status_code}: {resp.text[:500]}'
                if resp.status_code == 400 and not _limit_retry:
                    _detected_limit = _parse_token_limit_from_error(err_msg, model)
                    if _detected_limit:
                        _learn_model_limit(model, _detected_limit)
                        logger.warning('%s ⚙️ max_tokens %d exceeds %s limit %d — '
                                      'auto-learned and retrying with corrected value',
                                      log_prefix, max_tokens, model, _detected_limit)
                        content_r, usage_r = chat(
                            messages, model, max_tokens=_detected_limit,
                            temperature=temperature,
                            thinking_enabled=thinking_enabled,
                            preset=preset, effort=effort, extra=extra,
                            timeout=timeout, log_prefix=log_prefix,
                            api_key=api_key, base_url=base_url,
                            extra_headers=extra_headers,
                            max_retries=max_retries, _limit_retry=True,
                            thinking_format=thinking_format,
                            provider_id=provider_id, api_protocol=api_protocol)
                        usage_r['_model_limit_learned'] = {
                            'model': model,
                            'old_limit': max_tokens,
                            'new_limit': _detected_limit,
                        }
                        return content_r, usage_r
                _classify_http_error(resp.status_code, err_msg, model,
                                     log_prefix, max_tokens=max_tokens)
            break
        except (RateLimitError, PermissionError_, ContentFilterError, PromptTooLongError, StreamOnlyError, InvalidImageError):
            raise
        except _RETRYABLE as e:
            if attempt < retries:
                wait = retry_wait(attempt)
                logger.warning('%s ⚠ Attempt %d/%d failed '
                      '(%s), retrying in %.1fs…', log_prefix, attempt + 1, 1 + retries, type(e).__name__, wait, exc_info=True)
                time.sleep(wait)
            else:
                logger.error('%s ✖ All %d attempts failed (non-stream).', log_prefix, 1 + retries, exc_info=True)
                raise

    assert resp is not None, 'BUG: retry loop exited without assigning resp'

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        raise Exception(
            f'API returned invalid JSON (HTTP {resp.status_code}): '
            f'{resp.text[:500]}'
        ) from e
    if _anthropic:
        from lib.llm.anthropic_outbound import anthropic_response_to_openai
        data = anthropic_response_to_openai(data)
    choices = data.get('choices') or []
    if not choices:
        raise Exception(
            f'API returned no choices: {json.dumps(data)[:500]}'
        )
    msg = choices[0].get('message') or {}
    content = msg.get('content', '')
    usage = data.get('usage', {})

    _finish_reason = choices[0].get('finish_reason', '')
    if _finish_reason:
        usage['finish_reason'] = _finish_reason

    # Strip MiniMax-style <think>...</think> tags
    if content and '<think>' in content:
        raw_len = len(content)
        content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
        if '<think>' in content:
            content = content[:content.index('<think>')].strip()
        if len(content) != raw_len:
            logger.debug('[chat] Stripped <think> tags from non-stream response '
                        '(%d → %d chars)', raw_len, len(content))

    _tool_calls = msg.get('tool_calls')
    if _tool_calls:
        usage['_tool_calls'] = _tool_calls

    usage['trace_id'] = trace_id
    if resp_trace and resp_trace != trace_id:
        usage['resp_trace_id'] = resp_trace

    if log_prefix:
        tokens = usage.get('total_tokens', 0)
        logger.debug('%s Done: %d chars, ~%d tokens', log_prefix, len(content), tokens)

    return content, usage
