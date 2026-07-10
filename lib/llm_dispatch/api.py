"""lib/llm_dispatch/api.py — Module-level dispatch convenience functions.

High-level API for LLM dispatch: dispatch_chat, dispatch_stream,
dispatch_fastest, dispatch_parallel, smart_chat, smart_chat_batch, etc.
All functions call get_dispatcher() internally.

Usage:
    from lib.llm_dispatch import dispatch_chat, dispatch_stream, smart_chat
"""

import os
import threading
import time
from collections import defaultdict

from lib.log import get_logger

from .factory import get_dispatcher

logger = get_logger(__name__)

# How long (seconds) to cool a slot whose endpoint was unreachable at the
# connect phase, so the picker routes around the dead host instead of
# retrying it every cycle. The local health checker clears this early when
# the box recovers. Env-tunable for deployments with flaky self-hosted boxes.
try:
    _UNREACHABLE_COOLDOWN = float(os.environ.get('TOFU_UNREACHABLE_COOLDOWN', '30'))
    if _UNREACHABLE_COOLDOWN <= 0:
        _UNREACHABLE_COOLDOWN = 30.0
except (ValueError, TypeError) as e:
    logger.debug('[Dispatch] TOFU_UNREACHABLE_COOLDOWN parse failed, using default: %s', e)
    _UNREACHABLE_COOLDOWN = 30.0


def _raise_dispatch_exhausted(last_err, *, max_retries, capability,
                              prefer_model=None, what='dispatch'):
    """Raise the terminal error when a dispatch loop runs out of slots.

    When the exhausting failure was an endpoint-unreachable error, every
    candidate slot was a dead host — re-raise a single clear message
    naming the model instead of letting an opaque urllib3 ``MaxRetryError``
    bubble to the user. Otherwise propagate ``last_err`` unchanged (or a
    generic RuntimeError when there was no captured error).
    """
    from lib.llm_errors import EndpointUnreachableError
    if isinstance(last_err, EndpointUnreachableError):
        _target = prefer_model or capability
        _url = getattr(last_err, 'base_url', '') or ''
        msg = ('All endpoints for %s are unreachable — the model server(s) '
               'appear to be down or not accepting connections. '
               'Check that the endpoint is running and reachable.'
               % (('model %r' % _target) if prefer_model else
                  ('capability %r' % capability)))
        if _url:
            msg += ' (last tried: %s)' % _url
        raise EndpointUnreachableError(msg, base_url=_url) from last_err
    raise last_err or RuntimeError(
        'All %d %s attempts failed for capability=%s'
        % (max_retries, what, capability))



def _cool_slot_on_premature_close(slot, usage, prev_consecutive_errors=0):
    """Feed a premature-close stream anomaly into the slot's health scorer.

    ``lib/llm/_sse_core.py::SSEAccumulator.finalize`` sets ``usage['_missing_done']``
    when the upstream closed the SSE connection before the ``[DONE]`` marker —
    and ONLY when the close was neither a client abort nor a clean finish
    (``if not aborted and not saw_done``). That is a genuine soft failure:
    the HTTP call connected and streamed, but the body is incomplete/unusable.
    Historically it was only logged, so the offending upstream kept getting
    re-picked. Route it through the SAME ``record_truncation`` path the
    translate retry loop uses (bumps ``consecutive_errors`` → after 3 the slot
    is cooled with the existing exponential backoff / 300s cap — no new
    hyperparameter).

    ★ Accumulation across dispatches. This runs AFTER ``record_success``, whose
    latency/throughput/RPM-recovery bookkeeping is legitimately valid (the
    connection DID stream) — but it ALSO zeroes ``consecutive_errors``. If we
    let ``record_truncation`` bump the just-zeroed counter, every premature
    close would reset to 1 and the slot could NEVER reach the ``>=3`` cooldown
    threshold, no matter how many times the SAME upstream drops. So we restore
    the pre-``record_success`` error count first, then let ``record_truncation``
    do its usual ``+= 1`` + threshold-cooldown on top — accumulation survives.

    The over-cooling guard is the ``_missing_done`` predicate itself: a clean
    close (``saw_done``) or a client abort never sets the flag, so a healthy
    upstream and a user-cancelled stream are never cooled.
    """
    if not isinstance(usage, dict) or not usage.get('_missing_done'):
        return
    try:
        _elapsed = usage.get('stream_elapsed_ms', 0)
        _chunks = usage.get('_chunks_received', 0)
        # Undo record_success()'s zeroing so consecutive premature closes on
        # the same slot accumulate toward the cooldown threshold.
        with slot._lock:
            slot.consecutive_errors = max(slot.consecutive_errors,
                                          prev_consecutive_errors)
        slot.record_truncation(
            error='premature stream close (no [DONE]) '
                  'elapsed_ms=%s chunks=%s' % (_elapsed, _chunks))
        from lib.log import audit_log
        audit_log('premature_close_cooldown',
                  key_name=slot.key_name, model=slot.model,
                  provider_id=slot.provider_id,
                  consecutive_errors=slot.consecutive_errors,
                  cooldown_until=round(slot.cooldown_until, 1),
                  elapsed_ms=_elapsed, chunks=_chunks,
                  trace_id=usage.get('trace_id', ''))
        logger.warning('[Dispatch] Premature stream close on %s:%s — '
                       'recorded soft failure (consecutive_errors=%d)',
                       slot.key_name, slot.model, slot.consecutive_errors)
    except Exception as e:
        logger.warning('[Dispatch] _cool_slot_on_premature_close failed '
                       'for %s:%s: %s', slot.key_name, slot.model, e)


# ═══════════════════════════════════════════════════════════
#  Config-surface audit trail (CLAUDE.md §10)
# ═══════════════════════════════════════════════════════════

_AUDITED_SEVERITY_DOWNGRADE = False


def _audit_severity_downgrade() -> None:
    """Emit a one-time audit_log entry for the 2026-05-05 per-cycle 429
    severity downgrade (WARNING→INFO). Only logged once per process.

    This keeps CLAUDE.md §10 ("config changes must leave a trace") happy
    without flooding audit.log with the same entry on every dispatch.
    """
    global _AUDITED_SEVERITY_DOWNGRADE
    if _AUDITED_SEVERITY_DOWNGRADE:
        return
    _AUDITED_SEVERITY_DOWNGRADE = True
    try:
        from lib.log import audit_log
        audit_log('config_change',
                  param='dispatch_429_severity',
                  old='warning',
                  new='info',
                  reason='log-noise audit 2026-05-05 — per-cycle 429 is '
                         'routine backpressure, rotation handled by '
                         'dispatcher; only final key exhaustion is warning',
                  approved_by='plan')
    except Exception as e:
        logger.debug('[Dispatch] audit_log unavailable: %s', e)



__all__ = [
    'pick_key_for_model',
    'dispatch_chat',
    'dispatch_stream',
    'async_dispatch_stream',
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
                  timeout=None, strict_model=False,
                  exclude_models=None):
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
    from lib.llm import ContentFilterError, InvalidImageError, PermissionError_, PromptTooLongError, RateLimitError, StreamOnlyError, chat
    from lib.llm_errors import EndpointUnreachableError

    # 2026-05-05 config-surface change (CLAUDE.md §10): per-cycle 429
    # severity downgraded from WARNING → INFO (routine backpressure; the
    # dispatch handler already rotates to the next key). Only final key
    # exhaustion stays WARNING/ERROR. Audited here on first call so the
    # change is discoverable in audit.log without a code-search.
    _audit_severity_downgrade()
    dispatcher = get_dispatcher()
    exclude = set(exclude_models) if exclude_models else set()
    # Caller-provided model exclusions are permanent for this dispatch call —
    # remember them so the periodic exclusion-reset during 429 cycling doesn't
    # silently re-introduce a model the caller explicitly banned.
    _initial_exclude_models = set(exclude)
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
                # Note: dispatch_chat does NOT clear `exclude` here — that
                # set tracks model-level hard errors, which dispatch_chat
                # accumulates across attempts. Caller-provided
                # exclude_models is also preserved by virtue of being
                # added to `exclude` at construction time.
            _last_exclusion_reset = time.monotonic()

        # Caller-provided exclude_models must apply on attempt 1 too
        # (failure-driven exclusions only kick in after total_attempts>0).
        _eff_exclude = exclude if (total_attempts > 0 or _initial_exclude_models) else None
        slot = dispatcher.pick_and_reserve(
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=_eff_exclude,
            exclude_keys=exclude_keys if total_attempts > 0 else None,
            exclude_pairs=exclude_pairs if total_attempts > 0 else None,
            strict_model=strict_model)
        if slot is None:
            # All slots in cooldown / excluded — wait briefly and retry.
            # ★ Keep fast-polling when capable slots EXIST but are merely
            #   cooling (429-equivalent), even if THIS call hasn't caught a
            #   raw 429 yet — otherwise fresh concurrent requests bail on
            #   attempt 1 under heavy contention. Give up only when no
            #   capable slot exists at all. (Mirror of dispatch_stream.)
            _slots_exist = dispatcher.has_capable_slots(
                capability, exclude_models=exclude,
                exclude_keys=exclude_keys, exclude_pairs=exclude_pairs)
            if _429_count > 0 or _slots_exist:
                time.sleep(0.3)
                _429_count += 1
                if _429_count % 20 == 0:
                    logger.info(
                        '%s dispatch_chat: still cycling (slots cooling, %d times), '
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
                api_protocol=slot.protocol or 'openai',
                oauth=slot.oauth or '',
            )
            latency = (time.time() - t0) * 1000
            _out_tokens = 0
            if isinstance(usage, dict):
                _out_tokens = (usage.get('completion_tokens')
                               or usage.get('output_tokens') or 0)
                try:
                    _out_tokens = int(_out_tokens)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[api] dispatch_chat caught %s: %s', type(_e_audit).__name__, _e_audit)
                    _out_tokens = 0
            slot.record_success(latency, output_tokens=_out_tokens)
            # Inject dispatch metadata so callers know which slot served this
            if isinstance(usage, dict):
                usage['_dispatch'] = {
                    'key': slot.key_name, 'model': slot.model,
                    'key_tail': (slot.api_key or '')[-4:],
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
            # 2026-05-05 noise-reduction: per-cycle 429 is ROUTINE backpressure
              # (handler already rotates to the next key). Log at INFO, not
              # WARNING — only the final exhaustion path (all keys excluded)
              # remains WARNING/ERROR. See CLAUDE.md §10 / audit_log below.
            if _429_count <= 3 or _429_count % 100 == 0:
                # First few + every 100th kept informative (include body)
                logger.info(
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
            except Exception as e:
                logger.debug('%s is_key_enabled probe failed: %s', log_prefix, e)
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

        except EndpointUnreachableError as e:
            # ★ Endpoint host down (connect-phase failure). Cool the slot,
            #   exclude this (key, model) pair, and fail over to another
            #   slot — same handling as dispatch_stream.
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            slot.cooldown_until = time.time() + _UNREACHABLE_COOLDOWN
            last_err = e
            exclude_pairs.add((slot.key_name, slot.model))
            hard_attempts += 1
            logger.warning(
                '%s Endpoint unreachable on %s:%s (%s) — cooled %ds + '
                'excluded pair, failing over: %s',
                log_prefix, slot.key_name, slot.model,
                getattr(e, 'base_url', '') or '?', _UNREACHABLE_COOLDOWN,
                str(e)[:160])

        except ContentFilterError as e:
            # HTTP 450 — content policy violation. No point retrying with
            # different model/key since the same content will be blocked.
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Content filter (HTTP 450) — not retrying: %s', tag, str(e)[:200], exc_info=True)
            raise

        except PromptTooLongError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Prompt/request too large — not retrying on other slots '
                           '(same payload = same rejection)', tag)
            raise   # ★ Escape to orchestrator for reactive compaction

        except InvalidImageError:
            slot.release()  # payload-level reject — not a slot-health signal
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

    _raise_dispatch_exhausted(last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              what='dispatch')


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
      - Qwen/LongCat: enable_thinking = True/False
      - Gemini 3.x: reasoning_effort = 'minimal'/'low'/'medium'/'high'

    When dispatch swaps the model (e.g. Claude → Doubao), the body may carry
    the wrong format, causing HTTP 400 errors. This function detects mismatches
    and converts to the correct format for the new model.
    """
    from lib.llm import is_claude, is_doubao, is_gemini, is_glm, is_longcat, is_minimax, is_qwen

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
        # Carry the effort across the swap if no explicit effort was set.
        if not effort:
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
    elif _tf == 'reasoning_effort' or (not _tf and is_gemini(new_model)):
        from lib.llm import gemini_reasoning_effort
        body['reasoning_effort'] = gemini_reasoning_effort(effort, is_enabled)
    elif _tf == 'enable_thinking' or (not _tf and (is_longcat(new_model) or is_qwen(new_model))):
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
            from lib.llm import is_claude_opus_47
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
    from lib.llm import is_claude_opus_47
    if is_claude_opus_47(new_model):
        for _k in ('temperature', 'top_p', 'top_k'):
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


# ═══════════════════════════════════════════════════════════
#  Public API — dispatch_stream (streaming)
# ═══════════════════════════════════════════════════════════

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
    if is_body:
        body = dict(body_or_messages)
        body['model'] = slot.model
        if tools is not None:
            body['tools'] = tools
        if 'max_tokens' in body:
            from lib.llm import _clamp_max_tokens
            from lib.llm.body import _clamp_completion_to_context_window
            body['max_tokens'] = _clamp_max_tokens(slot.model, body['max_tokens'])
            body['max_tokens'] = _clamp_completion_to_context_window(
                slot.model, body.get('messages'), body['max_tokens'],
                provider_id=slot.provider_id or '')
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
        from lib.model_info import is_gemini as _is_gemini
        if _is_gemini(slot.model) and body.get('messages'):
            _inject_gemini_thought_signatures(body['messages'], slot.model)
        return body
    return build_body(
        slot.model, body_or_messages,
        max_tokens=max_tokens, temperature=temperature,
        thinking_enabled=thinking_enabled, preset=effort or preset,
        tools=tools, stream=True,
        thinking_format=slot.thinking_format or '',
        provider_id=slot.provider_id or '',
    )


class _StreamRetryState:
    """Shared retry/exclusion bookkeeping for the streaming dispatch loops.

    Both ``dispatch_stream`` (sync) and ``async_dispatch_stream`` (async) run
    the SAME three-tier exclusion state machine around their (different)
    transport calls:

      * ``exclude``       — models excluded entirely (hard model errors).
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
      * ``_429_count`` counts free 429 retries (never counts toward the cap).
    """

    _EXCLUSION_RESET_INTERVAL = 60  # reset hard-error exclusions every 60s of 429 cycling

    def __init__(self, exclude_models=None, avoid_pairs=None):
        self.exclude = set(exclude_models) if exclude_models else set()
        # Caller-provided model exclusions are permanent for this dispatch —
        # remembered so the periodic reset doesn't re-introduce them.
        self._initial_exclude_models = set(self.exclude)
        self.exclude_keys = set()
        self.exclude_pairs = set()
        # Caller-provided avoid set seeds exclude_pairs so the first pick
        # already steers around it; tracked separately so it can be relaxed
        # as a last resort when every other slot is cooled/excluded.
        self._initial_avoid = set(avoid_pairs) if avoid_pairs else set()
        if self._initial_avoid:
            self.exclude_pairs |= self._initial_avoid
        self.last_err = None
        self.hard_attempts = 0
        self._429_count = 0
        self._last_exclusion_reset = time.monotonic()

    @property
    def total_attempts(self):
        return self.hard_attempts + self._429_count

    def maybe_reset_exclusions(self, log_prefix, label):
        """Periodically clear hard-error exclusions during 429 cycling.

        502/timeout exclusions may be transient (gateway restart) but are
        otherwise permanent for the dispatch call.  After 60s of 429 cycling
        give excluded slots another chance — still-broken ones get re-excluded
        quickly.  Caller-provided ``exclude_models`` are preserved.  Returns
        the set of currently-active exclusions (for the caller's log line)
        before the reset, or None when nothing was reset.
        """
        if self._429_count > 0 and (
                time.monotonic() - self._last_exclusion_reset) >= self._EXCLUSION_RESET_INTERVAL:
            if self.exclude or self.exclude_keys or self.exclude_pairs:
                logger.info(
                    '%s %s: resetting hard-error exclusions after %ds of 429 '
                    'cycling (cycle #%d) — exclude_models=%s exclude_keys=%s '
                    'exclude_pairs=%s',
                    log_prefix, label, self._EXCLUSION_RESET_INTERVAL,
                    self._429_count, self.exclude, self.exclude_keys,
                    self.exclude_pairs)
                self.exclude.clear()
                self.exclude |= self._initial_exclude_models
                self.exclude_keys.clear()
                self.exclude_pairs.clear()
            self._last_exclusion_reset = time.monotonic()

    def eff_exclude_models(self):
        """``exclude_models`` to pass to pick_and_reserve.

        Caller-provided exclusions apply from attempt 1; failure-driven ones
        only after the first attempt.
        """
        return self.exclude if (self.total_attempts > 0
                                or self._initial_exclude_models) else None

    def eff_exclude_keys(self):
        return self.exclude_keys if self.total_attempts > 0 else None

    def eff_exclude_pairs(self):
        return self.exclude_pairs if self.total_attempts > 0 else None

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

    def note_free_429(self):
        """A routine (non-quota) 429 — free retry, does not count toward the cap."""
        self._429_count += 1

    def note_cooldown_cycle(self):
        """All slots in transient cooldown (slot is None, 429-equivalent)."""
        self._429_count += 1

    def note_quota_key(self, slot):
        """Quota-exhausted 429 — disable the key for this dispatch; hard attempt."""
        self.exclude_keys.add(slot.key_name)
        self.hard_attempts += 1

    def note_auto_exhausted_key(self, slot):
        """Key auto-exhausted after consecutive 429s — exclude key; hard attempt."""
        self.exclude_keys.add(slot.key_name)
        self.hard_attempts += 1

    def note_permission_pair(self, slot, dispatcher, capability, log_prefix):
        """Permission denied — exclude the (key, model) PAIR; escalate to a
        whole-key exclusion only if EVERY model for this key is now excluded."""
        self.exclude_pairs.add((slot.key_name, slot.model))
        self.hard_attempts += 1
        _key_pairs = {(kn, m) for kn, m in self.exclude_pairs if kn == slot.key_name}
        _key_models = {s.model for s in dispatcher.slots
                       if s.key_name == slot.key_name
                       and (not capability or capability in s.capabilities)}
        if _key_models and _key_models <= {m for _, m in _key_pairs}:
            self.exclude_keys.add(slot.key_name)
            logger.warning('%s Permission denied on ALL models for key %s — '
                           'excluding entire key', log_prefix, slot.key_name)
            return True
        return False

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


def dispatch_stream(body_or_messages, *, on_thinking=None, on_content=None,
                    on_tool_call_ready=None,
                    abort_check=None, max_tokens=4096, temperature=0,
                    thinking_enabled=False, preset='low', effort=None,
                    capability='text', prefer_model=None, tools=None,
                    max_retries=3, log_prefix='', strict_model=False,
                    on_retry=None, avoid_pairs=None,
                    exclude_models=None):
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

    avoid_pairs:
      Optional set of ``(key_name, model)`` tuples the caller wants the
      dispatcher to avoid for THIS call (in addition to whatever
      ``exclude_pairs`` accumulates internally).  Used by zero-byte retry
      logic in ``analyse_stream_result`` → ``stream_llm_response`` to
      force slot rotation away from a poisoned upstream pool, mirroring
      the ``gateway-5xx-treated-as-429`` pattern.  When the dispatcher
      runs out of non-avoided slots it falls back to the avoided pair
      rather than failing — better to retry the bad slot than to fail
      the whole task.

    Returns:
        (msg: str, finish_reason: str, usage: dict)
    """
    from lib.llm import (
        AbortedError,
        ContentFilterError,
        InvalidImageError,
        PermissionError_,
        PromptTooLongError,
        RateLimitError,
        stream_chat,
    )
    from lib.llm_errors import EndpointUnreachableError

    dispatcher = get_dispatcher()
    state = _StreamRetryState(exclude_models=exclude_models, avoid_pairs=avoid_pairs)

    # Detect if it's a pre-built body or raw messages
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

    # ★ hard_attempts counts only non-429 failures; 429 loops forever.
    #   Set _MAX_429_CYCLES = 0 to disable the cap (retry indefinitely).
    #   The abort_check runs every cycle so the user can always cancel.
    _MAX_429_CYCLES = 0  # 0 = infinite; set >0 to re-enable safety cap

    while state.hard_attempts < max_retries:
        # Abort check — let the user cancel during 429 cycling
        if abort_check and abort_check():
            from lib.llm import AbortedError as _AE
            raise _AE('Aborted during dispatch retry')

        # ★ Safety cap on 429 cycling — if we've been rate-limited for
        #   too many cycles, something is fundamentally wrong (e.g. payload
        #   too large causing 413 on some slots and 429 on the rest).
        if _MAX_429_CYCLES > 0 and state._429_count >= _MAX_429_CYCLES:
            logger.error(
                '%s dispatch_stream: 429 cycling exceeded safety cap '
                '(%d cycles) — giving up. Last error: %s',
                log_prefix, _MAX_429_CYCLES, str(state.last_err)[:200])
            raise state.last_err or RuntimeError(
                'Rate-limited %d times without success' % state._429_count)

        # Log available slots at start of each attempt for debugging
        logger.debug(
            '%s dispatch_stream attempt hard=%d/%d 429=%d: '
            'slots: %s',
            log_prefix, state.hard_attempts + 1, max_retries, state._429_count,
            dispatcher.summarize_slots(capability))

        # ★ Periodically reset hard-error exclusions during 429 cycling
        #   (502/timeout may be transient; give recovered slots another chance).
        state.maybe_reset_exclusions(log_prefix, 'dispatch_stream')

        # ★ Warm-key hold (pre-pick): if this conversation has a sticky key
        #   whose prompt-cache prefix is warm but that key is in a SHORT
        #   rate-limit cooldown, briefly wait it out BEFORE picking — otherwise
        #   the picker rebinds to a cold key and re-bills a full cache_creation
        #   (~100K tokens) to dodge a sub-second per-minute throttle. The
        #   warm-cache saving dwarfs the wait. Bounded by the hold budget and
        #   capped to ONE hold per dispatch call (``_warm_held``) so a key stuck
        #   in a longer cooldown can't stall the loop — after one hold it falls
        #   through to the normal cold-key rebind. Flag-gated + reversible.
        if not getattr(state, '_warm_held', False):
            try:
                from lib.llm_dispatch.conv_affinity import (
                    get_conv_affinity,
                    sticky_hold_budget_ms,
                    sticky_hold_enabled,
                    sticky_routing_enabled,
                )
                if sticky_routing_enabled() and sticky_hold_enabled():
                    _conv = get_conv_affinity()
                    if _conv:
                        _hold = dispatcher.sticky_cooldown_remaining_s(
                            _conv, prefer_model=prefer_model,
                            exclude_keys=state.eff_exclude_keys() or set(),
                            exclude_pairs=state.eff_exclude_pairs() or set())
                        if _hold is not None:
                            _remaining_s, _warm_key = _hold
                            _budget_s = sticky_hold_budget_ms() / 1000.0
                            if 0 < _remaining_s <= _budget_s:
                                _wait = min(_remaining_s + 0.05, _budget_s)
                                logger.info(
                                    '%s dispatch_stream: holding %.2fs for '
                                    'conv=%s warm key %s (short 429 cooldown) '
                                    'to keep prompt cache warm',
                                    log_prefix, _wait, _conv[:8], _warm_key)
                                if abort_check and abort_check():
                                    from lib.llm import AbortedError as _AE
                                    raise _AE('Aborted during warm-key hold')
                                time.sleep(_wait)
                                state._warm_held = True
            except ImportError as _imp_err:
                logger.debug('%s warm-key hold unavailable: %s',
                             log_prefix, _imp_err)

        slot = dispatcher.pick_and_reserve(
            capability=capability,
            prefer_model=prefer_model,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys(),
            exclude_pairs=state.eff_exclude_pairs(),
            strict_model=strict_model)
        if slot is None:
            # ── Last-resort: drop the caller-provided avoid set ──
            # Zero-byte force-rotate uses ``avoid_pairs`` to steer away
            # from a freshly-poisoned slot. If the rest of the pool is
            # already exhausted we'd rather retry the bad slot than fail
            # outright — failure here means the task aborts, while a
            # retry on the original slot might still succeed.
            if state.relax_avoid_if_exhausted():
                logger.info(
                    '%s dispatch_stream: relaxing avoid_pairs — every '
                    'other slot is in cooldown/excluded', log_prefix)
                continue
            # All slots in cooldown / excluded — wait briefly and retry.
            # ★ Two cases produce slot=None and need OPPOSITE handling:
            #   (a) capable slots EXIST but are all in transient 0.5s
            #       rate-limit cooldown — this is a 429-equivalent; keep
            #       fast-polling regardless of whether THIS call has yet
            #       caught a raw 429. Under high concurrency a fresh
            #       request routinely arrives while every slot is cooling
            #       (from OTHER requests' 429s); bailing here on attempt 1
            #       was the cause of spurious "All N attempts failed".
            #   (b) no capable slot exists at all → genuinely unservable.
            _slots_exist = dispatcher.has_capable_slots(
                capability, exclude_models=state.exclude,
                exclude_keys=state.exclude_keys, exclude_pairs=state.exclude_pairs)
            if state._429_count > 0 or _slots_exist:
                time.sleep(0.3)
                state.note_cooldown_cycle()
                # ★ Notify the caller the FIRST time we enter the cooldown
                #   wait and then sparsely (every 20 cycles ≈ 6s). This is the
                #   case the old on_retry calls MISSED: under contention every
                #   capable slot is cooling from OTHER requests' 429s, so this
                #   call returns slot=None WITHOUT ever catching a raw 429 of
                #   its own — yet it can wait minutes for a rate-limited
                #   strict_model. A flow/swarm worker in this state showed a
                #   bare "Waiting…" pulse with no signal. on_retry lets the
                #   caller surface a transient "waiting for model…" phase.
                if on_retry and (state._429_count == 1 or state._429_count % 20 == 0):
                    try:
                        on_retry(attempt=state._429_count,
                                 reason='Waiting for model (rate-limited)',
                                 status_code=429)
                    except Exception as _ore:
                        logger.debug('%s on_retry (cooldown) raised: %s',
                                     log_prefix, _ore)
                if state._429_count % 20 == 0:
                    logger.info(
                        '%s dispatch_stream: still cycling (slots cooling, %d times, '
                        'strict=%s), waiting for cooldown to expire…',
                        log_prefix, state._429_count, strict_model)
                continue
            logger.warning(
                '%s dispatch_stream: NO CAPABLE SLOT on attempt %d/%d. '
                'exclude_models=%s exclude_keys=%s exclude_pairs=%s strict_model=%s. '
                'Available slots: %s',
                log_prefix, state.hard_attempts + 1, max_retries,
                state.exclude, state.exclude_keys, state.exclude_pairs, strict_model,
                dispatcher.summarize_slots(capability))
            break

        t0 = time.time()
        ttft_recorded = False
        tag = f'{log_prefix}[D:{slot.key_name}:{slot.model}]'

        # Build body for this slot's model (shared helper — see
        # _adapt_stream_body_for_slot for the provider-specific quirks).
        body = _adapt_stream_body_for_slot(
            slot, body_or_messages, is_body,
            tools=tools, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, preset=preset, effort=effort)

        # Wrap on_content to capture TTFT
        ttft_value = [None]  # mutable cell so closure can write
        def _on_content_wrapper(text):
            nonlocal ttft_recorded
            if not ttft_recorded:
                ttft = (time.time() - t0) * 1000
                ttft_value[0] = ttft
                ttft_recorded = True
            if on_content:
                on_content(text)

        try:
            msg, finish, usage = stream_chat(
                body, api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                oauth=slot.oauth or '',
                on_thinking=on_thinking,
                on_content=_on_content_wrapper,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check,
                log_prefix=tag,
                api_protocol=slot.protocol or 'openai',
            )
            latency = (time.time() - t0) * 1000
            _out_tokens = 0
            if isinstance(usage, dict):
                _out_tokens = (usage.get('completion_tokens')
                               or usage.get('output_tokens') or 0)
                try:
                    _out_tokens = int(_out_tokens)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[api] dispatch_stream caught %s: %s', type(_e_audit).__name__, _e_audit)
                    _out_tokens = 0
            _prev_ce = slot.consecutive_errors
            slot.record_success(latency, ttft_ms=ttft_value[0],
                                output_tokens=_out_tokens)
            # Inject dispatch metadata so callers know which slot served this
            if isinstance(usage, dict):
                usage['_dispatch'] = {
                    'key': slot.key_name, 'model': slot.model,
                    'key_tail': (slot.api_key or '')[-4:],
                    'provider_id': slot.provider_id,
                    'latency_ms': round(latency),
                    'attempt': state.hard_attempts + 1,
                    '429_retries': state._429_count,
                }
            _cool_slot_on_premature_close(slot, usage, _prev_ce)
            if state._429_count > 0:
                logger.info('%s dispatch_stream OK after %d 429-retries: '
                            'finish_reason=%s model=%s provider=%s latency=%.0fms',
                            log_prefix, state._429_count, finish, slot.model,
                            slot.provider_id, latency)
            else:
                logger.debug('%s dispatch_stream OK: finish_reason=%s model=%s '
                            'provider=%s latency=%.0fms attempt=%d/%d',
                            log_prefix, finish, slot.model,
                            slot.provider_id, latency, state.hard_attempts + 1,
                            max_retries)
            return msg, finish, usage

        except RateLimitError as e:
            _is_quota = bool(getattr(e, 'is_quota', False))
            _err_str = str(e)[:200]
            slot.record_error(is_rate_limit=True,
                              is_quota_exhausted=_is_quota,
                              error=_err_str if _is_quota else '')
            state.last_err = e
            if _is_quota:
                # ★ Persistent billing/quota exhaustion — disable this key
                #   for the remainder of this dispatch and flag it so the
                #   user sees "out of balance" in Settings.
                state.note_quota_key(slot)
                logger.warning(
                    '%s Quota exhausted on %s:%s — disabling key for '
                    'today: %s',
                    log_prefix, slot.key_name, slot.model, _err_str)
                if on_retry:
                    on_retry(attempt=state.hard_attempts,
                             reason='Key balance exhausted', status_code=429)
                continue
            state.note_free_429()
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
            # 2026-05-05 noise-reduction: per-cycle 429 is ROUTINE backpressure
              # (handler already rotates to the next key). Log at INFO, not
              # WARNING — only the final exhaustion path (all keys excluded)
              # remains WARNING/ERROR.
            if state._429_count <= 3 or state._429_count % 100 == 0:
                logger.info(
                    '%s 429 rate-limited on %s:%s (cycle #%d) — body: %s',
                    log_prefix, slot.key_name, slot.model, state._429_count,
                    _err_body)
            else:
                logger.info(
                    '%s 429 rate-limited on %s:%s (cycle #%d)',
                    log_prefix, slot.key_name, slot.model, state._429_count)
            # ★ If the streak just tripped, the key is now flagged as
            #   exhausted — exclude it so we don't cycle back to it.
            try:
                from lib.key_stats import is_key_enabled
                if not is_key_enabled(slot.provider_id, slot.key_name):
                    state.note_auto_exhausted_key(slot)
                    logger.warning(
                        '%s Key %s auto-exhausted after %d consecutive 429s '
                        '— excluding for today',
                        log_prefix, slot.key_name, state._429_count)
                    if on_retry:
                        on_retry(attempt=state.hard_attempts,
                                 reason='Key auto-exhausted (consecutive 429s)',
                                 status_code=429)
                    continue
            except Exception as e:
                logger.debug('%s is_key_enabled probe failed (stream): %s', log_prefix, e)
            if on_retry:
                on_retry(attempt=state._429_count, reason='Rate limited (429)', status_code=429)
            time.sleep(0.3)
            # ★ Don't increment hard_attempts — 429 retries are free
            continue

        except PermissionError_ as e:
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            # ★ Exclude the (key, model) PAIR; escalate to a whole-key
            #   exclusion only if ALL models for this key are now excluded.
            if not state.note_permission_pair(slot, dispatcher, capability, log_prefix):
                logger.warning(
                    '%s Permission denied on %s:%s — excluding pair, '
                    'remaining slots: %s',
                    log_prefix, slot.key_name, slot.model,
                    dispatcher.summarize_slots(capability))

        except EndpointUnreachableError as e:
            # ★ The endpoint host is down (connect-phase failure). The
            #   transport already escaped its same-key retry loop, so here
            #   we cool the slot down and exclude this (key, model) pair,
            #   then immediately pick another slot — this is the failover.
            #   A whole-endpoint cooldown (not just 0.5s) keeps the picker
            #   off the dead host while we route around it; the local
            #   health checker clears the cooldown when the box recovers.
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            slot.cooldown_until = time.time() + _UNREACHABLE_COOLDOWN
            state.last_err = e
            state.note_unreachable_pair(slot)
            logger.warning(
                '%s Endpoint unreachable on %s:%s (%s) — cooled %ds + '
                'excluded pair, failing over: %s',
                log_prefix, slot.key_name, slot.model,
                getattr(e, 'base_url', '') or '?', _UNREACHABLE_COOLDOWN,
                str(e)[:160])
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Endpoint unreachable', status_code=0)

        except AbortedError:
            slot.release()  # user abort — not a slot-health signal
            logger.debug('%s User aborted — stopping dispatch immediately', tag)
            raise   # ★ Don't retry on other slots, user wants to stop

        except ContentFilterError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Content filter (HTTP 450) — not retrying', tag, exc_info=True)
            raise   # ★ Same content = same filter, no point retrying

        except PromptTooLongError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Prompt/request too large — not retrying on other slots '
                           '(same payload = same rejection)', tag)
            raise   # ★ Escape to orchestrator for reactive compaction

        except InvalidImageError:
            slot.release()  # payload-level reject — not a slot-health signal
            logger.warning('%s Image content error — not retrying on other slots '
                           '(same image = same rejection)', tag)
            raise   # ★ Same payload = same rejection on all keys

        except Exception as e:
            latency = (time.time() - t0) * 1000
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            _is_timeout = 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()
            # ★ Notify frontend about retry so user sees status instead of "Waiting…"
            if on_retry:
                _status = getattr(e, 'status_code', 0) or 0
                _reason = str(e)[:120]
                if _is_timeout:
                    _reason = 'Request timed out'
                elif _status:
                    _reason = f'HTTP {_status}'
                on_retry(attempt=state.hard_attempts + 1, reason=_reason, status_code=_status)
            # Timeout / strict-model → exclude the PAIR (other keys of the
            # model still tried); otherwise exclude the whole MODEL.
            state.note_generic_error(slot, is_timeout=_is_timeout, strict_model=strict_model)
            if _is_timeout:
                logger.debug('%s Timeout (%.0fms) — excluding pair '
                             '%s:%s, trying next slot', tag, latency, slot.key_name, slot.model, exc_info=True)
            elif strict_model:
                logger.debug('%s Stream error (strict_model): %s — excluding pair '
                             '%s:%s, trying other keys', tag, str(e)[:200],
                             slot.key_name, slot.model, exc_info=True)
            else:
                logger.debug('%s Stream error: %s — trying next slot', tag, str(e)[:200], exc_info=True)

    # All retries exhausted or no slot available — raise the last error
    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              what='dispatch_stream')


# ═══════════════════════════════════════════════════════════
#  Public API — async_dispatch_stream (async wrapper)
# ═══════════════════════════════════════════════════════════

async def async_dispatch_stream(body_or_messages, *, on_thinking=None,
                                on_content=None, on_tool_call_ready=None,
                                abort_check=None, max_tokens=4096, temperature=0,
                                thinking_enabled=False, preset='low', effort=None,
                                capability='text', prefer_model=None, tools=None,
                                max_retries=3, log_prefix='', strict_model=False,
                                on_retry=None, avoid_pairs=None,
                                exclude_models=None):
    """Native-async streaming dispatch — non-blocking on the event loop.

    Unlike the previous ``to_thread(dispatch_stream)`` stopgap, this drives the
    genuinely-async ``async_stream_chat`` (httpx) transport, so the streaming
    HTTP call runs ON the event loop without occupying a thread-pool worker.
    Slot selection, retry/429 cycling, and exclusion logic mirror the sync
    ``dispatch_stream`` loop; body adaptation is shared via
    ``_adapt_stream_body_for_slot``.

    Same signature and return shape as ``dispatch_stream``:
        (msg: str, finish_reason: str, usage: dict)

    This is the preferred entry point for async callers (the orchestrator once
    converted to native async, or any ``async def`` route handler).

    ⚠️ RESERVED-BY-DESIGN — NO PRODUCTION CALLER TODAY (verified 2026-06-25).
    Every streaming path in the app (the UI chat worker AND the
    ``/v1/chat/completions`` + ``/v1/messages`` compat endpoints) runs the task
    via ``spawn_task`` → ``asyncio.to_thread`` on an OFF-loop worker thread, then
    the ``async def`` route merely tails the task's event buffer. A worker thread
    is sync and cannot ``await`` this; it correctly uses the sync
    ``dispatch_stream`` (``requests``) instead. Making this go live would require
    EITHER converting ``run_task`` to native async — explicitly rejected (see
    docs/FOLLOWUPS_ASYNC_MIGRATION.md §1: it's off-loop already, so async gives
    no benefit and risks starving the loop with CPU-bound work) — OR a genuinely
    on-loop streaming endpoint that bypasses the thread worker (a real feature,
    not a wiring change). So this is NOT dead code: it's the tested substrate for
    a future native-async streaming caller, kept in lockstep with
    ``dispatch_stream`` (both share ``_StreamRetryState`` +
    ``_adapt_stream_body_for_slot``). It IS exercised by
    ``tests/test_async_dispatch_stream.py``. Do NOT "wire it to the worker" by
    wrapping it in ``asyncio.run()`` inside the thread — that spins a throwaway
    loop per stream and is a pure regression over the sync path.
    """
    from lib.llm import (
        AbortedError,
        ContentFilterError,
        InvalidImageError,
        PermissionError_,
        PromptTooLongError,
        RateLimitError,
    )
    from lib.llm._transport import async_abortable_sleep
    from lib.llm.astream import async_stream_chat
    from lib.llm_errors import EndpointUnreachableError

    dispatcher = get_dispatcher()
    state = _StreamRetryState(exclude_models=exclude_models, avoid_pairs=avoid_pairs)
    is_body = isinstance(body_or_messages, dict) and 'messages' in body_or_messages

    while state.hard_attempts < max_retries:
        if abort_check and abort_check():
            raise AbortedError('Aborted during dispatch retry')

        state.maybe_reset_exclusions(log_prefix, 'async_dispatch_stream')

        slot = dispatcher.pick_and_reserve(
            capability=capability, prefer_model=prefer_model,
            exclude_models=state.eff_exclude_models(),
            exclude_keys=state.eff_exclude_keys(),
            exclude_pairs=state.eff_exclude_pairs(),
            strict_model=strict_model)
        if slot is None:
            if state.relax_avoid_if_exhausted():
                continue
            _slots_exist = dispatcher.has_capable_slots(
                capability, exclude_models=state.exclude,
                exclude_keys=state.exclude_keys, exclude_pairs=state.exclude_pairs)
            if state._429_count > 0 or _slots_exist:
                await async_abortable_sleep(0.3, abort_check)
                state.note_cooldown_cycle()
                continue
            logger.warning('%s async_dispatch_stream: NO CAPABLE SLOT on '
                           'attempt %d/%d', log_prefix, state.hard_attempts + 1, max_retries)
            break

        t0 = time.time()
        ttft_recorded = False
        ttft_value = [None]
        tag = f'{log_prefix}[aD:{slot.key_name}:{slot.model}]'

        body = _adapt_stream_body_for_slot(
            slot, body_or_messages, is_body,
            tools=tools, max_tokens=max_tokens, temperature=temperature,
            thinking_enabled=thinking_enabled, preset=preset, effort=effort)

        def _on_content_wrapper(text):
            nonlocal ttft_recorded
            if not ttft_recorded:
                ttft_value[0] = (time.time() - t0) * 1000
                ttft_recorded = True
            if on_content:
                on_content(text)

        try:
            msg, finish, usage = await async_stream_chat(
                body, api_key=slot.api_key,
                base_url=slot.base_url or None,
                extra_headers=slot.extra_headers or None,
                oauth=slot.oauth or '',
                on_thinking=on_thinking,
                on_content=_on_content_wrapper,
                on_tool_call_ready=on_tool_call_ready,
                abort_check=abort_check, log_prefix=tag,
                api_protocol=slot.protocol or 'openai')
            latency = (time.time() - t0) * 1000
            _out_tokens = 0
            if isinstance(usage, dict):
                _out_tokens = (usage.get('completion_tokens')
                               or usage.get('output_tokens') or 0)
                try:
                    _out_tokens = int(_out_tokens)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[api] async_dispatch_stream caught %s: %s',
                                 type(_e_audit).__name__, _e_audit)
                    _out_tokens = 0
            _prev_ce = slot.consecutive_errors
            slot.record_success(latency, ttft_ms=ttft_value[0],
                                output_tokens=_out_tokens)
            if isinstance(usage, dict):
                usage['_dispatch'] = {
                    'key': slot.key_name, 'model': slot.model,
                    'key_tail': (slot.api_key or '')[-4:],
                    'provider_id': slot.provider_id,
                    'latency_ms': round(latency),
                    'attempt': state.hard_attempts + 1,
                    '429_retries': state._429_count,
                }
            _cool_slot_on_premature_close(slot, usage, _prev_ce)
            logger.debug('%s async_dispatch_stream OK: finish=%s model=%s '
                         'latency=%.0fms', log_prefix, finish, slot.model, latency)
            return msg, finish, usage

        except RateLimitError as e:
            _is_quota = bool(getattr(e, 'is_quota', False))
            slot.record_error(is_rate_limit=True, is_quota_exhausted=_is_quota,
                              error=str(e)[:200] if _is_quota else '')
            state.last_err = e
            if _is_quota:
                state.note_quota_key(slot)
                if on_retry:
                    on_retry(attempt=state.hard_attempts, reason='Key balance exhausted',
                             status_code=429)
                continue
            state.note_free_429()
            if on_retry:
                on_retry(attempt=state._429_count, reason='Rate limited (429)', status_code=429)
            await async_abortable_sleep(0.3, abort_check)
            continue

        except PermissionError_ as e:
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            if not state.note_permission_pair(slot, dispatcher, capability, log_prefix):
                logger.warning('%s Permission denied on %s:%s — excluding pair',
                               log_prefix, slot.key_name, slot.model)

        except EndpointUnreachableError as e:
            # ★ Endpoint host down — cool the slot, exclude the pair, fail
            #   over. Mirrors the sync dispatch_stream handler.
            slot.record_error(is_rate_limit=False, error=str(e)[:200])
            slot.cooldown_until = time.time() + _UNREACHABLE_COOLDOWN
            state.last_err = e
            state.note_unreachable_pair(slot)
            logger.warning(
                '%s Endpoint unreachable on %s:%s (%s) — cooled %ds + '
                'excluded pair, failing over',
                log_prefix, slot.key_name, slot.model,
                getattr(e, 'base_url', '') or '?', _UNREACHABLE_COOLDOWN)
            if on_retry:
                on_retry(attempt=state.hard_attempts,
                         reason='Endpoint unreachable', status_code=0)

        except AbortedError:
            slot.release()
            logger.debug('%s User aborted — stopping dispatch immediately', tag)
            raise

        except ContentFilterError:
            slot.release()
            logger.warning('%s Content filter (HTTP 450) — not retrying', tag, exc_info=True)
            raise

        except PromptTooLongError:
            slot.release()
            logger.warning('%s Prompt/request too large — not retrying', tag)
            raise

        except InvalidImageError:
            slot.release()
            logger.warning('%s Image content error — not retrying', tag)
            raise

        except Exception as e:
            slot.record_error(is_rate_limit=False)
            state.last_err = e
            _is_timeout = 'timed out' in str(e).lower() or 'timeout' in type(e).__name__.lower()
            if on_retry:
                _status = getattr(e, 'status_code', 0) or 0
                on_retry(attempt=state.hard_attempts + 1,
                         reason='Request timed out' if _is_timeout else (f'HTTP {_status}' if _status else str(e)[:120]),
                         status_code=_status)
            state.note_generic_error(slot, is_timeout=_is_timeout, strict_model=strict_model)
            logger.debug('%s async_dispatch_stream error: %s — next slot',
                         tag, str(e)[:200], exc_info=True)

    _raise_dispatch_exhausted(state.last_err, max_retries=max_retries,
                              capability=capability, prefer_model=prefer_model,
                              what='async_dispatch_stream')


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

    from lib.llm import chat

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
                thinking_format=slot.thinking_format or '',
                provider_id=slot.provider_id or '',
                api_protocol=slot.protocol or 'openai',
                oauth=slot.oauth or '',
            )
            latency = (time.time() - t0) * 1000
            _out_tokens = 0
            if isinstance(usage, dict):
                _out_tokens = (usage.get('completion_tokens')
                               or usage.get('output_tokens') or 0)
                try:
                    _out_tokens = int(_out_tokens)
                except (ValueError, TypeError) as _e_audit:
                    logger.debug('[api] _race_worker caught %s: %s', type(_e_audit).__name__, _e_audit)
                    _out_tokens = 0
            slot.record_success(latency, output_tokens=_out_tokens)
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
                                'key_tail': (winner.api_key or '')[-4:],
                                'provider_id': winner.provider_id,
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
               log_prefix='', max_retries=3, timeout=None,
               exclude_models=None, **_kw):
    """Drop-in replacement for ``lib.llm.chat()`` with auto dispatch.

    Uses the fastest available (key, model) slot across ALL keys.
    Falls back to direct ``chat()`` if dispatch fails entirely.

    Signature is intentionally close to ``chat()`` so call sites only
    need to change ``from lib.llm import chat`` →
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
            exclude_models=exclude_models,
        )
    except Exception as e:
        # Ultimate fallback — direct call with default key
        logger.warning('%s[Dispatch] All slots exhausted (%s), '
                    'falling back to direct chat()', log_prefix, e, exc_info=True)
        from lib.llm import chat
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
                   new=(_fb_model or '(lib.llm default)'),
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
