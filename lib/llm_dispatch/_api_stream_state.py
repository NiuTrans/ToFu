"""Streaming request-body adaptation, retry state and settlement helpers."""

import copy
import math
import time

from lib.log import get_logger
from lib.llm_dispatch._api_budget import (
    _remember_route_missing_error,
    _unix_time_ns,
)
from lib.llm_dispatch._api_contention import _note_shared_contention_recovered
from lib.llm_dispatch._api_errors import DispatchWaitStatus
from lib.llm_dispatch._api_hygiene import (
    _advance_credential_delivery_anomaly,
    _cool_slot_on_premature_close,
)

logger = get_logger('lib.llm_dispatch.api')


def _readjust_thinking_params(body: dict, new_model: str, thinking_format: str):
    """Fix thinking parameters when a pre-built body is dispatched to a different model family.

    Different model families use incompatible thinking parameter formats:
      - Claude:   thinking.type = 'adaptive'
      - Doubao:   thinking.type = 'enabled' / 'disabled'
      - GLM:      thinking.type = 'enabled' / 'disabled'; 5.2+ adds
                  reasoning_effort (+ clear_thinking when history carries
                  reasoning_content); 5.3 is forced-thinking — 'disabled'
                  degrades to reasoning_effort='low'
      - MiniMax:  reasoning_split = True
      - Qwen/LongCat: enable_thinking = True/False
      - Gemini 3.x: reasoning_effort = 'minimal'/'low'/'medium'/'high'
      - Kimi K3:    reasoning_effort = 'low'/'high'/'max', NO temperature
      - Kimi K2:    thinking.type = 'enabled'/'disabled'

    When dispatch swaps the model (e.g. Claude → Doubao), the body may carry
    the wrong format, causing HTTP 400 errors. This function detects mismatches
    and converts to the correct format for the new model.
    """
    from lib.llm import (
        is_claude, is_doubao, is_gemini, is_glm, is_gpt5, is_kimi,
        is_kimi_k3, is_longcat, is_minimax, is_qwen,
    )

    # Detect current thinking state from the body
    thinking_dict = body.get('thinking')
    enable_thinking = body.get('enable_thinking')
    reasoning_split = body.get('reasoning_split')
    reasoning_effort = body.get('reasoning_effort')
    effort = body.get('effort')

    has_thinking_params = (thinking_dict is not None or
                           enable_thinking is not None or
                           reasoning_split is not None or
                           reasoning_effort is not None)
    if not has_thinking_params:
        return  # No thinking params to adjust

    # Determine if thinking is currently enabled
    is_enabled = False
    if isinstance(thinking_dict, dict):
        t = thinking_dict.get('type', '')
        is_enabled = t in ('enabled', 'adaptive')
    elif enable_thinking is not None:
        is_enabled = bool(enable_thinking)
    elif reasoning_split is not None:
        is_enabled = bool(reasoning_split)
    elif reasoning_effort is not None:
        # Gemini dialect: 'minimal' is the closest thing to "thinking off".
        is_enabled = reasoning_effort != 'minimal'

    # Carry the effort across the swap if no explicit effort was set — also
    # when thinking_dict/enable_thinking declared the state: GLM bodies carry
    # the rung in top-level reasoning_effort, and without this a GLM→GLM
    # slot swap (e.g. 5.2 slot 503 → 5.3 slot) would silently reset it to
    # the model default.
    if not effort and reasoning_effort is not None:
        effort = reasoning_effort

    # Clean ALL thinking-related keys before re-setting
    body.pop('thinking', None)
    body.pop('enable_thinking', None)
    body.pop('reasoning_split', None)
    body.pop('reasoning_effort', None)
    body.pop('effort', None)

    # Re-apply for the new model using the same logic as build_body
    _tf = thinking_format
    # ── Plugin dialect (tofu.providers) first; built-ins fall through ──
    _plugin_dialect = None
    if _tf:
        from lib.llm_dispatch.provider_registry import get_dialect
        _plugin_dialect = get_dialect(_tf)
    if _plugin_dialect is None and _tf == 'enable_thinking':
        # Lockstep with build_body(): GLM-5.2+ has a native thinking
        # contract; a provider-level legacy enable_thinking format must not
        # claim it (dead field on GLM-native gateways — see the build_body
        # branch for the live evidence and the engine-vs-family rationale).
        from lib.model_info import glm_line_version
        _gv = glm_line_version(new_model)
        if _gv is not None and _gv >= (5, 2):
            _tf = ''
    if _plugin_dialect is not None:
        _readjust = _plugin_dialect.apply_readjust
        try:
            if _readjust is not None:
                _readjust(body, is_enabled=is_enabled, model=new_model,
                          effort=effort)
            else:
                _plugin_dialect.apply_build(
                    body, thinking_enabled=is_enabled,
                    temperature=body.get('temperature'), model=new_model,
                    effort=effort)
        except Exception as e:
            logger.error('[_readjust_thinking_params] plugin dialect %r failed: '
                         '%s', _tf, e, exc_info=True)
    elif _tf == 'none':
        # Engine accepts no thinking flag — leave the body without one.
        # We already popped enable_thinking / reasoning_split / thinking
        # above, so this branch is intentionally a no-op.
        pass
    elif _tf == 'chat_template_kwargs':
        # sglang / vLLM dual-mode: thinking is gated through Jinja
        # template, exposed as ``chat_template_kwargs.enable_thinking``.
        # Top-level ``enable_thinking`` would be silently ignored.
        kw = body.get('chat_template_kwargs')
        if not isinstance(kw, dict):
            kw = {}
        kw['enable_thinking'] = bool(is_enabled)
        body['chat_template_kwargs'] = kw
    elif not _tf and is_gpt5(new_model):
        from lib.llm import gpt_reasoning_effort
        body['reasoning_effort'] = gpt_reasoning_effort(effort, is_enabled, new_model)
    elif _tf == 'reasoning_effort' or (not _tf and is_gemini(new_model)):
        from lib.llm import gemini_reasoning_effort
        body['reasoning_effort'] = gemini_reasoning_effort(effort, is_enabled)
    elif _tf == 'enable_thinking' or (not _tf and (is_longcat(new_model) or is_qwen(new_model))):
        body['enable_thinking'] = is_enabled
    elif _tf == 'thinking_type' or (not _tf and is_doubao(new_model)):
        body['thinking'] = {'type': 'enabled' if is_enabled else 'disabled'}
    elif not _tf and is_kimi(new_model):
        if is_kimi_k3(new_model):
            # K3: top-level reasoning_effort only; temperature stripped below.
            from lib.llm import kimi_k3_reasoning_effort
            body['reasoning_effort'] = kimi_k3_reasoning_effort(
                effort, is_enabled)
        else:
            body['thinking'] = {'type': 'enabled' if is_enabled else 'disabled'}
            body['temperature'] = 1.0 if is_enabled else 0.6
    elif not _tf and is_glm(new_model):
        # Kept in lockstep with the GLM branch in build_body() — GLM-5.3 is
        # forced-thinking (see that branch for the contract).
        from lib.llm import glm_reasoning_effort
        from lib.model_info import glm_line_version
        _glm_v = glm_line_version(new_model)
        _glm_forced = _glm_v is not None and _glm_v >= (5, 3)
        if is_enabled or _glm_forced:
            body['thinking'] = {'type': 'enabled'}
            body['temperature'] = 1.0
            if _glm_v is not None and _glm_v >= (5, 2):
                body['reasoning_effort'] = glm_reasoning_effort(
                    effort, is_enabled, new_model)
                if any(m.get('reasoning_content')
                       for m in (body.get('messages') or [])
                       if isinstance(m, dict) and m.get('role') == 'assistant'):
                    body['thinking']['clear_thinking'] = False
        else:
            body['thinking'] = {'type': 'disabled'}
            body['temperature'] = max(body.get('temperature', 0.7), 0.01)
    elif not _tf and is_minimax(new_model):
        if is_enabled:
            body['reasoning_split'] = True
    elif not _tf and is_claude(new_model):
        from lib.llm import is_claude_opus_47
        if is_enabled:
            # Keep this in sync with the Claude branch in build_body().
            #   • 4.6 and earlier: adaptive + temperature=1.0
            #   • 4.7+: adaptive + display='summarized' (required to see
            #           reasoning trace), and NO temperature (ignored today,
            #           may be rejected in a future revision).
            body['thinking'] = {'type': 'adaptive'}
            if is_claude_opus_47(new_model):
                body['thinking']['display'] = 'summarized'
            else:
                body['temperature'] = 1.0
            # Effort rung -- kept in lockstep with build_body(). The adaptive
            # generation states EVERY rung (its default is `high`, so an
            # omitted `medium` would be a silent upgrade); pre-4.7 Claude
            # still defaults to medium and keeps the omit wire. The two paths
            # diverged on exactly this rung before
            # tests/test_claude_effort_rung_parity.py pinned them together.
            _omit_medium = not is_claude_opus_47(new_model)
            if effort and not (_omit_medium and effort == 'medium'):
                # xhigh is Opus 4.7-only; downgrade on older Claude to avoid HTTP 400.
                if effort == 'xhigh' and not is_claude_opus_47(new_model):
                    effort = 'high'
                elif effort == 'ultra':
                    # Tofu's legacy 'ultra' label maps to Claude's top rung.
                    effort = 'max'
                body['effort'] = effort
        elif is_claude_opus_47(new_model):
            # Thinking OFF must be STATED on the adaptive generation — Opus 5
            # defaults adaptive thinking ON, so the popped-key state above
            # would silently re-enable it after a swap (live-measured ~1.93x
            # completion tokens; see the build_body branch for the numbers).
            # No effort is set: disabled + xhigh/max is HTTP 400.
            body['thinking'] = {'type': 'disabled'}
    # else: standard OpenAI-compatible — no thinking params needed

    # ── Claude Opus 4.7+ rejects sampling params (HTTP 400) ──
    # Strip unconditionally after re-setting, since non-4.7 branches above
    # may have assigned temperature=1.0 before a potential model swap.
    from lib.llm import is_claude_opus_47
    if is_claude_opus_47(new_model):
        for _k in ('temperature', 'top_p', 'top_k'):
            body.pop(_k, None)

    # ── Kimi K3 fixes temperature=1.0 — any other value is HTTP 400 ──
    # Strip unconditionally: a body swapped in from another family (e.g.
    # Qwen's temperature=0.7) would otherwise carry a rejected value.
    if is_kimi_k3(new_model):
        for _k in ('temperature', 'top_p', 'presence_penalty',
                   'frequency_penalty'):
            body.pop(_k, None)

    # Observability: which dialect actually landed on the wire? Debug
    # level so production logs stay quiet; flip the logger when
    # diagnosing dialect mismatches across slot swaps.
    logger.debug(
        '_readjust_thinking_params: model=%s thinking_format=%r '
        'is_enabled=%s has_chat_template_kwargs=%s has_enable_thinking=%s '
        'has_thinking=%s', new_model, thinking_format or '(auto)',
        is_enabled,
        'chat_template_kwargs' in body, 'enable_thinking' in body,
        'thinking' in body,
    )


def _adapt_stream_body_for_slot(slot, body_or_messages, is_body, *,
                                tools, max_tokens, temperature,
                                thinking_enabled, preset, effort):
    """Build/adapt the request body for a specific dispatched slot.

    Pure CPU (no I/O) — shared by the sync ``dispatch_stream`` loop and the
    native-async ``async_dispatch_stream`` loop so the provider-specific body
    quirks (max_tokens re-clamp, thinking-param readjust, Claude prefill
    guard, Gemini thought-signature injection) live in ONE place.

    Returns the body dict ready to hand to ``stream_chat`` / ``async_stream_chat``.
    """
    from lib.llm import build_body
    from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY
    if is_body:
        body = dict(body_or_messages)
        source_model = body.get('model')
        body['model'] = slot.model
        # Admission was counted with the source model's tokenizer. Same-model
        # key/transport retries may reuse it; an actual model fallback must
        # estimate locally under its own context-window policy.
        if source_model != slot.model:
            body.pop(ADMITTED_INPUT_TOKENS_KEY, None)
        # dict() is a SHALLOW envelope copy. Clone the potentially huge message
        # history only for model families whose adaptation/wire preparation
        # actually mutates it: Gemini injects thought signatures, while Claude
        # and the opt-in GLM/Qwen/DeepSeek families add cache markers (Claude
        # also strips prefills and reconciles/downscales images below).
        #
        # Other OpenAI/Responses bodies are projected read-only all the way to
        # their outbound adapter. Reusing their canonical list avoids one
        # conversation-sized deepcopy on every slot pick and retry. The
        # mutation-gated clone still prevents a Gemini attempt from baking
        # extra_content.google.thought_signature into the caller's prefix and
        # flipping a later Claude wire/cache key (conv mrne3bqe R4/R5).
        from lib.llm import _gateway_honors_cache_markers
        from lib.model_info import is_gemini as _is_gemini
        mutates_messages = (
            _is_gemini(slot.model)
            or _gateway_honors_cache_markers(slot.model)
        )
        if mutates_messages and isinstance(body.get('messages'), list):
            body['messages'] = copy.deepcopy(body['messages'])
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
                slot.model,
                body['max_tokens'],
                route_key=route_limit_key,
                declared_limit=int(
                    getattr(slot, 'max_output_tokens', 0) or 0),
            )
            body['max_tokens'] = _clamp_completion_to_context_window(
                slot.model, body.get('messages'), body['max_tokens'],
                provider_id=(getattr(slot, 'routing_provider_id', '')
                             or slot.provider_id or ''),
                precomputed_input_tokens=body.get(
                    ADMITTED_INPUT_TOKENS_KEY))
        _readjust_thinking_params(body, slot.model, slot.thinking_format or '')
        from lib.llm import _downscale_oversized_images, _strip_trailing_assistant_for_claude, is_claude
        from lib.llm.body import _validate_image_blocks
        if is_claude(slot.model) and body.get('messages'):
            _strip_trailing_assistant_for_claude(body['messages'], slot.model)
            # Reconcile any mislabeled image data-URI media type BEFORE the
            # downscale pass. On this pre-built-body swap path (dispatch
            # swapped the model onto Claude), build_body's _validate_image_blocks
            # never ran, so a stored data:image/jpeg URI holding PNG bytes would
            # reach the strict Anthropic Messages API and 400 the turn. Run it
            # here so the swap path has the same self-consistency guarantee as
            # the fresh-build path.
            _validate_image_blocks(body['messages'])
            _downscale_oversized_images(body['messages'], slot.model)
        from lib.llm.body import _inject_gemini_thought_signatures
        if _is_gemini(slot.model) and body.get('messages'):
            _inject_gemini_thought_signatures(body['messages'], slot.model)
        if (slot.protocol or '') == 'responses':
            body['_responses_feature_profile'] = (
                getattr(slot, 'responses_profile', '') or 'compatible')
        return body
    body = build_body(
        slot.model, body_or_messages,
        max_tokens=max_tokens, temperature=temperature,
        thinking_enabled=thinking_enabled, preset=effort or preset,
        tools=tools, stream=True,
        thinking_format=slot.thinking_format or '',
        provider_id=(getattr(slot, 'routing_provider_id', '')
                     or slot.provider_id or ''),
    )
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
        slot.model,
        body.get('max_tokens'),
        route_key=route_limit_key,
        declared_limit=int(getattr(slot, 'max_output_tokens', 0) or 0),
    )
    if (slot.protocol or '') == 'responses':
        body['_responses_feature_profile'] = (
            getattr(slot, 'responses_profile', '') or 'compatible')
    return body


def _settle_stream_result(slot, usage, *, latency, ttft, state,
                          cache_conv_id, tag, dispatcher=None,
                          stream_result=None):
    """Settle one provider stream result exactly once for both dispatch loops.

    The sync ``dispatch_stream`` and async ``async_dispatch_stream`` loops
    differ in transport scheduling, while both carry the same cgroup-headroom
    and warm-key pre-send gates, so the LOOPS are intentionally NOT unified.
    Once a slot answers, its typed
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
        except (ValueError, TypeError) as _e_audit:
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
        usage['_dispatch'] = {
            'key': slot.key_name, 'model': slot.model,
            'key_tail': (slot.api_key or '')[-4:],
            'provider_id': (getattr(slot, 'routing_provider_id', '')
                            or slot.provider_id),
            'route_snapshot': dict(
                getattr(slot, 'route_snapshot', {}) or {}),
            'protocol': slot.protocol or 'openai',
            'responses_profile': getattr(
                slot, 'responses_profile', '') or '',
            'latency_ms': round(latency_ms),
            'ttft_ms': round(ttft_ms, 3) if ttft_ms is not None else None,
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


def _cycling_can_ever_serve(dispatcher, capability, *, initial_exclude_models,
                            durable_models, durable_keys, durable_pairs,
                            strict_model, prefer_model):
    """True iff the slot-cooldown cycle can EVER yield a slot again.

    Applies ONLY the caller's own model bans + durable route/permission/quota
    exclusions — the classes that cannot heal inside one dispatch call. An
    empty answer means every capable slot is dead on a non-healable class, so
    sleeping-and-retrying would cycle forever: the 2026-08-05 CI hang
    (233daa6 serial lane, insight second-pass) spun on
    ``_429_count > 0 or _slots_exist`` — self-sustaining, because
    ``note_cooldown_cycle()`` increments ``_429_count``, so after ANY single
    cooldown cycle the condition stayed true even with every key durably
    excluded by 401.

    Transient exclusions (unreachable / timeout / 502 / rate-limit cooldown)
    are deliberately IGNORED here: the 60s exclusion reset resurrects them
    and each resurrection burns a bounded hard attempt, so that cycling
    terminates on its own.
    """
    return dispatcher.has_capable_slots(
        capability,
        exclude_models=initial_exclude_models | durable_models,
        exclude_keys=durable_keys,
        exclude_pairs=durable_pairs,
        prefer_model=prefer_model if strict_model else None)


class _StreamRetryState:
    """Shared retry/exclusion bookkeeping for the streaming dispatch loops.

    Both ``dispatch_stream`` (sync) and ``async_dispatch_stream`` (async) run
    the SAME exclusion state machine around their (different)
    transport calls:

      * ``exclude``       — models excluded entirely (hard model errors or
                            an explicit provider route-missing verdict).
      * ``exclude_keys``  — keys excluded entirely (quota exhaustion / auto-
                            exhausted after consecutive 429s).
      * ``exclude_pairs`` — ``(key_name, model)`` pairs excluded (permission /
                            timeout / unreachable / strict-model errors), so
                            another key serving the same model is still tried.

    This class owns the state + the pure transitions that used to be hand-
    duplicated in both loops (init, the 60s periodic exclusion reset, the
    slot-pick exclusion kwargs, the avoid-set relaxation, and the per-error
    set mutations).  Loop control (``continue`` / ``break`` / sleep) and the
    transport call stay in each function — only the bookkeeping is shared, so
    the two can never drift on WHICH set a given error mutates.

    Counters:
      * ``hard_attempts`` counts genuine failures (capped by ``max_retries``).
      * ``_429_count`` counts completed upstream 429 attempts.
      * ``_cooldown_cycles`` counts slot-pool polling only; it is never exposed
        as a retry attempt.
    """

    _EXCLUSION_RESET_INTERVAL = 60  # reset hard-error exclusions every 60s of 429 cycling

    def __init__(self, exclude_models=None, avoid_pairs=None):
        self.exclude = set(exclude_models) if exclude_models else set()
        # Caller-provided model exclusions are permanent for this dispatch —
        # remembered so the periodic reset doesn't re-introduce them.
        self._initial_exclude_models = set(self.exclude)
        self.exclude_keys = set()
        self.exclude_pairs = set()
        # DURABLE exclusions — route-missing models, permission (401/403)
        # pairs/keys, and quota-dead keys. The transient sets above are cleared
        # every 60s of 429 cycling
        # (a 502/timeout may be a gateway restart that recovered);
        # rejection classes cannot heal inside one dispatch call, so
        # resurrecting them only re-burns a hard attempt on a guaranteed
        # failure (2026-08-03: a dead kimi-k3 key was re-tried every 60s,
        # consuming all 3 hard attempts over ~2min). They survive
        # maybe_reset_exclusions; the NEXT dispatch call still starts fresh,
        # so a fixed key self-heals between calls.
        self.exclude_keys_durable = set()
        self.exclude_pairs_durable = set()
        # A provider's explicit "model route not found" verdict is as durable
        # as permission/quota for this dispatch.  Unlike a 502/timeout, retrying
        # the same wire ID after the 60s transient reset cannot succeed.
        self.exclude_models_durable = set()
        # Caller-provided avoid set seeds exclude_pairs so the first pick
        # already steers around it; tracked separately so it can be relaxed
        # as a last resort when every other slot is cooled/excluded.
        self._initial_avoid = set(avoid_pairs) if avoid_pairs else set()
        if self._initial_avoid:
            self.exclude_pairs |= self._initial_avoid
        self.last_err = None
        # First deterministic PAYLOAD HTTP 400 of this dispatch. Route-missing
        # catalogue errors are deliberately excluded: when every provider-
        # reaching pair fails with different payload 400s, the first (usually
        # the preferred model's rejection) is the actionable cause — the last
        # last-ditch fallback's 400 would mask it at raise time.
        self.first_bad_request_err = None
        self.hard_attempts = 0
        self._429_count = 0
        self._credential_delivery_anomaly_count = 0
        self._cooldown_cycles = 0
        self._started_at = time.monotonic()
        self.queue_wait_ms = 0.0
        self._last_exclusion_reset = time.monotonic()
        # Monotonic timestamp when the current UNBROKEN gateway-5xx streak
        # began (None = no active streak). Set on the first gateway 502/503/504,
        # cleared by any real per-key 429 or a success. Used to bound a
        # whole-upstream outage without capping genuine per-key contention.
        self._gateway_streak_start = None
        # Monotonic timestamp of the FIRST genuine-429 starvation signal in
        # this dispatch call (None = not starving). Feeds the bounded
        # saturation escalation (): real per-key/contention 429s
        # and 429-equivalent cooldown cycles start the clock; gateway 5xx
        # has its own streak budget above and never starts this one.
        self._saturation_start = None

    def record_queue_wait(self, started_monotonic: float) -> None:
        """Accumulate only explicit dispatcher backpressure waits."""
        self.queue_wait_ms += max(
            0.0, (time.monotonic() - started_monotonic) * 1000)

    @property
    def total_attempts(self):
        return self.hard_attempts + self._429_count

    @property
    def capacity_wait_cycles(self):
        return self._429_count + self._cooldown_cycles

    def wait_status(self):
        elapsed = max(0.0, time.monotonic() - self._started_at)
        return DispatchWaitStatus(
            kind='waiting_slot',
            request_elapsed_s=elapsed,
            transport_idle_s=elapsed,
            semantic_idle_s=elapsed,
        )

    def gateway_outage_exceeded(self, budget_s):
        """True when nothing but gateway 5xx has come back for > ``budget_s``.

        ``budget_s <= 0`` disables the cap (legacy infinite-rotation behaviour).
        The streak must be active (a gateway 5xx was seen and no real 429 /
        success has cleared it since)."""
        if budget_s <= 0 or self._gateway_streak_start is None:
            return False
        return (time.monotonic() - self._gateway_streak_start) > budget_s

    def maybe_reset_exclusions(self, log_prefix, label):
        """Periodically clear hard-error exclusions during 429 cycling.

        502/timeout exclusions may be transient (gateway restart) but are
        otherwise permanent for the dispatch call.  After 60s of 429 cycling
        give excluded slots another chance — still-broken ones get re-excluded
        quickly.  Caller-provided ``exclude_models`` are preserved.  Returns
        the set of currently-active exclusions (for the caller's log line)
        before the reset, or None when nothing was reset.
        """
        if self.capacity_wait_cycles > 0 and (
                time.monotonic() - self._last_exclusion_reset) >= self._EXCLUSION_RESET_INTERVAL:
            if self.exclude or self.exclude_keys or self.exclude_pairs:
                logger.info(
                    '%s %s: resetting hard-error exclusions after %ds of 429 '
                    'cycling (cycle #%d) — exclude_models=%s exclude_keys=%s '
                    'exclude_pairs=%s',
                    log_prefix, label, self._EXCLUSION_RESET_INTERVAL,
                    self.capacity_wait_cycles, self.exclude, self.exclude_keys,
                    self.exclude_pairs)
                self.exclude.clear()
                self.exclude |= self._initial_exclude_models
                self.exclude |= self.exclude_models_durable
                self.exclude_keys.clear()
                self.exclude_pairs.clear()
            if (self.exclude_models_durable or self.exclude_keys_durable
                    or self.exclude_pairs_durable):
                logger.debug(
                    '%s %s: durable exclusions survive the reset '
                    '(route/permission/quota cannot heal mid-call): '
                    'models=%s keys=%s pairs=%s',
                    log_prefix, label, self.exclude_models_durable,
                    self.exclude_keys_durable,
                    self.exclude_pairs_durable)
            self._last_exclusion_reset = time.monotonic()

    def eff_exclude_models(self):
        """``exclude_models`` to pass to pick_and_reserve.

        Caller-provided exclusions apply from attempt 1; failure-driven ones
        only after the first attempt.
        """
        effective = self.exclude | self.exclude_models_durable
        return effective if (self.total_attempts > 0
                             or self._initial_exclude_models) else None

    def eff_exclude_keys(self):
        if self.total_attempts <= 0:
            return None
        return self.exclude_keys | self.exclude_keys_durable

    def eff_exclude_pairs(self):
        # Caller-provided avoid pairs are specifically a *first-pick* hint:
        # the previous semantic attempt already proved that slot unhealthy.
        # Waiting for this fresh dispatch call to fail once before exposing the
        # set silently routes the retry straight back to the poisoned slot.
        if self.total_attempts <= 0 and not self._initial_avoid:
            return None
        return self.exclude_pairs | self.exclude_pairs_durable

    def relax_avoid_if_exhausted(self):
        """Drop the caller-provided avoid set when every other slot is gone.

        Zero-byte force-rotate uses ``avoid_pairs`` to steer away from a
        freshly-poisoned slot; if the rest of the pool is exhausted we'd
        rather retry the bad slot than fail outright.  Relaxes at most once.
        Returns True if it relaxed (caller should ``continue``).
        """
        if self._initial_avoid and self._initial_avoid <= self.exclude_pairs:
            self.exclude_pairs -= self._initial_avoid
            self._initial_avoid = set()
            return True
        return False

    def note_free_429(self, *, is_gateway=False):
        """A routine (non-quota) 429 — free retry, does not count toward the cap.

        ``is_gateway`` distinguishes a genuine per-key 429 (contention — rotate
        forever) from a gateway 5xx mapped onto this path (whole-upstream
        outage — bounded by ``gateway_outage_exceeded``). A gateway 5xx opens /
        extends the outage streak; a REAL 429 (or a success) clears it, because
        it proves the upstream is answering and only this key is throttled."""
        self._429_count += 1
        if is_gateway:
            if self._gateway_streak_start is None:
                self._gateway_streak_start = time.monotonic()
        else:
            self._gateway_streak_start = None
            if self._saturation_start is None:
                self._saturation_start = time.monotonic()

    def note_credential_delivery_anomaly(self, error) -> bool:
        """Count a contradictory missing-credential response.

        Returns True when its dedicated actual-response budget is exhausted.
        The counter is independent of hard attempts and ordinary 429 cycling.
        """
        (self._credential_delivery_anomaly_count,
         exhausted) = _advance_credential_delivery_anomaly(
             error, self._credential_delivery_anomaly_count)
        return exhausted

    def note_success(self):
        """A slot answered — clear any active gateway-outage streak."""
        self._gateway_streak_start = None
        self._saturation_start = None

    def saturation_exceeded(self, budget_s):
        """True when every slot has been 429-starved for > ``budget_s``.

        ``budget_s <= 0`` disables the cap (legacy infinite-rotation
        behaviour). Requires an active starvation clock — a dispatch that
        never saw a genuine 429 (e.g. hard-error churn) is not 'saturated'.
        """
        if budget_s <= 0 or self._saturation_start is None:
            return False
        return (time.monotonic() - self._saturation_start) > budget_s

    def note_cooldown_cycle(self):
        """Record one pool poll with no upstream request attempt."""
        self._cooldown_cycles += 1
        if self._saturation_start is None:
            self._saturation_start = time.monotonic()

    def note_quota_key(self, slot):
        """Quota-exhausted 429 — disable the key for this dispatch; hard attempt.

        Durable: a dead balance cannot recover inside one dispatch call, so
        the periodic 429-cycling reset must not resurrect it."""
        self.exclude_keys_durable.add(slot.key_name)
        self.hard_attempts += 1

    def note_route_missing_model(self, model, error):
        """Durably exclude one unserved wire ID without making it the cause.

        Route-missing is catalog/routing evidence, not a payload rejection.
        Keep it as the terminal error only when no actionable error from a
        provider-reaching route has already been observed.
        """
        model = str(model or '')
        if model:
            self.exclude.add(model)
            self.exclude_models_durable.add(model)
        self.last_err = _remember_route_missing_error(self.last_err, error)
        self.hard_attempts += 1

    def note_permission_pair(self, slot, dispatcher, capability, log_prefix):
        """Permission denied — exclude the (key, model) PAIR; escalate to a
        whole-key exclusion only if EVERY model for this key is now excluded.

        Durable: an auth rejection cannot heal inside one dispatch call (see
        ``exclude_pairs_durable``)."""
        self.exclude_pairs_durable.add((slot.key_name, slot.model))
        self.hard_attempts += 1
        _key_pairs = {(kn, m) for kn, m in self.exclude_pairs_durable
                      if kn == slot.key_name}
        _key_models = {s.model for s in dispatcher.slots
                       if s.key_name == slot.key_name
                       and (not capability or capability in s.capabilities)}
        if _key_models and _key_models <= {m for _, m in _key_pairs}:
            self.exclude_keys_durable.add(slot.key_name)
            logger.warning('%s Permission denied on ALL models for key %s — '
                           'excluding entire key', log_prefix, slot.key_name)
            return True
        return False

    def note_permission_key(self, slot):
        """Credential-wide rejection — durably exclude the whole key.

        An OAuth HTTP 401 rejects the shared bearer credential, not one model
        route. Trying every sibling model with that same token only repeats a
        guaranteed failure and can fan one expired login out across a task.
        """
        self.exclude_keys_durable.add(slot.key_name)
        self.hard_attempts += 1

    def note_unreachable_pair(self, slot):
        """Endpoint unreachable — exclude the pair; hard attempt.  (The caller
        sets the slot cooldown — that's slot mutation, not retry bookkeeping.)"""
        self.exclude_pairs.add((slot.key_name, slot.model))
        self.hard_attempts += 1

    def note_generic_error(self, slot, *, is_timeout, strict_model):
        """Generic stream error: a timeout or a strict-model error excludes the
        PAIR (other keys of the model still tried); anything else excludes the
        whole MODEL.  Hard attempt either way."""
        if is_timeout or strict_model:
            self.exclude_pairs.add((slot.key_name, slot.model))
        else:
            self.exclude.add(slot.model)
        self.hard_attempts += 1


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
