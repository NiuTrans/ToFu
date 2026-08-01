# HOT_PATH
"""Streaming direction: Anthropic SSE events → OpenAI chat.completion chunks.

Holds ``AnthropicSSETranslator``. Depends on the inbound module for the
shared ``_STOP_REASON_MAP`` table and the ``_convert_usage`` helper.
"""

import json

from lib.log import get_logger

from lib.llm.anthropic_outbound._from_anthropic import (
    _STOP_REASON_MAP,
    _convert_usage,
)

logger = get_logger(__name__)


class AnthropicSSETranslator:
    """Translate Anthropic SSE event payloads into OpenAI chat.completion
    chunks. Plugs into ``SSEAccumulator`` like ``CodexSSETranslator``.

    ``translate(data_str)`` accepts the JSON string from one ``data:`` line
    (Anthropic payloads are self-describing via their ``type`` field, so the
    preceding ``event:`` line can be ignored) and returns a list of OpenAI
    chunk dicts and/or the literal ``'[DONE]'`` sentinel.
    """

    def __init__(self, model: str = ''):
        self.model = model
        # Per-request reverse map for cloaked (TitleCase) tool names — set by
        # the pre-flight when the OAuth cloak renamed request tools; only
        # names THIS request renamed are restored (lib/oauth/outbound).
        self.tool_name_reverse: dict = {}
        # content-block index → 'text' | 'tool_use' | 'thinking'
        self._block_types: dict = {}
        # Running RAW Anthropic usage, merged across message_start (carries
        # input_tokens + cache_creation_input_tokens + cache_read_input_tokens)
        # and message_delta (carries the growing output_tokens). Anthropic
        # splits usage across these two events: message_start reports the
        # prompt-side counts (incl. the all-important cache_read), message_delta
        # reports only the final output_tokens. The downstream SSEAccumulator
        # OVERWRITES usage on each chunk (self.usage = chunk['usage']), so if we
        # emitted a message_delta usage holding ONLY output_tokens the cache
        # counts from message_start would be clobbered to zero — every cached
        # turn mis-recorded as cache_read=0, corrupting the cost panel, the
        # wallet debit, AND detect_cache_break (which then blames 'server-side').
        # Merging here lets each emitted usage be the COMPLETE picture.
        self._usage_raw: dict = {}

    def translate(self, data_str: str) -> list:
        try:
            ev = json.loads(data_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[AnthropicOut] SSE JSON parse failed: %s', e)
            return []
        etype = ev.get('type', '')

        if etype == 'message_start':
            usage = (ev.get('message') or {}).get('usage') or {}
            if usage:
                self._usage_raw = dict(usage)
                return [{'choices': [{'delta': {}}], 'usage': _convert_usage(self._usage_raw)}]
            return []

        if etype == 'content_block_start':
            idx = ev.get('index', 0)
            block = ev.get('content_block') or {}
            btype = block.get('type')
            self._block_types[idx] = btype
            if btype == 'tool_use':
                _name = block.get('name', '')
                if self.tool_name_reverse:
                    _name = self.tool_name_reverse.get(_name, _name)
                return [{'choices': [{'delta': {'tool_calls': [{
                    'index': idx,
                    'id': block.get('id', ''),
                    'type': 'function',
                    'function': {'name': _name, 'arguments': ''},
                }]}}]}]
            return []

        if etype == 'content_block_delta':
            idx = ev.get('index', 0)
            delta = ev.get('delta') or {}
            dtype = delta.get('type')
            if dtype == 'text_delta':
                return [{'choices': [{'delta': {'content': delta.get('text', '')}}]}]
            if dtype == 'thinking_delta':
                return [{'choices': [{'delta': {'reasoning_content': delta.get('thinking', '')}}]}]
            if dtype == 'signature_delta':
                # Opaque signature for the thinking block — required when
                # replaying the thinking block on a later tool-use turn,
                # else the Messages API returns HTTP 400. Surface it as a
                # synthetic OpenAI delta field the accumulator collects.
                return [{'choices': [{'delta': {'thinking_signature': delta.get('signature', '')}}]}]
            if dtype == 'input_json_delta':
                return [{'choices': [{'delta': {'tool_calls': [{
                    'index': idx,
                    'function': {'arguments': delta.get('partial_json', '')},
                }]}}]}]
            return []

        if etype == 'message_delta':
            delta = ev.get('delta') or {}
            chunk = {'choices': [{'delta': {}}]}
            stop = delta.get('stop_reason')
            if stop:
                chunk['choices'][0]['finish_reason'] = _STOP_REASON_MAP.get(stop, 'stop')
            if ev.get('usage'):
                # Merge onto the message_start counts rather than replace them.
                # output_tokens is the growing cumulative count → take the
                # delta's value. input_tokens / cache_creation_input_tokens /
                # cache_read_input_tokens are fixed at prompt-processing time
                # and were reported by message_start; the delta usually OMITS
                # them (→ absent, not zero), so keep the larger of the two so a
                # cache-read count can never regress to zero here.
                for k, v in ev['usage'].items():
                    if v is None:
                        continue
                    if k == 'output_tokens':
                        self._usage_raw[k] = v
                    else:
                        try:
                            self._usage_raw[k] = max(int(self._usage_raw.get(k) or 0), int(v))
                        except (TypeError, ValueError):
                            self._usage_raw[k] = v
                chunk['usage'] = _convert_usage(self._usage_raw)
            return [chunk]

        if etype == 'message_stop':
            return ['[DONE]']

        if etype == 'error':
            return [{'error': ev.get('error') or {'message': 'anthropic stream error'}}]

        # ping / content_block_stop / unknown → no-op
        return []
