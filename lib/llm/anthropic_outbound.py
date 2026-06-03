# HOT_PATH
"""Outbound Anthropic Messages API adapter.

The rest of Tofu speaks OpenAI Chat Completions internally. Some gateways
(e.g. the sankuai AIGC gateway's Claude Code app) only accept the
**Anthropic Messages API** (``POST /v1/messages``) for certain models.
This module translates a fully-built OpenAI request body into an Anthropic
request, and translates the Anthropic response / SSE stream back into the
OpenAI shape the rest of the pipeline already understands.

Direction: Tofu-as-Anthropic-CLIENT (outbound). This is the inverse of
``lib/compat/anthropic.py`` (Tofu-as-Anthropic-SERVER, inbound).

Public API:
  - anthropic_messages_url(base_url) -> str
  - anthropic_headers(api_key, extra_headers=None) -> dict
  - openai_body_to_anthropic(body) -> dict
  - anthropic_response_to_openai(data) -> dict   (non-streaming)
  - AnthropicSSETranslator                        (streaming)
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)

ANTHROPIC_VERSION = '2023-06-01'

# stop_reason (Anthropic) → finish_reason (OpenAI)
_STOP_REASON_MAP = {
    'end_turn': 'stop',
    'stop_sequence': 'stop',
    'max_tokens': 'length',
    'tool_use': 'tool_calls',
    'pause_turn': 'stop',
    'refusal': 'content_filter',
}


def anthropic_messages_url(base_url: str) -> str:
    """Resolve the Messages endpoint from a provider base URL.

    Handles the three base-URL shapes we ship:
      * already a Messages endpoint (``…/messages``)        → used as-is
      * ends at the API version segment (``…/v1``,          → ``+ /messages``
        e.g. direct ``https://api.anthropic.com/v1``)
      * ends at a gateway root (``…/v1/anthropic``)         → ``+ /v1/messages``
    """
    u = (base_url or '').rstrip('/')
    if u.endswith('/messages'):
        return u
    if u.endswith('/v1'):
        return u + '/messages'
    return u + '/v1/messages'


def anthropic_headers(api_key: str, extra_headers: dict = None) -> dict:
    """Build Anthropic auth headers. Sends both ``x-api-key`` and a Bearer
    token (gateways differ on which they read), plus ``anthropic-version``."""
    hdrs = {
        'Content-Type': 'application/json',
        'anthropic-version': ANTHROPIC_VERSION,
    }
    if api_key:
        hdrs['x-api-key'] = api_key
        hdrs['Authorization'] = f'Bearer {api_key}'
    if extra_headers:
        hdrs.update(extra_headers)
    return hdrs


def _media_type_and_data(url: str):
    """Split a ``data:`` URI into (media_type, base64_data). Returns None on
    a non-data URL."""
    if not url.startswith('data:'):
        return None
    try:
        header, data = url.split(',', 1)
        media_type = header[len('data:'):].split(';', 1)[0] or 'image/png'
        return media_type, data
    except (ValueError, IndexError) as e:
        logger.debug('[AnthropicOut] malformed data URI: %s', e)
        return None


def _image_block(image_url: dict) -> dict:
    """OpenAI image_url block → Anthropic image block."""
    url = (image_url or {}).get('url', '')
    parsed = _media_type_and_data(url)
    if parsed:
        media_type, data = parsed
        return {'type': 'image',
                'source': {'type': 'base64', 'media_type': media_type, 'data': data}}
    return {'type': 'image', 'source': {'type': 'url', 'url': url}}


def _convert_content_blocks(content) -> list:
    """OpenAI message content (str | list) → Anthropic content blocks."""
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        cc = block.get('cache_control')
        if btype == 'text':
            nb = {'type': 'text', 'text': block.get('text', '')}
            if cc:
                nb['cache_control'] = cc
            out.append(nb)
        elif btype == 'image_url':
            nb = _image_block(block.get('image_url', {}))
            if cc:
                nb['cache_control'] = cc
            out.append(nb)
    return out


def _convert_tools(tools) -> list:
    """OpenAI function tools → Anthropic tools (cache_control hoisted out of
    the nested ``function`` object)."""
    out = []
    for tool in tools or []:
        fn = tool.get('function') if isinstance(tool, dict) else None
        if not fn:
            continue
        at = {
            'name': fn.get('name', ''),
            'description': fn.get('description', ''),
            'input_schema': fn.get('parameters') or {'type': 'object', 'properties': {}},
        }
        if fn.get('cache_control'):
            at['cache_control'] = fn['cache_control']
        out.append(at)
    return out


def _assistant_blocks(msg: dict) -> list:
    """Assistant message → Anthropic content blocks (text + tool_use).

    ``reasoning_content`` is intentionally dropped: replaying a thinking
    block to the Anthropic API requires its opaque ``signature``, which we
    don't carry, and sending one without it returns HTTP 400.
    """
    blocks = _convert_content_blocks(msg.get('content') or '')
    for tc in msg.get('tool_calls') or []:
        fn = tc.get('function', {})
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[AnthropicOut] tool_call args JSON decode failed: %s', e)
            args = {}
        blocks.append({
            'type': 'tool_use',
            'id': tc.get('id') or '',
            'name': fn.get('name', ''),
            'input': args,
        })
    return blocks


def openai_body_to_anthropic(body: dict) -> dict:
    """Translate a built OpenAI Chat Completions body to an Anthropic body.

    Preserves model / max_tokens / stream / temperature / top_p / thinking /
    effort (build_body already produces Claude-correct thinking params).
    Hoists ``system`` out of the message list and merges consecutive
    tool-result messages into a single Anthropic user turn.
    """
    out = {
        'model': body.get('model', ''),
        'max_tokens': body.get('max_tokens', 4096),
    }
    if body.get('stream'):
        out['stream'] = True

    # Carry model-level params Anthropic accepts (build_body already pruned
    # the ones Opus 4.7+ rejects, e.g. temperature/top_p/top_k).
    for k in ('temperature', 'top_p', 'top_k', 'stop_sequences', 'thinking',
              'effort', 'tool_choice'):
        if k in body:
            out[k] = body[k]

    tools = _convert_tools(body.get('tools'))
    if tools:
        out['tools'] = tools

    system_blocks = []
    messages = []
    for msg in body.get('messages', []):
        role = msg.get('role')
        if role == 'system':
            system_blocks.extend(_convert_content_blocks(msg.get('content') or ''))
        elif role == 'tool':
            block = {
                'type': 'tool_result',
                'tool_use_id': msg.get('tool_call_id', ''),
                'content': msg.get('content') if isinstance(msg.get('content'), str)
                else (_convert_content_blocks(msg.get('content')) or ''),
            }
            # Merge into the preceding user turn when it's a tool_result batch.
            if (messages and messages[-1]['role'] == 'user'
                    and isinstance(messages[-1]['content'], list)
                    and messages[-1]['content']
                    and messages[-1]['content'][0].get('type') == 'tool_result'):
                messages[-1]['content'].append(block)
            else:
                messages.append({'role': 'user', 'content': [block]})
        elif role == 'assistant':
            blocks = _assistant_blocks(msg)
            if not blocks:
                blocks = [{'type': 'text', 'text': ''}]
            messages.append({'role': 'assistant', 'content': blocks})
        elif role == 'user':
            blocks = _convert_content_blocks(msg.get('content') or '')
            if not blocks:
                blocks = [{'type': 'text', 'text': ''}]
            messages.append({'role': 'user', 'content': blocks})

    if system_blocks:
        # A single plain-text system block can stay a string; keep the block
        # list when cache_control is present so caching survives.
        if len(system_blocks) == 1 and not system_blocks[0].get('cache_control'):
            out['system'] = system_blocks[0].get('text', '')
        else:
            out['system'] = system_blocks
    out['messages'] = messages
    return out


def _blocks_to_openai_message(content_blocks: list) -> dict:
    """Anthropic response content blocks → OpenAI assistant message dict."""
    text_parts = []
    thinking_parts = []
    tool_calls = []
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        if btype == 'text':
            text_parts.append(block.get('text', ''))
        elif btype == 'thinking':
            thinking_parts.append(block.get('thinking', ''))
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
        # content-block index → 'text' | 'tool_use' | 'thinking'
        self._block_types: dict = {}

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
                return [{'choices': [{'delta': {}}], 'usage': _convert_usage(usage)}]
            return []

        if etype == 'content_block_start':
            idx = ev.get('index', 0)
            block = ev.get('content_block') or {}
            btype = block.get('type')
            self._block_types[idx] = btype
            if btype == 'tool_use':
                return [{'choices': [{'delta': {'tool_calls': [{
                    'index': idx,
                    'id': block.get('id', ''),
                    'type': 'function',
                    'function': {'name': block.get('name', ''), 'arguments': ''},
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
                chunk['usage'] = _convert_usage(ev['usage'])
            return [chunk]

        if etype == 'message_stop':
            return ['[DONE]']

        if etype == 'error':
            return [{'error': ev.get('error') or {'message': 'anthropic stream error'}}]

        # ping / content_block_stop / unknown → no-op
        return []


__all__ = [
    'ANTHROPIC_VERSION',
    'anthropic_messages_url',
    'anthropic_headers',
    'openai_body_to_anthropic',
    'anthropic_response_to_openai',
    'AnthropicSSETranslator',
]
