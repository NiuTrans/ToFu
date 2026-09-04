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

from lib.llm.responses_outbound._sse import (
    _CAPTURE_ITEM_TYPES,
    _MAX_RESPONSE_ITEM_ID_CHARS,
    _MAX_RESPONSE_TOOL_NAME_CHARS,
    _multi_agent_message_is_user_visible,
    _program_needs_followup,
    _usage_to_openai,
)
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['responses_response_to_openai']


def _invalid_response(message: str) -> dict:
    """Return the error envelope consumed by the shared chat tail."""
    logger.warning('[Responses] invalid non-stream response: %s', message[:200])
    return {'error': {'message': message, 'type': 'invalid_response'}}


def _string_part(value: object, *, field: str) -> tuple[str, str | None]:
    """Normalize an optional text part without allowing join-time crashes."""
    if value is None:
        return '', None
    if isinstance(value, str):
        return value, None
    return '', f'{field} must be text or null'


def responses_response_to_openai(data: object,
                                  tool_name_reverse: dict | None = None) -> dict:
    """Convert a Responses API response object to chat.completion shape.

    ``tool_name_reverse`` (the request converter's second return value)
    restores truncated tool names on echoed function_call items.
    """
    if not isinstance(data, dict):
        return _invalid_response('top-level Responses payload must be an object')

    status = data.get('status', 'completed')
    if not isinstance(status, str):
        return _invalid_response('Responses status must be text')
    if status == 'failed':
        err = data.get('error') or {}
        if not isinstance(err, dict):
            err = {}
        raw_message = err.get('message')
        raw_code = err.get('code')
        message = (raw_message if isinstance(raw_message, str) and raw_message
                   else 'response failed')
        code = (raw_code if isinstance(raw_code, str) and raw_code
                else 'response_failed')
        logger.warning('[Responses] non-stream response failed: %s: %s',
                       code, message[:200])
        return {'error': {'message': f'{code}: {message}', 'type': code}}
    if status not in ('completed', 'incomplete'):
        return _invalid_response(
            f'Responses returned non-terminal status {status!r}')

    raw_output = data.get('output', [])
    if raw_output is None:
        output: list = []
    elif isinstance(raw_output, list):
        output = raw_output
    else:
        return _invalid_response('Responses output must be an array or null')

    content_parts: list = []
    reasoning_parts: list = []
    tool_calls: list = []
    response_items: list[dict] = []

    for item_index, item in enumerate(output):
        if not isinstance(item, dict):
            return _invalid_response(
                f'Responses output[{item_index}] must be an object')
        itype = item.get('type')
        item_agent = item.get('agent')
        if ('agent' in item and item_agent is not None
                and (not isinstance(item_agent, dict)
                     or not isinstance(item_agent.get('agent_name'), str)
                     or not item_agent.get('agent_name'))):
            return _invalid_response(
                f'Responses output[{item_index}].agent must carry a '
                'non-empty text agent_name')
        if itype == 'message':
            if not _multi_agent_message_is_user_visible(item):
                continue
            raw_parts = item.get('content', [])
            if raw_parts is None:
                raw_parts = []
            if not isinstance(raw_parts, list):
                return _invalid_response(
                    f'Responses output[{item_index}].content must be an array')
            for part_index, part in enumerate(raw_parts):
                if not isinstance(part, dict):
                    return _invalid_response(
                        'Responses message content '
                        f'part[{part_index}] must be an object')
                if part.get('type') == 'output_text':
                    text, error = _string_part(
                        part.get('text', ''), field='output_text.text')
                    if error:
                        return _invalid_response(error)
                    content_parts.append(text)
                elif part.get('type') == 'refusal':
                    text, error = _string_part(
                        part.get('refusal', ''), field='refusal.refusal')
                    if error:
                        return _invalid_response(error)
                    content_parts.append(text)
        elif itype == 'reasoning':
            response_items.append(dict(item))
            for field_name in ('summary', 'content'):
                raw_parts = item.get(field_name, [])
                if raw_parts is None:
                    raw_parts = []
                if not isinstance(raw_parts, list):
                    return _invalid_response(
                        f'Responses reasoning.{field_name} must be an array')
                for part in raw_parts:
                    if not isinstance(part, dict):
                        return _invalid_response(
                            'Responses reasoning part must be an object')
                    text, error = _string_part(
                        part.get('text', ''),
                        field=f'reasoning.{field_name}.text')
                    if error:
                        return _invalid_response(error)
                    if text:
                        reasoning_parts.append(text)
        elif itype == 'function_call':
            call_id = item.get('call_id', '')
            name = item.get('name', '')
            arguments = item.get('arguments', '')
            if arguments is None:
                arguments = ''
            if (not isinstance(call_id, str)
                    or len(call_id) > _MAX_RESPONSE_ITEM_ID_CHARS):
                return _invalid_response(
                    f'Responses output[{item_index}].function_call call_id '
                    'must be bounded text')
            if (not isinstance(name, str)
                    or len(name) > _MAX_RESPONSE_TOOL_NAME_CHARS):
                return _invalid_response(
                    f'Responses output[{item_index}].function_call name '
                    'must be bounded text')
            if not isinstance(arguments, str):
                return _invalid_response(
                    f'Responses output[{item_index}].function_call arguments '
                    'must be text or null')
            if tool_name_reverse and isinstance(name, str):
                name = tool_name_reverse.get(name, name)
            tool_call = {
                'id': call_id,
                'type': 'function',
                'function': {'name': name,
                             'arguments': arguments},
            }
            if 'caller' in item and item.get('caller') is not None:
                raw_caller = item.get('caller')
                # Preserve malformed caller metadata.  The common typed
                # ingress validator must reject it; dropping it would promote
                # a delegated call into an apparent root call.
                tool_call['caller'] = (dict(raw_caller)
                                       if isinstance(raw_caller, dict)
                                       else raw_caller)
            agent = item.get('agent')
            if isinstance(agent, dict) and agent.get('agent_name'):
                caller = tool_call.get('caller')
                if 'caller' not in tool_call:
                    caller = {'type': 'multi_agent'}
                    tool_call['caller'] = caller
                if isinstance(caller, dict):
                    caller['agent_name'] = str(agent['agent_name'])
            tool_calls.append(tool_call)
        elif itype in _CAPTURE_ITEM_TYPES:
            response_items.append(dict(item))
        else:
            logger.warning('[Responses] unhandled non-stream output item '
                           'type=%s; response remains usable', itype or '?')

    finish = 'tool_calls' if tool_calls else 'stop'
    if status == 'incomplete':
        details = data.get('incomplete_details') or {}
        if not isinstance(details, dict):
            return _invalid_response(
                'Responses incomplete_details must be an object')
        reason = details.get('reason', '')
        if reason == 'max_output_tokens':
            finish = 'length'
        elif reason:
            return _invalid_response(
                f'Responses incomplete response: {str(reason)[:120]}')
        else:
            return _invalid_response(
                'Responses incomplete response omitted its reason')

    message: dict = {'role': 'assistant',
                     'content': '\n'.join(p for p in content_parts if p)}
    if reasoning_parts:
        # Paragraph-join: each summary part is its own markdown headline —
        # a bare '\n' collapses under markdown/plain rendering and fuses
        # adjacent '**…**' headlines (mirrors the SSE translator's part
        # boundary separator).
        message['reasoning_content'] = '\n\n'.join(reasoning_parts)
    if response_items:
        message['_responses_items'] = response_items
    if tool_calls:
        message['tool_calls'] = tool_calls

    usage = _usage_to_openai(data.get('usage') or {})
    if _program_needs_followup(output):
        usage['_program_pending'] = True
    return {
        'id': data.get('id', ''),
        'object': 'chat.completion',
        'model': data.get('model', ''),
        'choices': [{'index': 0, 'message': message,
                     'finish_reason': finish}],
        'usage': usage,
    }
