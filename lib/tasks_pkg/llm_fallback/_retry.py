"""Fallback-model resolution and empty-stop retry flagging.

Both helpers are pure decision functions with no cross-submodule state:
``_get_fallback_model`` reads global/ per-task config; ``_flag_empty_stop_for_retry``
mutates the passed ``usage`` dict in place.
"""

from lib.log import get_logger

logger = get_logger(__name__)

_DEFAULT_FALLBACK_MAX_429_ATTEMPTS = 3
_HARD_MAX_FALLBACK_429_ATTEMPTS = 16


def _fallback_max_429_attempts(task: dict | None = None) -> int:
    """Return the bounded upstream-429 budget for fallback/rescue attempts.

    Primary user-selected dispatch remains indefinitely waitable by default.
    Once that path has already failed and the system switches models, however,
    an unbounded fallback would leave the client permanently in ``retrying``
    with no terminal event. The per-task override is clamped to a small hard
    ceiling so malformed or stale config cannot defeat the resource bound.
    """
    raw_value = None
    if isinstance(task, dict):
        config = task.get('config')
        if isinstance(config, dict):
            raw_value = config.get('fallbackMax429Attempts')
    if raw_value is None or isinstance(raw_value, bool):
        return _DEFAULT_FALLBACK_MAX_429_ATTEMPTS
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_FALLBACK_MAX_429_ATTEMPTS
    return max(1, min(parsed, _HARD_MAX_FALLBACK_429_ATTEMPTS))


def _get_fallback_model(task: dict | None = None) -> str:
    """Return the configured fallback model, or empty string if disabled.

    Reads from ``lib.FALLBACK_MODEL`` which is backed by
    ``data/config/server_config.json`` → ``model_defaults.fallback_model``.
    Users can set this via Settings UI > 显示 > 模型默认.

    A per-task opt-out (``task['config']['disableModelFallback'] == True``)
    forces an empty string regardless of global config. This is essential
    for controlled experiments: without it, a transient primary-model
    error silently switches the round to the global fallback model
    (e.g. Opus), cross-contaminating a benchmark that is supposed to
    measure ONLY the requested model. With it, the round simply errors
    and the caller/harness retries on the SAME model.
    """
    if task is not None:
        try:
            if (task.get('config') or {}).get('disableModelFallback'):
                return ''
        except Exception as e:
            logger.debug('disableModelFallback check failed: %s', e)
    import lib as _lib
    return getattr(_lib, 'FALLBACK_MODEL', '') or ''


def _get_pool_rescue_model(failed_models=None) -> str:
    """Return the configured default as the pool rescue's soft preference.

    Pool rescue remains non-strict: if the default is unavailable, dispatch
    can still widen to any healthy slot. The preference prevents score-only
    selection from jumping first to a cheap/fast catalogue entry when the
    user's configured default is healthy.
    """
    import lib as _lib
    candidate = str(getattr(_lib, 'LLM_MODEL', '') or '').strip()
    return '' if not candidate or candidate in set(failed_models or ()) \
        else candidate



def _flag_empty_stop_for_retry(assistant_msg: dict, finish_reason, task: dict,
                               round_num: int, usage: dict) -> bool:
    """Recognise a round-0 empty/whitespace-only ``stop`` and flag it for RETRY.

    Replaces the old "empty stop on round 0 ⇒ content_filter" heuristic, which
    was too aggressive: it declared a terminal content-policy block (NO retry)
    on any 200-OK ``finish=stop`` that produced no visible content. But a
    GENUINE policy violation arrives as :class:`ContentFilterError` (HTTP 450),
    handled separately and terminal. A plain empty stop is a TRANSIENT gateway
    artifact — proven by ``debug/repro_conv_empty_stop.py``, which replays the
    exact large request that empty-stopped in production and gets clean content
    6/6 times.

    The stream layer (``lib/llm/_sse_core.py``) only sets the
    ``_empty_stop`` / ``_stream_anomaly`` flags when ``finish=stop AND not
    content AND chunk_count > 0`` — so a whitespace-only body (``content`` is
    truthy) or a zero-chunk clean-``[DONE]`` close slips through UNFLAGGED and
    the empty_stop retry bucket (cap 2, in ``analyse_stream_result``) never
    fires. This helper closes that gap: when the round is a bare empty stop on
    the FIRST round that the stream layer did NOT already flag, it sets the
    ``_empty_stop`` + ``_stream_anomaly`` flags on ``usage`` (mutated in place)
    so ``analyse_stream_result`` retries it via the empty_stop / zero_byte
    bucket. Only after those retries are exhausted does it surface honestly as
    ``abnormal_stop`` — never as a fabricated content-policy block.

    Returns ``True`` iff it flagged this round for retry (so the caller can log
    it). Round > 0 is left alone (empty content after tool calls is legitimate).
    """
    if finish_reason != 'stop':
        return False
    if round_num != 0:
        return False
    if (assistant_msg.get('content', '') or '').strip():
        return False
    if (task.get('content') or '').strip() or (task.get('thinking') or '').strip():
        return False
    # Already flagged by the stream layer → the existing retry machinery picks
    # it up unchanged; nothing to add.
    if usage.get('_stream_anomaly'):
        return False
    usage['_empty_stop'] = True
    usage['_stream_anomaly'] = True
    return True
