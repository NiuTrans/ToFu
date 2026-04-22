"""Planner, critic turn, and helper functions for the endpoint loop.

Three roles:
  1. Planner  — runs once at start, rewrites user goal into structured brief
  2. Worker   — full LLM + tools, executes the plan (handled by orchestrator)
  3. Critic   — full LLM + tools, verifies against the planner's checklist

Split out of endpoint.py for readability.  All symbols are re-imported
by the main ``endpoint`` module so external callers are unaffected.
"""

import os
import re

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_prompts import CRITIC_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT
from lib.tasks_pkg.orchestrator import _run_single_turn


# Kill-switch: when '0', the three-way verdict (CONTINUE_PLANNER) is
# downgraded to CONTINUE_WORKER so the redesign can be hot-disabled
# without a code rollback.  Defaults to enabled ('1').
def _replan_enabled() -> bool:
    return os.environ.get('CHATUI_ENDPOINT_REPLAN', '1').strip() != '0'

# ══════════════════════════════════════════════════════════
#  Planner turn
# ══════════════════════════════════════════════════════════

def _run_planner_turn(task, messages):
    """Execute the planner: rewrite the user's request into a structured brief.

    **Prefix-cache-friendly construction**.  The original ``system`` message
    and all prior conversation turns are passed to the planner EXACTLY
    AS-IS so the LLM provider's KV / prefix cache stays hot across planner,
    worker, and critic calls within the same task.  The only delta between
    the original conversation and what the planner sees is the **content of
    the last user message**, which is wrapped with the planner role
    description + "produce a plan" directive.  No extra ``system`` message
    is prepended and no fake ``assistant`` turn is injected.

    Returns
    -------
    dict with keys:
        content : str   — the planner's structured brief
        thinking : str  — planner's thinking (if extended thinking enabled)
        usage : dict    — token usage
        messages : list — the message list after the planner turn
        error : str|None — error message if the turn failed
    """
    tid = task['id'][:8]

    # Copy the conversation verbatim — same objects in the same order so
    # that [system, ...history] forms an identical prefix across calls.
    planner_messages = [dict(m) for m in messages]

    # Locate the last user message (= the current turn's request) and
    # wrap ONLY its content with the planner role + directive.  The full
    # PLANNER_SYSTEM_PROMPT is folded into the wrapper so we don't need
    # to mutate the real ``system`` message (which would bust the cache).
    wrapped = False
    for i in range(len(planner_messages) - 1, -1, -1):
        if planner_messages[i].get('role') == 'user':
            original_content = planner_messages[i].get('content', '') or ''
            planner_messages[i] = {
                'role': 'user',
                'content': (
                    '=== Your role for THIS turn: Planner ===\n'
                    f'{PLANNER_SYSTEM_PROMPT}\n'
                    '=== End planner role ===\n\n'
                    'Based on the system prompt and conversation history above, '
                    'and the user request below, produce your structured execution '
                    'brief for the worker per the format in your planner role.  '
                    'You MAY use read-only tools (list_dir, read_files, grep_search) '
                    'to explore the codebase, but DO NOT edit files or execute the '
                    'task itself — planning only.\n\n'
                    '───── User request ─────\n\n'
                    + original_content
                ),
            }
            wrapped = True
            break

    if not wrapped:
        # Edge case: no user message in the conversation yet.
        logger.warning('[Planner] No user message found for task %s; appending a synthetic one', tid)
        planner_messages.append({
            'role': 'user',
            'content': (
                '=== Your role for THIS turn: Planner ===\n'
                f'{PLANNER_SYSTEM_PROMPT}\n'
                '=== End planner role ===\n\n'
                'Produce a structured execution brief for the conversation above.'
            ),
        })

    logger.info('[Planner] Starting planner turn for task %s, %d messages (prefix-cache friendly)',
                tid, len(planner_messages))

    # ★ Full tool access for the planner.
    #   The planner gets the same tools as the worker so it can explore
    #   the project (list_dir, read_files, grep_search, etc.) and produce
    #   a well-informed plan grounded in actual code.  Context injection
    #   (CLAUDE.md, file tree, memory) also applies via _inject_system_contexts.
    result = _run_single_turn(task, messages_override=planner_messages)

    content = result.get('content', '')
    error = result.get('error')

    if error:
        logger.warning('[Planner] Planner turn error for %s: %s', tid, error)

    logger.info('[Planner] Task %s — plan=%d chars',
                tid, len(content))

    return {
        'content': content,
        'thinking': result.get('thinking', ''),
        'usage': result.get('usage', {}),
        'messages': result.get('messages', planner_messages),
        'error': error,
    }


# ══════════════════════════════════════════════════════════
#  Verdict parsing
# ══════════════════════════════════════════════════════════

# Match all three modern tags plus the legacy bare "CONTINUE" (maps to
# CONTINUE_WORKER).  Group 1 captures the full tag body.
_VERDICT_RE = re.compile(
    r'\[VERDICT:\s*(STOP|CONTINUE_WORKER|CONTINUE_PLANNER|CONTINUE)\s*\]',
    re.IGNORECASE,
)

# Patterns that indicate the Critic emitted STOP while the feedback still
# contains unresolved items.  Used by the defense-in-depth override.
_UNRESOLVED_EMOJI_RE = re.compile(r'❌')
_UNRESOLVED_PHRASE_RE = re.compile(
    r'\b(?:NOT met|still failing|still NOT met|unresolved)\b',
    re.IGNORECASE,
)


def _parse_verdict(text: str) -> tuple:
    """Parse the critic's output into (feedback_text, next_phase).

    ``next_phase`` is one of:
      * ``'stop'``    — Critic approved, terminate loop.
      * ``'worker'``  — Hand back to Worker with injected feedback
                        (legacy ``CONTINUE`` also maps here).
      * ``'planner'`` — Hand back to Planner for a full re-plan.

    Defense-in-depth: if the Critic emits STOP but the feedback body still
    contains ``❌`` / "NOT met" / "still failing" / "unresolved", the
    verdict is overridden to ``'planner'`` and a warning + audit trail is
    logged.  This blocks the rationalized-STOP-with-unresolved-checklist
    failure mode seen in conversation ``mo7yf56tewxhk7``.

    Defaults to ``'worker'`` if no tag is found.

    Returns
    -------
    (str, str)
        feedback_text — the critic's natural-language content with the
                        verdict tag + any trailing "### Verdict" header
                        stripped.
        next_phase    — one of 'stop', 'worker', 'planner'.
    """
    match = None
    # Find the LAST match (in case the critic accidentally emits more than one)
    for m in _VERDICT_RE.finditer(text):
        match = m

    if match is None:
        logger.warning('[Critic] No [VERDICT] tag found in critic output (%d chars), '
                       'defaulting to CONTINUE_WORKER', len(text))
        return text.strip(), 'worker'

    tag = match.group(1).upper()
    if tag == 'STOP':
        next_phase = 'stop'
    elif tag == 'CONTINUE_PLANNER':
        next_phase = 'planner'
    else:
        # CONTINUE_WORKER or legacy bare CONTINUE
        next_phase = 'worker'

    # Strip the verdict tag from the content
    feedback = text[:match.start()].rstrip()
    # Also strip any dangling "### Verdict" markdown header that prompted
    # the tag — otherwise the frontend shows an empty "### Verdict" section
    # at the end of the critic bubble and the next worker turn sees the
    # header as a conditioning cue to emit its own [VERDICT:] tag.
    feedback = re.sub(
        r'\n*#+\s*Verdict\s*:?\s*$',
        '',
        feedback,
        flags=re.IGNORECASE,
    ).rstrip()

    # ── Defense-in-depth guard: STOP with unresolved markers → replan ──
    # Only kicks in when replan is enabled; otherwise fall back to the
    # old "STOP is STOP" behavior (the LLM's rationalization wins, but
    # at least logs are noisy enough for the user to notice).
    if next_phase == 'stop':
        x_count = len(_UNRESOLVED_EMOJI_RE.findall(feedback))
        phrase_hits = _UNRESOLVED_PHRASE_RE.findall(feedback)
        if x_count > 0 or phrase_hits:
            if _replan_enabled():
                logger.warning(
                    '[Critic] Override STOP→CONTINUE_PLANNER: feedback still '
                    'contains %d ❌ markers and %d unresolved phrases',
                    x_count, len(phrase_hits),
                )
                audit_log(
                    'critic_verdict_override',
                    original='stop',
                    new='planner',
                    x_count=x_count,
                    phrase_hits=len(phrase_hits),
                    reason='unresolved_markers_in_stop_feedback',
                )
                next_phase = 'planner'
            else:
                logger.warning(
                    '[Critic] Would override STOP→CONTINUE_PLANNER (%d ❌, '
                    '%d phrases) but CHATUI_ENDPOINT_REPLAN=0 — leaving as STOP',
                    x_count, len(phrase_hits),
                )

    # ── Kill-switch: downgrade planner→worker when replan disabled ──
    if next_phase == 'planner' and not _replan_enabled():
        logger.info('[Critic] Replan disabled — CONTINUE_PLANNER downgraded to '
                    'CONTINUE_WORKER (CHATUI_ENDPOINT_REPLAN=0)')
        next_phase = 'worker'

    return feedback, next_phase


# ══════════════════════════════════════════════════════════
#  Run critic turn
# ══════════════════════════════════════════════════════════

def _run_critic_turn(task, original_messages, worker_messages):
    """Execute one full critic turn using the same LLM + tools as the worker.

    The critic sees:
    - A system prompt instructing it to review against the planner's checklist
    - The full conversation history (planner brief + all worker turns + feedback)

    It runs through ``_run_single_turn`` so it gets the same model, tools,
    thinking depth, etc. as the worker.

    Parameters
    ----------
    task : dict
        The live task dict (must be in ``tasks``).
    original_messages : list
        The original message list snapshot (for extracting the user's goal).
    worker_messages : list
        The current full conversation history after the worker's latest turn.
        This includes system prompt, planner brief, all assistant replies,
        and any previous critic feedback injected as user messages.

    Returns
    -------
    dict with keys:
        feedback : str     — natural-language critique (verdict tag stripped)
        next_phase : str   — one of 'stop', 'worker', 'planner'
        should_stop : bool — mirror of (next_phase == 'stop') for backward compat
        content : str      — raw full content from the critic (before stripping)
        thinking : str     — critic's thinking (if extended thinking enabled)
        usage : dict       — token usage
        error : str|None   — error message if the turn failed
    """
    tid = task['id'][:8]

    # **Prefix-cache-friendly construction**.  The critic sees the entire
    # worker conversation byte-for-byte identical (same ``system``, same
    # planner-directive user, same worker assistant turns, same prior
    # critic-feedback user turns).  The only delta is ONE freshly appended
    # user message at the end that (a) declares "your role for this turn
    # is Critic" by embedding CRITIC_SYSTEM_PROMPT, and (b) asks for the
    # review.  The caller discards this ephemeral user turn after parsing
    # the verdict — it never leaks back into worker_messages.
    critic_messages = [dict(m) for m in worker_messages]

    critic_messages.append({
        'role': 'user',
        'content': (
            '=== Your role for THIS turn: Critic ===\n'
            f'{CRITIC_SYSTEM_PROMPT}\n'
            '=== End critic role ===\n\n'
            'Please review the worker\'s latest response (the assistant turn '
            'immediately above) against the Planner\'s checklist and '
            'acceptance criteria (the wrapped user message earlier in this '
            'conversation).  Verify each checklist item using tools if '
            'needed, then provide your structured critique per the format '
            'in your critic role.\n\n'
            'If the worker asked any clarifying questions or presented '
            'options (e.g. short-term workaround vs. long-term fix, file A '
            'vs. file B, "should I also do X?"), you MUST answer them in '
            'the "Answers to Worker Questions" section — speak as the user '
            'would.  Apply the standing preferences from your critic role '
            '(prefer the robust long-term solution over short-term patches, '
            'etc.) unless the Planner\'s brief explicitly overrides them.\n\n'
            'End with exactly one of:\n'
            '  [VERDICT: STOP]             — all checklist items ✅ and all '
            'acceptance criteria met.\n'
            '  [VERDICT: CONTINUE_WORKER]  — some items still ❌, but they '
            'fit the current plan; worker just needs more iterations.\n'
            '  [VERDICT: CONTINUE_PLANNER] — the plan itself is wrong / '
            'out-of-scope / impossible under the current approach; request '
            'a full re-plan instead of grinding the worker.'
        ),
    })

    logger.debug('[Critic] Starting critic turn for task %s, %d messages',
                 tid, len(critic_messages))

    # Run through _run_single_turn — full tools, full thinking
    result = _run_single_turn(task, messages_override=critic_messages)

    raw_content = result.get('content', '')
    error = result.get('error')

    if error:
        logger.warning('[Critic] Critic turn error for %s: %s', tid, error)
        return {
            'feedback': f'Critic encountered an error: {error}',
            'next_phase': 'worker',
            'should_stop': False,
            'content': raw_content,
            'thinking': result.get('thinking', ''),
            'usage': result.get('usage', {}),
            'error': error,
        }

    feedback, next_phase = _parse_verdict(raw_content)
    should_stop = (next_phase == 'stop')

    verdict_label = {
        'stop': 'STOP',
        'worker': 'CONTINUE_WORKER',
        'planner': 'CONTINUE_PLANNER',
    }.get(next_phase, next_phase.upper())
    logger.info('[Critic] Task %s — verdict=%s, feedback=%d chars',
                tid, verdict_label, len(feedback))

    return {
        'feedback': feedback,
        'next_phase': next_phase,
        'should_stop': should_stop,
        'content': raw_content,
        'thinking': result.get('thinking', ''),
        'usage': result.get('usage', {}),
        'error': None,
    }


# ══════════════════════════════════════════════════════════
#  Stuck detection
# ══════════════════════════════════════════════════════════

def _detect_stuck(feedback_history):
    """Return True if the last two feedback messages are suspiciously similar.

    Uses a simple Jaccard similarity on word sets — if >60% overlap, the
    critic is probably repeating itself.
    """
    if len(feedback_history) < 2:
        return False

    def _word_set(text):
        return set(text.lower().split())

    prev = _word_set(feedback_history[-2])
    curr = _word_set(feedback_history[-1])

    if not curr or not prev:
        return False

    intersection = prev & curr
    union = prev | curr
    jaccard = len(intersection) / len(union) if union else 0

    return jaccard > 0.60


# ══════════════════════════════════════════════════════════
#  Usage accumulation
# ══════════════════════════════════════════════════════════

def _accumulate_usage(total, delta):
    """Merge delta usage dict into total (in-place)."""
    for k, v in (delta or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
