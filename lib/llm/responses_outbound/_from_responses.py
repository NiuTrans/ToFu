"""lib/llm/responses_outbound/_from_responses.py — Responses → Chat Completions.

Non-streaming back-conversion: a ``POST /v1/responses`` JSON response → an
OpenAI ``chat.completion``-shaped dict, so the shared non-stream tail in
``lib/llm/chat.py`` (choices/message/usage handling) works unchanged.

Shape notes:
  * ``output[]`` item ``message`` → ``choices[0].message.content`` (joined
    ``output_text`` parts); ``reasoning`` items → ``reasoning_content``
    (summary text and/or plain content text — DeepSeek carries plain text,
    OpenAI carries summaries).
  * ``function_call`` items → ``tool_calls`` keyed by ``call_id``.
  * ``status: incomplete`` + ``max_output_tokens`` → ``finish_reason:
    'length'``; any function_call present → ``'tool_calls'``.
  * ``status: failed`` → ``{'error': {...}}`` envelope (NO choices) so the
    caller classifies instead of manufacturing an empty assistant turn.
"""

from __future__ import annotations

from lib.llm.responses_outbound._sse import _usage_to_openai
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['responses_response_to_openai']


def responses_response_to_openai(data: dict,
                                  tool_name_reverse: dict | None = None) -> dict:
    """Convert a Responses API response object to chat.completion shape.

    ``tool_name_reverse`` (the request converter's second return value)
    restores truncated tool names on echoed function_call items.
    """
    if not isinstance(data, dict):
        return {'error': {'message': 'non-dict responses payload',
                          'type': 'invalid_response'}}

    status = data.get('status', 'completed')
    if status == 'failed':
        err = data.get('error') or {}
        message = err.get('message', '') or 'response failed'
        code = err.get('code', '') or 'response_failed'
        logger.warning('[Responses] non-stream response failed: %s: %s',
                       code, message[:200])
        return {'error': {'message': f'{code}: {message}', 'type': code}}

    content_parts: list = []
    reasoning_parts: list = []
    tool_calls: list = []

    for item in data.get('output') or []:
        if not isinstance(item, dict):
            continue
        itype = item.get('type')
        if itype == 'message':
            for part in item.get('content') or []:
                if not isinstance(part, dict):
                    continue
                if part.get('type') == 'output_text':
                    content_parts.append(part.get('text', ''))
                elif part.get('type') == 'refusal':
                    content_parts.append(part.get('refusal', ''))
        elif itype == 'reasoning':
            for summ in item.get('summary') or []:
                if isinstance(summ, dict) and summ.get('text'):
                    reasoning_parts.append(summ['text'])
            for cont in item.get('content') or []:
                if isinstance(cont, dict) and cont.get('text'):
                    reasoning_parts.append(cont['text'])
        elif itype == 'function_call':
            name = item.get('name', '')
            if tool_name_reverse:
                name = tool_name_reverse.get(name, name)
            tool_calls.append({
                'id': item.get('call_id', ''),
                'type': 'function',
                'function': {'name': name,
                             'arguments': item.get('arguments', '')},
            })

    finish = 'tool_calls' if tool_calls else 'stop'
    if status == 'incomplete':
        reason = (data.get('incomplete_details') or {}).get('reason', '')
        if reason == 'max_output_tokens':
            finish = 'length'

    message: dict = {'role': 'assistant',
                     'content': '\n'.join(p for p in content_parts if p)}
    if reasoning_parts:
        message['reasoning_content'] = '\n'.join(reasoning_parts)
    if tool_calls:
        message['tool_calls'] = tool_calls

    return {
        'id': data.get('id', ''),
        'object': 'chat.completion',
        'model': data.get('model', ''),
        'choices': [{'index': 0, 'message': message,
                     'finish_reason': finish}],
        'usage': _usage_to_openai(data.get('usage') or {}),
    }
