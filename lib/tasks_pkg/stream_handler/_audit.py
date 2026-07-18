"""One-time §10.1 audit when per-phase premature-retry scope is in effect.

Shared audit-once state (``_AUDIT_LOCK`` + ``_AUDIT_LOGGED``) lives here
together with the function that flips the flag (``_maybe_audit_phase_scope``)
so the ``global _AUDIT_LOGGED`` rebind stays inside a single module.
"""

import threading

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# ── One-time §10.1 audit when per-phase scope is in effect ──
# The premature-retry counter used to be local to ``run_task``'s round
# loop (i.e. ``per_round`` scope — counter reset on every iteration of
# the while loop).  The counter is now lifted to the task dict and
# survives across rounds within the same Worker / Planner phase.  This
# is a §10.1 hyperparameter-adjacent change (it tightens the effective
# retry budget against pathological gateways) and the user signed off
# in writing.  Emit one audit entry the first time the new path is
# exercised so the change is traceable in audit.log.
_AUDIT_LOCK = threading.Lock()
_AUDIT_LOGGED = False


def _maybe_audit_phase_scope() -> None:
    global _AUDIT_LOGGED
    if _AUDIT_LOGGED:
        return
    with _AUDIT_LOCK:
        if _AUDIT_LOGGED:
            return
        _AUDIT_LOGGED = True
        try:
            audit_log(
                'config_change',
                param='premature_retry_scope',
                old='per_round',
                new='per_phase',
                approved_by='user',
                reason='retry counter now survives across rounds within '
                       'the same Worker / Planner phase, resets only at '
                       'phase boundaries (CONTINUE_PLANNER, new task)',
            )
        except Exception as e:
            logger.debug('[stream_handler] phase-scope audit_log failed: %s', e)
