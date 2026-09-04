# HOT_PATH
"""Context-window completion clamp.

Cohesive group:
  - _clamp_completion_to_context_window(..., precomputed_input_tokens=None)
  - _COMPLETION_INPUT_MARGIN / _COMPLETION_MIN_FLOOR (module constants)
"""

from lib.log import get_logger
from lib.token_counter.evidence import validated_admitted_input_tokens

logger = get_logger(__name__)

# ── Context-window completion clamp ──
# _clamp_max_tokens() caps the completion budget against the model's
# *output* ceiling only; it is blind to the input size. When both the
# prompt and the requested completion budget are large, their sum can
# exceed the model's *total* context window and upstream rejects with
# HTTP 400 (PromptTooLongError) — e.g. "210328 tokens requested >
# 202752 maximum (82328 input + 128000 completion)".
_COMPLETION_INPUT_MARGIN = 0.10  # headroom over the (under-counting) estimate
_COMPLETION_MIN_FLOOR = 1024     # never hand the API an unusably tiny budget


def _clamp_completion_to_context_window(
    model,
    messages,
    max_tokens,
    provider_id='',
    *,
    precomputed_input_tokens=None,
):
    """Trim ``max_tokens`` so estimated input + completion fits the window.

    Returns a (possibly reduced) completion budget that leaves room for the
    estimated input tokens within the model's context window, plus a small
    margin for estimation error. Floored at ``_COMPLETION_MIN_FLOOR`` — if
    even that doesn't fit, the prompt itself is over-budget and the request
    will (correctly) raise PromptTooLongError, handing recovery to the
    reactive-compaction path rather than silently truncating here.

    Never raises: on any failure it returns ``max_tokens`` unchanged so a
    clamp helper can never block a request.
    """
    if not messages or not max_tokens or max_tokens <= 0:
        return max_tokens
    try:
        from lib.tasks_pkg.compaction._tokens import resolve_model_context_limit
    except Exception as e:
        logger.debug('[build_body] context-window clamp unavailable: %s', e)
        return max_tokens
    try:
        window = resolve_model_context_limit(model, provider_id)
        if not window or window <= 0:
            return max_tokens
        admitted_input_tokens = validated_admitted_input_tokens(
            precomputed_input_tokens)
        if admitted_input_tokens is not None:
            input_tokens = admitted_input_tokens
        else:
            from lib.token_counter.heuristic import cheap_estimate

            input_tokens = cheap_estimate(messages)
        # The fallback estimate can under-count non-CJK text, while the final
        # admission count can still contain bounded tokenizer/cache drift. Keep
        # the established margin for either source before reserving completion.
        reserved_input = int(input_tokens * (1 + _COMPLETION_INPUT_MARGIN)) + 512
        room = window - reserved_input
        if room < max_tokens:
            new_max = max(room, _COMPLETION_MIN_FLOOR)
            if new_max < max_tokens:
                logger.warning(
                    '[build_body] Clamping max_tokens %d → %d to fit context '
                    'window (model=%s window=%d est_input=%d reserved=%d)',
                    max_tokens, new_max, model, window, input_tokens,
                    reserved_input)
            return new_max
    except Exception as e:
        logger.warning('[build_body] context-window clamp failed: %s', e)
    return max_tokens
