"""lib/agent_verdict/_verdict.py — Verdict parsing + terminal-outcome
classification (the core of the STOP/CONTINUE gate).

Carries:

  * the verdict / plan-defect / unresolved-marker / loose-fallback regexes and
    the ``_WORKER_RATIONALIZATIONS`` blocklist, each kept WITH the function
    that uses it;
  * ``_clean_feedback`` + ``classify_verdict`` — the unified core behind
    endpoint mode's ``_parse_verdict`` and the engine's ``_classify_verdict``;
  * ``INCOMPLETE_STOP_REASONS`` + ``is_incomplete_stop`` — the shared
    "cut off by a safety cap, not genuinely finished" contract both loops
    escalate on.

Pure logic — imports only ``lib.log`` plus the sibling ``_handoff`` module
(for ``VU_DONE_SENTINEL``, ``replan_enabled``, and ``parse_progress`` used by
the backend-authoritative done gate).
"""

from __future__ import annotations

import re

from lib.log import audit_log, get_logger

from lib.agent_verdict._handoff import (
    VU_DONE_SENTINEL,
    parse_progress,
    replan_enabled,
)

logger = get_logger(__name__)


def _audit(event, **kw):
    """Emit an audit-log event through the PACKAGE-level ``audit_log`` name.

    The original single-module ``agent_verdict`` exposed ``audit_log`` as a
    module global, and tests monkeypatch ``lib.agent_verdict.audit_log`` to
    capture the verdict-override audits.  After the split, ``classify_verdict``
    lives here — so it resolves the callable off the ``lib.agent_verdict``
    package at call time, honouring that patch byte-identically.  Falls back to
    this module's imported ``audit_log`` if the package attribute is missing.
    """
    import lib.agent_verdict as _pkg
    fn = getattr(_pkg, 'audit_log', audit_log)
    return fn(event, **kw)


# ══════════════════════════════════════════════════════════
#  Verdict parsing
# ══════════════════════════════════════════════════════════

# Match all three modern tags plus the legacy bare "CONTINUE" (maps to
# CONTINUE_WORKER).
_VERDICT_RE = re.compile(
    r'\[VERDICT:\s*(STOP|CONTINUE_WORKER|CONTINUE_PLANNER|CONTINUE)\s*\]',
    re.IGNORECASE,
)

# Mandatory for CONTINUE_PLANNER: structured plan-defect reason.  Without
# this tag in the feedback body, CONTINUE_PLANNER is downgraded to
# CONTINUE_WORKER.
_PLAN_DEFECT_RE = re.compile(
    r'\[PLAN_DEFECT:\s*([^\]]+)\]',
    re.IGNORECASE,
)

# Patterns that indicate a STOP verdict whose feedback STILL contains
# unresolved items (a worker-didn't-finish problem, not a real done signal).
_UNRESOLVED_EMOJI_RE = re.compile(r'❌')
_UNRESOLVED_PHRASE_RE = re.compile(
    r'\b(?:NOT met|still failing|still NOT met|unresolved)\b',
    re.IGNORECASE,
)

# Loose, tag-free heuristics — used ONLY when ``loose_fallback=True`` and no
# explicit [VERDICT:] tag is present (plain-language critics / the original
# orchestration engine behaviour).
_LOOSE_STOP_RE = re.compile(
    r'\b(VERDICT:\s*STOP|approved|looks good|all (?:met|pass)|✅)\b', re.IGNORECASE)
_LOOSE_CONTINUE_RE = re.compile(
    r'\b(CONTINUE|not met|still (?:failing|broken)|unresolved|❌)\b', re.IGNORECASE)

# "Plan defect" reasons that are really worker-execution complaints in
# disguise — these are rejected so the critic can't escape into a replan
# spiral on a worker problem.
_WORKER_RATIONALIZATIONS = (
    'worker did',
    "worker didn't",
    'worker did not',
    'worker needs',
    'worker should',
    'still ❌',
    'remaining ❌',
    'remaining items',
    'more iterations',
)


def _clean_feedback(text: str, match: re.Match) -> str:
    """Strip the verdict tag, trailing '### Verdict' header, and PLAN_DEFECT
    tag from the critic content so the display feedback is clean."""
    feedback = text[:match.start()].rstrip()
    feedback = re.sub(
        r'\n*#+\s*Verdict\s*:?\s*$',
        '',
        feedback,
        flags=re.IGNORECASE,
    ).rstrip()
    feedback = _PLAN_DEFECT_RE.sub('', feedback).rstrip()
    return feedback


def classify_verdict(
    text: str,
    *,
    verifier_role: str = '',
    loose_fallback: bool = False,
    strip_feedback: bool = False,
) -> dict:
    """Parse a verifier's output into a structured verdict.

    This is the unified core behind endpoint mode's ``_parse_verdict`` and
    the orchestration engine's ``_classify_verdict``.  Both gate identically;
    they differ only in (a) whether a missing tag falls back to loose
    plain-language heuristics, (b) virtual-user inversion, and (c) whether the
    caller wants the cleaned feedback text back.  Those are the kwargs.

    Parameters
    ----------
    text : str
        Raw verifier / critic content.
    verifier_role : str
        When ``'virtual_user'``, autopilot inversion applies: only an explicit
        ``[VU: TASK_DONE]`` sentinel or a STOP verdict ends the loop; any other
        reply (including empty) means ``worker`` (keep going).
    loose_fallback : bool
        When True and no explicit ``[VERDICT:]`` tag is present, fall back to
        the loose STOP/CONTINUE heuristics (engine behaviour).  When False, a
        missing tag defaults to ``worker`` (endpoint behaviour).
    strip_feedback : bool
        When True, also compute the cleaned display feedback (tags + trailing
        '### Verdict' header removed).  Endpoint needs this; the engine does
        not.

    Returns
    -------
    dict with keys:
        phase : str               — 'stop' | 'worker' | 'planner'
        plan_defect : str | None  — extracted PLAN_DEFECT reason (gated)
        feedback : str | None     — cleaned feedback when ``strip_feedback``
                                    else None
        had_tag : bool            — whether an explicit [VERDICT:] tag matched
    """
    # ── Virtual-user inversion (autopilot) ──
    if verifier_role == 'virtual_user':
        low = (text or '').lower()
        fb = (text or '') if strip_feedback else None
        wants_stop = (VU_DONE_SENTINEL.lower() in low
                      or '[verdict: stop]' in low or 'verdict: stop' in low)
        if wants_stop:
            # Anti-premature-done guard: a TASK_DONE whose own text STILL
            # flags unresolved work (❌ / "NOT met" / "still failing" /
            # "unresolved") is the virtual user rubber-stamping the agent's
            # self-report rather than verifying the objective.  Downgrade to
            # 'worker' so the autopilot loop keeps going.  Reuses the exact
            # marker scan the endpoint critic's STOP guard uses — one policy,
            # one place.
            x_count = len(_UNRESOLVED_EMOJI_RE.findall(text or ''))
            phrase_hits = _UNRESOLVED_PHRASE_RE.findall(text or '')
            if x_count > 0 or phrase_hits:
                logger.warning(
                    '[Verdict] Override VU TASK_DONE→CONTINUE: reply still '
                    'contains %d ❌ markers and %d unresolved phrases',
                    x_count, len(phrase_hits),
                )
                _audit(
                    'vu_done_override',
                    original='stop',
                    new='worker',
                    x_count=x_count,
                    phrase_hits=len(phrase_hits),
                    reason='unresolved_markers_in_vu_done',
                )
                return {'phase': 'worker', 'plan_defect': None,
                        'feedback': fb, 'had_tag': False}
            # ── Backend-authoritative gate: a TASK_DONE is SELF-CONTRADICTORY
            #    when its own mandatory [PROGRESS: remaining=Y] has Y>0. This
            #    is the ROOT-CAUSE fix for "VU stops early" — it does NOT trust
            #    the model's done-claim over the hard progress signal it is
            #    required to emit (the same signal detect_diminishing_returns
            #    trusts). FAIL-OPEN: an absent/unparseable PROGRESS line yields
            #    (None, None) → we cannot prove incompleteness → allow the stop.
            _resolved, _remaining = parse_progress(text or '')
            if _remaining is not None and _remaining > 0:
                logger.warning(
                    '[Verdict] Override VU TASK_DONE→CONTINUE: PROGRESS reports '
                    'remaining=%d > 0 — done-claim contradicts its own signal',
                    _remaining,
                )
                _audit(
                    'vu_done_override',
                    original='stop',
                    new='worker',
                    remaining=_remaining,
                    reason='progress_remaining_in_vu_done',
                )
                return {'phase': 'worker', 'plan_defect': None,
                        'feedback': fb, 'had_tag': False}
            return {'phase': 'stop', 'plan_defect': None,
                    'feedback': fb, 'had_tag': False}
        return {'phase': 'worker', 'plan_defect': None,
                'feedback': fb, 'had_tag': False}

    # Extract the (last) PLAN_DEFECT reason if present.
    plan_defect = None
    for m in _PLAN_DEFECT_RE.finditer(text or ''):
        plan_defect = m.group(1).strip()

    # Find the LAST VERDICT match (in case the critic emits more than one).
    match = None
    for m in _VERDICT_RE.finditer(text or ''):
        match = m

    if match is None:
        if loose_fallback and text:
            # Tag-free: loose plain-language heuristics.
            if _LOOSE_STOP_RE.search(text):
                phase = 'stop'
            elif _LOOSE_CONTINUE_RE.search(text):
                phase = 'worker'
            else:
                phase = 'stop'   # ambiguous → stop, never spin forever
            feedback = None
            if strip_feedback:
                feedback = _PLAN_DEFECT_RE.sub('', text).strip()
            had_tag = False
        elif loose_fallback and not text:
            # Engine: empty verifier output ends the loop.
            return {'phase': 'stop', 'plan_defect': plan_defect,
                    'feedback': '' if strip_feedback else None,
                    'had_tag': False}
        else:
            logger.warning('[Verdict] No [VERDICT] tag found in verifier '
                           'output (%d chars), defaulting to CONTINUE_WORKER',
                           len(text or ''))
            phase = 'worker'
            feedback = None
            if strip_feedback:
                feedback = _PLAN_DEFECT_RE.sub('', text or '').strip()
            had_tag = False
    else:
        had_tag = True
        tag = match.group(1).upper()
        if tag == 'STOP':
            phase = 'stop'
        elif tag == 'CONTINUE_PLANNER':
            phase = 'planner'
        else:
            phase = 'worker'   # CONTINUE_WORKER or legacy bare CONTINUE
        feedback = _clean_feedback(text, match) if strip_feedback else None

    # The marker scan runs against the cleaned feedback when we have it
    # (endpoint), else against the raw text (engine) — both contain the
    # markers, and the engine never strips.
    marker_src = feedback if (strip_feedback and feedback is not None) else (text or '')

    # ── Guard: STOP with unresolved markers → downgrade to CONTINUE_WORKER ──
    if phase == 'stop':
        x_count = len(_UNRESOLVED_EMOJI_RE.findall(marker_src))
        phrase_hits = _UNRESOLVED_PHRASE_RE.findall(marker_src)
        if x_count > 0 or phrase_hits:
            # A single residual ❌ is almost always "worker didn't finish the
            # last step", not "the plan is structurally wrong".  Forcing a
            # re-plan wipes the worker's accumulated progress and tends to
            # escalate; CONTINUE_WORKER lets the worker address it directly.
            logger.warning(
                '[Verdict] Override STOP→CONTINUE_WORKER: feedback still '
                'contains %d ❌ markers and %d unresolved phrases',
                x_count, len(phrase_hits),
            )
            _audit(
                'critic_verdict_override',
                original='stop',
                new='worker',
                x_count=x_count,
                phrase_hits=len(phrase_hits),
                reason='unresolved_markers_in_stop_feedback',
            )
            phase = 'worker'

    # ── Guard: CONTINUE_PLANNER gating ──
    if phase == 'planner':
        if not plan_defect:
            logger.warning(
                '[Verdict] Override CONTINUE_PLANNER→CONTINUE_WORKER: no '
                '[PLAN_DEFECT: ...] tag supplied.  Replan requires an '
                'explicit structural reason.'
            )
            _audit(
                'critic_verdict_override',
                original='planner',
                new='worker',
                reason='missing_plan_defect_tag',
            )
            phase = 'worker'
        elif any(p in plan_defect.lower() for p in _WORKER_RATIONALIZATIONS):
            logger.warning(
                '[Verdict] Override CONTINUE_PLANNER→CONTINUE_WORKER: '
                'PLAN_DEFECT reason looks like a worker-execution problem: %r',
                plan_defect,
            )
            _audit(
                'critic_verdict_override',
                original='planner',
                new='worker',
                reason='plan_defect_is_worker_problem',
                defect_preview=plan_defect[:200],
            )
            phase = 'worker'
        elif not replan_enabled():
            logger.info('[Verdict] Replan disabled — CONTINUE_PLANNER '
                        'downgraded to CONTINUE_WORKER (TOFU_ENDPOINT_REPLAN=0)')
            phase = 'worker'

    return {'phase': phase, 'plan_defect': plan_defect,
            'feedback': feedback, 'had_tag': had_tag}


# ══════════════════════════════════════════════════════════
#  Loop terminal-outcome classification
# ══════════════════════════════════════════════════════════

# Stop reasons that mean "the loop was CUT OFF, not genuinely finished" — the
# objective is NOT verified-complete.  Endpoint's max_iterations / max_replans /
# stuck, autopilot's budget_exhausted / stuck / no_progress (the last from the
# Part-2 diminishing-returns guard), plus the three MID-FLIGHT autopilot cutoffs
# below.
INCOMPLETE_STOP_REASONS = frozenset({
    'max_iterations',
    'max_replans',
    'replan_exhausted',
    'stuck',
    'budget_exhausted',
    'no_progress',
    # ── Autopilot runs cut short while still working ──
    # The VU had already produced another turn (or was mid-flight) when a human
    # took the conversation back, the task was aborted, or a newer task
    # superseded the run. The objective is UNVERIFIED in all three, so the fold
    # must render "stopped early — needs review" rather than a clean
    # conclusion. Registering them here is what stops a yield from LOOKING like
    # a successful finish.
    'yielded_to_human',
    'aborted_mid_vu',
    'superseded',
})


def is_incomplete_stop(reason: str) -> bool:
    """True when a loop terminated by hitting a SAFETY CAP, not by finishing.

    Callers surface this as an "unfinished / needs review" outcome instead of a
    clean done, so a runaway that burned its budget on a triviality is visibly
    flagged rather than silently reported as success.  Single source of truth
    for both loops' escalation contract.
    """
    return (reason or '') in INCOMPLETE_STOP_REASONS
