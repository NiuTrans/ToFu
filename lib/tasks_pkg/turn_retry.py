"""Turn-level automatic retry — self-healing over transient terminal errors.

Tofu already has deep *intra-turn* resilience: the SSE layer retries premature
/ zero-byte / empty-stop stream anomalies up to 16× (see
``lib/tasks_pkg/stream_handler.py``), ``_llm_call_with_fallback`` transparently
swaps to a fallback model, and ``PromptTooLongError`` triggers reactive
compaction.  But once a turn *settles* into ``status='error'`` — even when the
typed error envelope is flagged ``retryable`` (``ratelimit`` / ``no_slot`` /
``timeout`` / ``network`` / ``premature_close`` / ``abnormal_stop`` /
``server_offline`` / ``tool_timeout``) — the conversation just stops and waits
for the user to click Retry.

A user firing many parallel conversations then has to babysit each one: a
transient 429 or a gateway hiccup on any single turn strands it.  This module
adds the missing layer: a **bounded turn-level auto-retry** so the runtime
re-runs a settled-but-transiently-failed turn on its own, and only surfaces the
error for manual intervention once the budget is exhausted.

Design
------
* PURE decision logic here (``should_auto_retry_turn``); the orchestrator owns
  the re-run mechanics (reset per-turn accumulators, emit ``phase:retrying`` +
  ``retry_reset``, sleep, re-enter the loop).  Keeping the verdict pure makes
  it unit-testable without a DB / route harness (the project's NC-bite habit).
* Only the ``_AUTO_RETRY_KINDS`` set is eligible — the transient transport /
  dispatch failures where re-running the SAME request is genuinely likely to
  succeed.  Persistent kinds (``quota`` / ``permission`` / ``content_filter`` /
  ``invalid_image`` / ``prompt_too_long`` / ``model_limit`` / ``internal`` /
  ``dispatch_exhausted``) are NEVER auto-retried — a re-run would just re-fail
  and waste tokens; those still surface immediately for the user to act on.
* Bounded: ``_AUTO_TURN_RETRY_MAX`` attempts, exponential backoff with jitter,
  interruptible by user abort.  This sits ABOVE the inner per-round retry
  budgets, so the two compose (inner retries exhaust → turn errors → at most a
  few whole-turn re-runs).
* Opt-out: ``cfg['disableAutoTurnRetry'] == True`` forces no auto-retry
  (headless benchmarks that must observe the raw error, mirroring
  ``disableModelFallback``).

The re-run replays from ``task['messages']``, which by the time the orchestrator
reaches finalization holds every COMPLETED tool round as history (the error
break falls through to the ``task['messages'] = messages`` write-back before
finalize).  A transient error fires at the LLM-stream boundary — BEFORE any
tool executes in the failing round — so re-running never double-executes a
side-effecting tool; the model simply resumes after the last committed round.
"""

from __future__ import annotations

import random

from lib.log import get_logger

logger = get_logger(__name__)


# Error kinds where transparently re-running the WHOLE turn is worthwhile.
# Strict subset of error_envelope._RETRYABLE_KINDS: every member is a
# transient transport / dispatch failure that fires at the stream boundary,
# so a fresh run of the same request has a real chance of succeeding and
# cannot double-execute a committed tool.
_AUTO_RETRY_KINDS = frozenset({
    'ratelimit',        # 429 / TPM-RPM throttle — window resets
    'no_slot',          # dispatch found zero usable slots — a slot frees up
    'timeout',          # upstream / network read timeout
    'network',          # connection reset / DNS / proxy blip
    'endpoint_unreachable',  # BYO endpoint briefly down / restarting
    'premature_close',  # gateway cut the SSE stream (inner retries exhausted)
    'abnormal_stop',    # missing finish marker (inner retries exhausted)
    'server_offline',   # transient server-side unavailability
    'tool_timeout',     # repeated tool-execution timeouts — often transient
})

# Max WHOLE-TURN re-runs before giving up and surfacing the error for manual
# retry.  Intentionally small: the inner per-round budgets (16× for stream
# anomalies) already absorb most transient blips, so this only catches the
# rarer case where the inner budget itself is exhausted or the failure is at
# the dispatch layer.  Each attempt re-bills the prompt, so keep it tight.
_AUTO_TURN_RETRY_MAX = 3

# Exponential backoff (seconds) before the Nth whole-turn re-run, + jitter.
# 3s, 6s, 12s (capped) — generous enough for a 429 window / gateway pool to
# recover, short enough that a user watching the conversation sees it heal
# rather than hang.
_AUTO_TURN_BACKOFF_BASE_S = 3.0
_AUTO_TURN_BACKOFF_MAX_S = 30.0
_AUTO_TURN_BACKOFF_JITTER_S = 1.0


def auto_turn_retry_max(cfg: dict | None = None) -> int:
    """Return the effective whole-turn retry cap.

    ``cfg['autoTurnRetryMax']`` (a non-negative int) overrides the default,
    letting an operator tune the budget per task without editing code.  A
    value of 0 disables auto-retry as surely as ``disableAutoTurnRetry``.
    """
    if cfg:
        try:
            v = cfg.get('autoTurnRetryMax')
            if v is not None:
                iv = int(v)
                if iv >= 0:
                    return iv
        except (ValueError, TypeError) as e:
            logger.debug('autoTurnRetryMax parse failed, using default: %s', e)
    return _AUTO_TURN_RETRY_MAX


def auto_turn_backoff_seconds(attempt: int) -> float:
    """Sleep (seconds) before the ``attempt``-th whole-turn re-run.

    ``attempt`` is 1-based: 1st ≈3s, 2nd ≈6s, 3rd ≈12s, capped at 30s, each
    plus uniform jitter in ``[0, _AUTO_TURN_BACKOFF_JITTER_S)`` so a fleet of
    conversations that all 429'd at once do not re-fire in lockstep.
    """
    base = min(
        _AUTO_TURN_BACKOFF_BASE_S * (2 ** max(0, attempt - 1)),
        _AUTO_TURN_BACKOFF_MAX_S,
    )
    return base + random.uniform(0.0, _AUTO_TURN_BACKOFF_JITTER_S)


def should_auto_retry_turn(error_envelope, attempt: int,
                           cfg: dict | None = None) -> tuple[bool, float]:
    """Decide whether a settled-error turn should be auto-re-run.

    Parameters
    ----------
    error_envelope : dict | None
        The typed error envelope on ``task['error']`` (see
        ``lib/error_envelope.py``).  ``None`` / non-dict → no retry.
    attempt : int
        How many whole-turn auto-retries have ALREADY happened (0 on the
        first failure).  The next attempt would be ``attempt + 1``.
    cfg : dict | None
        The task config.  Honours ``disableAutoTurnRetry`` (hard off) and
        ``autoTurnRetryMax`` (numeric override).

    Returns
    -------
    (retry, backoff_seconds)
        ``retry`` is True iff the envelope's kind is auto-retryable, the
        caller opted in, and the budget is not yet exhausted.  When True,
        ``backoff_seconds`` is how long to wait before the re-run; when
        False it is 0.0.
    """
    if cfg and cfg.get('disableAutoTurnRetry'):
        return False, 0.0

    if not isinstance(error_envelope, dict):
        return False, 0.0

    kind = error_envelope.get('kind')
    if kind not in _AUTO_RETRY_KINDS:
        return False, 0.0

    cap = auto_turn_retry_max(cfg)
    if attempt >= cap:
        return False, 0.0

    return True, auto_turn_backoff_seconds(attempt + 1)
