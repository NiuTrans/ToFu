"""Bounded automatic regeneration after transient provider/dispatch failures.

``should_auto_retry_turn`` is a pure decision shared by normal and fatal task
finalization. Eligible error kinds use the existing Turn recovery blocker,
attempt budget and caller opt-out. Explicit manual/condition-change policy,
protocol failures and exhausted automatic budgets cannot be retried here.

The orchestrator owns execution: it admits regeneration only before tool
entry, preserves paid usage and restores the pristine invocation input.
Owner cancellation and retained partial replies keep their existing guards.
Backoff, events, recursive invocation and terminal settlement are not duplicated
in this policy module. Inner transport retries retain their own lifecycles.
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
    'upstream_error',   # typed 5xx / gateway outage after inner recovery exits
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
        ``lib/error_envelope/``).  ``None`` / non-dict → no retry.
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
        shared recovery policy permits it, the caller has not opted out,
        and the budget is not yet exhausted. When True,
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

    # Settlement, child wakes and this retry path must agree on typed blockers.
    # A transient kind alone cannot override explicit manual/condition-change
    # recovery or restart an exhausted automatic budget.
    from lib.turn_lifecycle import automatic_recovery_blocker
    if automatic_recovery_blocker(error_envelope) is not None:
        return False, 0.0

    cap = auto_turn_retry_max(cfg)
    if attempt >= cap:
        return False, 0.0

    return True, auto_turn_backoff_seconds(attempt + 1)
