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


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity of two strings' lowercased word sets (0.0 when
    either side is empty)."""
    sa = set((a or '').lower().split())
    sb = set((b or '').lower().split())
    if not sa or not sb:
        return 0.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def detect_stuck(feedback_history, *, threshold: float = STUCK_JACCARD,
                 window: int = 2) -> bool:
    """Return True when the loop is repeating itself and not converging.

    Compares the last ``window`` entries of ``feedback_history`` pairwise
    (consecutive) on Jaccard word-set similarity; returns True iff EVERY
    adjacent pair in that window exceeds ``threshold`` — i.e. the verifier
    emitted ``window`` near-identical messages in a row.

    ``window`` defaults to 2, which is BYTE-IDENTICAL to the original
    behaviour (compare the last two feedbacks, True iff their overlap exceeds
    ``threshold``) — endpoint keeps calling it with the default so its
    semantics are unchanged.  Autopilot passes ``window=3`` (see
    :data:`AUTOPILOT_STUCK_WINDOW`): two near-identical VU nudges can be a
    legitimate "you didn't do it, try again", but three in a row is a genuine
    non-converging loop.
    """
    if window < 2:
        window = 2
    if not feedback_history or len(feedback_history) < window:
        return False
    tail = feedback_history[-window:]
    for i in range(len(tail) - 1):
        if _jaccard(tail[i], tail[i + 1]) <= threshold:
            return False
    return True


# ══════════════════════════════════════════════════════════
#  Usage accumulation
# ══════════════════════════════════════════════════════════

def accumulate_usage(total, delta):
    """Merge ``delta`` usage dict into ``total`` (in-place)."""
    for k, v in (delta or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v


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

# Max concluded-run records retained in ``settings.autopilotSummaries`` on a
# single long-lived conversation.  The map ACCRETES one record per run (each
# carrying a full close-out report) and is re-serialized into every settings
# PUT + IndexedDB write, so an unbounded map makes every turn's write cost grow
# O(n) on a year-scale conversation.  Cap it to the most-recent N by ``ts``.
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


# ══════════════════════════════════════════════════════════
#  Loop terminal-outcome classification
# ══════════════════════════════════════════════════════════

# Stop reasons that mean "the loop was CUT OFF by a safety cap, not genuinely
# finished" — the objective is NOT verified-complete.  Endpoint's
# max_iterations / max_replans / stuck, and autopilot's budget_exhausted /
# stuck / no_progress (the last from the Part-2 diminishing-returns guard).
INCOMPLETE_STOP_REASONS = frozenset({
    'max_iterations',
    'max_replans',
    'stuck',
    'budget_exhausted',
    'no_progress',
})


def is_incomplete_stop(reason: str) -> bool:
    """True when a loop terminated by hitting a SAFETY CAP, not by finishing.

    Callers surface this as an "unfinished / needs review" outcome instead of a
    clean done, so a runaway that burned its budget on a triviality is visibly
    flagged rather than silently reported as success.  Single source of truth
    for both loops' escalation contract.
    """
    return (reason or '') in INCOMPLETE_STOP_REASONS


# ══════════════════════════════════════════════════════════
#  Diminishing-returns / no-value-progress guard
# ══════════════════════════════════════════════════════════
#
#  WHY this exists — repetition (Jaccard) detection is NECESSARY BUT NOT
#  SUFFICIENT.  A verifier that keeps flagging a genuine-but-tiny item emits
#  NON-similar feedback each turn (so ``detect_stuck`` never fires) while the
#  worker ships a REAL edit each turn (so the zero-deliverable guard never
#  fires) — yet the loop makes no net progress toward the objective, burning
#  its whole budget polishing a triviality or tuning the same parameter.  This
#  guard catches exactly that: state-changing work that RE-TOUCHES the same
#  targets without resolving NEW objective items.
#
#  The signal is HARD, not prose-heuristic: the virtual user emits a structured
#  ``[PROGRESS: resolved=X remaining=Y]`` line each turn (X = acceptance-criteria
#  items verified DONE so far).  The per-turn ``resolved_delta`` (new items this
#  turn) + the worker's touched-file set feed a small ledger; the guard fires
#  only when the whole window carries a hard progress signal AND shows churn
#  without net progress.  Absent the structured signal it FAILS OPEN (cannot
#  prove no-progress → never fires).

DIMINISHING_WINDOW = 4          # consecutive edit-shipping turns to consider
DIMINISHING_MIN_RESOLVED = 1    # net NEW resolved items below this = no progress
DIMINISHING_TARGET_OVERLAP = 0.5  # per-pair touched-file Jaccard = "same spot"

_PROGRESS_RE = re.compile(
    r'\[PROGRESS:\s*resolved\s*=\s*(\d+)\s*(?:,|;|\s)\s*remaining\s*=\s*(\d+)\s*\]',
    re.IGNORECASE,
)


def parse_progress(text: str):
    """Extract the structured ``[PROGRESS: resolved=X remaining=Y]`` line.

    Returns ``(resolved, remaining)`` as ints, or ``(None, None)`` when no
    parseable line is present (the guard then fails open — it cannot conclude
    no-progress without the hard signal).  Uses the LAST match if the VU
    emitted more than one.
    """
    last = None
    for m in _PROGRESS_RE.finditer(text or ''):
        last = m
    if last is None:
        return None, None
    try:
        return int(last.group(1)), int(last.group(2))
    except (ValueError, TypeError) as e:
        logger.debug('[Verdict] parse_progress: non-int PROGRESS values (%s) — '
                     'failing open', e)
        return None, None


def autopilot_progress_window() -> int:
    """Diminishing-returns window (``TOFU_AUTOPILOT_PROGRESS_WINDOW``, default 4).

    FAIL-OPEN like :func:`autopilot_max_turns`: unset→default, ``0``/<=1→
    DISABLED (the guard never fires), garbage→default.  A window < 2 is
    meaningless (need at least two turns to see churn) so it disables.
    """
    raw = getenv_compat('TOFU_AUTOPILOT_PROGRESS_WINDOW', default='').strip()
    if not raw:
        return DIMINISHING_WINDOW
    try:
        val = int(raw)
    except (ValueError, TypeError):
        logger.warning('[Autopilot] TOFU_AUTOPILOT_PROGRESS_WINDOW=%r not an int '
                       '— using default %d', raw, DIMINISHING_WINDOW)
        return DIMINISHING_WINDOW
    return val if val >= 2 else 0


def detect_diminishing_returns(ledger, *, window: int = DIMINISHING_WINDOW,
                               min_resolved: int = DIMINISHING_MIN_RESOLVED,
                               overlap: float = DIMINISHING_TARGET_OVERLAP) -> bool:
    """True when the last ``window`` turns churn without net objective progress.

    ``ledger`` is a list of per-turn dicts (oldest→newest), each:
        ``{'resolved_delta': int | None, 'targets': list[str]}``
    where ``resolved_delta`` is the number of NEW acceptance-criteria items the
    VU verified done that turn (``None`` when the turn had no parseable
    ``[PROGRESS]`` line), and ``targets`` is the set of files the worker touched
    that turn.

    Fires (returns True) iff ALL hold over the last ``window`` entries:
      (a) every turn shipped edits (non-empty ``targets``) — this is churn, not
          legitimate read-only investigation;
      (b) every turn carries a hard progress signal (no ``None`` delta) — else
          FAIL OPEN, we cannot prove no-progress;
      (c) the NET new resolved items across the window is ``< min_resolved``
          (essentially zero forward progress); AND
      (d) every consecutive turn re-touched a substantially overlapping target
          set (per-pair Jaccard ``>= overlap``) — re-editing the same spot.

    ``window <= 1`` disables the guard (returns False) — used by the env
    kill-switch.
    """
    if window <= 1:
        return False
    if not ledger or len(ledger) < window:
        return False
    tail = ledger[-window:]

    target_sets = []
    for e in tail:
        tg = (e or {}).get('targets') or []
        if not tg:
            return False  # (a) a read-only / no-edit turn breaks the churn run
        target_sets.append(set(tg))

    deltas = [(e or {}).get('resolved_delta') for e in tail]
    if any(d is None for d in deltas):
        return False  # (b) missing hard signal → fail open

    if sum(deltas) >= min_resolved:
        return False  # (c) real net progress → not stuck

    for i in range(len(target_sets) - 1):
        a, b = target_sets[i], target_sets[i + 1]
        union = a | b
        j = (len(a & b) / len(union)) if union else 0.0
        if j < overlap:
            return False  # (d) turns touched different areas → not fixation
    return True
