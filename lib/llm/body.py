# HOT_PATH
"""LLM request body construction — model-aware payload builder.

Public API:
  - build_body(model, messages, ...) → dict
  - _validate_image_blocks(messages) → messages
  - _downscale_oversized_images(messages, model) → None
  - _strip_trailing_assistant_for_claude(messages, model) → None
"""

import base64 as _b64
import io
import os

import lib as _lib
from lib.llm_sanitize import (
    _fix_empty_user_messages,
    _fix_orphaned_tool_calls,
    _merge_consecutive_same_role,
    _sanitize_messages,
    _strip_non_api_fields,
)
from lib.log import get_logger
from lib.model_info import (
    _clamp_max_tokens,
    is_claude,
    is_claude_opus_47,
    is_doubao,
    is_ernie,
    is_gemini,
    is_glm,
    is_kimi,
    is_longcat,
    is_minimax,
    is_qwen,
    model_supports_vision,
)

logger = get_logger(__name__)

# ── Claude image dimension limits ──
_CLAUDE_SINGLE_IMAGE_MAX_PX = 7999
_CLAUDE_MANY_IMAGE_MAX_PX = 1999
_CLAUDE_MANY_IMAGE_THRESHOLD = 5


def _validate_image_blocks(messages: list) -> list:
    """Validate image_url blocks, replacing invalid ones with text placeholders.

    Handles:
      1. Local ``/api/images/`` URLs — resolve from disk to inline base64
      2. Corrupted base64
      3. Unrecognized formats
      4. Unknown URL schemes

    Mutates messages in-place.
    """
    _APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _MIN_IMAGE_BYTES = 32
    _IMAGE_MAGICS = {
        b'\x89PNG':    'image/png',
        b'\xff\xd8':   'image/jpeg',
        b'GIF8':       'image/gif',
        b'RIFF':       'image/webp',
    }
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

            # Case 1: Local /api/images/ URL
            _local_prefix = '/api/images/'
            _local_idx = url.find(_local_prefix)
            if _local_idx >= 0:
                filename = url[_local_idx + len(_local_prefix):]
                filename = filename.split('?')[0].split('#')[0]
                filename = os.path.basename(filename)
                filepath = os.path.join(_APP_ROOT, 'uploads', 'images', filename)
                try:
                    with open(filepath, 'rb') as f:
                        raw = f.read()
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
                    logger.warning('[ImageValidation] Local image file not found: %s', filepath)
                    new_blocks.append({
                        'type': 'text',
                        'text': f'[Image removed — file not found: {filename}]',
                    })
                except Exception as e:
                    _dropped += 1
                    logger.warning('[ImageValidation] Failed to read local image %s: %s', filepath, e)
                    new_blocks.append({
                        'type': 'text',
                        'text': '[Image removed — could not read image file]',
                    })
                continue

            # Case 2: Remote https:// URL — pass through
            if url.startswith('http://') or url.startswith('https://'):
                new_blocks.append(block)
                continue

            # Case 3: data: URI — validate base64 content
            if url.startswith('data:'):
                try:
                    parts = url.split(',', 1)
                    if len(parts) != 2 or not parts[1]:
                        raise ValueError('Missing base64 data after comma')
                    b64_data = parts[1]
                    if len(b64_data) < 50:
                        raise ValueError(f'Base64 data too short ({len(b64_data)} chars)')
                    sample = _b64.b64decode(b64_data[:1364])
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

            # Case 4: Unknown URL scheme
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
        logger.info('[ImageValidation] Replaced %d invalid image block(s) with text placeholders', _dropped)

    return messages


def _downscale_oversized_images(messages: list, model: str) -> None:
    """Downscale base64 images exceeding Claude's dimension limits.

    Only applies to Claude models. Requires Pillow.
    Mutates messages in-place.
    """
    if not is_claude(model):
        return

    try:
        from PIL import Image
    except ImportError:
        logger.debug('[ImageDownscale] Pillow not installed — skipping image size check')
        return

    total_images = 0
    for msg in messages:
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'image_url':
                total_images += 1

    max_px = (_CLAUDE_MANY_IMAGE_MAX_PX if total_images >= _CLAUDE_MANY_IMAGE_THRESHOLD
              else _CLAUDE_SINGLE_IMAGE_MAX_PX)

    _resized = 0
    for msg in messages:
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                continue
            url = block.get('image_url', {}).get('url', '')
            if not url.startswith('data:'):
                continue

            parts = url.split(',', 1)
            if len(parts) != 2:
                continue
            header, b64_data = parts
            try:
                raw_bytes = _b64.b64decode(b64_data)
                img = Image.open(io.BytesIO(raw_bytes))
                w, h = img.size

                if max(w, h) <= max_px:
                    continue

                scale = max_px / max(w, h)
                new_w = int(w * scale)
                new_h = int(h * scale)

                img = img.resize((new_w, new_h), Image.LANCZOS)
                img.info.pop('icc_profile', None)
                img.info.pop('exif', None)

                if img.mode == 'RGBA':
                    out_format = 'PNG'
                    mime = 'image/png'
                else:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    out_format = 'JPEG'
                    mime = 'image/jpeg'

                buf = io.BytesIO()
                img.save(buf, format=out_format, quality=85, optimize=True)
                new_b64 = _b64.b64encode(buf.getvalue()).decode('ascii')

                block['image_url']['url'] = f'data:{mime};base64,{new_b64}'
                _resized += 1
                logger.info('[ImageDownscale] Resized %dx%d → %dx%d '
                            '(max_px=%d, %d→%d bytes, images=%d)',
                            w, h, new_w, new_h, max_px,
                            len(raw_bytes), buf.tell(), total_images)

            except Exception as e:
                logger.warning('[ImageDownscale] Failed to check/resize image: %s', e)

    if _resized:
        logger.info('[ImageDownscale] Resized %d oversized image(s) for model %s '
                    '(limit=%dpx, total_images=%d)', _resized, model, max_px, total_images)


def _strip_trailing_assistant_for_claude(messages: list, model: str = ''):
    """Remove trailing assistant messages that trigger Claude's prefill error.

    Mutates messages in place.
    """
    if not messages:
        return

    stripped = 0
    while messages and messages[-1].get('role') == 'assistant':
        last = messages[-1]
        content = last.get('content', '') or ''
        has_tool_calls = bool(last.get('tool_calls'))

        if has_tool_calls:
            logger.warning('[Claude-prefill] Stripping trailing assistant with '
                           'tool_calls (orphaned). model=%s content=%dchars tool_calls=%d',
                           model, len(content), len(last.get('tool_calls', [])))
            messages.pop()
            stripped += 1
            continue

        if not content.strip():
            logger.debug('[Claude-prefill] Stripping trailing empty assistant. model=%s', model)
            messages.pop()
            stripped += 1
            continue

        logger.warning('[Claude-prefill] Converting trailing assistant to user context '
                       '(content=%dchars). model=%s', len(content), model)
        messages[-1] = {
            'role': 'user',
            'content': f'[Your previous response for context]:\n{content}',
        }
        stripped += 1
        break

    if stripped:
        logger.info('[Claude-prefill] Fixed %d trailing assistant message(s). '
                    'model=%s final_last_role=%s',
                    stripped, model,
                    messages[-1].get('role') if messages else 'empty')


# Dummy signature value recognized by Gemini to skip validation.
# Used when tool_calls originate from a non-Gemini model (cross-model fallback).
_GEMINI_SKIP_SIGNATURE = 'skip_thought_signature_validator'


def _inject_gemini_thought_signatures(messages: list, model: str) -> None:
    """Inject dummy thought_signature on tool_calls that lack one (Gemini 3.x).

    When conversation history contains tool_calls produced by a non-Gemini
    model (e.g. Claude) and dispatch falls back to Gemini, the API rejects
    with HTTP 400 because those tool_calls have no thought_signature.

    Per Google's docs, setting the signature to
    ``'skip_thought_signature_validator'`` skips validation for injected/
    cross-model tool_calls.

    Mutates messages in-place. Only runs for Gemini models.
    """
    if not is_gemini(model):
        return

    _patched = 0
    for msg in messages:
        if msg.get('role') != 'assistant':
            continue
        tool_calls = msg.get('tool_calls')
        if not tool_calls:
            continue
        # Gemini requires thought_signature on the first tool_call per step.
        first_tc = tool_calls[0]
        ec = first_tc.get('extra_content')
        if ec and isinstance(ec, dict):
            google = ec.get('google')
            if isinstance(google, dict) and google.get('thought_signature'):
                continue  # already has a real signature
        # Missing — inject the skip sentinel
        if not isinstance(ec, dict):
            ec = {}
            first_tc['extra_content'] = ec
        google = ec.get('google')
        if not isinstance(google, dict):
            google = {}
            ec['google'] = google
        google['thought_signature'] = _GEMINI_SKIP_SIGNATURE
        _patched += 1

    if _patched:
        logger.info('[build_body] Injected dummy thought_signature on %d '
                    'assistant tool_call message(s) for Gemini cross-model '
                    'compatibility', _patched)


def build_body(model, messages, *, max_tokens=128000, temperature=1.0,
               thinking_enabled=False, preset='medium', effort=None,
               thinking_depth=None, tools=None, response_format=None,
               stream=True, extra=None, thinking_format='',
               provider_id=''):
    """Build a model-aware request body for /chat/completions.

    Handles provider-specific parameters automatically:
      - Claude:   thinking.type='adaptive', effort param, cache breakpoints
      - Kimi:     thinking.type='enabled'/'disabled', fixed temp
      - GLM:      thinking.type='enabled', temperature clamped to (0, 1)
      - Doubao:   thinking.type='enabled'/'disabled'
      - LongCat:  enable_thinking flag, temperature adjustment
      - Qwen:     enable_thinking flag, temperature adjustment
      - ERNIE:    enable_thinking flag
      - sglang / vLLM self-hosted (``thinking_format='chat_template_kwargs'``):
                  passes ``chat_template_kwargs.enable_thinking`` —
                  the Jinja-template gate used by sglang's OpenAI shim
                  for Qwen3 / GLM / DeepSeek dual-mode models. Top-level
                  ``enable_thinking`` is silently ignored by sglang.
      - Others:   standard OpenAI-compatible body

    Raises:
        ValueError: if ``messages`` is empty or None.
    """
    if not messages:
        raise ValueError('build_body() requires a non-empty messages list')

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
    max_tokens = _clamp_max_tokens(model, max_tokens)

    clean_messages = _strip_non_api_fields(messages)

    _pid = provider_id.lower() if provider_id else ''
    if _pid == 'sankuai' or (not _pid and 'sankuai' in _lib.LLM_BASE_URL):
        _sanitize_messages(clean_messages)

    clean_messages = _fix_orphaned_tool_calls(clean_messages)
    clean_messages = _merge_consecutive_same_role(clean_messages)

    _validate_image_blocks(clean_messages)
    _downscale_oversized_images(clean_messages, model)

    # Strip images for non-vision models
    if not model_supports_vision(model):
        _stripped_img_count = 0
        for msg in clean_messages:
            content = msg.get('content')
            if not isinstance(content, list):
                continue
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'image_url':
                    _stripped_img_count += 1
                else:
                    new_blocks.append(block)
            if len(new_blocks) < len(content):
                if len(new_blocks) == 1 and new_blocks[0].get('type') == 'text':
                    msg['content'] = new_blocks[0]['text']
                elif len(new_blocks) == 0:
                    msg['content'] = ''
                else:
                    msg['content'] = new_blocks
        if _stripped_img_count:
            logger.warning('[build_body] Stripped %d image(s) from messages — '
                          'model %s does not support vision', _stripped_img_count, model)
            _last_user = None
            for msg in reversed(clean_messages):
                if msg.get('role') == 'user':
                    _last_user = msg
                    break
            if _last_user:
                notice = ('[System notice: %d image(s) were attached but removed '
                          'because model %s does not support vision/image inputs. '
                          'Please inform the user and suggest switching to a '
                          'vision-capable model.]' % (_stripped_img_count, model))
                content = _last_user.get('content', '')
                if isinstance(content, list):
                    content.append({'type': 'text', 'text': notice})
                elif isinstance(content, str):
                    _last_user['content'] = content + '\n\n' + notice

    _fix_empty_user_messages(clean_messages)
    _inject_gemini_thought_signatures(clean_messages, model)

    body = {
        'model': model,
        'messages': clean_messages,
        'max_tokens': max_tokens,
        'stream': stream,
    }

    # Provider-specific thinking / temperature parameters
    _tf = thinking_format
    # ── Plugin dialect (tofu.providers entry point) ──
    # Resolves ONLY for custom keys a plugin registered; built-in formats
    # return None and fall through to the unchanged ladder below.  Placed as
    # the first branch so all downstream post-processing (tools / extra /
    # Claude guards) still runs identically.
    _plugin_dialect = None
    if _tf:
        from lib.llm_dispatch.provider_registry import get_dialect
        _plugin_dialect = get_dialect(_tf)
    if _plugin_dialect is not None:
        try:
            _plugin_dialect.apply_build(
                body, thinking_enabled=thinking_enabled,
                temperature=temperature, model=model, effort=_effort)
        except Exception as e:
            logger.error('[build_body] plugin dialect %r apply_build failed: %s',
                         _tf, e, exc_info=True)
    elif _tf == 'none':
        # Engine declares it does not honor any thinking flag (e.g.
        # DeepSeek-Reasoner where thinking is on by definition, or
        # plain non-thinking endpoints). Send no thinking params; let
        # the engine default. ``temperature`` still flows through.
        body['temperature'] = temperature if temperature is not None else 1.0
    elif _tf == 'chat_template_kwargs':
        # sglang / vLLM self-hosted dual-mode models. Thinking is gated
        # by the Jinja chat template, exposed via the OpenAI-shim's
        # ``chat_template_kwargs`` extension. Top-level
        # ``enable_thinking`` is silently dropped by these engines.
        kw = body.get('chat_template_kwargs')
        if not isinstance(kw, dict):
            kw = {}
        kw['enable_thinking'] = bool(thinking_enabled)
        body['chat_template_kwargs'] = kw
        body['temperature'] = temperature if temperature is not None else 0.7
    elif _tf == 'enable_thinking' or (not _tf and (is_longcat(model) or is_qwen(model) or is_gemini(model) or is_ernie(model))):
        body['enable_thinking'] = thinking_enabled
        if is_longcat(model):
            body['temperature'] = 1.0 if thinking_enabled else (temperature or 0.7)
        else:
            body['temperature'] = temperature or 0.7
    elif _tf == 'thinking_type' or (not _tf and is_doubao(model)):
        if thinking_enabled:
            body['thinking'] = {'type': 'enabled'}
        else:
            body['thinking'] = {'type': 'disabled'}
        body['temperature'] = temperature or 0.7
    elif not _tf and is_glm(model):
        if thinking_enabled:
            body['thinking'] = {'type': 'enabled'}
            body['temperature'] = 1.0
        else:
            body['thinking'] = {'type': 'disabled'}
            body['temperature'] = max(temperature, 0.01) if temperature else 0.7
    elif not _tf and is_kimi(model):
        _is_k2_thinking = 'k2-thinking' in model.lower() and 'turbo' not in model.lower()
        if _is_k2_thinking or thinking_enabled:
            body['thinking'] = {'type': 'enabled'}
            body['temperature'] = 1.0
        else:
            body['thinking'] = {'type': 'disabled'}
            body['temperature'] = 0.6
        body.pop('top_p', None)
        body.pop('presence_penalty', None)
        body.pop('frequency_penalty', None)
    elif not _tf and is_minimax(model):
        body['temperature'] = temperature or 0.7
        body['reasoning_split'] = True
    elif not _tf and is_claude(model) and thinking_enabled:
        body['thinking'] = {'type': 'adaptive'}
        if is_claude_opus_47(model):
            body['thinking']['display'] = 'summarized'
        else:
            body['temperature'] = 1.0
        if _effort and _effort != 'medium':
            if _effort == 'xhigh' and not is_claude_opus_47(model):
                logger.info('[build_body] effort=xhigh not supported on %s — '
                            'downgrading to high', model)
                _effort = 'high'
            body['effort'] = _effort
    else:
        body['temperature'] = temperature

    if tools:
        body['tools'] = tools

    # OpenAI-style structured output. Forwarded verbatim to the upstream
    # engine; whether a given provider enforces json_schema vs. json_object
    # is provider-dependent. Placed before the `extra` merge so callers can
    # still override via extra={'response_format': ...}.
    if response_format:
        body['response_format'] = response_format

    if extra:
        body.update(extra)

    if is_claude(model):
        _strip_trailing_assistant_for_claude(body['messages'], model)

    if is_claude_opus_47(model):
        for _k in ('temperature', 'top_p', 'top_k'):
            body.pop(_k, None)

    # One-line observability for the dialect choice. Debug level so
    # production logs stay clean; flip a logger to DEBUG when triaging
    # "why is the engine ignoring my thinking flag?".
    logger.debug(
        'build_body: model=%s thinking_format=%r thinking_enabled=%s '
        'has_chat_template_kwargs=%s has_enable_thinking=%s has_thinking=%s',
        model, _tf or '(auto)', thinking_enabled,
        'chat_template_kwargs' in body,
        'enable_thinking' in body,
        'thinking' in body,
    )

    return body
