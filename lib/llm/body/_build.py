# HOT_PATH
"""build_body — model-aware request-body assembly (main entrypoint).

Orchestrates the cohesive helpers: image validation/downscaling
(``_images``), model-specific message tweaks (``_model_tweaks``), and the
context-window clamp (``_clamp``).
"""

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
    gemini_reasoning_effort,
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

from lib.llm.body._clamp import _clamp_completion_to_context_window
from lib.llm.body._images import (
    _downscale_oversized_images,
    _validate_image_blocks,
)
from lib.llm.body._canonical_wire import canonicalize_messages_inplace
from lib.llm.body._model_tweaks import (
    _inject_claude_reasoning_details,
    _inject_gemini_thought_signatures,
    _strip_trailing_assistant_for_claude,
)

logger = get_logger(__name__)


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
    max_tokens = _clamp_completion_to_context_window(
        model, messages, max_tokens, provider_id=provider_id)

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
    _inject_claude_reasoning_details(clean_messages, model)

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
    elif _tf == 'reasoning_effort' or (not _tf and is_gemini(model)):
        # Gemini 3.x is a reasoning model. The only knob the OpenAI-compat
        # gateway forwards to Vertex's thinkingLevel is the OpenAI-style
        # ``reasoning_effort`` string (verified via usage.reasoning_tokens:
        # minimal≈0 → high≈1000+). The legacy top-level ``enable_thinking``
        # boolean and the nested ``thinking.thinking_level`` field are both
        # silently ignored on this path.  Gemini 3.x also recommends NOT
        # sending temperature/top_p/top_k, so we omit temperature here.
        body['reasoning_effort'] = gemini_reasoning_effort(_effort, thinking_enabled)
    elif _tf == 'enable_thinking' or (not _tf and (is_longcat(model) or is_qwen(model) or is_ernie(model))):
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

    # ── Canonical wire-order normalization (class-③ prefix-cache root fix) ──
    # LAST message transform: rewrite every message dict with keys in ONE
    # canonical order so the live-stream and history-replay build paths emit
    # byte-identical wire bytes for the same semantic turn. Without this, an
    # already-cached assistant/tool_call turn re-serializes with a different
    # key order each round → WIRE PREFIX CHANGED → the whole prefix is re-billed.
    # Order-only (values untouched), so it can't change what the model sees —
    # only the byte layout the gateway prompt-cache matches on. See
    # _canonical_wire.py.
    canonicalize_messages_inplace(body['messages'])

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
