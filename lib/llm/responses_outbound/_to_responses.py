"""lib/llm/responses_outbound/_to_responses.py — Chat Completions → Responses API.

Converts a canonical OpenAI Chat Completions request body into the OpenAI
**Responses API** shape (``POST /v1/responses``). Extracted from
``lib/oauth/codex.py:codex_translate_request`` (2026-07-31, epic
pt_b7a29ea7) and generalised from a Codex-only converter into the shared
boundary for EVERY Responses-speaking provider — the Codex-OAuth path is
now just one profile of it.

Conversion contract (the canonical body is the single IR — nothing else in
the app knows about Responses):

  * ``messages`` → ``input`` items: system → ``developer`` role; string
    content → ``input_text`` / ``output_text`` by role; ``image_url``
    blocks → ``input_image``; bare-tool_calls assistant messages →
    top-level ``function_call`` items; ``role='tool'`` →
    ``function_call_output`` keyed by ``call_id``.
  * ``tools[].function`` flattened to top-level fields (``strict: False``).
  * ``tool_choice`` function-dict flattened likewise; strings pass through.
  * Tool names truncated to 64 chars (the OpenAI function-name limit —
    applies to every Responses upstream, not just Codex).
  * ``store: False`` always — Tofu owns conversation state; server-side
    state (``previous_response_id``) is deliberately unused (DeepSeek
    doesn't support it; see memory ``responses_协议调研与_tofu_接缝图``).

Profile knobs (``RESPONSES_PROFILES``):

  * ``default`` — generic providers (DeepSeek …): keeps ``temperature`` /
    ``top_p``, maps ``max_tokens`` → ``max_output_tokens``, omits
    ``instructions`` / ``include``, reasoning effort without a summary
    channel (DeepSeek has reasoning but no summary).
  * ``codex``   — chatgpt.com/backend-api/codex: ``instructions: ''``,
    drops sampling params, ``include: ['reasoning.encrypted_content']``,
    ``reasoning.summary: 'auto'``.
"""

from __future__ import annotations

import json

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['RESPONSES_PROFILES', 'openai_body_to_responses']

#: Per-upstream dialect knobs. ``instructions=None`` → omit the field.
RESPONSES_PROFILES: dict = {
    'default': {
        'instructions': None,
        'drop_params': (),
        'map_max_tokens': True,
        'reasoning_summary': None,
        'include': (),
        'parallel_tool_calls': True,
    },
    'codex': {
        'instructions': '',
        'drop_params': ('temperature', 'top_p', 'max_tokens'),
        'map_max_tokens': False,
        'reasoning_summary': 'auto',
        'include': ('reasoning.encrypted_content',),
        'parallel_tool_calls': True,
    },
}

#: OpenAI function-name hard limit (all Responses upstreams share it).
_MAX_TOOL_NAME = 64


def _truncate_name(name: str, reverse: dict) -> str:
    """Clamp a tool name to the 64-char limit, recording the mapping so
    the response side can restore the model's echo (first-original wins,
    mirroring CLIProxyAPI recordRename semantics)."""
    if len(name) <= _MAX_TOOL_NAME:
        return name
    truncated = name[:_MAX_TOOL_NAME]
    reverse.setdefault(truncated, name)
    return truncated


def openai_body_to_responses(body: dict, *, profile: str = 'default',
                             stream: bool = False) -> tuple:
    """Translate a Chat Completions request body to Responses API format.

    Args:
        body: the canonical OpenAI-shaped body (mutated NOT — a new dict
            is built from an allowlist, so internal keys like ``_task_id``
            never leak onto the wire).
        profile: key of :data:`RESPONSES_PROFILES`.
        stream: value for the ``stream`` field.

    Returns:
        ``(responses_body, tool_name_reverse)`` — the Responses API request
        body, plus the per-request reverse map ``{truncated: original}``
        for tool names shortened to the 64-char function-name limit. The
        map must ride the response-side translator so echoed names are
        restored before tool dispatch (mirrors ``apply_claude_cloak``).
    """
    prof = RESPONSES_PROFILES.get(profile)
    if prof is None:
        logger.warning('[Responses] unknown profile %r — falling back to '
                       "'default'", profile)
        prof = RESPONSES_PROFILES['default']

    out: dict = {
        'model': body.get('model', ''),
        'store': False,
        'stream': stream,
    }
    if prof['instructions'] is not None:
        out['instructions'] = prof['instructions']
    if prof['parallel_tool_calls']:
        out['parallel_tool_calls'] = True

    # Sampling params — kept for generic providers, dropped for Codex.
    drop = set(prof['drop_params'])
    for k in ('temperature', 'top_p'):
        if k in body and k not in drop:
            out[k] = body[k]
    if 'max_tokens' in body and 'max_tokens' not in drop:
        if prof['map_max_tokens']:
            out['max_output_tokens'] = body['max_tokens']
        else:
            out['max_tokens'] = body['max_tokens']

    # Reasoning effort. ``summary`` only where the upstream has that
    # channel (Codex); DeepSeek has reasoning but no summary. Codex is
    # always a reasoning model — its profile defaults effort to 'medium'.
    effort = body.get('reasoning_effort')
    if profile == 'codex' and not effort:
        effort = 'medium'
    if effort or prof['reasoning_summary'] is not None:
        reasoning: dict = {}
        if effort:
            reasoning['effort'] = effort
        if prof['reasoning_summary'] is not None:
            reasoning['summary'] = prof['reasoning_summary']
        out['reasoning'] = reasoning

    if prof['include']:
        out['include'] = list(prof['include'])

    reverse: dict = {}  # truncated tool name → original (response side restores)

    out['input'] = _messages_to_input(body.get('messages') or [], reverse)

    tools = body.get('tools')
    if tools:
        out['tools'] = _convert_tools(tools, reverse)
    choice = body.get('tool_choice')
    if choice:
        out['tool_choice'] = _convert_tool_choice(choice, reverse)

    return out, reverse


def _messages_to_input(messages: list, reverse: dict) -> list:
    """OpenAI messages[] → Responses input[] items."""
    items: list = []
    for msg in messages:
        role = msg.get('role', '')
        content = msg.get('content')

        if role == 'tool':
            # Tool results join their call by call_id — the stable identity
            # (the stream-side item id is only an in-stream index).
            items.append({
                'type': 'function_call_output',
                'call_id': msg.get('tool_call_id', ''),
                'output': content if isinstance(content, str)
                else json.dumps(content if content is not None else '',
                                ensure_ascii=False),
            })
            continue

        api_role = 'developer' if role == 'system' else role
        content_parts: list = []
        if isinstance(content, str) and content:
            part_type = 'output_text' if role == 'assistant' else 'input_text'
            content_parts.append({'type': part_type, 'text': content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get('type')
                if btype == 'text':
                    part_type = ('output_text' if role == 'assistant'
                                 else 'input_text')
                    content_parts.append(
                        {'type': part_type, 'text': block.get('text', '')})
                elif btype == 'image_url' and role == 'user':
                    url = (block.get('image_url') or {}).get('url', '')
                    if url:
                        content_parts.append(
                            {'type': 'input_image', 'image_url': url})

        # An assistant message with no text payload emits no message item —
        # its tool calls below stand alone as function_call items.
        if role != 'assistant' or content_parts:
            items.append({'type': 'message', 'role': api_role,
                          'content': content_parts})

        # Assistant tool_calls → top-level function_call items (whether or
        # not the message carried text).
        for tc in msg.get('tool_calls') or []:
            if tc.get('type') != 'function':
                continue
            func = tc.get('function') or {}
            items.append({
                'type': 'function_call',
                'call_id': tc.get('id', ''),
                'name': _truncate_name(func.get('name', ''), reverse),
                'arguments': func.get('arguments', '{}'),
            })
    return items


def _convert_tools(tools: list, reverse: dict) -> list:
    """Chat-Completions tools[] → Responses tools[] (flattened function).

    Non-function tools pass through untouched (server-side built-ins like
    ``web_search`` have no chat-completions wrapper). Function fields are
    carried only when the source specifies them — the converter mirrors,
    it does not invent.
    """
    converted: list = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get('type') != 'function':
            converted.append(tool)
            continue
        func = tool.get('function') or {}
        t: dict = {'type': 'function',
                   'name': _truncate_name(func.get('name', ''), reverse)}
        if func.get('description'):
            t['description'] = func['description']
        if func.get('parameters'):
            t['parameters'] = func['parameters']
        if func.get('strict') is not None:
            t['strict'] = func['strict']
        converted.append(t)
    return converted


def _convert_tool_choice(choice, reverse: dict):
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict) and choice.get('type') == 'function':
        return {'type': 'function',
                'name': _truncate_name(
                    (choice.get('function') or {}).get('name', ''), reverse)}
    return choice
