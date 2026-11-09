"""Streaming request-body adaptation, result settlement and callback helpers."""

import copy
import math
import time

from lib.log import get_logger

from lib.llm.diagnostic_timing import timed_stage
from lib.secret_envelope import secret_hint
from lib.llm_dispatch._api_budget import (
    _unix_time_ns,
)
from lib.llm_dispatch._api_contention import _note_shared_contention_recovered
from lib.llm_dispatch._api_hygiene import (
    _cool_slot_on_premature_close,
)

logger = get_logger('lib.llm_dispatch.api')


def _readjust_thinking_params(body: dict, new_model: str, thinking_format: str,
                              inference_profile=None):
    """Recompile original intent, never reverse-engineer a previous retry."""
    from lib.llm.reasoning import compile_reasoning
    compile_reasoning(body, new_model, inference_profile=inference_profile,
                      thinking_format=thinking_format)


def _adapt_stream_body_for_slot(slot, body_or_messages, is_body, *,
                                tools, max_tokens, temperature,
                                thinking_enabled, preset, effort,
                                thinking_depth=None, model_budget_admit=None,
                                tool_choice=None, generation_options=None):
    """Build/adapt the request body for a specific dispatched slot.

    Pure CPU (no I/O) — shared by the sync ``dispatch_stream`` loop and the
    native-async ``async_dispatch_stream`` loop so the provider-specific body
    quirks (max_tokens re-clamp, thinking-param readjust, Claude prefill
    guard, Gemini thought-signature injection) live in ONE place.

    Returns the body dict ready to hand to ``stream_chat`` / ``async_stream_chat``.
    """
    if model_budget_admit is not None:
        if not callable(model_budget_admit):
            raise ValueError('model_budget_admit must be callable')
        previous = body_or_messages.get('_model_budget_admit') if is_body else None
        if previous is not None and previous is not model_budget_admit:
            raise ValueError('model budget admission cannot replace an existing account')
    from lib.llm import build_body
    from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY, TEXT_COUNT_VIEW_KEY

    from lib.llm.body._admission import enforce_body_input_limit
    from lib.llm_dispatch.context_policy import slot_context_window
    context_window = slot_context_window(slot)
    # Model knowledge (family tweaks, output clamps, reasoning dialect) keys on
    # the Offering's logical model; slot.model stays the wire/route identity.
    knowledge_model = getattr(slot, 'logical_model', '') or slot.model
    if is_body:
        body = dict(body_or_messages)
        if generation_options:
            body.update(copy.deepcopy(generation_options))
        if tool_choice is not None:
            body['tool_choice'] = copy.deepcopy(tool_choice)
        body.pop('_route_context_window', None)
        body.pop('_route_context_model', None)
        # A prebuilt envelope may bypass build_body entirely. Capture explicit
        # caller controls once; a retained intent remains authoritative on retry.
        if '_reasoning_intent' not in body and any(
                value is not None for value in (thinking_enabled, thinking_depth, effort)):
            from lib.llm.reasoning import capture_intent
            body['_reasoning_intent'] = capture_intent(
                thinking_enabled=thinking_enabled, thinking_depth=thinking_depth,
                effort=effort, preset=preset,
                temperature=body.get('temperature', temperature), extra=body)
        source_model = body.get('model')
        body['model'] = slot.model
        # Admission was counted with the source model's tokenizer. Same-model
        # key/transport retries may reuse it; an actual model fallback must
        # estimate locally under its own context-window policy.
        if source_model != slot.model:
            body.pop(ADMITTED_INPUT_TOKENS_KEY, None)
        # dict() is a SHALLOW envelope copy: body['messages'] stays the
        # caller's (possibly settled) history. Every adaptation below and every
        # wire-preparation step returns adapted copies of only what it changes
        # and never writes that history, so a Gemini attempt cannot bake
        # extra_content.google.thought_signature into the prefix a later Claude
        # attempt re-serializes (conv mrne3bqe R4/R5) and no slot pick or retry
        # copies the conversation.
        #
        # Gemini thought signatures, Claude prefill/image adaptation and the
        # cache markers of Claude and the opt-in GLM/Qwen/DeepSeek families
        # change the counted message bytes, so those families recount input.
        from lib.llm import _gateway_honors_cache_markers
        from lib.model_info import is_gemini as _is_gemini
        rewrites_messages = (
            _is_gemini(knowledge_model)
            or _gateway_honors_cache_markers(knowledge_model)
            # Transport cache markers key on body['model'] (the wire id).
            or _gateway_honors_cache_markers(slot.model)
        )
        reuse_admission = (
            source_model == slot.model
            and not rewrites_messages
            and (tools is None or tools is body.get('tools'))
        )
        if tools is not None:
            body['tools'] = tools
        if 'max_tokens' in body:
            from lib.model_info import (
                _clamp_route_max_tokens,
                _route_output_limit_key,
            )
            from lib.llm.body import _clamp_completion_to_context_window
            route_limit_key = _route_output_limit_key(
                provider_id=str(getattr(slot, 'routing_provider_id', '')
                                or slot.provider_id or ''),
                offering_id=str(
                    getattr(slot, 'route_offering_id', '') or ''),
                deployment_id=str(
                    getattr(slot, 'route_deployment_id', '') or ''),
                protocol=str(slot.protocol or 'openai'),
                model=str(slot.model or ''),
            )
            body['_route_output_limit_key'] = route_limit_key
            body['max_tokens'] = _clamp_route_max_tokens(
                knowledge_model,
                body['max_tokens'],
                route_key=route_limit_key,
                declared_limit=int(
                    getattr(slot, 'max_output_tokens', 0) or 0),
                wire_model=slot.model,
            )
            body['max_tokens'] = _clamp_completion_to_context_window(
                knowledge_model, body.get('messages'), body['max_tokens'],
                provider_id=(getattr(slot, 'routing_provider_id', '')
                             or slot.provider_id or ''),
                precomputed_input_tokens=body.get(
                    ADMITTED_INPUT_TOKENS_KEY),
                context_window=context_window,
                text_count_memo=body.get(TEXT_COUNT_VIEW_KEY))
        _readjust_thinking_params(
            body, knowledge_model, slot.thinking_format or '',
            inference_profile=getattr(slot, 'inference_profile', None))
        from lib.llm import _downscale_oversized_images, _strip_trailing_assistant_for_claude, is_claude
        from lib.llm.body import _validate_image_blocks
        if is_claude(knowledge_model) and body.get('messages'):
            messages = _strip_trailing_assistant_for_claude(
                body['messages'], knowledge_model)
            # Reconcile any mislabeled image data-URI media type BEFORE the
            # downscale pass. On this pre-built-body swap path (dispatch
            # swapped the model onto Claude), build_body's _validate_image_blocks
            # never ran, so a stored data:image/jpeg URI holding PNG bytes would
            # reach the strict Anthropic Messages API and 400 the turn. Run it
            # here so the swap path has the same self-consistency guarantee as
            # the fresh-build path.
            messages = _validate_image_blocks(messages)
            body['messages'] = _downscale_oversized_images(
                messages, knowledge_model)
        from lib.llm.body import _inject_gemini_thought_signatures
        if _is_gemini(knowledge_model) and body.get('messages'):
            body['messages'] = _inject_gemini_thought_signatures(
                body['messages'], knowledge_model)
        if (slot.protocol or '') == 'responses':
            body['_responses_feature_profile'] = (
                getattr(slot, 'responses_profile', '') or 'compatible')
        # Stays on the wire id: transport re-admission matches the stamped
        # _route_context_model against body['model'] to keep the route window.
        enforce_body_input_limit(
            body, model=slot.model,
            provider_id=(getattr(slot, 'routing_provider_id', '')
                         or slot.provider_id or ''),
            reuse_admission=reuse_admission,
            context_window=context_window,
        )
        if model_budget_admit is not None:
            body['_model_budget_admit'] = model_budget_admit
        body['_admission_logical_model'] = getattr(slot, 'logical_model', '') or slot.model
        return body
    body = build_body(
        slot.model, body_or_messages,
        max_tokens=max_tokens, temperature=temperature,
        thinking_enabled=thinking_enabled, preset=preset, effort=effort,
        thinking_depth=thinking_depth,
        tools=tools, stream=True, context_window=context_window,
        extra=generation_options,
        thinking_format=slot.thinking_format or '',
        inference_profile=getattr(slot, 'inference_profile', None),
        provider_id=(getattr(slot, 'routing_provider_id', '')
                     or slot.provider_id or ''),
        knowledge_model=knowledge_model,
    )
    if tool_choice is not None:
        body['tool_choice'] = copy.deepcopy(tool_choice)
    from lib.model_info import _clamp_route_max_tokens, _route_output_limit_key
    route_limit_key = _route_output_limit_key(
        provider_id=str(getattr(slot, 'routing_provider_id', '')
                        or slot.provider_id or ''),
        offering_id=str(getattr(slot, 'route_offering_id', '') or ''),
        deployment_id=str(getattr(slot, 'route_deployment_id', '') or ''),
        protocol=str(slot.protocol or 'openai'),
        model=str(slot.model or ''),
    )
    body['_route_output_limit_key'] = route_limit_key
    body['max_tokens'] = _clamp_route_max_tokens(
        knowledge_model,
        body.get('max_tokens'),
        route_key=route_limit_key,
        declared_limit=int(getattr(slot, 'max_output_tokens', 0) or 0),
        wire_model=slot.model,
    )
    if (slot.protocol or '') == 'responses':
        body['_responses_feature_profile'] = (
            getattr(slot, 'responses_profile', '') or 'compatible')
    enforce_body_input_limit(
        body, model=slot.model, context_window=context_window,
        provider_id=(getattr(slot, 'routing_provider_id', '')
                     or slot.provider_id or ''),
    )
    if model_budget_admit is not None:
        body['_model_budget_admit'] = model_budget_admit
    body['_admission_logical_model'] = getattr(slot, 'logical_model', '') or slot.model
    return body


def _settle_stream_result(slot, usage, *, latency, ttft, state,
                          cache_conv_id, tag, dispatcher=None,
                          stream_result=None, route_trace=None):
    """Settle one provider stream result exactly once for both dispatch loops.

    The sync ``dispatch_stream`` and async ``async_dispatch_stream`` loops
    differ in transport scheduling. Both call this after leaving the transport
    retry boundary: receipt/cache failures must propagate without another
    settlement or provider attempt. Once a slot answers, its typed
    result selects exactly one mutually exclusive health action: verified
    completion records success, client abort releases neutrally, and every
    invalid stream records one provider/slot error. Both loops then stamp the
    same ``usage['_dispatch']`` metadata and cache-settle timestamp here.

    Mutates ``usage`` in place (stamps ``_dispatch``) and returns nothing.
    """
    _out_tokens = 0
    if isinstance(usage, dict):
        _out_tokens = (usage.get('completion_tokens')
                       or usage.get('output_tokens') or 0)
        try:
            _out_tokens = int(_out_tokens)
        except (ValueError, TypeError, OverflowError) as _e_audit:
            logger.debug('[api] _settle_stream_result caught %s: %s',
                         type(_e_audit).__name__, _e_audit)
            _out_tokens = 0
    raw_stream_state = ''
    if stream_result is not None:
        raw_stream_state = str(
            getattr(getattr(stream_result, 'state', None), 'value', '') or '')
    if not raw_stream_state and isinstance(usage, dict):
        raw_stream_state = str(usage.get('_stream_state') or '')
    verified_complete = raw_stream_state in {'', 'provider_finished'}
    client_aborted = raw_stream_state == 'client_aborted'
    if stream_result is not None:
        # Name the minting Deployment of any encrypted Responses state this
        # message carries, so later dispatches replay it only there.
        from lib.llm.responses_state_origin import stamp_state_origin
        stamp_state_origin(getattr(stream_result, 'message', None),
                           getattr(slot, 'route_deployment_id', ''))
    if verified_complete:
        slot.record_success(
            latency, ttft_ms=ttft, output_tokens=_out_tokens)
        _note_shared_contention_recovered(dispatcher, slot, tag)
    elif client_aborted:
        slot.release()
    else:
        _cool_slot_on_premature_close(
            slot, usage, stream_state=raw_stream_state)
    if isinstance(usage, dict):
        completed_at_unix_ns = _unix_time_ns()
        latency_ms = max(0.0, float(latency or 0))
        ttft_ms = None
        if ttft is not None:
            candidate_ttft = float(ttft)
            if math.isfinite(candidate_ttft) and candidate_ttft >= 0:
                ttft_ms = candidate_ttft
        # tb21 v12: a premature-close attempt left ``ttft_ms=None`` inside
        # persisted ``round_usage`` evidence and failed formal projection.
        # Persisted evidence must be well-formed for EVERY dispatch outcome,
        # including streams that closed before first content. Fall back to
        # the full latency as the conservative upper bound and stamp the
        # provenance so downstream analysis never reads it as measured.
        ttft_measurement = 'measured'
        if ttft_ms is None and not verified_complete:
            ttft_ms = latency_ms
            ttft_measurement = 'unobserved_stream_upper_bound'
        usage['_dispatch'] = {
            'key': slot.key_name, 'model': slot.model,
            'logical_model': getattr(slot, 'logical_model', '') or slot.model,
            'key_tail': (slot.api_key or '')[-4:],
            'key_hint': secret_hint(slot.api_key or '')[:64],
            'provider_id': (getattr(slot, 'routing_provider_id', '')
                            or slot.provider_id),
            'route_snapshot': (route_trace.snapshot() if route_trace is not None
                               else dict(getattr(slot, 'route_snapshot', {}) or {})),
            'protocol': slot.protocol or 'openai',
            'responses_profile': getattr(
                slot, 'responses_profile', '') or '',
            'latency_ms': round(latency_ms),
            'ttft_ms': round(ttft_ms, 3) if ttft_ms is not None else None,
            'ttft_measurement': ttft_measurement,
            'stream_started_at_unix_ns': (
                completed_at_unix_ns - round(latency_ms * 1_000_000)),
            'first_content_at_unix_ns': (
                completed_at_unix_ns
                - round((latency_ms - ttft_ms) * 1_000_000)
                if ttft_ms is not None else None),
            'stream_completed_at_unix_ns': completed_at_unix_ns,
            'attempt': state.hard_attempts + 1,
            '429_retries': state._429_count,
            'queue_wait_ms': round(getattr(state, 'queue_wait_ms', 0.0), 3),
            'queue_wait_measurement': 'dispatcher_backpressure_only',
            'route_selection_ms': round(getattr(state, 'route_selection_ms', 0.0), 3),
        }
    if cache_conv_id and verified_complete:
        try:
            from lib.llm_dispatch.cache_settle import (
                codex_cache_write_pending, generic_cache_write_pending,
                is_cold_write, observe_codex_cache, record_stream_end,
            )
            _cache_profile = slot.oauth or ''
            if _cache_profile == 'codex':
                observe_codex_cache(cache_conv_id, usage)
            record_stream_end(
                cache_conv_id,
                cold_write=is_cold_write(usage),
                cache_profile=_cache_profile,
                pending_write=(
                    codex_cache_write_pending(usage)
                    if _cache_profile == 'codex'
                    else generic_cache_write_pending(usage)),
            )
        except ImportError as _cs_err:
            logger.debug('%s cache-settle record unavailable: %s', tag, _cs_err)


def _first_output_callbacks(
    started_at,
    on_thinking,
    on_content,
    on_tool_call_ready,
    on_before_tool_call_ready=None,
):
    """Wrap every output channel around one per-attempt TTFT latch."""
    ttft_value = [None]
    recorded = False

    def _forward(callback, value, before=None):
        nonlocal recorded
        if not recorded:
            ttft_value[0] = (time.time() - started_at) * 1000
            recorded = True
        if before:
            before()
        if callback:
            callback(value)

    def _thinking(value):
        _forward(on_thinking, value)

    def _content(value):
        _forward(on_content, value)

    def _tool_call(value):
        _forward(
            on_tool_call_ready, value, before=on_before_tool_call_ready)

    return ttft_value, _thinking, _content, _tool_call


@timed_stage('dispatcher_wait')
def _sleep_and_record_queue_wait(state, seconds, *, abort_check=None):
    """Sleep abortably for sync backpressure and account for the whole wait."""
    if seconds <= 0:
        return
    started_at = time.monotonic()
    if abort_check is None:
        time.sleep(seconds)
    else:
        from lib.llm._transport import abortable_sleep
        abortable_sleep(seconds, abort_check)
    state.record_queue_wait(started_at)
