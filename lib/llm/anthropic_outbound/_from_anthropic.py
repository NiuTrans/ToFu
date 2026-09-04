# HOT_PATH
"""Inbound direction: non-streaming Anthropic Messages response → OpenAI.

Holds the ``_STOP_REASON_MAP`` table (shared with the SSE translator),
the content-block / usage converters, and ``anthropic_response_to_openai``.
Leaf module — no dependency on the outbound converters.
"""

import copy
import json

from lib.log import get_logger

logger = get_logger(__name__)

# stop_reason (Anthropic) → finish_reason (OpenAI)
_STOP_REASON_MAP = {
    'end_turn': 'stop',
    'stop_sequence': 'stop',
    'max_tokens': 'length',
    'tool_use': 'tool_calls',
    'pause_turn': 'stop',
    'refusal': 'content_filter',
}

_REPLAY_ONLY_BLOCK_TYPES = frozenset({
    'server_tool_use',
    'tool_search_tool_result',
    'compaction',
    'redacted_thinking',
})
_MAX_TOOL_USE_ID_CHARS = 512
_MAX_TOOL_NAME_CHARS = 512


def _is_replay_state_block(block: object) -> bool:
    """True for valid protocol state that must survive the OpenAI projection."""
    if not isinstance(block, dict):
        return False
    block_type = block.get('type')
    if block_type == 'compaction':
        # Anthropic documents a rare tool-enabled failure shape with
        # ``content: null``. Replaying it would make the server discard the
        # healthy history before an empty summary, so treat it as no compact
        # and let the next request retry (local hard-window fallback remains).
        content = block.get('content')
        return isinstance(content, str) and bool(content.strip())
    return block_type in _REPLAY_ONLY_BLOCK_TYPES


def _replayable_content_blocks(content_blocks: list) -> list:
    """Drop failed/null compaction markers while preserving all other order."""
    return [copy.deepcopy(block) for block in content_blocks or []
            if isinstance(block, dict)
            and not (block.get('type') == 'compaction'
                     and not _is_replay_state_block(block))]


def _map_stop_reason(value: object) -> str | None:
    """Preserve missing/unknown terminal evidence instead of inventing stop."""
    if value is None or value == '':
        return None
    raw = str(value)
    return _STOP_REASON_MAP.get(raw, raw)


def _blocks_to_openai_message(content_blocks: list) -> dict:
    """Anthropic response content blocks → OpenAI assistant message dict."""
    text_parts = []
    thinking_parts = []
    thinking_signature = ''
    tool_calls = []
    for block_index, block in enumerate(content_blocks):
        if not isinstance(block, dict):
            raise ValueError(
                f'Anthropic content[{block_index}] must be an object')
        btype = block.get('type')
        if not isinstance(btype, str) or not btype:
            raise ValueError(
                f'Anthropic content[{block_index}].type must be non-empty text')
        if btype == 'text':
            text = block.get('text', '')
            if text is None:
                text = ''
            if not isinstance(text, str):
                raise ValueError('Anthropic text block text must be text or null')
            text_parts.append(text)
        elif btype == 'thinking':
            thinking = block.get('thinking', '')
            if thinking is None:
                thinking = ''
            if not isinstance(thinking, str):
                raise ValueError(
                    'Anthropic thinking block thinking must be text or null')
            thinking_parts.append(thinking)
            signature = block.get('signature')
            if signature is not None and not isinstance(signature, str):
                raise ValueError(
                    'Anthropic thinking block signature must be text or null')
            if signature:
                thinking_signature = signature
        elif btype == 'tool_use':
            tool_use_id = block.get('id', '')
            tool_name = block.get('name', '')
            tool_input = block.get('input')
            if tool_input is None:
                tool_input = {}
            if (not isinstance(tool_use_id, str)
                    or not tool_use_id
                    or len(tool_use_id) > _MAX_TOOL_USE_ID_CHARS):
                raise ValueError(
                    'Anthropic tool_use id must be bounded non-empty text')
            if (not isinstance(tool_name, str)
                    or not tool_name
                    or len(tool_name) > _MAX_TOOL_NAME_CHARS):
                raise ValueError(
                    'Anthropic tool_use name must be bounded non-empty text')
            if not isinstance(tool_input, dict):
                raise ValueError('Anthropic tool_use input must be an object')
            try:
                arguments = json.dumps(
                    tool_input, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    'Anthropic tool_use input must be JSON serializable') from exc
            tool_calls.append({
                'id': tool_use_id,
                'type': 'function',
                'function': {
                    'name': tool_name,
                    'arguments': arguments,
                },
            })
    msg = {'role': 'assistant'}
    if any(_is_replay_state_block(block) for block in content_blocks or []):
        # Opaque protocol continuity sidecar.  It is consumed only by the
        # Anthropic outbound converter and stripped from every other wire.
        msg['_anthropic_content_blocks'] = _replayable_content_blocks(
            content_blocks)
    if thinking_parts:
        msg['reasoning_content'] = ''.join(thinking_parts)
    if thinking_signature:
        msg['thinking_signature'] = thinking_signature
    if tool_calls:
        msg['tool_calls'] = tool_calls
    msg['content'] = ''.join(text_parts)
    return msg


def _convert_usage(usage: dict) -> dict:
    """Anthropic usage → OpenAI usage with native-compaction accounting.

    Anthropic's compaction beta reports the effective final prompt in the
    top-level fields, but bills every internal sampling pass in ``iterations``.
    Standard token keys therefore carry the billed aggregate, while
    ``effective_prompt_tokens`` remains the context-size authority used by the
    next-round gate and UI. Without iterations the two values are identical.
    """
    if not isinstance(usage, dict):
        usage = {}

    def _int(row, key):
        try:
            return max(0, int((row or {}).get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    top_inp = _int(usage, 'input_tokens')
    top_out = _int(usage, 'output_tokens')
    top_cw = _int(usage, 'cache_creation_input_tokens')
    top_cr = _int(usage, 'cache_read_input_tokens')
    top_thinking = _int(usage, 'thinking_tokens')
    effective_prompt = top_inp + top_cw + top_cr

    raw_iterations = usage.get('iterations') or []
    if not isinstance(raw_iterations, list):
        raw_iterations = []
    iterations = [row for row in raw_iterations
                  if isinstance(row, dict)]
    if iterations:
        inp = sum(_int(row, 'input_tokens') for row in iterations)
        out = sum(_int(row, 'output_tokens') for row in iterations)
        cw = sum(_int(row, 'cache_creation_input_tokens')
                 for row in iterations)
        cr = sum(_int(row, 'cache_read_input_tokens')
                 for row in iterations)
        thinking = sum(_int(row, 'thinking_tokens') for row in iterations)
        # Some beta gateways omit cache/thinking details inside each iteration
        # while retaining them at the top level. Never regress below the final
        # message's authoritative fields.
        cw = max(cw, top_cw)
        cr = max(cr, top_cr)
        thinking = max(thinking, top_thinking)
    else:
        inp, out, cw, cr, thinking = (
            top_inp, top_out, top_cw, top_cr, top_thinking)

    full_prompt = inp + cr + cw
    result = {
        'prompt_tokens': full_prompt,
        'completion_tokens': out,
        'total_tokens': full_prompt + out,
        'input_tokens': inp,
        'output_tokens': out,
        'cache_creation_input_tokens': cw,
        'cache_read_input_tokens': cr,
        'effective_prompt_tokens': effective_prompt,
        'effective_output_tokens': top_out,
    }
    if thinking:
        result['thinking_tokens'] = thinking
        result['reasoning_tokens'] = thinking
    if iterations:
        compaction_rows = [row for row in iterations
                           if row.get('type') == 'compaction']
        result['compaction_iterations'] = len(compaction_rows)
        result['compaction_input_tokens'] = sum(
            _int(row, 'input_tokens')
            + _int(row, 'cache_creation_input_tokens')
            + _int(row, 'cache_read_input_tokens')
            for row in compaction_rows)
        result['compaction_output_tokens'] = sum(
            _int(row, 'output_tokens') for row in compaction_rows)
    return result


def anthropic_response_to_openai(data: object) -> dict:
    """Non-streaming Anthropic Messages response → OpenAI ChatCompletion."""
    if not isinstance(data, dict):
        return {'error': {
            'message': 'top-level Anthropic payload must be an object',
            'type': 'invalid_response',
        }}
    content = data.get('content')
    if not isinstance(content, list):
        return {'error': {
            'message': 'Anthropic content must be an array',
            'type': 'invalid_response',
        }}
    stop_reason = data.get('stop_reason')
    if stop_reason is not None and not isinstance(stop_reason, str):
        return {'error': {
            'message': 'Anthropic stop_reason must be text or null',
            'type': 'invalid_response',
        }}
    try:
        msg = _blocks_to_openai_message(content)
    except ValueError as exc:
        logger.warning('[Anthropic] invalid non-stream response: %s', exc)
        return {'error': {'message': str(exc), 'type': 'invalid_response'}}
    finish = _map_stop_reason(stop_reason)
    return {
        'id': data.get('id', ''),
        'object': 'chat.completion',
        'model': data.get('model', ''),
        'choices': [{'index': 0, 'message': msg, 'finish_reason': finish}],
        'usage': _convert_usage(data.get('usage')),
    }
