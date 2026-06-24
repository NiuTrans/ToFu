# HOT_PATH
"""Transport-agnostic core for streaming chat completions.

Both ``lib/llm/stream.py`` (sync, ``requests``) and ``lib/llm/astream.py``
(async, ``httpx``) used to carry a ~480-line copy of the *identical* SSE
chunk-parsing loop: error classification, MiniMax ``<think>`` demux,
tool-call accumulation, premature-close / empty-stop anomaly diagnostics,
and ``usage`` metadata injection. Every fix had to land twice and the two
copies drifted.

This module holds that logic exactly once. The two transport shells keep
only what genuinely differs:

  - opening the stream + iterating lines (``requests`` vs ``httpx``);
  - the retry/backoff loop's sleep call (blocking vs ``await``);
  - mapping ``httpx`` transport exceptions to ``RetryableAPIError``.

Public surface
--------------
  - ``prepare_request(body, *, attempt, log_prefix, api_key, base_url,
    extra_headers) -> RequestPlan`` — the identical pre-flight (cache
    breakpoints, extended-TTL header, Codex translation, header build,
    URL resolution, RawSSEDumper start).
  - ``classify_status_error(status_code, err_text, *, body, log_prefix,
    raw_dumper)`` — shared non-200 handling (delegates to
    ``_classify_http_error``); the caller reads the error body in its own
    transport-native way and passes the text in.
  - ``SSEAccumulator`` — feed it one raw SSE line at a time via
    ``feed_line(line)`` (returns ``True`` when the stream should stop),
    then call ``finalize(...)`` for the ``(msg, finish_reason, usage)``
    tuple. ``feed_line`` raises the same exceptions the inline loop did
    (``ModelLimitError`` / ``RateLimitError`` / ``PromptTooLongError`` /
    ``RetryableAPIError`` / ``Exception('SSE error: …')``), so the
    transport shell's retry wrapper handles them unchanged.

This is a pure code-motion refactor: no retry cap, timeout, backoff, or
cache constant is changed here. The ``usage`` anomaly fields
(``_chunks_received``, ``_stream_anomaly``, ``_missing_done``,
``_missing_finish_reason``, ``_empty_stop``, ``stream_elapsed_ms``,
``trace_id``, ``resp_trace_id``) are emitted byte-for-byte as before
because ``lib/tasks_pkg/stream_handler.py::analyse_stream_result`` keys
its retry buckets off them.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import lib as _lib
from lib.llm._transport import chat_url, headers
from lib.llm.cache import add_cache_breakpoints
from lib.llm.diagnostics import RawSSEDumper
from lib.llm_errors import (
    ModelLimitError,
    PromptTooLongError,
    RateLimitError,
    RetryableAPIError,
    _GATEWAY_THROTTLE_STATUS,
    _classify_http_error,
    _is_prompt_too_long,
)
from lib.log import get_logger
from lib.model_info import (
    _learn_model_limit,
    _parse_token_limit_from_error,
    is_claude,
    is_minimax,
)

logger = get_logger(__name__)

_INTERNAL_TOOL_PREFIXES = ('antml:', 'anthropic.', '__')
_MAX_CONSECUTIVE_PARSE_ERRORS = 10


@dataclass
class RequestPlan:
    """Everything a transport shell needs to open the stream."""
    url: str
    hdrs: dict
    body: dict
    trace_id: str
    raw_dumper: RawSSEDumper
    codex_translator: Any
    t0: float
    anthropic_translator: Any = None


def prepare_request(body, *, attempt=0, log_prefix='', api_key=None,
                    base_url=None, extra_headers=None,
                    api_protocol='openai', oauth='') -> RequestPlan:
    """Identical pre-flight for both transports.

    Mutates ``body`` in place (cache breakpoints, ``_task_id`` pop, Codex
    translation) exactly as the inline code did, then returns the plan.
    """
    _task_id_for_latch = body.get('_task_id', '')
    add_cache_breakpoints(body, log_prefix)
    body.pop('_task_id', None)

    # Auto-inject extended cache TTL beta header for Claude
    if is_claude(body.get('model', '')):
        if _task_id_for_latch:
            from lib.tasks_pkg.cache_tracking import latch_extended_ttl
            _use_ext_ttl = latch_extended_ttl(_task_id_for_latch)
        else:
            _use_ext_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
        if _use_ext_ttl:
            if extra_headers is None:
                extra_headers = {}
            _existing_beta = extra_headers.get('anthropic-beta', '')
            _ttl_beta = 'extended-cache-ttl-2025-04-11'
            if _ttl_beta not in _existing_beta:
                if _existing_beta:
                    extra_headers['anthropic-beta'] = f'{_existing_beta},{_ttl_beta}'
                else:
                    extra_headers['anthropic-beta'] = _ttl_beta

    # Subscription-OAuth slot: swap in a live token + client-identity headers,
    # and (for Claude) prepend the mandatory identity system block — all BEFORE
    # the body translation below reads messages / builds headers.
    if oauth:
        from lib.oauth.outbound import resolve_oauth_request
        api_key, extra_headers, body = resolve_oauth_request(oauth, body, extra_headers)

    # Codex OAuth translation
    codex_translator = None
    anthropic_translator = None
    if base_url and 'codex' in base_url and 'chatgpt.com' in base_url:
        from lib.oauth.codex import codex_translate_request, CodexSSETranslator
        body = codex_translate_request(body)
        codex_translator = CodexSSETranslator(model=body.get('model', ''))
        url = f'{base_url.rstrip("/")}/responses'
        logger.debug('%s [Codex] Translated request for Responses API', log_prefix)
    elif api_protocol == 'anthropic':
        from lib.llm.anthropic_outbound import (
            AnthropicSSETranslator, anthropic_messages_url,
            openai_body_to_anthropic,
        )
        _model_name = body.get('model', '')
        body = openai_body_to_anthropic(body)
        anthropic_translator = AnthropicSSETranslator(model=_model_name)
        url = anthropic_messages_url(base_url)
        if oauth == 'claude':
            from lib.oauth.outbound import claude_oauth_url
            url = claude_oauth_url(url)
        logger.debug('%s [Anthropic] Translated request for Messages API', log_prefix)
    else:
        url = f'{base_url.rstrip("/")}/chat/completions' if base_url else chat_url()

    attempt_tag = f' (attempt {attempt+1})' if attempt > 0 else ''
    if log_prefix:
        logger.debug('%s%s POST %s msgs=%d tools=%s', log_prefix, attempt_tag, url,
                     len(body.get('messages', [])), 'yes' if body.get('tools') else 'no')

    trace_id = uuid.uuid4().hex
    if anthropic_translator is not None:
        from lib.llm.anthropic_outbound import anthropic_headers
        hdrs = anthropic_headers(api_key, extra_headers)
        if oauth == 'claude':
            # Subscription tokens are rejected on Authorization: Bearer
            # (401 since 2026); the token must ride x-api-key only.
            hdrs.pop('Authorization', None)
    else:
        hdrs = headers()
        if api_key:
            hdrs['Authorization'] = f'Bearer {api_key}'
        if extra_headers:
            hdrs.update(extra_headers)
    hdrs['M-TraceId'] = trace_id

    if log_prefix:
        logger.debug('%s M-TraceId=%s', log_prefix, trace_id)

    t0 = time.time()
    raw_dumper = RawSSEDumper(body.get('model', ''), trace_id, body)
    raw_dumper.start()

    return RequestPlan(url=url, hdrs=hdrs, body=body, trace_id=trace_id,
                       raw_dumper=raw_dumper, codex_translator=codex_translator,
                       t0=t0, anthropic_translator=anthropic_translator)


def classify_status_error(status_code, err_text, *, body, log_prefix, raw_dumper):
    """Shared non-200 handling. Caller reads the body text per-transport.

    Always raises (via ``_classify_http_error``) — never returns normally
    when ``status_code != 200``.
    """
    err_msg = f'API HTTP {status_code}: {err_text[:800]}'
    if raw_dumper.enabled:
        raw_dumper.line(f'[HTTP-{status_code}] {err_text[:2000]}')
    _classify_http_error(status_code, err_msg, body.get('model', ''),
                         log_prefix, max_tokens=body.get('max_tokens', 0))


class SSEAccumulator:
    """Transport-agnostic SSE chunk parser + assistant-message builder.

    Usage::

        acc = SSEAccumulator(body, trace_id, raw_dumper, codex_translator,
                             t0, log_prefix=..., on_thinking=..., ...)
        for raw_line in transport_lines():
            if abort_check and abort_check():
                acc.mark_aborted(); break
            if acc.feed_line(raw_line):   # True → saw [DONE]
                break
        acc.fire_final_tool_callback()
        msg, finish_reason, usage = acc.finalize(resp_trace=...)
    """

    def __init__(self, body, trace_id, raw_dumper, codex_translator, t0, *,
                 url='', log_prefix='', on_thinking=None, on_content=None,
                 on_tool_call_ready=None, anthropic_translator=None):
        self.body = body
        self.trace_id = trace_id
        self.raw_dumper = raw_dumper
        self.codex_translator = codex_translator
        self.anthropic_translator = anthropic_translator
        self.t0 = t0
        self.url = url
        self.log_prefix = log_prefix
        self.on_thinking = on_thinking
        self.on_content = on_content
        self.on_tool_call_ready = on_tool_call_ready

        self.content = ''
        self.thinking_text = ''
        self.thinking_signature = ''
        self.tool_calls_acc: dict = {}
        self.finish_reason = 'stop'
        self.usage: Optional[dict] = None
        self.saw_done = False
        self.saw_finish_reason = False
        self.chunk_count = 0
        self.aborted_by_client = False

        self._mm_mode = is_minimax(body.get('model', ''))
        self._mm_in_think = False
        self._mm_buf = ''
        self._consecutive_parse_errors = 0

    def mark_aborted(self):
        self.aborted_by_client = True
        logger.debug('%s Stream aborted by client after %d chunks',
                     self.log_prefix, self.chunk_count)

    def feed_line(self, line) -> bool:
        """Process one raw SSE line. Returns True when the stream should stop.

        Mirrors the inline per-line handling exactly: dumps the raw line,
        skips non-``data:`` lines, detects ``[DONE]``, and dispatches the
        JSON chunk. Raises the same typed exceptions on SSE error objects.
        """
        self.raw_dumper.line(line if line is not None else '')
        if not line or not line.startswith('data:'):
            return False
        data_str = line[5:].strip()
        if data_str == '[DONE]':
            self.saw_done = True
            return True
        if not data_str:
            return False
        self.chunk_count += 1

        # Codex SSE translation
        if self.codex_translator is not None:
            return self._feed_codex(data_str)

        # Anthropic SSE translation → OpenAI chunks
        if self.anthropic_translator is not None:
            return self._feed_anthropic(data_str)

        try:
            chunk = json.loads(data_str)
        except Exception as e:
            self._consecutive_parse_errors += 1
            logger.warning('%s ⚠ SSE chunk JSON parse error (chunk #%d, consecutive=%d) '
                           'model=%s trace=%s: %s — %s',
                           self.log_prefix, self.chunk_count, self._consecutive_parse_errors,
                           self.body.get('model', '?'), self.trace_id, data_str[:200], e,
                           exc_info=True)
            if self._consecutive_parse_errors >= _MAX_CONSECUTIVE_PARSE_ERRORS:
                self.raw_dumper.dump_anomaly(
                    'parse_error',
                    consecutive_errors=self._consecutive_parse_errors,
                    chunk_count=self.chunk_count,
                    last_data_preview=data_str[:200],
                    model=self.body.get('model', '?'),
                )
                raise RetryableAPIError(
                    f'{self._consecutive_parse_errors} consecutive SSE parse errors '
                    f'— stream appears corrupt') from e
            return False

        self._consecutive_parse_errors = 0
        self._process_openai_chunk(chunk)
        return False

    def _process_openai_chunk(self, chunk):
        """Accumulate one OpenAI-shaped chat.completion chunk dict."""
        if 'error' in chunk:
            self._handle_sse_error(chunk['error'])

        if chunk.get('usage'):
            self.usage = chunk['usage']

        choices = chunk.get('choices', [])
        if not choices:
            return

        delta = choices[0].get('delta', {})
        fr = choices[0].get('finish_reason')
        if fr:
            self.finish_reason = fr
            self.saw_finish_reason = True
        if choices[0].get('usage'):
            self.usage = choices[0]['usage']

        self._handle_delta(delta)

    def _feed_anthropic(self, data_str) -> bool:
        """Translate one Anthropic SSE payload into OpenAI chunks + accumulate.

        Returns True when the translator emits the ``[DONE]`` sentinel.
        """
        for chunk in self.anthropic_translator.translate(data_str):
            if chunk == '[DONE]':
                self.saw_done = True
                return True
            self._process_openai_chunk(chunk)
        return False

    def _feed_codex(self, data_str) -> bool:
        """Translate a Codex Responses-API SSE payload and accumulate.

        Returns True when ``[DONE]`` was seen inside the translation.
        """
        translated = self.codex_translator.translate(data_str)
        for t_str in translated:
            if t_str == '[DONE]':
                self.saw_done = True
                break
            try:
                t_chunk = json.loads(t_str)
            except Exception as e:
                logger.debug('[LLM] Codex SSE chunk parse failed: %s', e)
                continue
            choices = t_chunk.get('choices', [])
            if choices:
                delta = choices[0].get('delta', {})
                fr = choices[0].get('finish_reason')
                if fr:
                    self.finish_reason = fr
                    self.saw_finish_reason = True
                _c = delta.get('content', '')
                if _c and self.on_content:
                    self.content += _c
                    self.on_content(_c)
                _t = delta.get('reasoning_content', '')
                if _t and self.on_thinking:
                    self.thinking_text += _t
                    self.on_thinking(_t)
                for tc in (delta.get('tool_calls') or []):
                    idx = tc.get('index', 0)
                    if idx not in self.tool_calls_acc:
                        self.tool_calls_acc[idx] = {
                            'id': tc.get('id', ''),
                            'type': 'function',
                            'function': {'name': '', 'arguments': ''},
                        }
                    if tc.get('id'):
                        self.tool_calls_acc[idx]['id'] = tc['id']
                    fn = tc.get('function', {})
                    if fn.get('name'):
                        self.tool_calls_acc[idx]['function']['name'] = fn['name']
                    if fn.get('arguments'):
                        self.tool_calls_acc[idx]['function']['arguments'] += fn['arguments']
            if t_chunk.get('usage'):
                self.usage = t_chunk['usage']
        return self.saw_done

    def _handle_sse_error(self, eo):
        """Classify an SSE-embedded error object; always raises."""
        err_text = eo.get('message', '') if isinstance(eo, dict) else str(eo)
        _err_lower = err_text.lower()
        _model_id = self.body.get('model', '')
        _detected_limit = _parse_token_limit_from_error(err_text, _model_id)
        if _detected_limit:
            _learn_model_limit(_model_id, _detected_limit)
            raise ModelLimitError(
                f'SSE error (token limit): {err_text}',
                _model_id, _detected_limit,
                self.body.get('max_tokens', 0))
        if _is_prompt_too_long(err_text):
            logger.warning('%s Prompt too long detected in SSE error: %s',
                           self.log_prefix, err_text[:300])
            raise PromptTooLongError(f'SSE error: {err_text}')
        _sse_err_type = eo.get('type', '') if isinstance(eo, dict) else ''
        _sse_http_code = str(eo.get('http_code', '')) if isinstance(eo, dict) else ''
        # ★ Some upstream gateways (AWS Bedrock, GCP Vertex) embed the HTTP
        #   status inside the message text instead of a structured field,
        #   e.g. "(Service: BedrockRuntime, Status Code: 429, …)".
        if not _sse_http_code:
            _m = re.search(r'status code[:\s]+(\d{3})', _err_lower)
            if _m:
                _sse_http_code = _m.group(1)
        _sse_quota_patterns = [
            'too many tokens', 'too many requests',
            'quota exceeded', 'rate exceeded',
            'tokens per day', 'tokens per minute',
            'requests per day', 'requests per minute',
            'throttling', 'throttled',
        ]
        _sse_retryable_patterns = [
            '负载较高', 'server overload', 'service overload',
            'capacity', 'try again later', '稍后重试',
            'temporarily unavailable',
        ]
        _sse_non_retryable_patterns = [
            'not support model', 'invalid api key',
            'unauthorized', 'forbidden', 'not found',
            'plan not support', 'permission denied',
        ]
        _is_sse_non_retryable = any(p in _err_lower for p in _sse_non_retryable_patterns)
        _is_sse_quota = (
            not _is_sse_non_retryable
            and (
                _sse_http_code == '429'
                or any(p in _err_lower for p in _sse_quota_patterns)
            )
        )
        _is_sse_retryable = (
            not _is_sse_non_retryable
            and not _is_sse_quota
            and (
                _sse_err_type == 'server_error'
                or _sse_http_code.startswith('5')
                or any(p in _err_lower for p in _sse_retryable_patterns)
            )
        )
        if _is_sse_quota:
            logger.warning('%s SSE rate-limit/quota detected — escalating to '
                           'dispatch layer: %s', self.log_prefix, err_text[:300])
            raise RateLimitError(
                f'SSE error: {err_text}',
                reason=f'HTTP 429: {err_text[:180]}')
        if _is_sse_retryable:
            _sse_status = int(_sse_http_code) if _sse_http_code.isdigit() else 500
            if _sse_status in _GATEWAY_THROTTLE_STATUS:
                logger.warning('%s SSE gateway throttle (HTTP %d) — escalating to '
                               'dispatch layer: %s', self.log_prefix, _sse_status,
                               err_text[:300])
                raise RateLimitError(
                    f'SSE error: {err_text}',
                    reason=f'HTTP {_sse_status}: {err_text[:180]}')
            logger.warning('%s SSE server error (retryable): %s',
                           self.log_prefix, err_text[:300])
            raise RetryableAPIError(
                f'SSE error: {err_text}',
                status_code=_sse_status)
        if not err_text:
            err_text = (f'<empty error body> sse_type={_sse_err_type or "?"} '
                        f'http_code={_sse_http_code or "?"} '
                        f'model={self.body.get("model", "?")} '
                        f'trace={self.trace_id}')
        raise Exception(f'SSE error: {err_text}')

    def _handle_delta(self, delta):
        """Accumulate thinking / content / tool-call deltas from one chunk."""
        # Thinking / reasoning delta
        td = (delta.get('thinking')
              or delta.get('reasoning_content')
              or (delta.get('content', '')
                  if delta.get('role') == 'thinking' else ''))
        # OpenRouter-style ``reasoning_details`` carry both the thinking text
        # and the opaque Claude signature, in separate chunks:
        #   [{"type":"thinking","thinking":"…"}]    ← text delta
        #   [{"type":"thinking","signature":"…"}]   ← signature (once per turn)
        # The Meituan/sankuai OpenAI-compat gateway uses exactly this shape
        # for Claude models, so harvest both keys here.
        rd_parts = delta.get('reasoning_details')
        if isinstance(rd_parts, list):
            if not td:
                td = ''.join(
                    (d.get('thinking') or d.get('text') or '')
                    for d in rd_parts if isinstance(d, dict))
            for d in rd_parts:
                if isinstance(d, dict) and d.get('signature'):
                    self.thinking_signature += d['signature']
        if td:
            self.thinking_text += td
            if self.on_thinking:
                self.on_thinking(td)

        # Opaque thinking-block signature (Anthropic Messages API path).
        # Surfaced by the AnthropicSSETranslator as a synthetic delta field;
        # needed to replay the thinking block on a later tool-use turn.
        _tsig = delta.get('thinking_signature')
        if _tsig:
            self.thinking_signature += _tsig

        # Content delta
        if 'content' in delta and delta.get('role') != 'thinking':
            cd = delta['content'] or ''
            if cd:
                if self._mm_mode:
                    self._feed_minimax(cd)
                else:
                    self.content += cd
                    if self.on_content:
                        self.on_content(cd)

        # Tool call deltas
        _tc_list = delta.get('tool_calls') or []
        if _tc_list:
            for tc in _tc_list:
                idx = tc.get('index', 0)
                if idx not in self.tool_calls_acc:
                    if self.on_tool_call_ready and idx > 0 and (idx - 1) in self.tool_calls_acc:
                        _prev = self.tool_calls_acc[idx - 1]
                        try:
                            self.on_tool_call_ready(_prev)
                        except Exception as _tcr_err:
                            logger.debug('%s on_tool_call_ready callback error: %s',
                                         self.log_prefix, _tcr_err)
                    self.tool_calls_acc[idx] = {
                        'id': '', 'type': 'function',
                        'function': {'name': '', 'arguments': ''},
                    }
                if tc.get('id'):
                    self.tool_calls_acc[idx]['id'] = tc['id']
                if tc.get('extra_content'):
                    self.tool_calls_acc[idx]['extra_content'] = tc['extra_content']
                fn = tc.get('function', {})
                if fn.get('name'):
                    self.tool_calls_acc[idx]['function']['name'] += fn['name']
                if fn.get('arguments') is not None:
                    self.tool_calls_acc[idx]['function']['arguments'] += fn.get('arguments', '')

    def _feed_minimax(self, cd):
        """MiniMax inline ``<think>…</think>`` demux into thinking vs content."""
        self._mm_buf += cd
        while self._mm_buf:
            if self._mm_in_think:
                end_idx = self._mm_buf.find('</think>')
                if end_idx == -1:
                    self.thinking_text += self._mm_buf
                    if self.on_thinking:
                        self.on_thinking(self._mm_buf)
                    self._mm_buf = ''
                else:
                    think_part = self._mm_buf[:end_idx]
                    if think_part:
                        self.thinking_text += think_part
                        if self.on_thinking:
                            self.on_thinking(think_part)
                    self._mm_buf = self._mm_buf[end_idx + len('</think>'):]
                    self._mm_in_think = False
            else:
                start_idx = self._mm_buf.find('<think>')
                if start_idx == -1:
                    if len(self._mm_buf) > 7 and '<' in self._mm_buf[-7:]:
                        safe = self._mm_buf[:self._mm_buf.rfind('<', max(0, len(self._mm_buf) - 7))]
                        if safe:
                            self.content += safe
                            if self.on_content:
                                self.on_content(safe)
                        self._mm_buf = self._mm_buf[len(safe):]
                    else:
                        self.content += self._mm_buf
                        if self.on_content:
                            self.on_content(self._mm_buf)
                        self._mm_buf = ''
                else:
                    before = self._mm_buf[:start_idx]
                    if before:
                        self.content += before
                        if self.on_content:
                            self.on_content(before)
                    self._mm_buf = self._mm_buf[start_idx + len('<think>'):]
                    self._mm_in_think = True

    def fire_final_tool_callback(self):
        """Fire on_tool_call_ready for the LAST accumulated tool call."""
        if self.on_tool_call_ready and self.tool_calls_acc:
            _last_idx = max(self.tool_calls_acc.keys())
            _last_tc = self.tool_calls_acc[_last_idx]
            if _last_tc['function']['name']:
                try:
                    self.on_tool_call_ready(_last_tc)
                except Exception as _tcr_err:
                    logger.debug('%s on_tool_call_ready callback error (final): %s',
                                 self.log_prefix, _tcr_err)

    def finalize(self, *, resp_trace=''):
        """Flush buffers, build the assistant msg, emit diagnostics + usage.

        Returns ``(msg, finish_reason, usage)`` — identical shape and
        anomaly fields to the former inline implementation.
        """
        # Flush MiniMax buffer
        if self._mm_mode and self._mm_buf:
            if self._mm_in_think:
                self.thinking_text += self._mm_buf
                if self.on_thinking:
                    self.on_thinking(self._mm_buf)
            else:
                self.content += self._mm_buf
                if self.on_content:
                    self.on_content(self._mm_buf)
            self._mm_buf = ''

        # MiniMax: normalize reasoning_tokens into usage
        if self._mm_mode and self.usage and self.thinking_text:
            ctd = self.usage.get('completion_tokens_details', {})
            rt = ctd.get('reasoning_tokens', 0)
            if rt > 0 and 'reasoning_tokens' not in self.usage:
                self.usage['reasoning_tokens'] = rt

        # Filter out spurious tool calls
        if self.tool_calls_acc:
            _filtered = {}
            _names_with_args = {
                tc['function']['name']
                for tc in self.tool_calls_acc.values()
                if (tc['function'].get('arguments', '') or '').strip()
            }
            for idx, tc_entry in self.tool_calls_acc.items():
                fn_name = tc_entry['function']['name']
                fn_args_str = tc_entry['function'].get('arguments', '')
                if any(fn_name.startswith(p) for p in _INTERNAL_TOOL_PREFIXES):
                    logger.debug('%s Filtering spurious internal tool call: %s',
                                 self.log_prefix, fn_name)
                    continue
                if not fn_args_str.strip() and fn_name in _names_with_args:
                    logger.warning(
                        '%s Filtering phantom tool call: %s (tc_id=%s) has '
                        'empty arguments — duplicate of another %s call with real args',
                        self.log_prefix, fn_name, tc_entry.get('id', '?')[:12], fn_name,
                    )
                    continue
                _filtered[idx] = tc_entry
            self.tool_calls_acc = _filtered

        content = self.content
        thinking_text = self.thinking_text
        tool_calls_acc = self.tool_calls_acc
        finish_reason = self.finish_reason
        usage = self.usage

        # Build assistant message
        msg = {'role': 'assistant'}
        if thinking_text:
            msg['reasoning_content'] = thinking_text
        if self.thinking_signature:
            msg['thinking_signature'] = self.thinking_signature
        if tool_calls_acc:
            msg['tool_calls'] = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
            if content:
                msg['content'] = content
        else:
            msg['content'] = content

        # Log cache info
        cache_info = ''
        if usage:
            cw = usage.get('cache_write_tokens',
                           usage.get('cache_creation_input_tokens', 0))
            cr = usage.get('cache_read_tokens',
                           usage.get('cache_read_input_tokens', 0))
            if cw or cr:
                cache_info = f' cache_w={cw} cache_r={cr}'
                if cr > 0:
                    inp = usage.get('prompt_tokens', usage.get('input_tokens', 0))
                    cache_info += f' (saved ~{round(cr / max(inp, 1) * 100)}%)'

        if self.log_prefix:
            logger.debug('%s Done: finish=%s content=%d think=%d%s', self.log_prefix,
                         finish_reason, len(content), len(thinking_text), cache_info)

        _stream_elapsed_s = time.time() - self.t0
        aborted = self.aborted_by_client
        chunk_count = self.chunk_count

        # Diagnostics: detect premature stream close
        if not aborted and not self.saw_done:
            logger.warning(
                '%s ⚠ PREMATURE STREAM CLOSE: Server never sent [DONE] marker. '
                'M-TraceId=%s resp_trace=%s elapsed=%.1fs chunks_received=%d '
                'saw_finish_reason=%s finish_reason=%s content_len=%d thinking_len=%d '
                'tool_calls=%d model=%s url=%s',
                self.log_prefix, self.trace_id, resp_trace or 'none',
                _stream_elapsed_s, chunk_count,
                self.saw_finish_reason, finish_reason,
                len(content), len(thinking_text),
                len(tool_calls_acc), self.body.get('model', '?'), self.url)
            self.raw_dumper.dump_anomaly(
                'missing_done',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                saw_finish_reason=self.saw_finish_reason,
                finish_reason=finish_reason,
                content_len=len(content),
                thinking_len=len(thinking_text),
                tool_calls=len(tool_calls_acc),
                resp_trace=resp_trace or 'none',
            )
        elif not aborted and not self.saw_finish_reason and chunk_count > 0:
            logger.warning(
                '%s ⚠ MISSING FINISH_REASON: [DONE] received but no finish_reason chunk. '
                'M-TraceId=%s elapsed=%.1fs Using default=%s chunks=%d '
                'content_len=%d model=%s',
                self.log_prefix, self.trace_id, _stream_elapsed_s,
                finish_reason, chunk_count, len(content), self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'missing_finish_reason',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                content_len=len(content),
                thinking_len=len(thinking_text),
                tool_calls=len(tool_calls_acc),
            )

        # Diagnostics: detect empty responses
        if (not aborted and finish_reason == 'stop'
                and not content and not tool_calls_acc and chunk_count > 0):
            logger.warning(
                '%s ⚠ EMPTY STOP RESPONSE: finish=stop but no content and no tool_calls. '
                'M-TraceId=%s elapsed=%.1fs chunks=%d thinking_len=%d model=%s',
                self.log_prefix, self.trace_id, _stream_elapsed_s,
                chunk_count, len(thinking_text), self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'empty_stop',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                thinking_len=len(thinking_text),
                finish_reason=finish_reason,
                resp_trace=resp_trace or 'none',
            )

        # Inject metadata into usage
        if usage is None:
            usage = {}
        usage['trace_id'] = self.trace_id
        if resp_trace and resp_trace != self.trace_id:
            usage['resp_trace_id'] = resp_trace
        usage['stream_elapsed_ms'] = round(_stream_elapsed_s * 1000)
        usage['_chunks_received'] = chunk_count

        # Stream anomaly flags
        _has_anomaly = False
        if not aborted and not self.saw_done:
            usage['_missing_done'] = True
            if not self.saw_finish_reason:
                _has_anomaly = True
        if not aborted and not self.saw_finish_reason and chunk_count > 0:
            usage['_missing_finish_reason'] = True
            _has_anomaly = True
        if (not aborted and finish_reason == 'stop'
                and not content and not tool_calls_acc and chunk_count > 0):
            usage['_empty_stop'] = True
            _has_anomaly = True
        if _has_anomaly:
            usage['_stream_anomaly'] = True

        self.raw_dumper.finish(
            finish_reason=finish_reason,
            content_len=len(content),
            thinking_len=len(thinking_text),
            tool_calls=len(tool_calls_acc),
            saw_done=self.saw_done,
            saw_finish_reason=self.saw_finish_reason,
        )

        return msg, finish_reason, usage
