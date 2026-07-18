"""lib/agent_verdict/_config.py — Autopilot loop-budget guards + env config.

Env-driven, FAIL-OPEN configuration readers for the autopilot loop, plus the
module-level defaults they fall back to:

  * ``AUTOPILOT_MAX_TURNS_DEFAULT`` / ``autopilot_max_turns`` — hard VU-turn
    ceiling (safety valve);
  * ``AUTOPILOT_STUCK_WINDOW`` — the detect_stuck window autopilot feeds;
  * ``AUTOPILOT_SUMMARY_RETENTION_DEFAULT`` / ``autopilot_summary_retention`` —
    max concluded-run (fold) records retained in settings.

Pure logic — imports only ``lib.log`` and ``lib.env_compat``.
"""

from __future__ import annotations

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Autopilot loop-budget guards
# ══════════════════════════════════════════════════════════

# Hard ceiling on VU turns per autopilot run — the safety valve the loop
# historically lacked ("No turn cap, no state-change watchdog" — see
# autopilot.py docstring).  Endpoint caps a SINGLE task at MAX_ITERATIONS=10
# worker↔critic rounds; an autopilot run is coarser (each turn is a whole
# agent task) and legitimately longer-horizon, so the default is higher.
AUTOPILOT_MAX_TURNS_DEFAULT = 40

# Autopilot feeds detect_stuck its VU REQUEST-text history with this window:
# three near-identical nudges in a row = a non-converging loop (two can be a
# legitimate "you didn't do it, try again").
AUTOPILOT_STUCK_WINDOW = 3

# Max concluded-run (fold) records retained in ``settings.autopilotSummaries``
# on a single long-lived conversation.  The map ACCRETES one record per run and
# is re-serialized into every settings PUT + IndexedDB write, so an unbounded
# map makes every turn's write cost grow O(n) on a year-scale conversation.
# Cap it to the most-recent N by ``ts``.
AUTOPILOT_SUMMARY_RETENTION_DEFAULT = 30


def autopilot_summary_retention() -> int:
    """Max concluded-run records to keep in ``settings.autopilotSummaries``.

    Reads ``TOFU_AUTOPILOT_SUMMARY_RETENTION`` (default 30).  FAIL-OPEN like
    :func:`autopilot_max_turns`: unset→default, ``0``/<=0→UNLIMITED (never
    prune — the pre-cap behaviour), garbage→default.
    """
    raw = getenv_compat('TOFU_AUTOPILOT_SUMMARY_RETENTION', default='').strip()
    if not raw:
        return AUTOPILOT_SUMMARY_RETENTION_DEFAULT
    try:
        val = int(raw)
    except (ValueError, TypeError):
        logger.warning('[Autopilot] TOFU_AUTOPILOT_SUMMARY_RETENTION=%r not an '
                       'int — using default %d', raw,
                       AUTOPILOT_SUMMARY_RETENTION_DEFAULT)
        return AUTOPILOT_SUMMARY_RETENTION_DEFAULT
    return val if val > 0 else 0


def autopilot_max_turns() -> int:
    """VU turn budget per autopilot run (hard ceiling / safety valve).

    Reads ``TOFU_AUTOPILOT_MAX_TURNS`` (default 40).  A value of ``0`` (or any
    value <= 0) means UNLIMITED — the pre-guard behaviour — so the budget is
    FAIL-OPEN: an unset var uses the default 40, an explicit ``0`` disables the
    cap, and a garbage/non-int var falls back to the default rather than
    accidentally wedging the loop.  Mirrors the env-gated, fail-open rollout
    convention (lib/rate_limit_store.py).

    Returns
    -------
    int
        The turn budget, or ``0`` for unlimited.
    """
    raw = getenv_compat('TOFU_AUTOPILOT_MAX_TURNS', default='').strip()
    if not raw:
        return AUTOPILOT_MAX_TURNS_DEFAULT
    try:
        val = int(raw)
    except (ValueError, TypeError):
        logger.warning('[Autopilot] TOFU_AUTOPILOT_MAX_TURNS=%r not an int — '
                       'using default %d', raw, AUTOPILOT_MAX_TURNS_DEFAULT)
        return AUTOPILOT_MAX_TURNS_DEFAULT
    return val if val > 0 else 0
