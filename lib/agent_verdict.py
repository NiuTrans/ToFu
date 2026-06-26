"""lib/agent_verdict.py — Shared verdict / loop-control heuristics.

Single source of truth for the small bundle of *decision logic* that the
agent loops share:

  * the set of "state-changing" (deliverable) tool names;
  * the autopilot virtual-user completion sentinel;
  * counting state-changing vs exploratory tool rounds in a worker turn;
  * parsing a critic / verifier verdict into a next-phase
    (``stop`` / ``worker`` / ``planner``) with the anti-analysis-spiral
    gating (STOP-with-unresolved-markers downgrade, CONTINUE_PLANNER
    requires a gated PLAN_DEFECT reason, replan kill-switch);
  * Jaccard "stuck" detection on consecutive verifier feedbacks;
  * usage-dict accumulation.

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
``lib.env_compat`` (the replan kill-switch).  No app/runtime coupling.
"""

from __future__ import annotations

import re

from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  State-changing ("deliverable") tools
# ══════════════════════════════════════════════════════════

# Calls to these tools are what we count as real work; everything else
# (list_dir, read_files, grep_search, find_files, web_search, fetch_url, …)
# is exploration.
#
# ``code_exec`` is deliberately NOT a member here: endpoint's round counter
# special-cases it (a code_exec round carries a different toolName), so the
# membership test must NOT match it.  Callers that count from a flat list of
# tool names — and therefore have no special-casing — should use
# :data:`STATE_CHANGING_TOOLS_WITH_CODE_EXEC` instead.
STATE_CHANGING_TOOLS = frozenset({
    'write_file',
    'apply_diff',
    'apply_diffs',
    'insert_content',
    'insert_contents',
    'run_command',
    'create_project',
    'generate_image',
})

# Same set plus ``code_exec`` — for callers (e.g. the orchestration engine's
# flat-tool-name snapshot) that do not special-case code_exec separately.
STATE_CHANGING_TOOLS_WITH_CODE_EXEC = STATE_CHANGING_TOOLS | {'code_exec'}


# ══════════════════════════════════════════════════════════
#  Autopilot virtual-user completion sentinel
# ══════════════════════════════════════════════════════════

# A virtual_user emits this verbatim when it judges the task finished.
# Used by autopilot's role prompt + done check, and by the engine's
# virtual_user verdict inversion.
VU_DONE_SENTINEL = '[VU: TASK_DONE]'


# ══════════════════════════════════════════════════════════
#  Replan kill-switch
# ══════════════════════════════════════════════════════════

def replan_enabled() -> bool:
    """Replan kill-switch: ``TOFU_ENDPOINT_REPLAN=0`` disables CONTINUE_PLANNER.

    When disabled, a ``planner`` phase is downgraded to ``worker`` so the
    redesign can be hot-disabled without a code rollback.  Defaults to
    enabled (``'1'``).  Documented in CLAUDE.md §9.
    """
    return getenv_compat('TOFU_ENDPOINT_REPLAN', default='1').strip() != '0'


# ══════════════════════════════════════════════════════════
#  State-changing tool round counter
# ══════════════════════════════════════════════════════════

def count_state_changing_rounds(tool_rounds) -> tuple:
    """Count state-changing vs exploratory tool rounds in a single worker turn.

    Parameters
    ----------
    tool_rounds : list[dict] | None
        ``task['toolRounds']`` snapshot — each entry has ``toolName``.

    Returns
    -------
    (int, int, list[str])
        ``(state_changing_count, exploratory_count, state_changing_tool_names)``.
        ``state_changing_tool_names`` preserves order + duplicates so the
        deliverables snapshot can show "apply_diff×2, write_file".

    ``code_exec`` rounds (whose ``toolName`` differs — see executor.py) are
    treated as state-changing.
    """
    if not tool_rounds:
        return 0, 0, []

    state_changing_names: list[str] = []
    exploratory_count = 0

    for entry in tool_rounds:
        if not isinstance(entry, dict):
            continue
        name = entry.get('toolName') or entry.get('tool_name') or ''
        if name == 'code_exec':
            state_changing_names.append('code_exec')
            continue
        if name in STATE_CHANGING_TOOLS:
            state_changing_names.append(name)
        else:
            exploratory_count += 1

    return len(state_changing_names), exploratory_count, state_changing_names


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
                audit_log(
                    'vu_done_override',
                    original='stop',
                    new='worker',
                    x_count=x_count,
                    phrase_hits=len(phrase_hits),
                    reason='unresolved_markers_in_vu_done',
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
            audit_log(
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
            audit_log(
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
            audit_log(
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
#  Stuck detection
# ══════════════════════════════════════════════════════════

STUCK_JACCARD = 0.60


def detect_stuck(feedback_history, *, threshold: float = STUCK_JACCARD) -> bool:
    """Return True if the last two feedback messages are suspiciously similar.

    Uses a simple Jaccard similarity on word sets — if overlap exceeds
    ``threshold`` (default 0.60), the verifier is probably repeating itself
    and the loop is not converging.
    """
    if not feedback_history or len(feedback_history) < 2:
        return False

    prev = set(feedback_history[-2].lower().split())
    curr = set(feedback_history[-1].lower().split())

    if not curr or not prev:
        return False

    union = prev | curr
    jaccard = len(prev & curr) / len(union) if union else 0
    return jaccard > threshold


# ══════════════════════════════════════════════════════════
#  Usage accumulation
# ══════════════════════════════════════════════════════════

def accumulate_usage(total, delta):
    """Merge ``delta`` usage dict into ``total`` (in-place)."""
    for k, v in (delta or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
