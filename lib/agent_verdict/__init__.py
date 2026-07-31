"""lib/agent_verdict — Shared verdict / loop-control heuristics (facade package).

Single source of truth for the small bundle of *decision logic* that the
agent loops share:

  * the set of "state-changing" (deliverable) tool names;
  * the autopilot virtual-user completion sentinel;
  * counting state-changing vs exploratory tool rounds in a worker turn;
  * parsing a critic / verifier verdict into a next-phase
    (``stop`` / ``worker`` / ``planner``) with the
    anti-analysis-spiral gating (STOP-with-unresolved-markers downgrade,
    CONTINUE_PLANNER requires a gated PLAN_DEFECT reason, replan kill-switch,
    the backend-authoritative done gate);
  * Jaccard "stuck" detection on consecutive verifier feedbacks + the
    diminishing-returns / no-value-progress guard;
  * usage-dict accumulation;
  * the autopilot loop-budget env config (fail-open readers);
  * the shared "cut off by a safety cap, not finished" outcome contract.

Before this module existed, all of the above were hand-copied across
``lib/tasks_pkg/endpoint_review.py``, ``lib/tasks_pkg/endpoint.py``,
``lib/orchestration_engine.py`` (and the VU sentinel in
``lib/tasks_pkg/autopilot.py``) — with explicit "Kept as a local copy …
update BOTH sets" comments.  The three copies had begun to diverge.  This
module reconciles them: callers that want the strict endpoint policy and
callers that want the engine's loose-fallback + virtual-user inversion both
drive the SAME core, parameterised by ``loose_fallback`` and
``verifier_role``.

The module is pure logic — it imports only ``lib.log`` (audit/log) and
``lib.env_compat`` (the env config).  No app/runtime coupling.

This file is a PURE RE-EXPORT FACADE.  The implementations live in the
sub-modules (``_handoff``, ``_verdict``, ``_stuck``, ``_rounds``,
``_config``); ``from lib.agent_verdict import X`` continues to work
byte-identically for every public + consumer-imported symbol.

``audit_log`` is re-exported at the package level so tests that monkeypatch
``lib.agent_verdict.audit_log`` still capture ``classify_verdict``'s override
audits (the ``_verdict._audit`` shim resolves the callable off this package at
call time) — byte-identical to the original single-module contract.
"""

from __future__ import annotations

from lib.log import audit_log, get_logger  # noqa: F401 — audit_log re-exported for monkeypatch parity

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Sentinels, tool sets, structured-token parsers  (from ._handoff)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.agent_verdict._handoff import (  # noqa: E402,F401
    STATE_CHANGING_TOOLS,
    STATE_CHANGING_TOOLS_WITH_CODE_EXEC,
    VU_DONE_SENTINEL,
    VU_ROLE_PROMPT,
    replan_enabled,
    count_state_changing_rounds,
    _PROGRESS_RE,
    parse_progress,
    strip_machine_tokens,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Verdict parsing + terminal-outcome classification  (from ._verdict)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.agent_verdict._verdict import (  # noqa: E402,F401
    _VERDICT_RE,
    _PLAN_DEFECT_RE,
    _UNRESOLVED_EMOJI_RE,
    _UNRESOLVED_PHRASE_RE,
    _LOOSE_STOP_RE,
    _LOOSE_CONTINUE_RE,
    _WORKER_RATIONALIZATIONS,
    _clean_feedback,
    classify_verdict,
    INCOMPLETE_STOP_REASONS,
    is_incomplete_stop,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Non-convergence detectors  (from ._stuck)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.agent_verdict._stuck import (  # noqa: E402,F401
    STUCK_JACCARD,
    _jaccard,
    detect_stuck,
    DIMINISHING_WINDOW,
    DIMINISHING_MIN_RESOLVED,
    DIMINISHING_TARGET_OVERLAP,
    autopilot_progress_window,
    detect_diminishing_returns,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Round bookkeeping  (from ._rounds)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.agent_verdict._rounds import (  # noqa: E402,F401
    accumulate_usage,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Autopilot loop-budget guards + env config  (from ._config)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.agent_verdict._config import (  # noqa: E402,F401
    AUTOPILOT_MAX_TURNS_DEFAULT,
    AUTOPILOT_STUCK_WINDOW,
    AUTOPILOT_SUMMARY_RETENTION_DEFAULT,
    autopilot_summary_retention,
    autopilot_max_turns,
)


__all__ = [
    # ._handoff
    'STATE_CHANGING_TOOLS',
    'STATE_CHANGING_TOOLS_WITH_CODE_EXEC',
    'VU_DONE_SENTINEL',
    'VU_ROLE_PROMPT',
    'replan_enabled',
    'count_state_changing_rounds',
    'parse_progress',
    'strip_machine_tokens',
    # ._verdict
    'classify_verdict',
    'is_incomplete_stop',
    'INCOMPLETE_STOP_REASONS',
    # ._stuck
    'STUCK_JACCARD',
    'detect_stuck',
    'DIMINISHING_WINDOW',
    'DIMINISHING_MIN_RESOLVED',
    'DIMINISHING_TARGET_OVERLAP',
    'autopilot_progress_window',
    'detect_diminishing_returns',
    # ._rounds
    'accumulate_usage',
    # ._config
    'AUTOPILOT_MAX_TURNS_DEFAULT',
    'AUTOPILOT_STUCK_WINDOW',
    'AUTOPILOT_SUMMARY_RETENTION_DEFAULT',
    'autopilot_summary_retention',
    'autopilot_max_turns',
]
