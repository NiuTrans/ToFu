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
from lib.model_info import _clamp_max_tokens, is_claude, model_supports_vision

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
               thinking_enabled=None, preset='medium', effort=None,
               thinking_depth=None, tools=None, response_format=None,
               stream=True, extra=None, thinking_format='',
               provider_id='', precomputed_input_tokens=None,
               vision_fallback_from='', image_reuse=None, image_user_id=None,
               inference_profile=None, reasoning_intent=None, defer_reasoning=False,
               context_window=0):
    """Build a model-aware request body for /chat/completions.

    Reasoning and sampling use the selected route's ``inference_profile``
    through the shared intent compiler. ``reasoning_intent`` retains the
    source choice during rebuilds; provider wire fields are never its authority.

    ``defer_reasoning=True`` stages intent before route selection; that body
    must be compiled by a selected slot before any transport may consume it.
    Older templates without capability declarations use the isolated legacy
    compatibility module. Image, context and message normalization remain owned
    by their respective helpers, independently of reasoning controls.

    Raises:
        ValueError: if ``messages`` is empty or None.
    """
    if not messages:
        raise ValueError('build_body() requires a non-empty messages list')

    max_tokens = _clamp_max_tokens(model, max_tokens)
    max_tokens = _clamp_completion_to_context_window(
        model,
        messages,
        max_tokens,
        provider_id=provider_id,
        precomputed_input_tokens=precomputed_input_tokens,
        context_window=context_window,
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
        for _sidecar in ('_responses_items', '_anthropic_content_blocks',
                         '_reasoning_continuity'):
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
        _validate_image_blocks(clean_messages, user_id=image_user_id)
        _downscale_oversized_images(
            clean_messages, model, image_reuse=image_reuse, user_id=image_user_id)
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

    if tools:
        body['tools'] = tools

    # OpenAI-style structured output. Forwarded verbatim to the upstream
    # engine; whether a given provider enforces json_schema vs. json_object
    # is provider-dependent. Placed before the `extra` merge so callers can
    # still override via extra={'response_format': ...}.
    if response_format:
        body['response_format'] = response_format

    if extra:
        body.update(copy.deepcopy(extra))

    from lib.llm.reasoning import capture_intent, compile_reasoning
    body['_reasoning_intent'] = (
        copy.deepcopy(reasoning_intent) if reasoning_intent is not None else
        capture_intent(thinking_enabled=thinking_enabled, thinking_depth=thinking_depth,
                       effort=effort, preset=preset, temperature=temperature, extra=extra))
    if defer_reasoning:
        from lib.llm.reasoning import defer_reasoning_compilation
        defer_reasoning_compilation(body)
    else:
        compile_reasoning(body, model, inference_profile=inference_profile,
                          thinking_format=thinking_format)

    if is_claude(model):
        _strip_trailing_assistant_for_claude(body['messages'], model)

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
        model, thinking_format or '(auto)', thinking_enabled,
        'chat_template_kwargs' in body,
        'enable_thinking' in body,
        'thinking' in body,
    )

    return body
