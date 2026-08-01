"""lib/llm/responses_outbound/_sse.py — Responses API SSE → OpenAI chunks.

``ResponsesSSETranslator`` is a stateful per-request translator plugged into
``SSEAccumulator`` exactly like ``AnthropicSSETranslator`` (same
``translate(data_str) -> list`` contract — the accumulator's shared
``_feed_translated`` path consumes both).

Extracted from ``lib/oauth/codex.py:CodexSSETranslator`` (2026-07-31, epic
pt_b7a29ea7) with four generalisations the Codex-only original lacked:

  1. **``response.reasoning_text.delta``** — DeepSeek's reasoning channel
     (no summary variant), mapped to ``reasoning_content`` like summaries.
  2. **item_id routing** — parallel function calls interleave their
     ``function_call_arguments.delta`` events; each delta is routed by its
     ``item_id`` to the slot allocated at ``output_item.added`` (the old
     "current index" routing concatenated parallel calls into one).
  3. **terminal failures are errors, not silence** — ``response.failed`` /
     ``response.error`` / ``response.incomplete``(non-token reasons) emit
     an OpenAI-shaped ``{'error': …}`` chunk so the accumulator's shared
     ``_handle_sse_error`` classifier (429 → RateLimitError, 5xx →
     RetryableAPIError, …) decides retry/failover. The old path dropped
     them and the stream ended EMPTY — the '无结果' failure shape.
  4. **usage details** — ``input_tokens_details.cached_tokens`` and
     ``output_tokens_details.reasoning_tokens`` are carried into the
     OpenAI usage spelling (cache-hit accounting depends on them).

Terminal events (``response.completed`` / ``response.incomplete``) emit
the finish chunk + the ``'[DONE]'`` sentinel — a Responses stream has no
``[DONE]`` frame of its own, so the sentinel is synthesised here.
"""

from __future__ import annotations

import json
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['ResponsesSSETranslator']

#: Responses error ``code`` → the HTTP status the shared SSE classifier
#: understands (``_handle_sse_error`` reads ``http_code`` first).
_ERROR_HTTP = {
    'rate_limit_exceeded': '429',
    'insufficient_quota': '429',
    'server_error': '500',
    'overloaded': '529',
}


def _usage_to_openai(usage: dict) -> dict:
    """Responses usage → OpenAI Chat Completions usage spelling."""
    if not isinstance(usage, dict) or not usage:
        return {}
    out = {
        'prompt_tokens': usage.get('input_tokens', 0),
        'completion_tokens': usage.get('output_tokens', 0),
        'total_tokens': usage.get(
            'total_tokens',
            usage.get('input_tokens', 0) + usage.get('output_tokens', 0)),
    }
    itd = usage.get('input_tokens_details')
    if isinstance(itd, dict) and 'cached_tokens' in itd:
        out['prompt_tokens_details'] = {'cached_tokens': itd['cached_tokens']}
    otd = usage.get('output_tokens_details')
    if isinstance(otd, dict) and 'reasoning_tokens' in otd:
        out['completion_tokens_details'] = {
            'reasoning_tokens': otd['reasoning_tokens']}
    return out


class ResponsesSSETranslator:
    """Translate Responses-API SSE events into OpenAI chunk dicts.

    Usage::

        translator = ResponsesSSETranslator(model='deepseek-v4-flash')
        for chunk in translator.translate(raw_data_str):
            ...  # OpenAI-shaped chat.completion.chunk dicts (+ '[DONE]')
    """

    def __init__(self, model: str = ''):
        self.model = model
        # Function-call slots: how many function_call items have been seen
        # (the OpenAI chunk's ``index``), and the item_id → slot map for
        # routing argument deltas of PARALLEL calls.
        self._tc_count = 0
        self._item_slot: dict = {}
        # Per-request {truncated: original} tool-name map, stamped by the
        # caller from the request converter's second return value — the
        # model echoes the TRUNCATED name and the executor's lookup would
        # miss without the restore (mirrors AnthropicSSETranslator's
        # ``tool_name_reverse``).
        self.tool_name_reverse: dict = {}

    # ──────────────────────────────────────────────────────────

    def _chunk(self, delta: dict | None = None,
               finish_reason: str | None = None,
               usage: dict | None = None) -> dict:
        chunk = {
            'id': 'chatcmpl-responses',
            'object': 'chat.completion.chunk',
            'created': int(time.time()),
            'model': self.model,
            'choices': [{'index': 0, 'delta': delta or {},
                         'finish_reason': finish_reason}],
        }
        if usage:
            chunk['usage'] = usage
        return chunk

    def _slot_for(self, event: dict):
        """Resolve which tool-call slot an arguments delta belongs to."""
        item_id = event.get('item_id')
        if item_id:
            slot = self._item_slot.get(item_id)
            if slot is not None:
                return slot
            logger.debug('[Responses] arguments delta for unknown item_id %s '
                         '— falling back to current slot', item_id)
        return self._tc_count - 1 if self._tc_count > 0 else None

    # ──────────────────────────────────────────────────────────

    def translate(self, data_str: str) -> list:
        """Translate one Responses-API SSE ``data:`` payload.

        Returns a list of OpenAI-shaped chunk dicts (plus the ``'[DONE]'``
        sentinel after a terminal event). Unknown event types are ignored
        by design — the Responses event vocabulary is large (56 types) and
        grows; unrecognised structure must never kill the stream.
        """
        try:
            event = json.loads(data_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Responses] SSE JSON parse failed: %s', e)
            return []

        etype = event.get('type', '')
        out: list = []

        if etype == 'response.output_text.delta':
            out.append(self._chunk(delta={'content': event.get('delta', '')}))

        elif etype in ('response.reasoning_summary_text.delta',
                       'response.reasoning_text.delta'):
            out.append(self._chunk(
                delta={'reasoning_content': event.get('delta', '')}))

        elif etype == 'response.output_item.added':
            item = event.get('item') or {}
            if item.get('type') == 'function_call':
                slot = self._tc_count
                self._tc_count += 1
                item_id = item.get('id') or ''
                if item_id:
                    self._item_slot[item_id] = slot
                name = item.get('name', '')
                if self.tool_name_reverse:
                    name = self.tool_name_reverse.get(name, name)
                out.append(self._chunk(delta={'tool_calls': [{
                    'index': slot,
                    'id': item.get('call_id', ''),
                    'type': 'function',
                    'function': {'name': name,
                                 'arguments': ''}}]}))

        elif etype == 'response.function_call_arguments.delta':
            slot = self._slot_for(event)
            if slot is not None:
                out.append(self._chunk(delta={'tool_calls': [{
                    'index': slot,
                    'function': {'arguments': event.get('delta', '')}}]}))

        elif etype == 'response.completed':
            resp = event.get('response') or {}
            finish = 'tool_calls' if self._tc_count > 0 else 'stop'
            usage = _usage_to_openai(resp.get('usage') or {})
            out.append(self._chunk(finish_reason=finish,
                                   usage=usage or None))
            out.append('[DONE]')

        elif etype == 'response.incomplete':
            resp = event.get('response') or {}
            reason = (resp.get('incomplete_details') or {}).get('reason', '')
            if reason and reason != 'max_output_tokens':
                # content_filter & friends are failures, not finishes.
                out.append({'error': {
                    'message': f'response.incomplete: {reason}',
                    'type': reason,
                    'http_code': _ERROR_HTTP.get(reason, '')}})
            else:
                finish = 'length' if reason == 'max_output_tokens' else (
                    'tool_calls' if self._tc_count > 0 else 'stop')
                usage = _usage_to_openai(resp.get('usage') or {})
                out.append(self._chunk(finish_reason=finish,
                                       usage=usage or None))
            out.append('[DONE]')

        elif etype in ('response.failed', 'response.error'):
            resp = event.get('response') or {}
            err = resp.get('error') or event.get('error') or {}
            code = err.get('code', '') or etype
            message = err.get('message', '') or etype
            out.append({'error': {
                'message': f'{code}: {message}',
                'type': code,
                'http_code': _ERROR_HTTP.get(code, '')}})

        # Everything else — response.created / in_progress / queued,
        # output_item.done, content_part.*, output_text.done,
        # function_call_arguments.done, reasoning_summary_part.*,
        # web_search_call.*, … — carries no delta we need.
        return out
