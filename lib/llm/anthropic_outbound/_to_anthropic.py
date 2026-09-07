# HOT_PATH
"""Outbound direction: OpenAI Chat Completions body → Anthropic Messages body.

Contains the block/tool/message converters and the top-level
``openai_body_to_anthropic`` entry point. Depends only on stdlib + the
``lib.llm.body`` image sniffer; no dependency on the inbound module.
"""

import copy
import json

from lib.log import get_logger

logger = get_logger(__name__)

# Keep small native catalogs flat: discovery costs more than their schemas.
# This mirrors the local gateway threshold without importing lib.tools on this
# protocol hot path (which would initialize the full tool registry).
_TOOL_SEARCH_MIN_FUNCTIONS = 12
_COMPACTION_MIN_TRIGGER_TOKENS = 50_000
_COMPACTION_INSTRUCTIONS = (
    'Summarize the conversation for seamless continuation in a future '
    'context window. Preserve the user goal, constraints, exact file paths, '
    'identifiers, code decisions, tool findings, completed work, failures, '
    'and concrete next steps. Wrap the summary in <summary></summary>. Do not '
    'call tools while summarizing; return summary text only.'
)


def _media_type_and_data(url: str):
    """Split a ``data:`` URI into (media_type, base64_data). Returns None on
    a non-data URL.

    Boundary reconciliation: this is the LAST transform before the Anthropic
    Messages API, which HARD-REJECTS a request whose declared ``media_type``
    disagrees with the actual image bytes (HTTP 400 "messages.N.content.0.
    image.source.base64: The image was specified using the image/jpeg media
    type, but the image does not appear to be in that format."). Every upstream
    feeder (``build_body`` → ``_validate_image_blocks``, both dispatch swap-path
    branches) already reconciles, but to make the strict path self-consistent
    REGARDLESS of which seam fed it, we sniff the real MIME from the base64
    prefix here and prefer it over the declared header. Best-effort: if the
    bytes can't be decoded/recognized, keep the declared header unchanged.
    """
    if not url.startswith('data:'):
        return None
    try:
        header, data = url.split(',', 1)
        media_type = header[len('data:'):].split(';', 1)[0] or 'image/png'
        try:
            import base64 as _b64

            from lib.llm.body import sniff_image_mime
            _sample = _b64.b64decode(data[:1364])
            _true = sniff_image_mime(_sample)
            if _true and _true != media_type:
                logger.warning(
                    '[AnthropicOut] Reconciled image media type %r → %r at the '
                    'Anthropic boundary (bytes sniffed as %s)',
                    media_type, _true, _true)
                media_type = _true
        except Exception as _se:
            logger.debug('[AnthropicOut] media-type sniff skipped: %s', _se)
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


def _block_cache_control(content):
    """Extract a ``cache_control`` marker carried on OpenAI-shape message
    content, if any.

    ``add_cache_breakpoints`` places the conversation-tail (and system)
    breakpoint by wrapping ``str`` content into ``[{'type':'text','text':…,
    'cache_control':…}]`` or by stamping the LAST block of an existing list.
    When such a message is a ``tool`` role (→ Anthropic ``tool_result``) or an
    ``assistant`` (→ ``tool_use``/``thinking`` blocks), the marker must be
    hoisted onto the emitted Anthropic block ITSELF — Anthropic reads
    ``cache_control`` on the tool_result/tool_use block, not on a text block
    nested inside it. Returns the marker dict or ``None``.
    """
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('cache_control'):
                return block['cache_control']
    return None


def _strip_cc(blocks) -> list:
    """Return ``blocks`` with any ``cache_control`` removed from every block.

    Used when hoisting a message-level marker onto the OUTER Anthropic block
    (tool_result / last assistant block): the inner text block that carried the
    marker in the OpenAI shape must be stripped, else the request ends up with
    TWO markers for one message and blows past Anthropic's 4-marker ceiling.
    """
    out = []
    for b in blocks or []:
        if isinstance(b, dict) and 'cache_control' in b:
            out.append({k: v for k, v in b.items() if k != 'cache_control'})
        else:
            out.append(b)
    return out


def _convert_content_blocks(content) -> list:
    """OpenAI message content (str | list) → Anthropic content blocks.

    NEVER emits an empty/whitespace-only text block: Anthropic (and every
    strict Anthropic-shape validator — Moonshot's "text content is empty",
    the 2026-07-31 kimi-k3 incident class) hard-400s the whole request on
    one. A phantom block carries zero information, so dropping it AT THE
    EMISSION SEAM keeps this boundary self-consistent regardless of which
    caller fed it — this is the Anthropic-side counterpart of
    ``lib.llm_sanitize._strip_empty_text_blocks`` (which guards the OpenAI
    wire inside build_body; callers that bypass build_body get the same
    guarantee here).
    """
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get('type')
        cc = block.get('cache_control')
        if btype == 'text':
            text = block.get('text') or ''
            if not text.strip():
                continue  # phantom empty text block — hard-400 upstream
            nb = {'type': 'text', 'text': text}
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
    """Assistant message → Anthropic content blocks (thinking + text + tool_use).

    When the message carries both ``reasoning_content`` and its opaque
    ``thinking_signature`` (captured from the stream and round-tripped on
    Continue), the thinking block is re-emitted FIRST so the Messages API
    can verify tool-use continuity. A ``reasoning_content`` without a
    signature is dropped: sending a thinking block without its signature
    returns HTTP 400, so a lossy "fresh reasoning" continuation is better.
    """
    _opaque = msg.get('_anthropic_content_blocks')
    if isinstance(_opaque, list) and _opaque:
        # This turn contains Anthropic-owned replay state (for example hosted
        # server-tool blocks, redacted thinking, or a compaction summary).
        # Replaying a regenerated text/tool_use projection is insufficient;
        # the complete original content array and its order are protocol state.
        blocks = [copy.deepcopy(block) for block in _opaque
                  if isinstance(block, dict)]
        _cc = _block_cache_control(msg.get('content'))
        if _cc and blocks:
            blocks = _strip_cc(blocks)
            blocks[-1] = {**blocks[-1], 'cache_control': _cc}
        return blocks

    blocks = []
    th_text = msg.get('reasoning_content') or ''
    th_sig = msg.get('thinking_signature') or ''
    if th_text and th_sig:
        blocks.append({'type': 'thinking', 'thinking': th_text, 'signature': th_sig})
    blocks.extend(_convert_content_blocks(msg.get('content') or ''))
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
    # Carry a cache breakpoint onto the LAST emitted block. When an
    #   assistant turn is the conversation tail (e.g. a prefill-resume round),
    #   add_cache_breakpoints stamps the marker on its content; it must ride
    #   the last Anthropic block (tool_use / text) so the tail breakpoint is
    #   not lost — same class of bug as the tool_result path above.
    _cc = _block_cache_control(msg.get('content'))
    if _cc and blocks:
        # Strip any inner marker first so exactly ONE lands on the last block
        # (a doubled marker would overflow Anthropic's 4-marker ceiling).
        blocks = _strip_cc(blocks)
        blocks[-1] = {**blocks[-1], 'cache_control': _cc}
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

    # Public Anthropic API server-side compaction. The exact route capability
    # and beta header are owned by ``llm._sse_core``; this translator only
    # projects the already-authorized provider-neutral working-set threshold
    # into Anthropic's wire shape. Claude Code subscription OAuth never sets
    # this private latch.
    if body.get('_anthropic_native_compaction'):
        try:
            _trigger = max(
                _COMPACTION_MIN_TRIGGER_TOKENS,
                int(body.get('_working_set_tokens') or 0),
            )
        except (TypeError, ValueError, OverflowError):
            _trigger = 0
        if _trigger > 0:
            out['context_management'] = {
                'edits': [{
                    'type': 'compact_20260112',
                    'trigger': {
                        'type': 'input_tokens',
                        'value': _trigger,
                    },
                    # Anthropic documents that tool-equipped requests can
                    # otherwise call a tool during the internal summary and
                    # return a null compaction block. This complete custom
                    # prompt replaces the default and explicitly forbids it.
                    'instructions': _COMPACTION_INSTRUCTIONS,
                }],
            }

    tools = _convert_tools(body.get('tools'))
    if tools and body.get('_anthropic_native_tool_search'):
        policy = body.get('_tool_discovery_policy_by_name') or {}
        pins = {str(name) for name in (
            body.get('_frontend_selected_tool_names') or ())}
        deferred_tools = []
        for tool in tools:
            name = str(tool.get('name') or '')
            if (name not in pins
                    and policy.get(name, 'eager') != 'eager'):
                deferred_tools.append(tool)
        # Anthropic's server-hosted BM25 tool must remain non-deferred.  It
        # searches the deferred definitions without changing the prompt-cache
        # prefix and returns ordinary tool_use blocks for our existing parser.
        # For a small directory, or an all-eager one, the complete native
        # schemas are cheaper and avoid an unnecessary discovery round.
        if (len(tools) >= _TOOL_SEARCH_MIN_FUNCTIONS
                and deferred_tools):
            for tool in deferred_tools:
                tool['defer_loading'] = True
            tools.insert(0, {
                'type': 'tool_search_tool_bm25_20251119',
                'name': 'tool_search_tool_bm25',
            })
    if tools:
        out['tools'] = tools

    system_blocks = []
    messages = []
    for msg in body.get('messages', []):
        role = msg.get('role')
        if role == 'system':
            system_blocks.extend(_convert_content_blocks(msg.get('content') or ''))
        elif role == 'tool':
            _tool_content = (msg.get('content') if isinstance(msg.get('content'), str)
                             else (_convert_content_blocks(msg.get('content')) or ''))
            # Carry the cache breakpoint onto the tool_result block itself.
            #   add_cache_breakpoints stamps the tail marker on the (wrapped)
            #   tool message content; Anthropic reads cache_control on the
            #   tool_result block, NOT on a text block nested inside it. Without
            #   this hoist the tail breakpoint is silently lost on every
            #   tool-ending round → cache_read pins at the system floor. Strip
            #   the inner marker first so we place EXACTLY ONE (else 2 markers
            #   for one message overflows Anthropic's 4-marker ceiling).
            _cc = _block_cache_control(msg.get('content'))
            if _cc and isinstance(_tool_content, list):
                _tool_content = _strip_cc(_tool_content)
            block = {
                'type': 'tool_result',
                'tool_use_id': msg.get('tool_call_id', ''),
                'content': _tool_content,
            }
            if _cc:
                block['cache_control'] = _cc
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
                # Ghost assistant (no signed thinking, no content, no
                # tool_calls) — a caller bypassed build_body's
                # _drop_empty_assistant_messages. A phantom '' text block
                # hard-400s upstream; a placeholder keeps the turn AND the
                # user/assistant alternation (dropping the message would
                # make two same-role turns adjacent — also a 400).
                logger.warning('[AnthropicOut] Ghost assistant reached the '
                               'boundary with no blocks — emitting placeholder '
                               '(upstream healer bypassed)')
                blocks = [{'type': 'text', 'text': '[empty response]'}]
            messages.append({'role': 'assistant', 'content': blocks})
        elif role == 'user':
            blocks = _convert_content_blocks(msg.get('content') or '')
            if not blocks:
                # Same class on the user side; placeholder vocabulary matches
                # _fix_empty_user_messages on the OpenAI wire.
                logger.warning('[AnthropicOut] User message reached the '
                               'boundary with no content — emitting placeholder '
                               '(upstream healer bypassed)')
                blocks = [{'type': 'text', 'text': '[empty message]'}]
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
