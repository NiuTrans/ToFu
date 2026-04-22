"""lib/llm_dispatch/api.py — Module-level dispatch convenience functions.

High-level API for LLM dispatch: dispatch_chat, dispatch_stream,
dispatch_fastest, dispatch_parallel, smart_chat, smart_chat_batch, etc.
All functions call get_dispatcher() internally.

Usage:
    from lib.llm_dispatch import dispatch_chat, dispatch_stream, smart_chat
"""

import threading
import time
from collections import defaultdict

from lib.log import get_logger

from .factory import get_dispatcher

logger = get_logger(__name__)



__all__ = [
    'pick_key_for_model',
    'dispatch_chat',
    'dispatch_stream',
    'dispatch_fastest',
    'dispatch_parallel',
    'get_dispatch_status',
    '_group_by_capability',
    'smart_chat',
    'smart_chat_batch',
]


# ═══════════════════════════════════════════════════════════
#  Key rotation convenience
# ═══════════════════════════════════════════════════════════

def pick_key_for_model(model: str) -> tuple:
    """Public convenience: pick the best API key for *model*.

    Returns (api_key, key_name, slot_or_None).
    Callers who already choose their own model (orchestrator, route handlers)
    use this to **spread RPM across keys** instead of always using the same key.

    Example::

        from lib.llm_dispatch import pick_key_for_model
        api_key, key_name, slot = pick_key_for_model(model)
        body = build_body(model, messages, ...)
        result = stream_chat(body, ..., api_key=api_key)
        if slot:
            slot.record_request(success=True, latency_ms=..., ttft_ms=...)
    """
    return get_dispatcher().pick_key_for_model(model)


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_chat (non-streaming)
# ═══════════════════════════════════════════════════════════

def dispatch_chat(messages, *, max_tokens=4096, temperature=0,
                  thinking_enabled=False, preset='low', effort=None,
                  capability='text', prefer_model=None, tools=None,
                  extra=None, max_retries=3, log_prefix='',
                  timeout=None, strict_model=False):
    """Smart dispatch: pick the best available slot and send a non-streaming chat.

    Auto-retries on failure with fallback to different slots.

    Args:
        messages: List of chat messages
        max_tokens: Max output tokens
        temperature: Sampling temperature
        thinking_enabled: Enable extended thinking
        preset/effort: Thinking effort level
        capability: Required capability ('text', 'vision', 'thinking', 'cheap')
        prefer_model: Preferred model name
        tools: Tool definitions for function calling
        extra: Extra body parameters
        max_retries: Number of slots to try before giving up

    Returns:
        (content_text: str, usage_dict: dict)
    """
    from lib.llm_client import ContentFilterError, InvalidImageError, PermissionError_, PromptTooLongError, RateLimitError, StreamOnlyError, chat

    dispatcher = get_dispatcher()
    exclude = set()           # models to exclude entirely (hard model errors)
    exclude_keys = set()      # keys to exclude entirely
    exclude_pairs = set()     # (key_name, model) pairs to exclude (permission errors)
    last_err = None

    # ★ Pre-exclude stream-only models from non-streaming dispatch.
    #   Models like qwq-plus only support stream=True and will reject
    #   non-streaming requests with HTTP 400.
    for slot in dispatcher.slots:
        if slot.stream_only and slot.model not in exclude:
            exclude.add(slot.model)
            logger.debug('%s Excluding stream-only model %s from non-streaming dispatch',
                        log_prefix, slot.model)

    # ★ Total time budget — all attempts share this deadline to prevent
    #   serial timeout accumulation (e.g. 5 × 35s = 175s).
    #   429 retries do NOT consume the budget — only real errors do.
    _per_attempt_timeout = timeout if timeout is not None else (
        30 if capability == 'cheap' else 120)
    _total_budget = _per_attempt_timeout * min(max_retries, 3)  # cap at 3× single timeout
    _deadline = time.time() + _total_budget

    # ★ hard_attempts counts only non-429 failures; 429 loops forever.
    hard_attempts = 0
    _429_count = 0
    _last_exclusion_reset = time.monotonic()  # ★ track when we last reset hard-error exclusions
    _EXCLUSION_RESET_INTERVAL = 60  # reset exclude_pairs every 60s during 429 cycling

    while hard_attempts < max_retries:
        _remaining = _deadline - time.time()
        if _remaining < 3:   # less than 3s left — not worth trying
            logger.debug('%s Total budget exhausted (%.1fs left), stopping dispatch',
                         log_prefix, _remaining)
            break

        total_attempts = hard_attempts + _429_count

        # ★ Periodically reset hard-error exclusions during 429 cycling.
        #   502/timeout errors may be transient (gateway restart), but
        #   exclude_pairs is permanent per dispatch call.  After 60s,
        #   give excluded slots another chance.
        if _429_count > 0 and (time.monotonic() - _last_exclusion_reset) >= _EXCLUSION_RESET_INTERVAL:
            if exclude_pairs or exclude_keys:
                logger.info(
                    '%s dispatch_chat: resetting hard-error exclusions '
                    'after %ds of 429 cycling (cycle #%d) — '
                    'exclude_keys=%s exclude_pairs=%s',
                    log_prefix, _EXCLUSION_RESET_INTERVAL, _429_count,
                    exclude_keys, exclude_pairs)
                exclude_keys.clear()
                exclude_pairs.clear()
            _last_exclusion_reset = time.monotonic()

        slot = dispatcher.pick_and_reserve(
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=exclude if total_attempts > 0 else None,
            exclude_keys=exclude_keys if total_attempts > 0 else None,
            exclude_pairs=exclude_pairs if total_attempts > 0 else None,
            strict_model=strict_model)
        if slot is None:
            # All slots in cooldown / excluded — wait briefly and retry
            if _429_count > 0:
                time.sleep(0.3)
                _429_count += 1
                if _429_count % 20 == 0:
                    logger.info(
                        '%s dispatch_chat: still cycling 429 (%d times), '
                        'waiting for cooldown to expire…',
                        log_prefix, _429_count)
                continue
            break

        t0 = time.time()
        tag = f'{log_prefix}[D:{slot.key_name}:{slot.model}]'

        try:
            # Merge tools into extra dict (chat() doesn't have a tools param)
            _extra = dict(extra) if extra else {}
            if tools:
                _extra['tools'] = tools

            # Use the lesser of per-attempt timeout and remaining budget
            _timeout = min(_per_attempt_timeout, _remaining)

            content, usage = chat(
                model=slot.model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                thinking_enabled=thinking_enabled,
                effort=effort or preset,
                api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                extra=_extra or None,
                log_prefix=tag,
                max_retries=0,  # fail fast — dispatcher handles retries
                timeout=_timeout,
                thinking_format=slot.thinking_format or '',
                provider_id=slot.provider_id or '',
            )
            latency = (time.time() - t0) * 1000
            slot.record_success(latency)
            # Inject dispatch metadata so callers know which slot served this
            if isinstance(usage, dict):
                usage['_dispatch'] = {
                    'key': slot.key_name, 'model': slot.model,
                    'provider_id': slot.provider_id,
                    'latency_ms': round(latency),
                    'attempt': hard_attempts + 1,
                    '429_retries': _429_count,
                }
            return content, usage

        except RateLimitError as e:
            _is_quota = bool(getattr(e, 'is_quota', False))
            _err_str = str(e)[:200]
            slot.record_error(is_rate_limit=True,
                              is_quota_exhausted=_is_quota,
                              error=_err_str if _is_quota else '')
            last_err = e
            if _is_quota:
                # ★ Persistent billing/quota exhaustion (HTTP 402 or
                #   429+insufficient_quota). Retrying on this key all day
                #   is pointless — exclude the entire key and move on.
                exclude_keys.add(slot.key_name)
                hard_attempts += 1
                logger.warning(
                    '%s Quota exhausted on %s:%s — disabling key for '
                    'today: %s',
                    log_prefix, slot.key_name, slot.model, _err_str)
                continue
            _429_count += 1
            # ★ Don't exclude anything — slot.record_error() sets a 0.5s
            #   cooldown which naturally steers pick_and_reserve to another
            #   slot.  After cooldown the slot is eligible again.
            # ★ Fast 0.3s sleep — 429 in shared-key = contention, poll aggressively.
            _err_body = str(e)[:300]
            if _429_count <= 3 or _429_count % 100 == 0:
                logger.warning(
                    '%s 429 rate-limited on %s:%s (cycle #%d) — body: %s',
                    log_prefix, slot.key_name, slot.model, _429_count, _err_body)
            else:
                logger.info(
                    '%s 429 rate-limited on %s:%s (cycle #%d)',
                    log_prefix, slot.key_name, slot.model, _429_count)
            # ★ If this 429 just tripped the consecutive-429 streak threshold,
            #   the key was auto-marked as exhausted — exclude it explicitly
            #   so the current retry loop moves on immediately.
            try:
                from lib.key_stats import is_key_enabled
                if not is_key_enabled(slot.provider_id, slot.key_name):
                    exclude_keys.add(slot.key_name)
                    hard_attempts += 1
                    logger.warning(
                        '%s Key %s auto-exhausted after %d consecutive 429s '
                        '— excluding for today',
                        log_prefix, slot.key_name, _429_count)
                    continue
            except Exception:
                pass
            time.sleep(0.3)
            # ★ Don't increment hard_attempts — 429 retries are free
            continue

        except PermissionError_ as e:
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            last_err = e
            exclude_pairs.add((slot.key_name, slot.model))
            hard_attempts += 1
            # ★ If ALL models for this key have been excluded (all got 401),
            #   exclude the entire key to avoid further wasted attempts.
            _key_pairs = {(kn, m) for kn, m in exclude_pairs if kn == slot.key_name}
            _key_models = {s.model for s in dispatcher.slots
                           if s.key_name == slot.key_name
                           and (not capability or capability in s.capabilities)}
            if _key_models and _key_models <= {m for _, m in _key_pairs}:
                exclude_keys.add(slot.key_name)
                logger.warning(
                    '%s Permission denied on ALL models for key %s — '
                    'excluding entire key',
                    log_prefix, slot.key_name)
            else:
                logger.warning(
                    '%s Permission denied on %s:%s — excluding pair, '
                    'remaining slots: %s',
                    log_prefix, slot.key_name, slot.model,
                    dispatcher.summarize_slots(capability))

        except ContentFilterError as e:
            # HTTP 450 — content policy violation. No point retrying with
            # different model/key since the same content will be blocked.
            logger.warning('%s Content filter (HTTP 450) — not retrying: %s', tag, str(e)[:200], exc_info=True)
            raise

        except PromptTooLongError:
            logger.warning('%s Prompt/request too large — not retrying on other slots '
                           '(same payload = same rejection)', tag)
            raise   # ★ Escape to orchestrator for reactive compaction

        except InvalidImageError:
            logger.warning('%s Image content error — not retrying on other slots '
                           '(same image = same rejection)', tag)
            raise   # ★ Same payload = same rejection on all keys

        except StreamOnlyError as e:
            # ★ Model only supports streaming — exclude entire model and
            #   try a different one. Mark the slot so future dispatches
            #   don't repeat this mistake.
            slot.stream_only = True
            slot.record_error(is_rate_limit=False)
            exclude.add(slot.model)
            last_err = e
            hard_attempts += 1
            logger.warning('%s Model %s only supports streaming — excluding '
                          'from non-streaming dispatch, trying next model',
                          log_prefix, slot.model)

        except Exception as e:
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False)
            last_err = e
            # Timeout errors → exclude only this (key, model) pair,
            # not the entire model — other backends for different models
            # may still be fast.  True model-level failures (4xx, etc.)
            # still exclude the whole model.
            _is_timeout = 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()
            if _is_timeout:
                exclude_pairs.add((slot.key_name, slot.model))
                logger.debug('%s Timeout (%.0fms) — excluding pair '
                             '%s:%s, trying next slot', tag, latency, slot.key_name, slot.model, exc_info=True)
            elif strict_model:
                # ★ strict_model: only exclude pair, keep other keys
                exclude_pairs.add((slot.key_name, slot.model))
                logger.debug('%s Error (strict_model): %s — excluding pair '
                             '%s:%s, trying other keys', tag, str(e)[:200],
                             slot.key_name, slot.model, exc_info=True)
            else:
                exclude.add(slot.model)
                logger.debug('%s Error: %s — trying next slot', tag, str(e)[:200], exc_info=True)
            hard_attempts += 1

    raise last_err or RuntimeError(
        'All %d dispatch attempts failed for capability=%s' % (max_retries, capability))


# ═══════════════════════════════════════════════════════════
#  Helpers — pre-built body adaptation for model swap
# ═══════════════════════════════════════════════════════════

def _readjust_thinking_params(body: dict, new_model: str, thinking_format: str):
    """Fix thinking parameters when a pre-built body is dispatched to a different model family.

    Different model families use incompatible thinking parameter formats:
      - Claude:   thinking.type = 'adaptive'
      - Doubao:   thinking.type = 'enabled' / 'disabled'
      - GLM:      thinking.type = 'enabled' / 'disabled'
      - MiniMax:  reasoning_split = True
      - Qwen/Gemini/LongCat: enable_thinking = True/False

    When dispatch swaps the model (e.g. Claude → Doubao), the body may carry
    the wrong format, causing HTTP 400 errors. This function detects mismatches
    and converts to the correct format for the new model.
    """
    from lib.llm_client import is_claude, is_doubao, is_gemini, is_glm, is_longcat, is_minimax, is_qwen

    # Detect current thinking state from the body
    thinking_dict = body.get('thinking')
    enable_thinking = body.get('enable_thinking')
    reasoning_split = body.get('reasoning_split')
    effort = body.get('effort')

    has_thinking_params = (thinking_dict is not None or
                           enable_thinking is not None or
                           reasoning_split is not None)
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

    # Clean ALL thinking-related keys before re-setting
    body.pop('thinking', None)
    body.pop('enable_thinking', None)
    body.pop('reasoning_split', None)
    body.pop('effort', None)

    # Re-apply for the new model using the same logic as build_body
    _tf = thinking_format
    if _tf == 'enable_thinking' or (not _tf and (is_longcat(new_model) or is_qwen(new_model) or is_gemini(new_model))):
        body['enable_thinking'] = is_enabled
    elif _tf == 'thinking_type' or (not _tf and is_doubao(new_model)):
        body['thinking'] = {'type': 'enabled' if is_enabled else 'disabled'}
    elif not _tf and is_glm(new_model):
        body['thinking'] = {'type': 'enabled' if is_enabled else 'disabled'}
        if is_enabled:
            body['temperature'] = 1.0
        else:
            body['temperature'] = max(body.get('temperature', 0.7), 0.01)
    elif not _tf and is_minimax(new_model):
        if is_enabled:
            body['reasoning_split'] = True
    elif not _tf and is_claude(new_model):
        if is_enabled:
            # Keep this in sync with the Claude branch in build_body().
            #   • 4.6 and earlier: adaptive + temperature=1.0
            #   • 4.7+: adaptive + display='summarized' (required to see
            #           reasoning trace), and NO temperature (ignored today,
            #           may be rejected in a future revision).
            from lib.llm_client import is_claude_opus_47
            body['thinking'] = {'type': 'adaptive'}
            if is_claude_opus_47(new_model):
                body['thinking']['display'] = 'summarized'
            else:
                body['temperature'] = 1.0
            if effort:
                # xhigh is Opus 4.7-only; downgrade on older Claude to avoid HTTP 400.
                if effort == 'xhigh' and not is_claude_opus_47(new_model):
                    effort = 'high'
                body['effort'] = effort
    # else: standard OpenAI-compatible — no thinking params needed

    # ── Claude Opus 4.7+ rejects sampling params (HTTP 400) ──
    # Strip unconditionally after re-setting, since non-4.7 branches above
    # may have assigned temperature=1.0 before a potential model swap.
    from lib.llm_client import is_claude_opus_47
    if is_claude_opus_47(new_model):
        for _k in ('temperature', 'top_p', 'top_k'):
            body.pop(_k, None)


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_stream (streaming)
# ═══════════════════════════════════════════════════════════

def dispatch_stream(body_or_messages, *, on_thinking=None, on_content=None,
                    on_tool_call_ready=None,
                    abort_check=None, max_tokens=4096, temperature=0,
                    thinking_enabled=False, preset='low', effort=None,
                    capability='text', prefer_model=None, tools=None,
                    max_retries=3, log_prefix='', strict_model=False,
                    on_retry=None):
    """Smart dispatch for streaming requests.

    Accepts either:
      - A pre-built body dict (with 'messages' key) — legacy path; the
        body is re-adapted to each dispatched slot via
        ``_readjust_thinking_params`` / ``_clamp_max_tokens``.
      - Raw messages list — PREFERRED path: ``build_body`` is called for
        the actual slot picked, so provider-specific quirks (Claude Opus 4.7
        sampling param rejection, GLM temperature clamp, Qwen enable_thinking
        shape, …) are always correct for the final target model.

    The raw-messages path accepts ``tools=`` so callers doing tool loops
    (e.g. paper report, swarm agents) no longer need to pre-build the body.

    429 handling:
      - Does NOT count toward max_retries — retries indefinitely.
      - Does NOT exclude any pair — the 0.5s slot cooldown naturally
        steers the picker to the next slot.  After cooldown expires the
        slot becomes eligible again, so slots rotate automatically.
      - Sleeps 0.3s between 429 retries to avoid a tight spin loop.
      - Respects abort_check so the user can still cancel.

    strict_model:
      When True AND prefer_model is set, the dispatcher will NEVER silently
      fall back to a different model.  429 retries stay within the preferred
      model's slots (different keys / alias group members).  Use this for
      user-facing requests where the frontend explicitly chose a model.

    Returns:
        (msg: str, finish_reason: str, usage: dict)
    """
    from lib.llm_client import (
        AbortedError,
        ContentFilterError,
        InvalidImageError,
        PermissionError_,
        PromptTooLongError,
        RateLimitError,
        build_body,
        stream_chat,
    )

    dispatcher = get_dispatcher()
    exclude = set()           # models to exclude entirely (hard model errors)
    exclude_keys = set()      # keys to exclude entirely
    exclude_pairs = set()     # (key_name, model) pairs to exclude (permission errors)
    last_err = None

    # Detect if it's a pre-built body or raw messages
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

    # ★ hard_attempts counts only non-429 failures; 429 loops forever.
    #   Set _MAX_429_CYCLES = 0 to disable the cap (retry indefinitely).
    #   The abort_check runs every cycle so the user can always cancel.
    _MAX_429_CYCLES = 0  # 0 = infinite; set >0 to re-enable safety cap
    hard_attempts = 0
    _429_count = 0
    _last_exclusion_reset = time.monotonic()  # ★ track when we last reset hard-error exclusions
    _EXCLUSION_RESET_INTERVAL = 60  # reset exclude_pairs every 60s during 429 cycling

    while hard_attempts < max_retries:
        # Abort check — let the user cancel during 429 cycling
        if abort_check and abort_check():
            from lib.llm_client import AbortedError as _AE
            raise _AE('Aborted during dispatch retry')

        # ★ Safety cap on 429 cycling — if we've been rate-limited for
        #   too many cycles, something is fundamentally wrong (e.g. payload
        #   too large causing 413 on some slots and 429 on the rest).
        if _MAX_429_CYCLES > 0 and _429_count >= _MAX_429_CYCLES:
            logger.error(
                '%s dispatch_stream: 429 cycling exceeded safety cap '
                '(%d cycles) — giving up. Last error: %s',
                log_prefix, _MAX_429_CYCLES, str(last_err)[:200])
            raise last_err or RuntimeError(
                'Rate-limited %d times without success' % _429_count)

        total_attempts = hard_attempts + _429_count
        # Log available slots at start of each attempt for debugging
        logger.debug(
            '%s dispatch_stream attempt hard=%d/%d 429=%d: '
            'slots: %s',
            log_prefix, hard_attempts + 1, max_retries, _429_count,
            dispatcher.summarize_slots(capability))

        # ★ Periodically reset hard-error exclusions during 429 cycling.
        #   502/timeout errors may be transient (gateway restart), but
        #   exclude_pairs is permanent per dispatch call.  After 60s,
        #   give excluded slots another chance — if still broken, they'll
        #   get re-excluded quickly.  This prevents permanently losing
        #   slots that recovered while we were in the 429 retry loop.
        if _429_count > 0 and (time.monotonic() - _last_exclusion_reset) >= _EXCLUSION_RESET_INTERVAL:
            if exclude or exclude_keys or exclude_pairs:
                logger.info(
                    '%s dispatch_stream: resetting hard-error exclusions '
                    'after %ds of 429 cycling (cycle #%d) — '
                    'exclude_models=%s exclude_keys=%s exclude_pairs=%s',
                    log_prefix, _EXCLUSION_RESET_INTERVAL, _429_count,
                    exclude, exclude_keys, exclude_pairs)
                exclude.clear()
                exclude_keys.clear()
                exclude_pairs.clear()
                # Don't reset hard_attempts — we still want to count
                # genuinely broken slots toward the retry limit
            _last_exclusion_reset = time.monotonic()

        slot = dispatcher.pick_and_reserve(
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=exclude if total_attempts > 0 else None,
            exclude_keys=exclude_keys if total_attempts > 0 else None,
            exclude_pairs=exclude_pairs if total_attempts > 0 else None,
            strict_model=strict_model)
        if slot is None:
            # All slots in cooldown / excluded — wait briefly and retry
            if _429_count > 0:
                time.sleep(0.3)
                _429_count += 1
                if _429_count % 20 == 0:
                    logger.info(
                        '%s dispatch_stream: still cycling 429 (%d times, strict=%s), '
                        'waiting for cooldown to expire…',
                        log_prefix, _429_count, strict_model)
                continue
            logger.warning(
                '%s dispatch_stream: NO SLOT available on attempt %d/%d. '
                'exclude_models=%s exclude_keys=%s exclude_pairs=%s strict_model=%s. '
                'Available slots: %s',
                log_prefix, hard_attempts + 1, max_retries,
                exclude, exclude_keys, exclude_pairs, strict_model,
                dispatcher.summarize_slots(capability))
            break

        t0 = time.time()
        ttft_recorded = False
        tag = f'{log_prefix}[D:{slot.key_name}:{slot.model}]'

        # Build body for this slot's model
        if is_body:
            body = dict(body_or_messages)
            body['model'] = slot.model
            # Allow callers to inject tools even on the pre-built path
            # (uncommon, but keeps the API symmetric).
            if tools is not None:
                body['tools'] = tools
            # ★ Re-clamp max_tokens for the new model — the pre-built body
            #   may have been constructed for a model with a higher limit
            #   (e.g. Claude 128000) but dispatch swapped to a lower-limit
            #   model (e.g. gpt-4.1-mini 32768).
            if 'max_tokens' in body:
                from lib.llm_client import _clamp_max_tokens
                body['max_tokens'] = _clamp_max_tokens(
                    slot.model, body['max_tokens'])
            # ★ Re-adjust thinking parameters when model family changes.
            #   Pre-built body may carry thinking.type='adaptive' (Claude)
            #   but dispatch swapped to Doubao/GLM which expects 'enabled'.
            _readjust_thinking_params(body, slot.model,
                                      slot.thinking_format or '')
            # ★ Claude 4.6 prefill guard: if dispatch swapped the model
            #   to Claude on a pre-built body from a non-Claude model,
            #   ensure messages don't end with an assistant message.
            from lib.llm_client import _downscale_oversized_images, _strip_trailing_assistant_for_claude, is_claude
            if is_claude(slot.model) and body.get('messages'):
                _strip_trailing_assistant_for_claude(body['messages'], slot.model)
                # ★ Downscale oversized images for Claude (pre-built body path)
                _downscale_oversized_images(body['messages'], slot.model)
        else:
            body = build_body(
                slot.model, body_or_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_enabled=thinking_enabled,
                preset=effort or preset,
                tools=tools,
                stream=True,
                thinking_format=slot.thinking_format or '',
                provider_id=slot.provider_id or '',
            )

        # Wrap on_content to capture TTFT
        def _on_content_wrapper(text):
            nonlocal ttft_recorded
            if not ttft_recorded:
                ttft = (time.time() - t0) * 1000
                slot.ttft_ema = (slot.ema_alpha * ttft +
                                 (1 - slot.ema_alpha) * slot.ttft_ema)
                ttft_recorded = True
            if on_content:
                on_content(text)

        try:
            msg, finish, usage = stream_chat(
                body, api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                on_thinking=on_thinking,
                on_content=_on_content_wrapper,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check,
                log_prefix=tag,
            )
            latency = (time.time() - t0) * 1000
            slot.record_success(latency)
            # Inject dispatch metadata so callers know which slot served this
            if isinstance(usage, dict):
                usage['_dispatch'] = {
                    'key': slot.key_name, 'model': slot.model,
                    'provider_id': slot.provider_id,
                    'latency_ms': round(latency),
                    'attempt': hard_attempts + 1,
                    '429_retries': _429_count,
                }
            if _429_count > 0:
                logger.info('%s dispatch_stream OK after %d 429-retries: '
                            'finish_reason=%s model=%s provider=%s latency=%.0fms',
                            log_prefix, _429_count, finish, slot.model,
                            slot.provider_id, latency)
            else:
                logger.debug('%s dispatch_stream OK: finish_reason=%s model=%s '
                            'provider=%s latency=%.0fms attempt=%d/%d',
                            log_prefix, finish, slot.model,
                            slot.provider_id, latency, hard_attempts + 1,
                            max_retries)
            return msg, finish, usage

        except RateLimitError as e:
            _is_quota = bool(getattr(e, 'is_quota', False))
            _err_str = str(e)[:200]
            slot.record_error(is_rate_limit=True,
                              is_quota_exhausted=_is_quota,
                              error=_err_str if _is_quota else '')
            last_err = e
            if _is_quota:
                # ★ Persistent billing/quota exhaustion — disable this key
                #   for the remainder of this dispatch and flag it so the
                #   user sees "out of balance" in Settings.
                exclude_keys.add(slot.key_name)
                hard_attempts += 1
                logger.warning(
                    '%s Quota exhausted on %s:%s — disabling key for '
                    'today: %s',
                    log_prefix, slot.key_name, slot.model, _err_str)
                if on_retry:
                    on_retry(attempt=hard_attempts,
                             reason='Key balance exhausted', status_code=429)
                continue
            _429_count += 1
            # ★ Don't exclude anything — slot.record_error() sets a 0.5s
            #   cooldown which naturally steers pick_and_reserve to another
            #   slot.  After cooldown expires the slot is eligible again,
            #   so all slots rotate automatically.
            # ★ Fast 0.3s sleep — 429 in a shared-key environment means
            #   contention with other users.  We need to poll aggressively
            #   to grab slots the instant they become available.  Backoff
            #   would let competing users grab slots while we sleep.
            # ★ Log response body periodically to diagnose persistent 429s
            _err_body = str(e)[:300]
            if _429_count <= 3 or _429_count % 100 == 0:
                logger.warning(
                    '%s 429 rate-limited on %s:%s (cycle #%d) — body: %s',
                    log_prefix, slot.key_name, slot.model, _429_count,
                    _err_body)
            else:
                logger.info(
                    '%s 429 rate-limited on %s:%s (cycle #%d)',
                    log_prefix, slot.key_name, slot.model, _429_count)
            # ★ If the streak just tripped, the key is now flagged as
            #   exhausted — exclude it so we don't cycle back to it.
            try:
                from lib.key_stats import is_key_enabled
                if not is_key_enabled(slot.provider_id, slot.key_name):
                    exclude_keys.add(slot.key_name)
                    hard_attempts += 1
                    logger.warning(
                        '%s Key %s auto-exhausted after %d consecutive 429s '
                        '— excluding for today',
                        log_prefix, slot.key_name, _429_count)
                    if on_retry:
                        on_retry(attempt=hard_attempts,
                                 reason='Key auto-exhausted (consecutive 429s)',
                                 status_code=429)
                    continue
            except Exception:
                pass
            if on_retry:
                on_retry(attempt=_429_count, reason='Rate limited (429)', status_code=429)
            time.sleep(0.3)
            # ★ Don't increment hard_attempts — 429 retries are free
            continue

        except PermissionError_ as e:
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False)
            last_err = e
            exclude_pairs.add((slot.key_name, slot.model))
            hard_attempts += 1
            # ★ If ALL models for this key have been excluded (all got 401),
            #   exclude the entire key to avoid further wasted attempts.
            _key_pairs = {(kn, m) for kn, m in exclude_pairs if kn == slot.key_name}
            _key_models = {s.model for s in dispatcher.slots
                           if s.key_name == slot.key_name
                           and (not capability or capability in s.capabilities)}
            if _key_models and _key_models <= {m for _, m in _key_pairs}:
                exclude_keys.add(slot.key_name)
                logger.warning(
                    '%s Permission denied on ALL models for key %s — '
                    'excluding entire key',
                    log_prefix, slot.key_name)
            else:
                logger.warning(
                    '%s Permission denied on %s:%s — excluding pair, '
                    'remaining slots: %s',
                    log_prefix, slot.key_name, slot.model,
                    dispatcher.summarize_slots(capability))

        except AbortedError:
            logger.debug('%s User aborted — stopping dispatch immediately', tag)
            raise   # ★ Don't retry on other slots, user wants to stop

        except ContentFilterError:
            logger.warning('%s Content filter (HTTP 450) — not retrying', tag, exc_info=True)
            raise   # ★ Same content = same filter, no point retrying

        except PromptTooLongError:
            logger.warning('%s Prompt/request too large — not retrying on other slots '
                           '(same payload = same rejection)', tag)
            raise   # ★ Escape to orchestrator for reactive compaction

        except InvalidImageError:
            logger.warning('%s Image content error — not retrying on other slots '
                           '(same image = same rejection)', tag)
            raise   # ★ Same payload = same rejection on all keys

        except Exception as e:
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False)
            last_err = e
            _is_timeout = 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()
            # ★ Notify frontend about retry so user sees status instead of "Waiting…"
            if on_retry:
                _status = getattr(e, 'status_code', 0) or 0
                _reason = str(e)[:120]
                if _is_timeout:
                    _reason = 'Request timed out'
                elif _status:
                    _reason = f'HTTP {_status}'
                on_retry(attempt=hard_attempts + 1, reason=_reason, status_code=_status)
            if _is_timeout:
                exclude_pairs.add((slot.key_name, slot.model))
                logger.debug('%s Timeout (%.0fms) — excluding pair '
                             '%s:%s, trying next slot', tag, latency, slot.key_name, slot.model, exc_info=True)
            elif strict_model:
                # ★ strict_model: user explicitly chose this model — only
                #   exclude this (key, model) pair so OTHER keys of the same
                #   model are still tried.  If all pairs fail, the error
                #   propagates to _llm_call_with_fallback which handles
                #   explicit (logged + user-notified) fallback.
                exclude_pairs.add((slot.key_name, slot.model))
                logger.debug('%s Stream error (strict_model): %s — excluding pair '
                             '%s:%s, trying other keys', tag, str(e)[:200],
                             slot.key_name, slot.model, exc_info=True)
            else:
                exclude.add(slot.model)
                logger.debug('%s Stream error: %s — trying next slot', tag, str(e)[:200], exc_info=True)
            hard_attempts += 1

    raise last_err or RuntimeError(
        'All %d dispatch_stream attempts failed for capability=%s' % (max_retries, capability))


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_fastest (race N slots)
# ═══════════════════════════════════════════════════════════

def dispatch_fastest(messages, *, max_tokens=4096, temperature=0,
                     thinking_enabled=False, preset='low', effort=None,
                     capability='text', prefer_model=None,
                     n_race=2, tools=None, extra=None,
                     log_prefix=''):
    """Fire requests to N slots simultaneously, return the first successful result.

    This wastes some API quota but guarantees the fastest possible response.
    Best for latency-critical tasks.

    Args:
        n_race: Number of slots to race (default 2)
        (other args same as dispatch_chat)

    Returns:
        (content_text: str, usage_dict: dict)
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from lib.llm_client import chat

    dispatcher = get_dispatcher()
    slots = dispatcher.pick_top_n(n=n_race, capability=capability,
                                   prefer_model=prefer_model)

    if not slots:
        raise RuntimeError(f'No slots available for capability={capability}')

    if len(slots) == 1:
        # Only one slot — just call it directly
        return dispatch_chat(messages, max_tokens=max_tokens, temperature=temperature,
                             thinking_enabled=thinking_enabled, preset=preset,
                             effort=effort, capability=capability,
                             prefer_model=slots[0].model,
                             tools=tools, extra=extra, log_prefix=log_prefix)

    cancel_event = threading.Event()

    def _race_worker(slot):
        if cancel_event.is_set():
            return None
        # Note: record_request() was already called atomically in pick_top_n(reserve=True)
        t0 = time.time()
        tag = f'{log_prefix}[Race:{slot.key_name}:{slot.model}]'
        try:
            _extra = dict(extra) if extra else {}
            if tools:
                _extra['tools'] = tools

            content, usage = chat(
                model=slot.model, messages=messages,
                max_tokens=max_tokens, temperature=temperature,
                thinking_enabled=thinking_enabled,
                effort=effort or preset,
                api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                extra=_extra or None,
                log_prefix=tag,
                max_retries=0,
            )
            latency = (time.time() - t0) * 1000
            slot.record_success(latency)
            return (content, usage, slot)
        except Exception as e:
            latency = (time.time() - t0) * 1000
            err_str = str(e)
            is_429 = '429' in err_str or 'rate' in err_str.lower()
            slot.record_error(is_rate_limit=is_429)
            raise

    with ThreadPoolExecutor(max_workers=n_race) as pool:
        futures = {pool.submit(_race_worker, s): s for s in slots}

        # Wait for the first successful result
        last_err = None
        done, pending = wait(futures, return_when=FIRST_COMPLETED)

        while done:
            for fut in done:
                try:
                    result = fut.result()
                    if result is not None:
                        content, usage, winner = result
                        cancel_event.set()
                        # Cancel pending futures
                        for p in pending:
                            p.cancel()
                        # Inject dispatch metadata
                        if isinstance(usage, dict):
                            usage['_dispatch'] = {
                                'key': winner.key_name,
                                'model': winner.model,
                                'latency_ms': round(winner.latency_ema),
                                'mode': 'race',
                            }
                        logger.debug('%s[Race] Winner: %s:%s', log_prefix, winner.key_name, winner.model)
                        return content, usage
                except Exception as e:
                    logger.debug('[Dispatch] race candidate failed: %s', e, exc_info=True)
                    last_err = e

            if pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
            else:
                break

        raise last_err or RuntimeError(
            'All %d race participants failed for capability=%s' % (n_race, capability))


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_parallel (fan-out for multiple tasks)
# ═══════════════════════════════════════════════════════════

def dispatch_parallel(tasks, *, capability='text', max_workers=4, log_prefix=''):
    """Execute multiple LLM tasks in parallel, distributing across slots.

    Args:
        tasks: List of dicts, each with:
            - 'messages': chat messages
            - 'max_tokens': (optional, default 4096)
            - 'temperature': (optional, default 0)
            - 'prefer_model': (optional)
            - 'extra': (optional)
        capability: Required capability for all tasks
        max_workers: Max concurrent requests

    Returns:
        List of (content, usage) tuples in the same order as tasks.
    """
    results = [None] * len(tasks)

    def _do_task(idx, task):
        try:
            content, usage = dispatch_chat(
                task['messages'],
                max_tokens=task.get('max_tokens', 4096),
                temperature=task.get('temperature', 0),
                capability=capability,
                prefer_model=task.get('prefer_model'),
                extra=task.get('extra'),
                log_prefix=f'{log_prefix}[P{idx}]',
            )
            return idx, (content, usage)
        except Exception as e:
            logger.debug('[Dispatch] parallel task[%d] failed (model=%s): %s', idx, task.get('prefer_model', '?'), e, exc_info=True)
            return idx, (None, {'error': str(e)})

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_do_task, i, t) for i, t in enumerate(tasks)]
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result

    return results


# ═══════════════════════════════════════════════════════════
#  Monitoring endpoint helpers
# ═══════════════════════════════════════════════════════════

def get_dispatch_status() -> dict:
    """Return current dispatcher status for monitoring/debugging."""
    d = get_dispatcher()
    slots_info = d.get_slots_info()
    return {
        'slots': slots_info,
        'total_slots': len(d.slots),
        'available_slots': sum(1 for s in d.slots if s.is_available),
        'by_capability': _group_by_capability(slots_info),
    }


def _group_by_capability(slots_info):
    """Group slot info by capability for easy overview."""
    caps = defaultdict(list)
    for s in slots_info:
        for c in s.get('capabilities', []):
            caps[c].append({
                'slot': f"{s['key']}:{s['model']}",
                'score': s['score'],
                'available': s['available'],
                'rpm_headroom_pct': s['rpm_headroom_pct'],
            })
    return dict(caps)


# ═══════════════════════════════════════════════════════════
#  Convenience — drop-in replacement for chat()
# ═══════════════════════════════════════════════════════════

def smart_chat(messages, *, model=None, max_tokens=4096, temperature=0,
               thinking_enabled=False, preset='low', effort=None,
               capability='text', tools=None, extra=None,
               log_prefix='', max_retries=3, timeout=None, **_kw):
    """Drop-in replacement for ``llm_client.chat()`` with auto dispatch.

    Uses the fastest available (key, model) slot across ALL keys.
    Falls back to direct ``chat()`` if dispatch fails entirely.

    Signature is intentionally close to ``chat()`` so call sites only
    need to change ``from lib.llm_client import chat`` →
    ``from lib.llm_dispatch import smart_chat as chat``.

    Extra kwargs (api_key, etc.) are silently ignored so callers that
    sometimes pass api_key don't break.
    """
    try:
        return dispatch_chat(
            messages, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, preset=preset,
            effort=effort, capability=capability,
            prefer_model=model, tools=tools, extra=extra,
            max_retries=max_retries, log_prefix=log_prefix,
            timeout=timeout,
        )
    except Exception as e:
        # Ultimate fallback — direct call with default key
        logger.warning('%s[Dispatch] All slots exhausted (%s), '
                    'falling back to direct chat()', log_prefix, e, exc_info=True)
        from lib.llm_client import chat
        _fb_timeout = timeout if timeout is not None else 120
        # ★ For 'cheap' tasks (translate etc.), fall back to a cheap model,
        #   NOT the default LLM_MODEL (which is Opus — way too slow/expensive).
        _fb_model = model
        if not _fb_model and capability == 'cheap':
            from lib import GEMINI_MODEL
            _fb_model = GEMINI_MODEL   # gemini-2.5-flash — fast & cheap
            logger.info('%s Fallback using cheap model: %s', log_prefix, _fb_model)
        # ── Audit trail: record the model switch on exhaustion fallback ──
        try:
            from lib.log import audit_log as _audit
            _audit('model_switch',
                   old=(model or '(dispatch-auto)'),
                   new=(_fb_model or '(llm_client default)'),
                   reason='dispatch_exhausted',
                   capability=capability,
                   error=str(e)[:200])
        except Exception as _aerr:
            logger.debug('%s audit_log model_switch failed: %s', log_prefix, _aerr)
        return chat(messages=messages, model=_fb_model,
                    max_tokens=max_tokens, temperature=temperature,
                    thinking_enabled=thinking_enabled, effort=effort or preset,
                    log_prefix=log_prefix, timeout=_fb_timeout)


def smart_chat_batch(prompts, *, max_tokens=4096, temperature=0,
                     capability='text', log_prefix='',
                     max_concurrent=8, **kw):
    """Send multiple independent prompts concurrently via dispatch.

    Each prompt is dispatched to a different slot (potentially different
    keys and models), maximising throughput across all available RPM.

    Args:
        prompts: list of str | list of list[dict]  (raw text or messages)
        max_concurrent: max parallel workers (default 8)
        **kw: forwarded to dispatch_chat

    Returns:
        list of (content, usage) tuples — same order as input
    """
    import concurrent.futures

    def _to_messages(p):
        if isinstance(p, str):
            return [{'role': 'user', 'content': p}]
        return p  # already messages list

    results = [None] * len(prompts)

    def _worker(idx, msgs):
        try:
            return idx, dispatch_chat(
                msgs, max_tokens=max_tokens, temperature=temperature,
                capability=capability,
                log_prefix=f'{log_prefix}[batch:{idx}]', **kw)
        except Exception as e:
            logger.warning('%s[batch:%d] Failed: %s', log_prefix, idx, e, exc_info=True)
            return idx, (f'[Error] {e}', {})

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(prompts), max_concurrent)) as pool:
        futures = [pool.submit(_worker, i, _to_messages(p))
                   for i, p in enumerate(prompts)]
        for f in concurrent.futures.as_completed(futures):
            idx, result = f.result()
            results[idx] = result

    return results
