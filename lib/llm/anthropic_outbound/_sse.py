# HOT_PATH
"""Streaming direction: Anthropic SSE events → OpenAI chat.completion chunks.

Holds ``AnthropicSSETranslator``. Depends on the inbound module for the
shared ``_STOP_REASON_MAP`` table and the ``_convert_usage`` helper.
"""

import copy
import json

from lib.log import get_logger

from lib.llm.anthropic_outbound._from_anthropic import (
    _convert_usage,
    _is_replay_state_block,
    _map_stop_reason,
    _replayable_content_blocks,
)

logger = get_logger(__name__)

_MAX_CONTENT_BLOCK_INDEX = 4095
_MAX_TOOL_USE_ID_CHARS = 512
_MAX_TOOL_NAME_CHARS = 512


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
        # Anthropic hosted tools (notably Tool Search) add protocol blocks that
        # have no OpenAI Chat-Completions equivalent: ``server_tool_use`` and
        # ``tool_search_tool_result``.  Anthropic requires the complete
        # assistant content array to be replayed unchanged on the next request.
        # Keep a request-private copy while still projecting ordinary text /
        # thinking / client tool_use into the existing OpenAI-shaped stream.
        self._anthropic_blocks: dict[int, dict] = {}
        self._anthropic_input_json: dict[int, str] = {}
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
        # A block index is the provider response position.  Repeating the same
        # start with the same immutable identity is a transport replay;
        # changing identity at that position is protocol corruption.  Equal
        # calls at distinct indices remain independent occurrences.
        self._block_start_identity: dict[int, dict] = {}
        self._block_stopped: set[int] = set()
        self._initial_complete_input: set[int] = set()

    @staticmethod
    def _protocol_error(message: str) -> list[dict]:
        logger.warning('[Anthropic] invalid SSE event: %s', message[:200])
        return [{'error': {
            'message': f'invalid Anthropic stream: {message}',
            'type': 'server_error',
            'http_code': '500',
        }}]

    @staticmethod
    def _block_index(event: dict) -> int | None:
        index = event.get('index')
        if (isinstance(index, int) and not isinstance(index, bool)
                and 0 <= index <= _MAX_CONTENT_BLOCK_INDEX):
            return index
        return None

    @property
    def anthropic_content_blocks(self) -> list[dict]:
        """Complete Anthropic assistant blocks, in original wire order.

        Returned values are detached from translator state so downstream
        cache-control decoration cannot mutate a later retry/reconciliation.
        """
        blocks = [self._anthropic_blocks[index]
                  for index in sorted(self._anthropic_blocks)]
        if not any(_is_replay_state_block(block) for block in blocks):
            return []
        return _replayable_content_blocks(blocks)

    def _capture_delta(self, idx: int, delta: dict) -> None:
        block = self._anthropic_blocks.get(idx)
        if not isinstance(block, dict):
            return
        dtype = delta.get('type')
        if dtype == 'text_delta':
            block['text'] = (block.get('text') or '') + delta.get('text', '')
        elif dtype == 'thinking_delta':
            block['thinking'] = ((block.get('thinking') or '')
                                 + delta.get('thinking', ''))
        elif dtype == 'signature_delta':
            block['signature'] = ((block.get('signature') or '')
                                  + delta.get('signature', ''))
        elif dtype == 'compaction_delta':
            # Unlike text, Anthropic emits the complete compaction summary in
            # one delta. It is protocol state, not user-visible assistant text.
            block['content'] = delta.get('content')
        elif dtype == 'input_json_delta':
            raw = (self._anthropic_input_json.get(idx, '')
                   + delta.get('partial_json', ''))
            self._anthropic_input_json[idx] = raw
            try:
                block['input'] = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, TypeError):
                # Partial JSON is expected until content_block_stop.  Keep the
                # last complete ``input`` (normally {}) until it closes.
                pass

    def translate(self, data_str: str) -> list:
        try:
            ev = json.loads(data_str)
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[AnthropicOut] SSE JSON parse failed: %s', e)
            return []
        if not isinstance(ev, dict):
            return self._protocol_error('event must be an object')
        etype = ev.get('type', '')
        if not isinstance(etype, str):
            return self._protocol_error('event.type must be text')

        if etype == 'message_start':
            message = ev.get('message')
            if not isinstance(message, dict):
                return self._protocol_error(
                    'message_start.message must be an object')
            raw_usage = message.get('usage')
            usage = {} if raw_usage is None else raw_usage
            if not isinstance(usage, dict):
                return self._protocol_error(
                    'message_start usage must be an object or null')
            if usage:
                self._usage_raw = dict(usage)
                return [{'choices': [{'delta': {}}], 'usage': _convert_usage(self._usage_raw)}]
            return []

        if etype == 'content_block_start':
            idx = self._block_index(ev)
            if idx is None:
                return self._protocol_error(
                    'content_block_start index must be a bounded integer')
            block = ev.get('content_block')
            if not isinstance(block, dict):
                return self._protocol_error(
                    'content_block_start content_block must be an object')
            btype = block.get('type')
            if not isinstance(btype, str) or not btype:
                return self._protocol_error(
                    'content_block_start block type must be non-empty text')
            if idx in self._block_types and idx not in self._block_start_identity:
                return self._protocol_error(
                    'content_block_start arrived after a standalone delta')
            normalized_block = copy.deepcopy(block)
            initial_delta: dict = {}
            initial_input_json = ''
            if btype == 'text':
                initial_text = block.get('text', '')
                if initial_text is None:
                    initial_text = ''
                if not isinstance(initial_text, str):
                    return self._protocol_error(
                        'text block initial text must be text or null')
                normalized_block['text'] = initial_text
                if initial_text:
                    initial_delta['content'] = initial_text
            elif btype == 'thinking':
                initial_thinking = block.get('thinking', '')
                initial_signature = block.get('signature', '')
                if initial_thinking is None:
                    initial_thinking = ''
                if initial_signature is None:
                    initial_signature = ''
                if not isinstance(initial_thinking, str):
                    return self._protocol_error(
                        'thinking block initial thinking must be text or null')
                if not isinstance(initial_signature, str):
                    return self._protocol_error(
                        'thinking block initial signature must be text or null')
                normalized_block['thinking'] = initial_thinking
                if 'signature' in block:
                    normalized_block['signature'] = initial_signature
                if initial_thinking:
                    initial_delta['reasoning_content'] = initial_thinking
                if initial_signature:
                    initial_delta['thinking_signature'] = initial_signature
            elif btype in ('tool_use', 'server_tool_use'):
                initial_input = block.get('input')
                if initial_input is None:
                    initial_input = {}
                if not isinstance(initial_input, dict):
                    return self._protocol_error(
                        f'{btype} initial input must be an object or null')
                normalized_block['input'] = copy.deepcopy(initial_input)
                if initial_input:
                    initial_input_json = json.dumps(
                        initial_input, ensure_ascii=False)
            elif btype == 'compaction':
                initial_content = block.get('content')
                if (initial_content is not None
                        and not isinstance(initial_content, str)):
                    return self._protocol_error(
                        'compaction initial content must be text or null')
            identity = copy.deepcopy(normalized_block)
            if idx in self._block_start_identity:
                if self._block_start_identity[idx] != identity:
                    return self._protocol_error(
                        'content block index was reused with a different identity')
                return []
            self._block_start_identity[idx] = copy.deepcopy(identity)
            self._block_types[idx] = btype
            self._anthropic_blocks[idx] = normalized_block
            if btype in ('tool_use', 'server_tool_use'):
                if initial_input_json:
                    self._anthropic_input_json[idx] = initial_input_json
                    self._initial_complete_input.add(idx)
            if btype == 'tool_use':
                _name = block.get('name', '')
                _call_id = block.get('id', '')
                if not isinstance(_name, str):
                    return self._protocol_error(
                        'tool_use name must be text')
                if not _name or len(_name) > _MAX_TOOL_NAME_CHARS:
                    return self._protocol_error(
                        'tool_use name must be bounded non-empty text')
                if not isinstance(_call_id, str):
                    return self._protocol_error(
                        'tool_use id must be text')
                if not _call_id or len(_call_id) > _MAX_TOOL_USE_ID_CHARS:
                    return self._protocol_error(
                        'tool_use id must be bounded non-empty text')
                if self.tool_name_reverse:
                    _name = self.tool_name_reverse.get(_name, _name)
                return [{'choices': [{'delta': {'tool_calls': [{
                    'index': idx,
                    'id': _call_id,
                    'type': 'function',
                    'function': {
                        'name': _name, 'arguments': initial_input_json},
                }]}}]}]
            if initial_delta:
                return [{'choices': [{'delta': initial_delta}]}]
            return []

        if etype == 'content_block_delta':
            idx = self._block_index(ev)
            if idx is None:
                return self._protocol_error(
                    'content_block_delta index must be a bounded integer')
            if idx in self._block_stopped:
                return self._protocol_error(
                    'content_block_delta arrived after its block stop')
            delta = ev.get('delta')
            if not isinstance(delta, dict):
                return self._protocol_error(
                    'content_block_delta delta must be an object')
            dtype = delta.get('type')
            if not isinstance(dtype, str) or not dtype:
                return self._protocol_error(
                    'content_block_delta type must be non-empty text')
            if idx not in self._block_types:
                # A few compatibility gateways omit the lifecycle start for
                # plain text/thinking deltas. These fields cannot authorize a
                # tool, so inferring their display-only block is safe. Tool
                # input and protocol-state deltas still require an owner.
                inferred_type = {
                    'text_delta': 'text',
                    'thinking_delta': 'thinking',
                    'signature_delta': 'thinking',
                }.get(dtype)
                if inferred_type is None:
                    return self._protocol_error(
                        'content_block_delta arrived before its block start')
                self._block_types[idx] = inferred_type
                self._anthropic_blocks[idx] = {'type': inferred_type}
            text_field = {
                'text_delta': 'text',
                'thinking_delta': 'thinking',
                'signature_delta': 'signature',
                'compaction_delta': 'content',
                'input_json_delta': 'partial_json',
            }.get(dtype)
            if text_field is not None:
                value = delta.get(text_field, '')
                if value is None:
                    value = ''
                    delta = dict(delta)
                    delta[text_field] = value
                if not isinstance(value, str):
                    return self._protocol_error(
                        f'{dtype}.{text_field} must be text or null')
            block_type = self._block_types.get(idx)
            expected_block_types = {
                'text_delta': {'text'},
                'thinking_delta': {'thinking'},
                'signature_delta': {'thinking'},
                'compaction_delta': {'compaction'},
                'input_json_delta': {'tool_use', 'server_tool_use'},
            }.get(dtype)
            if (expected_block_types is not None
                    and block_type not in expected_block_types):
                return self._protocol_error(
                    f'{dtype} is invalid for {block_type!r} block {idx}')
            if dtype == 'input_json_delta' and idx in self._initial_complete_input:
                return self._protocol_error(
                    'input_json_delta followed an already-complete initial input')
            self._capture_delta(idx, delta)
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
                # ``server_tool_use`` is Anthropic's own hosted search call,
                # not an application tool request.  Project only real client
                # ``tool_use`` blocks into our standard execution pipeline.
                if self._block_types.get(idx) != 'tool_use':
                    return []
                return [{'choices': [{'delta': {'tool_calls': [{
                    'index': idx,
                    'function': {'arguments': delta.get('partial_json', '')},
                }]}}]}]
            return []

        if etype == 'content_block_stop':
            idx = self._block_index(ev)
            if idx is None:
                return self._protocol_error(
                    'content_block_stop index must be a bounded integer')
            if idx not in self._block_types:
                return self._protocol_error(
                    'content_block_stop arrived before its block start')
            if idx in self._block_stopped:
                return []
            if self._block_types.get(idx) in ('tool_use', 'server_tool_use'):
                raw_input = self._anthropic_input_json.get(idx, '')
                if raw_input:
                    try:
                        parsed_input = json.loads(raw_input)
                    except (json.JSONDecodeError, TypeError):
                        return self._protocol_error(
                            'tool input JSON was incomplete at block stop')
                    if not isinstance(parsed_input, dict):
                        return self._protocol_error(
                            'tool input JSON must decode to an object')
                    self._anthropic_blocks[idx]['input'] = parsed_input
            self._block_stopped.add(idx)
            return []

        if etype == 'message_delta':
            delta = ev.get('delta')
            if not isinstance(delta, dict):
                return self._protocol_error(
                    'message_delta delta must be an object')
            chunk = {'choices': [{'delta': {}}]}
            stop = delta.get('stop_reason')
            if stop is not None and not isinstance(stop, str):
                return self._protocol_error(
                    'message_delta stop_reason must be text or null')
            if stop:
                chunk['choices'][0]['finish_reason'] = _map_stop_reason(stop)
            raw_usage = ev.get('usage')
            if raw_usage is not None and not isinstance(raw_usage, dict):
                return self._protocol_error(
                    'message_delta usage must be an object or null')
            if raw_usage:
                # Merge onto the message_start counts rather than replace them.
                # output_tokens is the growing cumulative count → take the
                # delta's value. input_tokens / cache_creation_input_tokens /
                # cache_read_input_tokens are fixed at prompt-processing time
                # and were reported by message_start; the delta usually OMITS
                # them (→ absent, not zero), so keep the larger of the two so a
                # cache-read count can never regress to zero here.
                for k, v in raw_usage.items():
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
            # Some compatibility gateways omit content_block_stop. Validate
            # every accumulated tool input at the terminal boundary so a
            # truncated JSON prefix cannot be repaired into a guessed action.
            for idx, raw_input in self._anthropic_input_json.items():
                if not raw_input:
                    continue
                try:
                    parsed_input = json.loads(raw_input)
                except (json.JSONDecodeError, TypeError):
                    return self._protocol_error(
                        f'tool input JSON for block {idx} was incomplete at message stop')
                if not isinstance(parsed_input, dict):
                    return self._protocol_error(
                        f'tool input JSON for block {idx} must decode to an object')
                self._anthropic_blocks[idx]['input'] = parsed_input
            return ['[DONE]']

        if etype == 'error':
            error = ev.get('error')
            if not isinstance(error, dict):
                error = {'message': 'anthropic stream error'}
            else:
                error = dict(error)
                if not isinstance(error.get('message'), str):
                    error['message'] = 'anthropic stream error'
                if ('type' in error
                        and not isinstance(error.get('type'), str)):
                    error['type'] = 'server_error'
            return [{'error': error}]

        # ping / unknown → no-op
        return []
