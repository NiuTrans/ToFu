"""Planner, critic turn, and helper functions for the endpoint loop.

Three roles:
  1. Planner  — runs once at start, rewrites user goal into structured brief
  2. Worker   — full LLM + tools, executes the plan (handled by orchestrator)
  3. Critic   — full LLM + tools, verifies against the planner's checklist

Split out of endpoint.py for readability.  All symbols are re-imported
by the main ``endpoint`` module so external callers are unaffected.
"""

import re
import time

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.endpoint_prompts import CRITIC_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT
from lib.tasks_pkg.orchestrator import _run_single_turn

# ══════════════════════════════════════════════════════════
#  Planner turn
# ══════════════════════════════════════════════════════════

def _run_planner_turn(task, messages):
    """Execute the planner: rewrite the user's request into a structured brief.

    The planner sees:
    - A system prompt instructing it to plan (not execute)
    - The full conversation history (user messages + any prior assistant replies)

    It produces a structured brief with Goal, Checklist, and Acceptance
    Criteria that replaces the user's raw request in subsequent turns.

    Parameters
    ----------
    task : dict
        The live task dict.
    messages : list
        The original message list from the conversation.

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

    # Build planner messages: planner system prompt + full conversation
    planner_messages = []

    # 1. System prompt: planner role
    planner_messages.append({
        'role': 'system',
        'content': PLANNER_SYSTEM_PROMPT,
    })

    # 2. Include all messages from the conversation.
    #    Skip the original system message (replaced above) but include
    #    all user/assistant context so the planner understands the full picture.
    for msg in messages:
        if msg.get('role') == 'system':
            # Include the original system prompt as context
            original_sys = msg.get('content', '')
            if original_sys and isinstance(original_sys, str) and len(original_sys.strip()) > 20:
                planner_messages.append({
                    'role': 'user',
                    'content': (
                        f'[Context: The worker will receive this system prompt]\n\n'
                        f'{original_sys}'
                    ),
                })
                planner_messages.append({
                    'role': 'assistant',
                    'content': 'Understood. I will incorporate this context into my plan.',
                })
            continue
        planner_messages.append(dict(msg))

    # 3. Final instruction to produce the plan
    planner_messages.append({
        'role': 'user',
        'content': (
            'Based on the conversation above, produce your structured execution '
            'brief for the AI worker. Follow the exact output format specified '
            'in your system prompt. Remember: plan, don\'t execute.'
        ),
    })

    logger.info('[Planner] Starting planner turn for task %s, %d messages',
                tid, len(planner_messages))

    # ★ Full tool access for the planner.
    #   The planner gets the same tools as the worker so it can explore
    #   the project (list_dir, read_files, grep_search, etc.) and produce
    #   a well-informed plan grounded in actual code.  Context injection
    #   (CLAUDE.md, file tree, skills) also applies via _inject_system_contexts.
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

_VERDICT_RE = re.compile(
    r'\[VERDICT:\s*(STOP|CONTINUE)\s*\]',
    re.IGNORECASE,
)


def _parse_verdict(text: str) -> tuple:
    """Parse the critic's output into (feedback_text, should_stop).

    Returns
    -------
    (str, bool)
        feedback_text — the critic's natural-language content with the
                        verdict tag stripped out.
        should_stop   — True if STOP, False if CONTINUE.
                        Defaults to False if no tag found.
    """
    match = None
    # Find the LAST match (in case the critic accidentally emits more than one)
    for m in _VERDICT_RE.finditer(text):
        match = m

    if match is None:
        logger.warning('[Critic] No [VERDICT] tag found in critic output (%d chars), '
                       'defaulting to CONTINUE', len(text))
        return text.strip(), False

    should_stop = match.group(1).upper() == 'STOP'
    # Strip the verdict tag from the content
    feedback = text[:match.start()].rstrip()
    # Also strip any trailing content after the tag (shouldn't exist, but be safe)
    return feedback, should_stop


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
        feedback : str   — natural-language critique (verdict tag stripped)
        should_stop : bool — True = approved, False = needs more work
        content : str    — raw full content from the critic (before stripping)
        thinking : str   — critic's thinking (if extended thinking enabled)
        usage : dict     — token usage
        error : str|None — error message if the turn failed
    """
    tid = task['id'][:8]

    # Build critic messages: swap in the critic system prompt, keep the
    # full conversation history so the critic has full context.
    critic_messages = []

    # 1. System prompt: critic role
    critic_messages.append({
        'role': 'system',
        'content': CRITIC_SYSTEM_PROMPT,
    })

    # 2. Include all user/assistant messages from the conversation.
    #    Skip the original system message (we replaced it above).
    for msg in worker_messages:
        if msg.get('role') == 'system':
            # Append the original system content as context so the critic
            # knows what instructions the worker was given
            original_sys = msg.get('content', '')
            if original_sys and isinstance(original_sys, str) and len(original_sys.strip()) > 20:
                critic_messages.append({
                    'role': 'user',
                    'content': (
                        f'[Context: The worker was given this system prompt]\n\n'
                        f'{original_sys}'
                    ),
                })
                critic_messages.append({
                    'role': 'assistant',
                    'content': 'Understood. I will review the worker\'s output with this context in mind.',
                })
            continue
        critic_messages.append(dict(msg))

    # 3. Final user message: explicit instruction to review
    critic_messages.append({
        'role': 'user',
        'content': (
            'Please review the worker\'s latest response against the Planner\'s '
            'checklist and acceptance criteria (in the first user message above). '
            'Verify each checklist item using tools if needed, then provide your '
            'structured critique. End with [VERDICT: STOP] or [VERDICT: CONTINUE].'
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
            'should_stop': False,
            'content': raw_content,
            'thinking': result.get('thinking', ''),
            'usage': result.get('usage', {}),
            'error': error,
        }

    feedback, should_stop = _parse_verdict(raw_content)

    logger.info('[Critic] Task %s — verdict=%s, feedback=%d chars',
                tid, 'STOP' if should_stop else 'CONTINUE', len(feedback))

    return {
        'feedback': feedback,
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
