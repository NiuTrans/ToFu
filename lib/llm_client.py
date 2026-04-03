# HOT_PATH — functions in this module are called per-request.
# Use logger.debug() for normal flow; logger.warning()/error() only for
# actual problems (retries, failures).  Avoid logger.info() to keep
# per-request log volume low.
"""lib/llm_client.py — Centralized LLM API client.

All LLM API request logic in one place:
  • Model-aware request body construction
  • Anthropic prompt caching (cache breakpoints)
  • Streaming & non-streaming chat completions
  • SSE parsing and response normalization

To adapt to a different API endpoint, only this file needs to change.
"""

import json
import os
import random
import re
import threading
import time
import uuid

import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError

import lib as _lib  # module ref for hot-reload (Settings changes take effect without restart)
from lib.log import get_logger

logger = get_logger(__name__)

# ── Retry config for transient API errors (streaming & non-streaming) ──
MAX_STREAM_RETRIES = 4          # retry up to 4 times (5 attempts total)
RETRY_BACKOFF_BASE = 3          # base backoff in seconds (exponential: 3, 6, 12, 24)
RETRY_BACKOFF_MAX  = 30         # cap backoff at 30s
RETRY_JITTER       = 1.0        # random ±1s jitter

def _retry_wait(attempt: int) -> float:
    """Exponential backoff with jitter: base 3s, 6s, 12s, 24s (capped at 30s) ±1s jitter."""
    base = min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_MAX)
    return base + random.uniform(-RETRY_JITTER, RETRY_JITTER)

def _abortable_sleep(seconds: float, abort_check=None, interval: float = 0.5):
    """Sleep for `seconds` but check abort_check every `interval`.
    Raises AbortedError if abort is detected during the sleep."""
    if not abort_check:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if abort_check():
            raise AbortedError('User aborted during retry backoff')
        remaining = deadline - time.monotonic()
        time.sleep(min(interval, max(0, remaining)))

class RetryableAPIError(Exception):
    """HTTP 5xx from the API gateway — worth retrying on the same key."""
    def __init__(self, msg='', status_code=0):
        super().__init__(msg)
        self.status_code = status_code

class RateLimitError(Exception):
    """HTTP 429 — should NOT retry on the same key; bubble up to dispatch layer to switch keys."""
    pass

class PermissionError_(Exception):
    """HTTP 401/403 — should NOT retry on the same key; bubble up to dispatch layer to switch keys."""
    pass

class ContentFilterError(Exception):
    """HTTP 450 — content policy violation. Should NOT fallback to another model (same content = same filter)."""
    pass

class AbortedError(Exception):
    """User requested abort — stop all retries immediately."""
    pass

class ModelLimitError(Exception):
    """HTTP 400 indicating max_tokens exceeds model's limit — auto-learnable.

    Carries the detected limit so callers can auto-correct and retry.
    """
    def __init__(self, message, model, detected_limit, requested_limit):
        super().__init__(message)
        self.model = model
        self.detected_limit = detected_limit
        self.requested_limit = requested_limit


class PromptTooLongError(Exception):
    """HTTP 400 indicating the prompt/context exceeds the model's input limit.

    Triggers reactive compaction in the orchestrator — the conversation is
    compressed and the LLM call is retried automatically.
    """
    pass


class StreamOnlyError(Exception):
    """HTTP 400 indicating the model only supports stream mode.

    Should NOT retry on the same model — bubble up to dispatch layer to
    exclude this model and try a different one.
    """
    def __init__(self, message, model):
        super().__init__(message)
        self.model = model

# Status codes that indicate a transient server-side issue (retry on same key)
# NOTE: 429 is NOT here — it gets RateLimitError which escapes to dispatch layer
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504, 529}

# Permission error status codes — escape immediately to dispatch layer
_PERMISSION_STATUS_CODES = {401, 403}

# Errors considered transient and worth retrying ON THE SAME KEY
_RETRYABLE = (ConnectionError, ChunkedEncodingError, BrokenPipeError,
              ConnectionResetError, RetryableAPIError)


def _is_stream_only_error(error_text: str) -> bool:
    """Detect if an API error indicates the model only supports streaming.

    Recognizes error messages like:
      - "This model only support stream mode"
      - "please enable the stream parameter"
    """
    _lower = error_text.lower()
    return ('only support stream' in _lower
            or 'only supports stream' in _lower
            or 'enable the stream parameter' in _lower
            or 'stream mode only' in _lower)


# ── Proxy bypass for internal endpoints ──
# Centralized in lib/proxy — see proxy.py for full docs.
# Short version: some corporate proxies silently drop long-lived SSE
# streams → BrokenPipeError.  proxies_for(url) returns {'no_proxy': '*'}
# for domains configured in Settings UI → Network → Proxy Bypass Domains
# (and/or the PROXY_BYPASS_DOMAINS env var).
# ══════════════════════════════════════════════════════════
#  Model Detection & Token Limits (extracted to lib/model_info.py)
#  Re-exported here for backward compatibility.
# ══════════════════════════════════════════════════════════
from lib.model_info import (  # noqa: F401
    _LEARNED_MODEL_LIMITS,
    _MODEL_MAX_OUTPUT,
    _clamp_max_tokens,
    _learn_model_limit,
    _load_learned_limits,
    _parse_token_limit_from_error,
    _qwen_max_output,
    is_claude,
    is_doubao,
    is_gemini,
    is_glm,
    is_gpt,
    is_longcat,
    is_minimax,
    is_qwen,
)
from lib.proxy import proxies_for as _proxies_for

# ══════════════════════════════════════════════════════════
#  Headers & URL  (single source of truth)
# ══════════════════════════════════════════════════════════

def _headers():
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {_lib.LLM_API_KEY}',
    }


def _chat_url():
    return f'{_lib.LLM_BASE_URL}/chat/completions'


# ══════════════════════════════════════════════════════════
#  Request Body Construction
# ══════════════════════════════════════════════════════════

# Fields that are valid in OpenAI-compatible chat/completions API messages.
# Everything else is frontend/display metadata and must be stripped to avoid
# bloating the request body (searchRounds alone can be >1 MB).
_API_MESSAGE_FIELDS = frozenset({
    'role', 'content', 'name',              # standard OpenAI
    'tool_calls', 'tool_call_id',           # tool use
    'reasoning_content',                    # thinking models (vendor extension)
    'cache_control',                        # Anthropic prompt caching
})


def _validate_image_blocks(messages: list) -> list:
    """Validate image_url blocks in messages, replacing invalid ones with text placeholders.

    Handles three cases:
      1. **Local ``/api/images/`` URLs** — The frontend stores images as server
         URLs (``/api/images/xxx.png``) in the DB.  On reload, ``_hydrateImageBase64``
         fetches them to populate ``base64``, but if hydration fails (e.g. proxy
         error, race condition), ``buildApiMessages`` falls back to the raw URL.
         The LLM API can't resolve relative URLs → HTTP 400 "Could not process
         image".  Fix: read the file from disk and convert to inline base64.
      2. **Corrupted base64** — truncated, wrong format, or broken encoding.
      3. **Unrecognized formats** — not PNG/JPEG/GIF/WebP.

    Mutates messages in-place for efficiency (called after _strip_non_api_fields
    which already returns copies).
    """
    import base64 as _b64

    _APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Minimum valid image sizes (bytes) — anything smaller is likely corrupt.
    _MIN_IMAGE_BYTES = 32
    # Known valid image magic bytes → MIME type
    _IMAGE_MAGICS = {
        b'\x89PNG':    'image/png',
        b'\xff\xd8':   'image/jpeg',
        b'GIF8':       'image/gif',
        b'RIFF':       'image/webp',  # RIFF....WEBP
    }
    # Extension → MIME fallback (when magic bytes not needed)
    _EXT_MIME = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    }
    _dropped = 0
    _resolved = 0

    for msg in messages:
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        new_blocks = []
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                new_blocks.append(block)
                continue
            url = block.get('image_url', {}).get('url', '')

            # ── Case 1: Local /api/images/ URL (un-hydrated) ──────────
            # Frontend failed to fetch+convert to base64; resolve from disk.
            # Also handles URLs with proxy prefix: /proxy/15000/api/images/...
            _local_prefix = '/api/images/'
            _local_idx = url.find(_local_prefix)
            if _local_idx >= 0:
                filename = url[_local_idx + len(_local_prefix):]
                # Strip query string / fragment if any
                filename = filename.split('?')[0].split('#')[0]
                filename = os.path.basename(filename)  # safety: no path traversal
                filepath = os.path.join(_APP_ROOT, 'uploads', 'images', filename)
                try:
                    with open(filepath, 'rb') as f:
                        raw = f.read()
                    # Detect MIME from magic bytes, fall back to extension
                    mime = None
                    for magic, mtype in _IMAGE_MAGICS.items():
                        if raw.startswith(magic):
                            mime = mtype
                            break
                    if not mime:
                        ext = os.path.splitext(filename)[1].lower()
                        mime = _EXT_MIME.get(ext, 'image/png')
                    b64 = _b64.b64encode(raw).decode('ascii')
                    block['image_url']['url'] = f'data:{mime};base64,{b64}'
                    _resolved += 1
                    logger.info('[ImageValidation] Resolved local image %s '
                                '(%d bytes) to inline base64', filename, len(raw))
                    new_blocks.append(block)
                except FileNotFoundError:
                    _dropped += 1
                    logger.warning('[ImageValidation] Local image file not found: %s',
                                   filepath)
                    new_blocks.append({
                        'type': 'text',
                        'text': f'[Image removed — file not found: {filename}]',
                    })
                except Exception as e:
                    _dropped += 1
                    logger.warning('[ImageValidation] Failed to read local image %s: %s',
                                   filepath, e)
                    new_blocks.append({
                        'type': 'text',
                        'text': '[Image removed — could not read image file]',
                    })
                continue

            # ── Case 2: Remote https:// URL — pass through (API can fetch) ──
            if url.startswith('http://') or url.startswith('https://'):
                new_blocks.append(block)
                continue

            # ── Case 3: data: URI — validate base64 content ──────────
            if url.startswith('data:'):
                try:
                    parts = url.split(',', 1)
                    if len(parts) != 2 or not parts[1]:
                        raise ValueError('Missing base64 data after comma')
                    b64_data = parts[1]
                    if len(b64_data) < 50:
                        raise ValueError(f'Base64 data too short ({len(b64_data)} chars)')
                    # Decode first chunk to verify format (don't decode entire image)
                    sample = _b64.b64decode(b64_data[:1364])  # 1364 b64 chars → 1023 bytes
                    if len(sample) < _MIN_IMAGE_BYTES:
                        raise ValueError(f'Decoded image too small ({len(sample)} bytes)')
                    is_known = any(sample.startswith(magic) for magic in _IMAGE_MAGICS)
                    if not is_known:
                        raise ValueError(f'Unrecognized image format (magic: {sample[:4].hex()})')
                    new_blocks.append(block)
                except Exception as e:
                    _dropped += 1
                    logger.warning('[ImageValidation] Dropping invalid base64 image: %s '
                                   '(data_len=%d)', e, len(url))
                    new_blocks.append({
                        'type': 'text',
                        'text': '[Image removed — invalid or corrupted image data]',
                    })
                continue

            # ── Case 4: Unknown URL scheme (relative paths, etc.) — drop ──
            if url:
                _dropped += 1
                logger.warning('[ImageValidation] Dropping unresolvable image URL: %.100s', url)
                new_blocks.append({
                    'type': 'text',
                    'text': '[Image removed — unresolvable image URL]',
                })
            else:
                _dropped += 1
                new_blocks.append({
                    'type': 'text',
                    'text': '[Image removed — empty image URL]',
                })

        msg['content'] = new_blocks

    if _resolved:
        logger.info('[ImageValidation] Resolved %d local image(s) to inline base64', _resolved)
    if _dropped:
        logger.info('[ImageValidation] Replaced %d invalid image block(s) with text placeholders',
                    _dropped)

    return messages


def _strip_non_api_fields(messages: list) -> list:
    """Return a new message list with only API-relevant fields.

    Strips frontend metadata (searchRounds, thinking, translatedContent,
    apiRounds, toolSummary, usage, timestamp, images, originalContent, …)
    that inflate the JSON body sent to the LLM gateway.

    Does NOT mutate the original messages — returns shallow copies.
    """
    cleaned = []
    stripped_keys = set()
    for msg in messages:
        clean = {}
        for k, v in msg.items():
            if k in _API_MESSAGE_FIELDS:
                clean[k] = v
            else:
                stripped_keys.add(k)
        cleaned.append(clean)
    if stripped_keys:
        logger.debug('[build_body] Stripped non-API fields from %d messages: %s',
                     len(messages), ', '.join(sorted(stripped_keys)))
    return cleaned


def build_body(model, messages, *, max_tokens=128000, temperature=1.0,
               thinking_enabled=False, preset='medium', effort=None,
               thinking_depth=None, tools=None,
               stream=True, extra=None, thinking_format=''):
    """Build a model-aware request body for /chat/completions.

    Handles provider-specific parameters automatically:
      • Claude:   thinking.type='adaptive', effort param, cache breakpoints
      • GLM:      thinking.type='enabled', temperature clamped to (0, 1)
      • Doubao:   thinking.type='enabled'/'disabled'
      • LongCat:  enable_thinking flag, temperature adjustment
      • Qwen:     enable_thinking flag, temperature adjustment
      • Others:   standard OpenAI-compatible body

    ``thinking_depth`` is the preferred depth param (medium/high/max).
    ``preset`` and ``effort`` are also used in the resolution chain
    (thinking_depth > effort > preset > 'medium').

    ``thinking_format`` allows per-provider override of how thinking
    parameters are sent to the API:
      • '' (empty):        auto-detect from model name (default)
      • 'enable_thinking':  {enable_thinking: bool} (LongCat, Qwen, Gemini)
      • 'thinking_type':    {thinking: {type: enabled/disabled}} (Doubao, Claude, GLM)
      • 'none':             no thinking parameters sent

    Raises:
        ValueError: if ``messages`` is empty or None.
    """
    if not messages:
        raise ValueError('build_body() requires a non-empty messages list')
    # thinking_depth takes priority; ignore model-name presets (opus/qwen etc.)
    # Values that are model names or non-depth flags — filter them out when
    # resolving the effective thinking depth/effort level.
    # ── thinking_depth='off' → force-disable thinking ──
    if thinking_depth == 'off':
        thinking_enabled = False
        logger.debug('build_body: model=%s thinking DISABLED (depth=off)', model)
    _MODEL_PRESETS = {'opus', 'qwen', 'gemini', 'minimax', 'doubao', 'off', 'low'}
    _effort = (thinking_depth if thinking_depth and thinking_depth != 'off'
               else None)
    _effort = (_effort
               or (effort if effort not in _MODEL_PRESETS else None)
               or (preset if preset not in _MODEL_PRESETS else None)
               or 'medium')
    logger.debug('build_body: model=%s effort=%s thinking_enabled=%s (thinking_depth=%s effort=%s preset=%s)',
                 model, _effort, thinking_enabled, thinking_depth, effort, preset)
    max_tokens = _clamp_max_tokens(model, max_tokens)   # ← per-model API limits

    # ── Strip non-API fields from messages ──────────────────────
    # Frontend messages carry display-only fields (searchRounds, thinking,
    # translatedContent, apiRounds, toolSummary, etc.) that are irrelevant
    # to the LLM API but can bloat the request body by >1 MB, causing
    # gateway BrokenPipe errors on large conversations.
    clean_messages = _strip_non_api_fields(messages)

    # ── Validate image blocks before sending to API ────────────
    # Claude/Bedrock returns HTTP 400 "Could not process image" on corrupt
    # base64.  Catch these early and replace with text placeholders so the
    # rest of the conversation is not lost.
    _validate_image_blocks(clean_messages)

    body = {
        'model': model,
        'messages': clean_messages,
        'max_tokens': max_tokens,
        'stream': stream,
    }

    # ── Provider-specific thinking / temperature parameters ──
    # When thinking_format is set (from provider config), it overrides
    # model-name-based auto-detection. This allows any endpoint to
    # configure how thinking parameters are sent, regardless of model name.
    _tf = thinking_format  # per-provider override (or '' for auto-detect)
    if _tf == 'enable_thinking' or (not _tf and (is_longcat(model) or is_qwen(model) or is_gemini(model))):
        # enable_thinking format: LongCat, Qwen, Gemini, or any provider that opts in
        body['enable_thinking'] = thinking_enabled
        if is_longcat(model):
            body['temperature'] = 1.0 if thinking_enabled else (temperature or 0.7)
        else:
            body['temperature'] = temperature or 0.7
    elif _tf == 'thinking_type' or (not _tf and is_doubao(model)):
        # thinking.type format: Doubao, or any provider that opts in
        if thinking_enabled:
            body['thinking'] = {'type': 'enabled'}
        else:
            body['thinking'] = {'type': 'disabled'}
        body['temperature'] = temperature or 0.7
    elif not _tf and is_glm(model):
        # GLM (Zhipu AI) uses thinking.type format:
        #   {"thinking": {"type": "enabled"}}  — enables thinking
        #   {"thinking": {"type": "disabled"}} — disables thinking
        # ⚠️ GLM-5 / GLM-5.1 have thinking ON by default — omitting the
        # param does NOT disable it.  Must explicitly send type=disabled.
        # Boolean values (thinking=false) cause HTTP 400 on Sankuai gateway.
        # Docs: https://docs.z.ai/guides/capabilities/thinking-mode
        # Temperature must be in open interval (0, 1) — 0 is rejected
        # with misleading error "输入不能为空" (code 1214).  Clamp to 0.01 minimum.
        if thinking_enabled:
            body['thinking'] = {'type': 'enabled'}
            body['temperature'] = 1.0
        else:
            body['thinking'] = {'type': 'disabled'}
            body['temperature'] = max(temperature, 0.01) if temperature else 0.7
    elif not _tf and is_minimax(model):
        # MiniMax M2.5/M2.7/M2.1: send reasoning_split=True so the API returns
        # thinking content in a separate reasoning_details field instead of
        # embedding <think> tags in content.  The <think> tag state machine
        # is kept as fallback for older API responses.
        body['temperature'] = temperature or 0.7
        body['reasoning_split'] = True
    elif not _tf and is_claude(model) and thinking_enabled:
        # Anthropic / Claude style (adaptive thinking)
        body['thinking'] = {'type': 'adaptive'}
        body['temperature'] = 1.0
        if _effort and _effort != 'medium':
            body['effort'] = _effort  # Claude API parameter is literally 'effort'
    elif _tf == 'none':
        # Provider explicitly opts out of thinking parameters
        body['temperature'] = temperature or 0.7
    else:
        body['temperature'] = temperature

    if tools:
        body['tools'] = tools

    if extra:
        body.update(extra)

    # ── Claude 4.6+ prefill guard ──────────────────────────────
    # Claude 4.6 removed assistant message prefill support entirely.
    # If messages end with role=assistant, the API returns HTTP 400:
    #   "This model does not support assistant message prefill.
    #    The conversation must end with a user message."
    # This can happen when:
    #   (a) Frontend buildApiMessages produces trailing assistant
    #       (e.g. after orphan recovery or conversation state corruption)
    #   (b) Premature-close recovery or compaction edge cases
    # Defence-in-depth: strip trailing assistant messages here in the
    # transport layer so ALL callers are protected.
    if is_claude(model):
        _strip_trailing_assistant_for_claude(body['messages'], model)

    return body


def _strip_trailing_assistant_for_claude(messages: list, model: str = ''):
    """Remove trailing assistant messages that would trigger Claude 4.6 prefill error.

    Claude 4.6+ requires conversations to end with a user or tool message.
    If the last message is role=assistant (with or without content), we either:
      - Remove it if it's empty/placeholder (no real content)
      - Wrap it as a user message if it has real content (rare edge case)

    Mutates messages in place.
    """
    if not messages:
        return

    stripped = 0
    while messages and messages[-1].get('role') == 'assistant':
        last = messages[-1]
        content = last.get('content', '') or ''
        has_tool_calls = bool(last.get('tool_calls'))

        # Assistant with tool_calls should never be the last message — this
        # indicates missing tool results.  Remove it to avoid a confusing error.
        if has_tool_calls:
            logger.warning('[Claude-prefill] Stripping trailing assistant with '
                           'tool_calls (orphaned — no tool results follow). '
                           'model=%s content=%dchars tool_calls=%d',
                           model, len(content), len(last.get('tool_calls', [])))
            messages.pop()
            stripped += 1
            continue

        # Empty assistant → just remove it
        if not content.strip():
            logger.debug('[Claude-prefill] Stripping trailing empty assistant. model=%s', model)
            messages.pop()
            stripped += 1
            continue

        # Non-empty assistant with real content → convert to user context
        # This is a last-resort safety net; normal flows shouldn't produce this.
        logger.warning('[Claude-prefill] Converting trailing assistant to user context '
                       '(content=%dchars). model=%s — this indicates a message '
                       'ordering bug upstream.', len(content), model)
        messages[-1] = {
            'role': 'user',
            'content': f'[Your previous response for context]:\n{content}',
        }
        stripped += 1
        break  # Now it's a user message; stop

    if stripped:
        logger.info('[Claude-prefill] Fixed %d trailing assistant message(s). '
                    'model=%s final_last_role=%s',
                    stripped, model,
                    messages[-1].get('role') if messages else 'empty')


# ══════════════════════════════════════════════════════════
#  Anthropic Prompt Caching  (cache breakpoints)
# ══════════════════════════════════════════════════════════

def add_cache_breakpoints(body, log_prefix=''):
    """Add Anthropic-style ephemeral cache breakpoints.

    Annotates up to 4 content blocks with cache_control for:
      1. System messages (1-2 breakpoints for static/dynamic blocks)
      2. Last tool definition
      3. Second-to-last message (any role) — the conversation tail

    The tail breakpoint advances each round as new messages are appended,
    ensuring the entire growing prefix is covered by the cache.  This is
    critical for multi-round tool conversations: previously, the breakpoint
    was on the "second-to-last user message" which in tool-heavy convos
    was always the original user message (index ~1), so the growing
    assistant/tool prefix was never cached.

    Only applied to Claude/Anthropic models.  OpenAI & Qwen use
    automatic server-side prefix caching — no client annotation needed.

    IMPORTANT: This function is called every round in a multi-round
    orchestrator loop with the same body/messages references.  We
    first STRIP all previous cache_control annotations to avoid
    exceeding Anthropic's 4-block limit (which causes HTTP 400).
    """
    model = body.get('model', '')
    if not is_claude(model):
        return

    messages = body.get('messages', [])

    # ── Phase 0: Strip ALL existing cache_control from messages & tools ──
    # This prevents stale markers from prior rounds from accumulating.
    for i, msg in enumerate(messages):
        content = msg.get('content')
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and 'cache_control' in block:
                    content[j] = {k: v for k, v in block.items() if k != 'cache_control'}
    tools = body.get('tools')
    if tools:
        for t_idx, tool in enumerate(tools):
            fn = tool.get('function')
            if fn and 'cache_control' in fn:
                tools[t_idx] = {**tool,
                                'function': {k: v for k, v in fn.items() if k != 'cache_control'}}

    bp = 0

    # ── Cache system messages ──
    # When the system message has multiple text blocks (static + dynamic),
    # place cache breakpoints on EACH block independently.  This way the
    # static guidance (FRC, tool usage, output efficiency) that never changes
    # gets its own cache entry, and the dynamic context (project/skills)
    # gets a separate one.  Up to 2 breakpoints used here.
    for i, msg in enumerate(messages):
        if msg.get('role') != 'system' or bp >= 4:
            continue
        content = msg.get('content', '')
        if isinstance(content, str) and content.strip():
            messages[i] = {**msg, 'content': [
                {'type': 'text', 'text': content,
                 'cache_control': {'type': 'ephemeral'}}
            ]}
            bp += 1
        elif isinstance(content, list) and content:
            # Cache each text block independently (up to bp limit)
            for blk_idx, blk in enumerate(content):
                if bp >= 4:
                    break
                if isinstance(blk, dict) and blk.get('type') == 'text':
                    content[blk_idx] = {**blk, 'cache_control': {'type': 'ephemeral'}}
                    bp += 1

    # ── Cache last tool definition ──
    tools = body.get('tools')
    if tools and bp < 4:
        fn = tools[-1].get('function')
        if fn:
            # Deep copy to avoid mutating module-level tool constants
            tools[-1] = {**tools[-1],
                         'function': {**fn, 'cache_control': {'type': 'ephemeral'}}}
            bp += 1

    # ── Cache conversation tail: second-to-last message ──
    # In multi-round tool conversations, the conversation grows as:
    #   [system, user, asst+tc, tool, asst+tc, tool, ...]
    # The original code cached the "second-to-last user message", but in
    # tool-heavy conversations there's usually only ONE user message, so
    # this breakpoint never moved — the growing assistant/tool prefix was
    # never covered by a cache breakpoint.
    #
    # Fix: cache the second-to-last message (any role), which advances as
    # the conversation grows.  This ensures the entire prefix up to the
    # penultimate message is cached, and only the final message (new content)
    # is uncached.  This matches Claude Code's approach of placing a
    # breakpoint near the tail of the conversation to maximize cache hits.
    if len(messages) >= 3 and bp < 4:
        # Second-to-last message — the breakpoint advances each round
        idx = len(messages) - 2
        msg = messages[idx]
        content = msg.get('content', '')
        if isinstance(content, str) and content:
            messages[idx] = {**msg, 'content': [
                {'type': 'text', 'text': content,
                 'cache_control': {'type': 'ephemeral'}}
            ]}
            bp += 1
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                content[-1] = {**last, 'cache_control': {'type': 'ephemeral'}}
                bp += 1

    if bp > 0 and log_prefix:
        logger.debug('%s Cache: %d breakpoint(s)', log_prefix, bp)


# ══════════════════════════════════════════════════════════
#  Non-Streaming Chat Completion
# ══════════════════════════════════════════════════════════

def chat(messages, model=None, *, max_tokens=4096, temperature=0,
         thinking_enabled=False, preset='low', effort=None, extra=None,
         timeout=120, log_prefix='', api_key=None, base_url=None,
         extra_headers=None, max_retries=None, _limit_retry=False,
         thinking_format=''):
    """Non-streaming chat completion.

    Args:
        api_key:      optional API key override (from dispatch slot).
        base_url:     optional base URL override (from dispatch slot's provider).
                      If None/empty, uses the global LLM_BASE_URL.
        extra_headers: optional dict of additional headers (from provider config).
        max_retries:  override retry count (default: MAX_STREAM_RETRIES).

    Returns:
        (content_text: str, usage_dict: dict)

    Raises:
        RateLimitError:      on HTTP 429 (rate limit).
        PermissionError_:    on HTTP 401/403 (auth failure).
        ContentFilterError:  on HTTP 450 (content policy).
        RetryableAPIError:   on HTTP 5xx after all retries exhausted.
        Exception:           on other non-retryable HTTP errors.
    """
    model = model or _lib.LLM_MODEL
    url = f'{base_url.rstrip("/")}/chat/completions' if base_url else _chat_url()

    body = build_body(
        model, messages,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking_enabled=thinking_enabled,
        preset=effort or preset,
        stream=False,
        extra=extra,
        thinking_format=thinking_format,
    )

    if log_prefix:
        logger.debug('%s POST %s model=%s msgs=%d', log_prefix, url, model, len(messages))

    retries = MAX_STREAM_RETRIES if max_retries is None else max_retries
    last_err = None
    resp = None
    resp_trace = ''
    trace_id = ''  # defensive default in case loop body raises before assignment
    for attempt in range(1 + retries):
        try:
            trace_id = uuid.uuid4().hex
            hdrs = _headers()
            hdrs['M-TraceId'] = trace_id
            if api_key:
                hdrs['Authorization'] = f'Bearer {api_key}'
            if extra_headers:
                hdrs.update(extra_headers)
            if log_prefix:
                logger.debug('%s M-TraceId=%s', log_prefix, trace_id)
            resp = requests.post(url, headers=hdrs, json=body,
                                 timeout=(30, timeout),
                                 proxies=_proxies_for(url))
            # ★ Log response M-TraceId
            resp_trace = resp.headers.get('M-TraceId', '')
            if resp_trace and resp_trace != trace_id:
                logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)
            if resp.status_code != 200:
                err_msg = f'API HTTP {resp.status_code}: {resp.text[:500]}'
                if resp.status_code == 429:
                    raise RateLimitError(err_msg)
                if resp.status_code == 450:
                    logger.warning('%s Content filter triggered (HTTP 450)', log_prefix)
                    raise ContentFilterError(err_msg)
                if resp.status_code in _PERMISSION_STATUS_CODES:
                    logger.warning('%s Permission error (HTTP %d)', log_prefix, resp.status_code)
                    raise PermissionError_(err_msg)
                # ★ Detect and auto-learn max_tokens limit errors (HTTP 400)
                if resp.status_code == 400 and not _limit_retry:
                    _detected_limit = _parse_token_limit_from_error(err_msg, model)
                    if _detected_limit:
                        _learn_model_limit(model, _detected_limit)
                        logger.warning('%s ⚙️ max_tokens %d exceeds %s limit %d — '
                                      'auto-learned and retrying with corrected value',
                                      log_prefix, max_tokens, model, _detected_limit)
                        # Recursive retry with corrected max_tokens (one level only)
                        content_r, usage_r = chat(
                            messages, model, max_tokens=_detected_limit,
                            temperature=temperature,
                            thinking_enabled=thinking_enabled,
                            preset=preset, effort=effort, extra=extra,
                            timeout=timeout, log_prefix=log_prefix,
                            api_key=api_key, base_url=base_url,
                            extra_headers=extra_headers,
                            max_retries=max_retries, _limit_retry=True)
                        usage_r['_model_limit_learned'] = {
                            'model': model,
                            'old_limit': max_tokens,
                            'new_limit': _detected_limit,
                        }
                        return content_r, usage_r
                # ★ Detect prompt-too-long errors for reactive compaction (non-streaming)
                if resp.status_code == 400:
                    _ptl_patterns = [
                        'prompt is too long', 'context length exceeded',
                        'maximum context length', 'prompt too long',
                        'input too long', 'exceeds the model',
                        'token limit', 'context_length_exceeded',
                        'max_prompt_tokens', 'request too large',
                    ]
                    _err_lower = err_msg.lower()
                    if any(p in _err_lower for p in _ptl_patterns):
                        logger.warning('%s Prompt too long detected (HTTP 400): %s',
                                       log_prefix, err_msg[:300])
                        raise PromptTooLongError(err_msg)
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    raise RetryableAPIError(err_msg, status_code=resp.status_code)
                # ★ Detect stream-only model errors (HTTP 400)
                if resp.status_code == 400 and _is_stream_only_error(err_msg):
                    logger.warning('%s Model %s only supports stream mode — '
                                  'non-streaming request rejected', log_prefix, model)
                    raise StreamOnlyError(err_msg, model)
                logger.error('%s Non-retryable API error (HTTP %d): %s', log_prefix, resp.status_code, err_msg[:300])
                raise Exception(err_msg)
            break   # success
        except (RateLimitError, PermissionError_, ContentFilterError, PromptTooLongError, StreamOnlyError):
            raise   # escape to dispatch layer immediately — don't retry same key
        except _RETRYABLE as e:
            last_err = e
            if attempt < retries:
                wait = _retry_wait(attempt)
                logger.warning('%s ⚠ Attempt %d/%d failed '
                      '(%s), retrying in %.1fs…', log_prefix, attempt + 1, 1 + retries, type(e).__name__, wait, exc_info=True)
                time.sleep(wait)
            else:
                logger.error('%s ✖ All %d attempts failed (non-stream).', log_prefix, 1 + retries, exc_info=True)
                raise
    # Safety net: resp should always be assigned because the loop either
    # breaks on success or re-raises on the final failed attempt.
    assert resp is not None, 'BUG: retry loop exited without assigning resp'

    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as e:
        raise Exception(
            f'API returned invalid JSON (HTTP {resp.status_code}): '
            f'{resp.text[:500]}'
        ) from e
    choices = data.get('choices') or []
    if not choices:
        raise Exception(
            f'API returned no choices: {json.dumps(data)[:500]}'
        )
    msg = choices[0].get('message') or {}
    content = msg.get('content', '')
    usage = data.get('usage', {})

    # ★ Strip MiniMax-style <think>...</think> tags from non-streaming responses
    # MiniMax M2.5 embeds reasoning in <think> tags inside content;
    # the streaming path has a state-machine for this, but non-streaming
    # returns the raw content — strip it here for all callers.
    if content and '<think>' in content:
        raw_len = len(content)
        content = re.sub(r'<think>[\s\S]*?</think>\s*', '', content).strip()
        # Also handle unclosed <think> tag (model started thinking but
        # response was cut off — discard everything from <think> onward)
        if '<think>' in content:
            content = content[:content.index('<think>')].strip()
        if len(content) != raw_len:
            logger.debug('[chat] Stripped <think> tags from non-stream response '
                        '(%d → %d chars)', raw_len, len(content))

    # ★ Inject tool_calls into usage so callers (timer, etc.) can access them
    #   without changing the (content, usage) return signature.
    _tool_calls = msg.get('tool_calls')
    if _tool_calls:
        usage['_tool_calls'] = _tool_calls

    # ★ Inject trace_id into usage so it flows to api_rounds → frontend
    usage['trace_id'] = trace_id
    if resp_trace and resp_trace != trace_id:
        usage['resp_trace_id'] = resp_trace

    if log_prefix:
        tokens = usage.get('total_tokens', 0)
        logger.debug('%s Done: %d chars, ~%d tokens', log_prefix, len(content), tokens)

    return content, usage


# ══════════════════════════════════════════════════════════
#  Streaming Chat Completion
# ══════════════════════════════════════════════════════════

def stream_chat(body, *, on_thinking=None, on_content=None,
                on_tool_call_ready=None,
                abort_check=None, log_prefix='', api_key=None, base_url=None,
                extra_headers=None):
    """Streaming chat completion with callbacks.

    Automatically retries on transient connection errors (BrokenPipe,
    ConnectionReset, ChunkedEncodingError) up to MAX_STREAM_RETRIES times.

    Args:
        body:        Complete request body (use build_body() to construct).
        on_thinking: callback(text) — called for each thinking/reasoning delta.
        on_content:  callback(text) — called for each content delta.
        on_tool_call_ready: callback(tool_call_dict) — called when each tool
            call's arguments are fully accumulated during streaming. Enables
            parallel tool execution while the model still streams.
        abort_check: callable() → bool — if True, stop reading the stream.
        log_prefix:  string prefix for log messages.
        api_key:     optional API key override (from dispatch slot).
        base_url:    optional base URL override (from dispatch slot's provider).
                     If None/empty, uses the global LLM_BASE_URL.
        extra_headers: optional dict of additional headers (from provider config).

    Returns:
        (assistant_msg, finish_reason, usage)

    The assistant_msg is a dict with:
        role, content, reasoning_content (optional), tool_calls (optional)

    Raises:
        RateLimitError:      on HTTP 429 (rate limit).
        PermissionError_:    on HTTP 401/403 (auth failure).
        ContentFilterError:  on HTTP 450 (content policy).
        AbortedError:        if abort_check() returns True.
        RetryableAPIError:   on HTTP 5xx after all retries exhausted.
        Exception:           on other non-retryable HTTP errors.
    """
    last_err = None
    _limit_learned = None  # ★ Track auto-learned token limits for notification
    for attempt in range(1 + MAX_STREAM_RETRIES):
        try:
            msg, finish_reason, usage = _stream_chat_once(
                body, on_thinking=on_thinking, on_content=on_content,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=log_prefix,
                attempt=attempt, api_key=api_key, base_url=base_url,
                extra_headers=extra_headers)
            # ★ Inject learned limit info into usage for upstream notification
            if _limit_learned:
                if usage is None:
                    usage = {}
                usage['_model_limit_learned'] = _limit_learned
            return msg, finish_reason, usage
        except (RateLimitError, PermissionError_, AbortedError, ContentFilterError, PromptTooLongError):
            raise   # escape to dispatch layer immediately — don't retry same key
        except ModelLimitError as e:
            # ★ Auto-learned token limit — clamp body and retry immediately (no backoff)
            body['max_tokens'] = e.detected_limit
            _limit_learned = {
                'model': e.model,
                'old_limit': e.requested_limit,
                'new_limit': e.detected_limit,
            }
            logger.warning('%s ⚙️ Auto-learned max_tokens for %s: %d → %d, retrying…',
                          log_prefix, e.model, e.requested_limit, e.detected_limit)
            continue  # retry with corrected body — doesn't count as transient error
        except _RETRYABLE as e:
            last_err = e
            if attempt < MAX_STREAM_RETRIES:
                # ★ Check abort BEFORE sleeping — fail fast if user already hit stop
                if abort_check and abort_check():
                    logger.debug('%s ✋ Abort detected before retry sleep, stopping.', log_prefix)
                    raise AbortedError('User aborted before retry')
                wait = _retry_wait(attempt)
                logger.warning('%s ⚠ Transient error (attempt %d): '
                      '%s: %s — retrying in %.1fs …', log_prefix, attempt + 1, type(e).__name__, e, wait, exc_info=True)
                _abortable_sleep(wait, abort_check)  # ★ abort-aware sleep
            else:
                logger.error('%s ✖ All %d attempts failed.', log_prefix, 1 + MAX_STREAM_RETRIES, exc_info=True)
                raise
    raise last_err  # should not reach here, but just in case


def _stream_chat_once(body, *, on_thinking=None, on_content=None,
                      on_tool_call_ready=None,
                      abort_check=None, log_prefix='', attempt=0,
                      api_key=None, base_url=None, extra_headers=None):
    """Single attempt at a streaming chat completion (inner impl).

    Args:
        on_tool_call_ready: callback(tool_call_dict) — called as soon as a
            tool call's arguments are fully accumulated (before the stream
            finishes).  Inspired by Claude Code's StreamingToolExecutor
            which starts executing read-only tools while the model is still
            generating subsequent tool calls.
    """
    add_cache_breakpoints(body, log_prefix)
    url = f'{base_url.rstrip("/")}/chat/completions' if base_url else _chat_url()

    attempt_tag = f' (attempt {attempt+1})' if attempt > 0 else ''
    if log_prefix:
        logger.debug('%s%s POST %s '
              'msgs=%d '
              'tools=%s', log_prefix, attempt_tag, url, len(body.get('messages', [])), 'yes' if body.get('tools') else 'no')

    trace_id = uuid.uuid4().hex
    hdrs = _headers()
    hdrs['M-TraceId'] = trace_id
    if api_key:
        hdrs['Authorization'] = f'Bearer {api_key}'
    if extra_headers:
        hdrs.update(extra_headers)

    if log_prefix:
        logger.debug('%s M-TraceId=%s', log_prefix, trace_id)

    _stream_t0 = time.time()  # ★ Track stream wall-clock time for gateway diagnostics

    # Streaming timeout: (connect=1200s, read=3000s) — intentionally large
    # because SSE streams for complex tasks can run for many minutes;
    # abort_check handles early termination on the client side.
    resp = requests.post(url, headers=hdrs, json=body,
                         stream=True, timeout=(1200, 3000),
                         proxies=_proxies_for(url))

    try:
        # ★ Log response M-TraceId (server may return a different one)
        # Moved inside try/finally so resp.close() is guaranteed even if
        # header access or status-code handling raises unexpectedly.
        resp_trace = resp.headers.get('M-TraceId', '')
        if resp_trace and resp_trace != trace_id:
            logger.debug('%s resp M-TraceId=%s', log_prefix, resp_trace)

        if resp.status_code != 200:
            err_msg = f'API HTTP {resp.status_code}: {resp.text[:800]}'
            if resp.status_code == 429:
                raise RateLimitError(err_msg)
            if resp.status_code == 450:
                logger.warning('%s Content filter triggered (HTTP 450)', log_prefix)
                raise ContentFilterError(err_msg)
            if resp.status_code in _PERMISSION_STATUS_CODES:
                logger.warning('%s Permission error (HTTP %d)', log_prefix, resp.status_code)
                raise PermissionError_(err_msg)
            # ★ Detect and auto-learn max_tokens limit errors (HTTP 400)
            if resp.status_code == 400:
                _detected_limit = _parse_token_limit_from_error(
                    err_msg, body.get('model', ''))
                if _detected_limit:
                    _learn_model_limit(body.get('model', ''), _detected_limit)
                    raise ModelLimitError(
                        err_msg, body.get('model', ''),
                        _detected_limit, body.get('max_tokens', 0))
                # ★ Detect prompt-too-long errors for reactive compaction
                _ptl_patterns = [
                    'prompt is too long', 'context length exceeded',
                    'maximum context length', 'prompt too long',
                    'input too long', 'exceeds the model',
                    'token limit', 'context_length_exceeded',
                    'max_prompt_tokens', 'request too large',
                ]
                _err_lower = err_msg.lower()
                if any(p in _err_lower for p in _ptl_patterns):
                    logger.warning('%s Prompt too long detected (HTTP 400): %s',
                                   log_prefix, err_msg[:300])
                    raise PromptTooLongError(err_msg)
            if resp.status_code in _RETRYABLE_STATUS_CODES:
                raise RetryableAPIError(err_msg, status_code=resp.status_code)
            logger.error('%s Non-retryable API error (HTTP %d): %s', log_prefix, resp.status_code, err_msg[:300])
            raise Exception(err_msg)

        resp.encoding = 'utf-8'
        content = ''
        thinking_text = ''
        tool_calls_acc = {}
        finish_reason = 'stop'
        usage = None
        _saw_done = False       # ★ track whether [DONE] was actually received
        _saw_finish_reason = False  # ★ track whether server sent finish_reason
        _chunk_count = 0           # ★ count SSE chunks for diagnostics
        _aborted_by_client = False # ★ track if WE aborted

        # ★ MiniMax <think> tag state machine
        # MiniMax M2.5/M2.7 puts reasoning in <think>...</think> tags inside content
        _mm_mode = is_minimax(body.get('model', ''))
        _mm_in_think = False       # currently inside <think> block
        _mm_buf = ''               # buffer for partial tag detection
        _consecutive_parse_errors = 0  # ★ guard against streams of unparseable data
        _MAX_CONSECUTIVE_PARSE_ERRORS = 10

        for line in resp.iter_lines(decode_unicode=True):
            if abort_check and abort_check():
                _aborted_by_client = True
                logger.debug('%s Stream aborted by client (abort_check=True) after %d chunks', log_prefix, _chunk_count)
                break
            if not line or not line.startswith('data: '):
                continue
            data_str = line[6:].strip()
            if data_str == '[DONE]':
                _saw_done = True
                break
            _chunk_count += 1
            try:
                chunk = json.loads(data_str)
            except Exception as e:
                _consecutive_parse_errors += 1
                logger.warning('%s ⚠ SSE chunk JSON parse error (chunk #%d, consecutive=%d) model=%s trace=%s: %s — %s',
                               log_prefix, _chunk_count, _consecutive_parse_errors,
                               body.get('model', '?'), trace_id, data_str[:200], e, exc_info=True)
                if _consecutive_parse_errors >= _MAX_CONSECUTIVE_PARSE_ERRORS:
                    raise RetryableAPIError(
                        f'{_consecutive_parse_errors} consecutive SSE parse errors — stream appears corrupt') from e
                continue

            _consecutive_parse_errors = 0  # reset on successful parse

            if 'error' in chunk:
                eo = chunk['error']
                err_text = eo.get('message', '') if isinstance(eo, dict) else str(eo)
                # ★ Check if this is a max_tokens limit error — auto-learn & retry
                _model_id = body.get('model', '')
                _detected_limit = _parse_token_limit_from_error(err_text, _model_id)
                if _detected_limit:
                    _learn_model_limit(_model_id, _detected_limit)
                    raise ModelLimitError(
                        f'SSE error (token limit): {err_text}',
                        _model_id, _detected_limit,
                        body.get('max_tokens', 0))
                # ★ Check if this is a prompt-too-long error — trigger compaction
                _err_lower = err_text.lower()
                _ptl_patterns_sse = [
                    'prompt is too long', 'context length exceeded',
                    'maximum context length', 'prompt too long',
                    'input too long', 'exceeds the model',
                    'token limit', 'context_length_exceeded',
                    'max_prompt_tokens', 'request too large',
                ]
                if any(p in _err_lower for p in _ptl_patterns_sse):
                    logger.warning('%s Prompt too long detected in SSE error: %s',
                                   log_prefix, err_text[:300])
                    raise PromptTooLongError(f'SSE error: {err_text}')
                raise Exception(f'SSE error: {err_text}')

            if chunk.get('usage'):
                usage = chunk['usage']

            choices = chunk.get('choices', [])
            if not choices:
                continue

            delta = choices[0].get('delta', {})
            fr = choices[0].get('finish_reason')
            if fr:
                finish_reason = fr
                _saw_finish_reason = True
            if choices[0].get('usage'):
                usage = choices[0]['usage']

            # ── Thinking / reasoning delta ──
            # Check standard thinking fields first
            td = (delta.get('thinking')
                  or delta.get('reasoning_content')
                  or (delta.get('content', '')
                      if delta.get('role') == 'thinking' else ''))
            # ★ MiniMax reasoning_details: list of dicts with 'text' keys
            # Returned when reasoning_split=True is sent in the request.
            if not td and delta.get('reasoning_details'):
                rd_parts = delta['reasoning_details']
                if isinstance(rd_parts, list):
                    td = ''.join(d.get('text', '') for d in rd_parts if isinstance(d, dict))
            if td:
                thinking_text += td
                if on_thinking:
                    on_thinking(td)

            # ── Content delta ──
            if 'content' in delta and delta.get('role') != 'thinking':
                cd = delta['content'] or ''
                if cd:
                    if _mm_mode:
                        # ★ MiniMax: parse <think>...</think> tags from content stream
                        _mm_buf += cd
                        while _mm_buf:
                            if _mm_in_think:
                                end_idx = _mm_buf.find('</think>')
                                if end_idx == -1:
                                    # Still inside <think>, emit all as thinking
                                    thinking_text += _mm_buf
                                    if on_thinking:
                                        on_thinking(_mm_buf)
                                    _mm_buf = ''
                                else:
                                    # Found </think>, emit up to it as thinking
                                    think_part = _mm_buf[:end_idx]
                                    if think_part:
                                        thinking_text += think_part
                                        if on_thinking:
                                            on_thinking(think_part)
                                    _mm_buf = _mm_buf[end_idx + len('</think>'):]
                                    _mm_in_think = False
                            else:
                                start_idx = _mm_buf.find('<think>')
                                if start_idx == -1:
                                    # No <think> tag, check for partial tag at end
                                    # Buffer up to 7 chars in case of partial '<think>'
                                    if len(_mm_buf) > 7 and '<' in _mm_buf[-7:]:
                                        safe = _mm_buf[:_mm_buf.rfind('<', max(0, len(_mm_buf)-7))]
                                        if safe:
                                            content += safe
                                            if on_content:
                                                on_content(safe)
                                        _mm_buf = _mm_buf[len(safe):]
                                    else:
                                        content += _mm_buf
                                        if on_content:
                                            on_content(_mm_buf)
                                        _mm_buf = ''
                                else:
                                    # Found <think>, emit content before it
                                    before = _mm_buf[:start_idx]
                                    if before:
                                        content += before
                                        if on_content:
                                            on_content(before)
                                    _mm_buf = _mm_buf[start_idx + len('<think>'):]
                                    _mm_in_think = True
                    else:
                        content += cd
                        if on_content:
                            on_content(cd)

            # ── Tool call deltas ──
            if 'tool_calls' in delta:
                for tc in delta['tool_calls']:
                    idx = tc.get('index', 0)
                    # ★ Streaming tool execution: when a NEW index appears,
                    # the previous index's arguments are complete → fire callback
                    if idx not in tool_calls_acc:
                        if on_tool_call_ready and idx > 0 and (idx - 1) in tool_calls_acc:
                            _prev = tool_calls_acc[idx - 1]
                            try:
                                on_tool_call_ready(_prev)
                            except Exception as _tcr_err:
                                logger.debug('%s on_tool_call_ready callback error: %s',
                                             log_prefix, _tcr_err)
                        tool_calls_acc[idx] = {
                            'id': '', 'type': 'function',
                            'function': {'name': '', 'arguments': ''},
                        }
                    if tc.get('id'):
                        tool_calls_acc[idx]['id'] = tc['id']
                    # ── Gemini thought_signature: preserve extra_content ──
                    # Gemini 3.x returns thought_signature inside
                    # extra_content.google.thought_signature on tool_call
                    # deltas.  This MUST be sent back in subsequent requests
                    # or the API returns HTTP 400.  Capture it verbatim.
                    if tc.get('extra_content'):
                        tool_calls_acc[idx]['extra_content'] = tc['extra_content']
                    fn = tc.get('function', {})
                    if fn.get('name'):
                        tool_calls_acc[idx]['function']['name'] += fn['name']
                    if fn.get('arguments') is not None:
                        tool_calls_acc[idx]['function']['arguments'] += \
                            fn.get('arguments', '')

        # ★ Streaming tool execution: fire callback for the LAST tool call
        # when stream ends (it never gets a "next index" to trigger it).
        if on_tool_call_ready and tool_calls_acc:
            _last_idx = max(tool_calls_acc.keys())
            _last_tc = tool_calls_acc[_last_idx]
            if _last_tc['function']['name']:
                try:
                    on_tool_call_ready(_last_tc)
                except Exception as _tcr_err:
                    logger.debug('%s on_tool_call_ready callback error (final): %s',
                                 log_prefix, _tcr_err)

        # ★ Flush MiniMax buffer
        if _mm_mode and _mm_buf:
            if _mm_in_think:
                thinking_text += _mm_buf
                if on_thinking:
                    on_thinking(_mm_buf)
            else:
                content += _mm_buf
                if on_content:
                    on_content(_mm_buf)
            _mm_buf = ''

        # ★ MiniMax: normalize reasoning_tokens into usage
        if _mm_mode and usage and thinking_text:
            ctd = usage.get('completion_tokens_details', {})
            rt = ctd.get('reasoning_tokens', 0)
            if rt > 0 and 'reasoning_tokens' not in usage:
                usage['reasoning_tokens'] = rt

        # ── Filter out spurious tool calls (e.g. Anthropic internal artifacts) ──
        # Some proxies leak internal streaming tokens like 'antml:thinking',
        # 'antml:invocation' etc. as tool_call deltas.  These are NOT real tool
        # calls — filter them out before building the assistant message.
        # Also filter phantom tool calls: valid function name but completely
        # empty arguments — these appear when the model starts a tool_call
        # slot, never populates arguments, then emits the real call at the
        # next index.  Keeping them wastes a round (the tool gets called with
        # {} and returns an error back to the model).
        _INTERNAL_TOOL_PREFIXES = ('antml:', 'anthropic.', '__')
        if tool_calls_acc:
            _filtered = {}
            # Build a set of function names that have non-empty arguments,
            # so we can identify phantom duplicates (same name, empty args).
            _names_with_args = {
                tc['function']['name']
                for tc in tool_calls_acc.values()
                if (tc['function'].get('arguments', '') or '').strip()
            }
            for idx, tc_entry in tool_calls_acc.items():
                fn_name = tc_entry['function']['name']
                fn_args_str = tc_entry['function'].get('arguments', '')
                if any(fn_name.startswith(p) for p in _INTERNAL_TOOL_PREFIXES):
                    logger.debug('%s Filtering spurious internal tool call: %s (likely proxy artifact)',
                                 log_prefix, fn_name)
                    continue
                # Phantom tool call: has a name but completely empty arguments,
                # AND another tool call with the SAME name has real arguments.
                # This avoids dropping legitimate no-arg tools (e.g.
                # check_error_logs) that happen to appear alongside others.
                if not fn_args_str.strip() and fn_name in _names_with_args:
                    logger.warning(
                        '%s Filtering phantom tool call: %s (tc_id=%s) has '
                        'empty arguments — duplicate of another %s call with '
                        'real args',
                        log_prefix, fn_name, tc_entry.get('id', '?')[:12],
                        fn_name,
                    )
                    continue
                _filtered[idx] = tc_entry
            tool_calls_acc = _filtered

        # ── Build assistant message ──
        msg = {'role': 'assistant'}
        if thinking_text:
            msg['reasoning_content'] = thinking_text
        if tool_calls_acc:
            msg['tool_calls'] = [tool_calls_acc[i]
                                 for i in sorted(tool_calls_acc.keys())]
            if content:
                msg['content'] = content
        else:
            msg['content'] = content

        # ── Log cache info ──
        cache_info = ''
        if usage:
            cw = usage.get('cache_write_tokens',
                           usage.get('cache_creation_input_tokens', 0))
            cr = usage.get('cache_read_tokens',
                           usage.get('cache_read_input_tokens', 0))
            if cw or cr:
                cache_info = f' cache_w={cw} cache_r={cr}'
                if cr > 0:
                    inp = usage.get('prompt_tokens',
                                    usage.get('input_tokens', 0))
                    cache_info += f' (saved ~{round(cr / max(inp, 1) * 100)}%)'

        if log_prefix:
            logger.debug('%s Done: finish=%s '
                  'content=%d think=%d%s', log_prefix, finish_reason, len(content), len(thinking_text), cache_info)

        # ★ Compute stream elapsed time for all diagnostics below
        _stream_elapsed_s = time.time() - _stream_t0

        # ★ DIAGNOSTIC: detect premature stream close (silent completion bug)
        if not _aborted_by_client and not _saw_done:
            logger.warning(
                '%s ⚠ PREMATURE STREAM CLOSE: '
                'Server never sent [DONE] marker. '
                'M-TraceId=%s resp_trace=%s elapsed=%.1fs '
                'chunks_received=%d '
                'saw_finish_reason=%s finish_reason=%s '
                'content_len=%d thinking_len=%d '
                'tool_calls=%d model=%s url=%s',
                log_prefix, trace_id, resp_trace or 'none',
                _stream_elapsed_s, _chunk_count,
                _saw_finish_reason, finish_reason,
                len(content), len(thinking_text),
                len(tool_calls_acc), body.get('model', '?'), url)
        elif not _aborted_by_client and not _saw_finish_reason and _chunk_count > 0:
            logger.warning(
                '%s ⚠ MISSING FINISH_REASON: '
                '[DONE] received but no finish_reason chunk. '
                'M-TraceId=%s elapsed=%.1fs '
                'Using default=%s chunks=%d '
                'content_len=%d model=%s',
                log_prefix, trace_id, _stream_elapsed_s,
                finish_reason, _chunk_count,
                len(content), body.get('model', '?'))

        # ★ DIAGNOSTIC: detect suspiciously empty responses
        if (not _aborted_by_client and finish_reason == 'stop'
                and not content and not tool_calls_acc
                and _chunk_count > 0):
            logger.warning(
                '%s ⚠ EMPTY STOP RESPONSE: '
                'finish=stop but no content and no tool_calls. '
                'M-TraceId=%s elapsed=%.1fs '
                'chunks=%d thinking_len=%d model=%s',
                log_prefix, trace_id, _stream_elapsed_s,
                _chunk_count, len(thinking_text),
                body.get('model', '?'))

        # ★ Inject trace_id and timing into usage so it flows to api_rounds → frontend
        if usage is None:
            usage = {}
        usage['trace_id'] = trace_id
        if resp_trace and resp_trace != trace_id:
            usage['resp_trace_id'] = resp_trace
        usage['stream_elapsed_ms'] = round(_stream_elapsed_s * 1000)

        # ★ Inject stream anomaly flags so orchestrator/stream_handler can
        #   detect abnormal terminations (proxy cut, missing finish, empty stop)
        #   and take corrective action (retry or expose to user).
        _has_anomaly = False
        if not _aborted_by_client and not _saw_done:
            usage['_missing_done'] = True
            _has_anomaly = True
        if not _aborted_by_client and not _saw_finish_reason and _chunk_count > 0:
            usage['_missing_finish_reason'] = True
            _has_anomaly = True
        if (not _aborted_by_client and finish_reason == 'stop'
                and not content and not tool_calls_acc
                and _chunk_count > 0):
            usage['_empty_stop'] = True
            _has_anomaly = True
        if _has_anomaly:
            usage['_stream_anomaly'] = True

        return msg, finish_reason, usage
    finally:
        resp.close()
