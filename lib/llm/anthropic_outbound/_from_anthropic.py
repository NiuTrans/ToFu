# HOT_PATH
"""Inbound direction: non-streaming Anthropic Messages response → OpenAI.

Holds the ``_STOP_REASON_MAP`` table (shared with the SSE translator),
the content-block / usage converters, and ``anthropic_response_to_openai``.
Leaf module — no dependency on the outbound converters.
"""

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


def _blocks_to_openai_message(content_blocks: list) -> dict:
    """Anthropic response content blocks → OpenAI assistant message dict."""
    text_parts = []
    thinking_parts = []
    thinking_signature = ''
    tool_calls = []
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        if btype == 'text':
            text_parts.append(block.get('text', ''))
        elif btype == 'thinking':
            thinking_parts.append(block.get('thinking', ''))
            if block.get('signature'):
                thinking_signature = block['signature']
        elif btype == 'tool_use':
            tool_calls.append({
                'id': block.get('id', ''),
                'type': 'function',
                'function': {
                    'name': block.get('name', ''),
                    'arguments': json.dumps(block.get('input') or {}, ensure_ascii=False),
                },
            })
    msg = {'role': 'assistant'}
    if thinking_parts:
        msg['reasoning_content'] = ''.join(thinking_parts)
    if thinking_signature:
        msg['thinking_signature'] = thinking_signature
    if tool_calls:
        msg['tool_calls'] = tool_calls
    msg['content'] = ''.join(text_parts)
    return msg


def _convert_usage(usage: dict) -> dict:
    """Anthropic usage → OpenAI usage (keeps cache fields the tracker reads)."""
    usage = usage or {}
    inp = int(usage.get('input_tokens') or 0)
    out = int(usage.get('output_tokens') or 0)
    cw = int(usage.get('cache_creation_input_tokens') or 0)
    cr = int(usage.get('cache_read_input_tokens') or 0)
    return {
        'prompt_tokens': inp + cr + cw,
        'completion_tokens': out,
        'total_tokens': inp + cr + cw + out,
        'input_tokens': inp,
        'output_tokens': out,
        'cache_creation_input_tokens': cw,
        'cache_read_input_tokens': cr,
    }


def anthropic_response_to_openai(data: dict) -> dict:
    """Non-streaming Anthropic Messages response → OpenAI ChatCompletion."""
    msg = _blocks_to_openai_message(data.get('content'))
    finish = _STOP_REASON_MAP.get(data.get('stop_reason') or 'end_turn', 'stop')
    return {
        'id': data.get('id', ''),
        'object': 'chat.completion',
        'model': data.get('model', ''),
        'choices': [{'index': 0, 'message': msg, 'finish_reason': finish}],
        'usage': _convert_usage(data.get('usage')),
    }
