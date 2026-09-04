# HOT_PATH
"""build_body — model-aware request-body assembly (main entrypoint).

Orchestrates the cohesive helpers: image validation/downscaling
(``_images``), model-specific message tweaks (``_model_tweaks``), and the
context-window clamp (``_clamp``).
"""

import copy

import lib as _lib
from lib.llm_sanitize import (
    _drop_empty_assistant_messages,
    _fix_empty_user_messages,
    _fix_orphaned_tool_calls,
    _fix_tool_call_wire_shape,
    _merge_consecutive_same_role,
    _sanitize_messages,
    _strip_empty_text_blocks,
    _strip_non_api_fields,
)
from lib.log import get_logger
from lib.model_info import (
    _clamp_max_tokens,
    gemini_reasoning_effort,
    glm_line_version,
    glm_reasoning_effort,
    gpt_reasoning_effort,
    is_claude,
    is_claude_opus_47,
    is_doubao,
    is_ernie,
    is_gemini,
    is_glm,
    is_gpt5,
    is_kimi,
    is_kimi_k3,
    is_longcat,
    is_minimax,
    is_qwen,
    kimi_k3_reasoning_effort,
    model_supports_vision,
)

from lib.llm.body._clamp import _clamp_completion_to_context_window
from lib.llm.body._images import (
    _downscale_oversized_images,
    _project_images_for_text_model,
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
               provider_id='', precomputed_input_tokens=None,
               vision_fallback_from=''):
    """Build a model-aware request body for /chat/completions.

    Handles provider-specific parameters automatically:
      - Claude:   thinking.type='adaptive', effort param, cache breakpoints
      - Kimi K3:  top-level reasoning_effort (low/high/max), no temperature
      - Kimi K2:  thinking.type='enabled'/'disabled', fixed temp
      - GLM:      thinking.type='enabled'/'disabled', temperature clamped
                  to (0, 1); GLM-5.2+ adds reasoning_effort + preserved
                  thinking (clear_thinking=false when history carries
                  reasoning_content); GLM-5.3 is forced-thinking — 'disabled'
                  degrades to reasoning_effort='low' (an API error otherwise)
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
        model,
        messages,
        max_tokens,
        provider_id=provider_id,
        precomputed_input_tokens=precomputed_input_tokens,
    )

    clean_messages = _strip_non_api_fields(
        messages, carry_same_role_seam_hints=True)
    # Protocol-private assistant state is not an OpenAI API field, but native
    # protocol converters need it after the generic body builder has run.
    # Restore it onto the canonical in-process body here; the final OpenAI
    # serialization branch strips it, while Responses/Anthropic consume their
    # respective sidecar during conversion.  This also closes the historical
    # restart/rebuild hole for Responses output items.
    _valid_source_messages = [
        message for message in messages if isinstance(message, dict)
    ]
    for _source_msg, _clean_msg in zip(
            _valid_source_messages, clean_messages):
        for _sidecar in ('_responses_items', '_anthropic_content_blocks'):
            if _source_msg.get(_sidecar):
                _clean_msg[_sidecar] = copy.deepcopy(_source_msg[_sidecar])

    _pid = provider_id.lower() if provider_id else ''
    # Keyed on the GATEWAY, not one provider id's exact spelling: every
    # example-corp* provider (example-corp, example_corp_anthropic, …) terminates at
    # your-llm-gateway.example.com, whose keyword filter is surface-agnostic — the
    # 2026-07-28 Claude → Anthropic-native migration proved exact-equality
    # silently strips the ZWSP sanitizer from the new surface (HTTP 450
    # re-exposure on blocked-term conversations).
    if _pid.startswith('example-corp') or (not _pid and 'example-corp' in _lib.LLM_BASE_URL):
        _sanitize_messages(clean_messages)

    # Strip phantom empty text blocks BEFORE the whole-content healers so a
    # list left empty by the strip is claimed by them (Kimi 400 "text content
    # is empty" — see _strip_empty_text_blocks).
    _strip_empty_text_blocks(clean_messages)
    # Heal tool_call wire shape for ANY model BEFORE the orphan/adjacency
    # fixer (which pairs BY id): empty/missing function.name → placeholder,
    # missing type/id, non-string arguments, unpairable tool_call_id.
    # Kimi hard-400s the whole request on these with the misleading
    # "tokenization failed" (live-probed 2026-08-07, task 9a8196f3).
    clean_messages = _fix_tool_call_wire_shape(clean_messages)
    clean_messages = _fix_orphaned_tool_calls(clean_messages)
    clean_messages = _drop_empty_assistant_messages(clean_messages)
    clean_messages = _merge_consecutive_same_role(clean_messages)

    if model_supports_vision(model):
        _validate_image_blocks(clean_messages)
        _downscale_oversized_images(clean_messages, model)
    else:
        # Projection runs before validation/resolution so a text-only request
        # never reads local image files, expands them to base64, or pays image
        # processing cost for bytes it cannot send. The operation is confined
        # to ``clean_messages`` (a deep request copy), preserving the durable
        # multimodal transcript for a later switch back to a VLM.
        _projected_image_count = _project_images_for_text_model(
            clean_messages,
            model,
            fallback_from=vision_fallback_from,
        )
        if _projected_image_count:
            logger.warning(
                '[build_body] Projected %d image(s) into bounded text markers '
                'for non-vision model %s (fallback_from=%s); durable history '
                'unchanged',
                _projected_image_count, model,
                vision_fallback_from or 'none')

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
    if _plugin_dialect is None and _tf == 'enable_thinking':
        # GLM-5.2+ carries its own native thinking contract (effort rungs +
        # preserved thinking). A provider-level legacy ``enable_thinking`` —
        # e.g. the YourProvider template's Qwen-oriented default — is a dead field
        # on GLM-native gateways (live-verified 2026-08-14 on
        # your-llm-gateway.example.com: glm-5.3 ignores it, so a user-disabled model
        # still thinks and bills) and it would silently kill the effort knob
        # and clear_thinking. Deliberate engine declarations (plugin / none /
        # chat_template_kwargs) still win — they describe the engine, not a
        # model-family default.
        _gv = glm_line_version(model)
        if _gv is not None and _gv >= (5, 2):
            _tf = ''
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
        # GLM-5.3 (2026-08-14) is a FORCED-thinking model: thinking.type
        # 'disabled' is an API ERROR on its native endpoint, so a depth of
        # 'off' degrades to the lightest legal rung (reasoning_effort='low')
        # instead of a 400 — same policy as Kimi K3, the other
        # always-thinks family.
        _glm_v = glm_line_version(model)
        _glm_forced = _glm_v is not None and _glm_v >= (5, 3)
        if thinking_enabled or _glm_forced:
            body['thinking'] = {'type': 'enabled'}
            body['temperature'] = 1.0
            if _glm_v is not None and _glm_v >= (5, 2):
                # Effort knob + preserved thinking ship with GLM-5.2+; older
                # GLM endpoints reject both fields.
                body['reasoning_effort'] = glm_reasoning_effort(
                    _effort, thinking_enabled, model)
                # Preserved thinking: GLM clears replayed reasoning_content
                # server-side by default (clear_thinking=true); opting out
                # keeps the trace in context — officially recommended for
                # tool/agent loops (better quality AND cache hits). Only
                # meaningful when the history actually carries some.
                if any(m.get('reasoning_content')
                       for m in clean_messages
                       if isinstance(m, dict) and m.get('role') == 'assistant'):
                    body['thinking']['clear_thinking'] = False
        else:
            body['thinking'] = {'type': 'disabled'}
            body['temperature'] = max(temperature, 0.01) if temperature else 0.7
    elif not _tf and is_kimi(model):
        if is_kimi_k3(model):
            # K3 contract (official quickstart + verified live against the
            # example-corp gateway 2026-07-24): top-level ``reasoning_effort``
            # (low/high/max, default max); K3 always thinks, so depth 'off'
            # degrades to 'low'. Temperature is FIXED at 1.0 — sending any
            # other value is HTTP 400 — so it must be omitted entirely.
            body['reasoning_effort'] = kimi_k3_reasoning_effort(
                _effort, thinking_enabled)
        else:
            _is_k2_thinking = ('k2-thinking' in model.lower()
                               and 'turbo' not in model.lower())
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
    elif not _tf and is_gpt5(model):
        # OpenAI GPT-5 family is a reasoning model driven by the native
        # ``reasoning_effort`` string. Current Codex models use ``none`` when
        # disabled, add ``xhigh`` on 5.3+, and add ``max`` on 5.6+.
        # gpt_reasoning_effort owns that generation-aware mapping.
        # Temperature is omitted — GPT-5 reasoning ignores sampling params.
        body['reasoning_effort'] = gpt_reasoning_effort(
            _effort, thinking_enabled, model)
    elif not _tf and is_claude(model) and thinking_enabled:
        body['thinking'] = {'type': 'adaptive'}
        if is_claude_opus_47(model):
            body['thinking']['display'] = 'summarized'
        else:
            body['temperature'] = 1.0
        # Effort rung. On the ADAPTIVE generation (Opus 4.7+) EVERY rung is
        # stated, including ``medium``. That exclusion used to be correct --
        # medium WAS the model default, so omitting it was a free byte saving.
        # Opus 5 moved the default to ``high``, which silently turned the
        # omission into an UPGRADE of the user's explicit choice. Measured on
        # the example-corp gateway (yuju-claude-opus-5-evaDaily, identical prompt,
        # n=6, 2026-07-26): effort dropped -> median 2812.5 completion tokens,
        # effort=medium -> median 1855.0 (~1.52x). index.html ships
        # data-depth="medium" as the ACTIVE default on both the desktop (:795)
        # and mobile (:1708) selectors, so this is the common path.
        #
        # Pre-4.7 Claude still defaults to medium, so omitting it there means
        # the same thing -- that wire stays byte-identical.
        _omit_medium = not is_claude_opus_47(model)
        if _effort and not (_omit_medium and _effort == 'medium'):
            if _effort == 'xhigh' and not is_claude_opus_47(model):
                logger.info('[build_body] effort=xhigh not supported on %s — '
                            'downgrading to high', model)
                _effort = 'high'
            elif _effort == 'ultra':
                # ``ultra`` is Tofu's legacy GPT-5.6 compatibility label;
                # the public GPT value is ``max`` and Claude also tops at max.
                # map to Claude's top rung (max) so the effort is honoured.
                logger.info('[build_body] effort=ultra has no Claude tier on '
                            '%s — mapping to max', model)
                _effort = 'max'
            body['effort'] = _effort
    elif not _tf and is_claude_opus_47(model):
        # ── Thinking OFF on the ADAPTIVE-thinking generation (Opus 4.7+) ──
        # Claude Opus 5 runs adaptive thinking ON BY DEFAULT: a body with NO
        # ``thinking`` key thinks anyway. Every earlier Claude defaulted OFF,
        # so "disabled" used to be expressible by omitting the key — correct
        # then, silently wrong now. The user picks depth=off, we send nothing,
        # and Opus 5 bills them for reasoning they declined.
        #
        # Live-measured on the example-corp gateway (yuju-claude-opus-5-evaDaily,
        # identical prompt, 4 samples, 2026-07-26):
        #   omit thinking       → median 2925 completion tokens (36.3s, 24.8s)
        #   {"type":"disabled"} → median 1518 completion tokens (19.4s, 19.6s)
        #
        # Gated on is_claude_opus_47 (the adaptive generation) rather than all
        # Claude: this is the documented disable form for 4.7+, and it was
        # verified a live no-op on 4.7 (omit [562,536,628] vs disabled
        # [462,535,601]), so it corrects 5+ without changing 4.7/4.8 outcomes.
        # Pre-4.7 Claude keeps the byte-identical omit wire — it already
        # defaults off, and those revisions were never verified to accept the
        # key, so adding one would be an unforced 400 risk.
        #
        # No ``effort`` is set here, and that is load-bearing: Anthropic
        # returns HTTP 400 for thinking=disabled combined with xhigh/max.
        body['thinking'] = {'type': 'disabled'}
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
