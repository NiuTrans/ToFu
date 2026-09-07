"""Layer 2 — summary prompt + input-formatting helpers.

Holds the cheap-model system prompt (``_SUMMARY_SYSTEM_PROMPT``) and the pure
helpers that shape the summary LLM's input:

  * ``_format_messages_for_summary`` — render user/assistant turns as text.
  * ``_summary_input_char_budget``   — model-window-aware char ceiling.
  * ``_build_summary_user_content``  — verbatim goal evidence + history.
  * ``_ensure_summary_objective``    — failure-floor Objective fallback.
  * ``_extract_summary_objective``   — parse the receipt's Objective body.
"""

import re

from lib.log import get_logger
from lib.tasks_pkg.compaction._constants import (
    _SUMMARY_MAX_TOKENS,
    summary_input_char_cap,
)
from lib.tasks_pkg.compaction._tokens import (
    _get_context_limit,
    _usable_context,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Summary prompt
# ═══════════════════════════════════════════════════════════════════════════════

_SUMMARY_SYSTEM_PROMPT = """\
You compress old agent history into a small, continuation-ready state receipt.
The receipt may serve coding, research, writing, or operational tasks. Preserve
only information that changes the next correct action.

<analysis>
Privately identify the current effective objective, the latest binding human
steering, binding constraints, verified work, unresolved failures, durable
evidence, and the exact next action. Distinguish human requests from [context]
carriers. Do not output this scratchpad.
</analysis>

Produce these sections, omitting empty bullets but not the headings:

### Objective
State the user's CURRENT EFFECTIVE objective — the goal that binds the next
action — in at most two sentences. Derive it from ALL user messages, which
reach you verbatim: an earlier request that was completed, abandoned, or
explicitly replaced by a later human message is history, not the objective.
A transient obstacle (login error, status question, UI fragment) or a short
correction is steering inside the objective, never the objective itself;
record those under Errors & Blockers or Pending / Next Steps.

### Binding Constraints & Decisions
Only still-binding user preferences, architecture choices, rejected options,
versions, budgets, or protocols that constrain future work.

### Completed & Verified
Concrete completed work and its verification. Pair important claims with a
resolvable evidence ID, artifactRef, source URL, test name, or durable path.

### Current Working State
Current files or artifacts that matter, what works, and what remains broken.
For code, list modified/current project files only. For research, retain
claim-to-source mappings and precise citations. Never list transport staging,
temporary tool-result, cache, or generated-bundle paths as working files.

### Errors & Blockers
Only unresolved errors or resolved failures whose cause changes the next step.
Keep exact short error text when needed; omit stack dumps.

### Pending / Next Steps
Ordered, executable continuation steps, including the immediate next action.

Rules:
- The Objective is your judgment of the latest binding human goal. Never copy
  the earliest request into the Objective mechanically, and never promote the
  newest message into the Objective when it is only an obstacle, status
  fragment, or short correction.
- Relevance to the current objective determines detail selection.
- Do not reproduce all user messages: they are retained verbatim outside this
  lossy receipt. Capture only binding consequences from them.
- [context] rows are engine/project context, not human requests. Mention them
  only when they impose a still-binding constraint.
- Prefer compact facts over chronology. Drop greetings, superseded attempts,
  generic concepts, raw tool output, and repeated conclusions.
- Include code snippets only when exact code is necessary to continue.
- Every recovery reference must be durable and resolvable; never emit local
  transport paths such as data/tool-results.
- Strip the <analysis> section from the final output.
- Output in the same language as the conversation.
- Target 800-1,600 tokens; use at most 2,200 tokens for complex state.
"""


_ELISION_MARKER = '\n\n... [middle of conversation elided for summary] ...\n\n'


def _build_summary_user_content(
    *,
    anchor_text: str,
    latest_user_message: str = '',
    formatted_history: str,
    formatted_ledger: str = '',
) -> str:
    """Build the summary model's input: verbatim goal evidence + history.

    Every real user message reaches the model VERBATIM — the elision policy
    never drops user turns; the earliest request (pulled out of the lossy
    region for verbatim re-insertion into the live context) is re-supplied
    here; and the newest message, which lives in the preserved region rather
    than the history, is shown for reference. The model authors the receipt's
    Objective itself from this evidence: no receipt section is pre-determined,
    so the Objective can track goal replacement across a long conversation.
    Callers use this same helper for dispatch and proactive token projection
    so the cost gate estimates the prompt that is actually sent.
    """
    anchor = (anchor_text or '').strip()
    latest = (latest_user_message or '').strip()
    history = (formatted_history or '').strip()

    sections = []
    if anchor:
        sections.append(
            '## Earliest User Request (verbatim)\n'
            'The opening human request, preserved verbatim. It may already be '
            'completed or explicitly replaced by a later message — treat it '
            'as evidence, not as the current objective by default.\n'
            f'{anchor}')
    if latest:
        sections.append(
            '## Latest User Message (verbatim — already preserved outside '
            'this receipt)\n'
            f'{latest}')
    sections.append(f'## Conversation History to Compress\n\n{history}')
    if formatted_ledger:
        sections.append(formatted_ledger.strip())
    return '\n\n'.join(sections)


_OBJECTIVE_SECTION_RE = re.compile(
    r'^### Objective[^\n]*\n.*?(?=^### [^\n]+\n|\Z)',
    flags=re.MULTILINE | re.DOTALL,
)


def _extract_summary_objective(summary_text: str) -> str:
    """Body text of the receipt's ``### Objective`` section.

    Returns '' when the section is absent or its body is blank — the two
    cases :func:`_ensure_summary_objective` treats as missing.
    """
    match = _OBJECTIVE_SECTION_RE.search(summary_text or '')
    if not match:
        return ''
    section = match.group(0)
    body = section.split('\n', 1)[1] if '\n' in section else ''
    return body.strip()


def _ensure_summary_objective(summary_text: str, *, anchor_text: str = '') -> str:
    """Guarantee the receipt carries a non-empty Objective section.

    The model authors the Objective itself from the verbatim user-message
    evidence — that is what lets the receipt track goal replacement across a
    long conversation. This is only the failure floor: when the model omitted
    the section or left it empty, fill it with the earliest-request anchor
    (the best available evidence of the goal). A model-authored Objective is
    NEVER overwritten.
    """
    summary = (summary_text or '').strip()
    if _extract_summary_objective(summary):
        return summary
    anchor = (anchor_text or '').strip()
    if not anchor:
        return summary
    section = f'### Objective\n{anchor}'
    match = _OBJECTIVE_SECTION_RE.search(summary)
    if match:
        # Replace by span, not through ``re.sub`` replacement semantics.
        # The anchor is untrusted verbatim text: plan envelopes contain
        # literal ``\uXXXX`` fragments and users paste paths/backreferences.
        # String slicing has no second parser that can reinterpret that data.
        return (
            summary[:match.start()]
            + section
            + '\n\n'
            + summary[match.end():]
        ).strip()
    return (section + ('\n\n' + summary if summary else '')).strip()


def _format_messages_for_summary(messages: list,
                                 char_budget: int | None = None) -> str:
    """Render messages as readable text for the summary LLM.

    INCLUDES user msgs and assistant msgs with non-empty natural-language
    content.  EXCLUDES tool messages and tool-call-only assistant
    messages — they don't help a relevance-rating cheap model.

    When ``char_budget`` is given and the full render would exceed it, the
    input is trimmed MESSAGE-AWARE rather than by a blind string slice:

      * EVERY real ``[user]`` part is kept VERBATIM. The live compaction path
        retains user text independently from this lossy receipt, and summary
        input shaping must likewise never silently remove an instruction.
      * Only ASSISTANT parts are elided, from the MIDDLE outward (keep the
        earliest goals + the most recent working state), until the total fits.
      * A single ``_ELISION_MARKER`` records where assistant content was
        dropped. If the user parts alone exceed the budget they are STILL all
        kept (correctness over budget — never silently drop an instruction).

    ``char_budget=None`` (the default / legacy call) renders everything with no
    elision, byte-identical to the pre-budget behaviour.
    """
    parts: list[tuple[str, str]] = []   # (role, rendered_part)
    skipped_tool = 0
    skipped_tool_only_assistant = 0

    for msg in messages:
        role = msg.get('role', '?')

        if role == 'tool' or role == 'system':
            if role == 'tool':
                skipped_tool += 1
            continue

        content = msg.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(
                b.get('text', '') for b in content
                if isinstance(b, dict) and b.get('type') == 'text'
            )
        if not isinstance(content, str):
            content = ''
        text = content.strip()

        if role == 'assistant':
            if not text:
                skipped_tool_only_assistant += 1
                continue

        if not text:
            continue

        if len(text) > 3000:
            text = text[:1500] + '\n...[truncated]...\n' + text[-1000:]

        rendered_role = 'context' if msg.get('_isMeta') else role
        parts.append((rendered_role, f'[{rendered_role}] {text}'))

    if skipped_tool or skipped_tool_only_assistant:
        logger.debug(
            '[Compact] Relevance-format filter: skipped %d tool results, '
            '%d tool-call-only assistant msgs; kept %d user/assistant turns',
            skipped_tool, skipped_tool_only_assistant, len(parts),
        )

    rendered = [p for _, p in parts]
    if char_budget is None:
        return '\n\n'.join(rendered)

    joined = '\n\n'.join(rendered)
    if len(joined) <= char_budget:
        return joined

    return _elide_to_budget(parts, char_budget)


def _elide_to_budget(parts: list[tuple[str, str]], char_budget: int) -> str:
    """Trim ``parts`` to ``char_budget`` by eliding MIDDLE assistant content only.

    ``parts`` is the ordered ``(role, rendered)`` list from
    :func:`_format_messages_for_summary`.  Every ``user`` part is always kept;
    assistant parts are dropped from the middle outward (nearest the centre
    first) so the earliest goals and the most recent working state both
    survive.  A single :data:`_ELISION_MARKER` marks the elision.  If the user
    parts alone still exceed the budget, they are ALL kept regardless (never
    drop a user instruction).
    """
    sep = '\n\n'
    keep = [True] * len(parts)
    asst_idx = [i for i, (role, _) in enumerate(parts) if role != 'user']

    def _rendered_size() -> int:
        """Exact size of the reassembled output, including EVERY marker run —
        so the greedy loop never under-estimates (multiple dropped runs each
        emit their own marker)."""
        out: list[str] = []
        prev_dropped = False
        for i, (_, p) in enumerate(parts):
            if keep[i]:
                out.append(p)
                prev_dropped = False
            elif not prev_dropped:
                out.append(_ELISION_MARKER.strip())
                prev_dropped = True
        return len(sep.join(out)) if out else 0

    # Drop assistant parts nearest the CENTRE first, working outward, so the
    # head (early goals) and tail (recent working state) are the last to go.
    # Real user parts are never in ``asst_idx`` → always kept. Synthetic
    # [context] carriers may be elided like assistant prose because the context
    # composer re-injects their authoritative state after compaction.
    mid = len(parts) / 2.0
    for i in sorted(asst_idx, key=lambda i: abs(i - mid)):
        if _rendered_size() <= char_budget:
            break
        keep[i] = False

    # Reassemble, collapsing every maximal run of dropped parts into one marker.
    out: list[str] = []
    prev_dropped = False
    for i, (_, p) in enumerate(parts):
        if keep[i]:
            out.append(p)
            prev_dropped = False
        elif not prev_dropped:
            out.append(_ELISION_MARKER.strip())
            prev_dropped = True
    return sep.join(out)


def _summary_input_char_budget(task: dict | None) -> int:
    """Char ceiling for the summary LLM's INPUT, sized to the model window.

    The old fixed 200k-char cap was model-agnostic and token-blind: on a
    small-window model (e.g. 128k qwen/gpt-4) 200k chars of dense or CJK
    text is ~130k–200k tokens (the heuristic counts 1 token/CJK char) —
    well OVER the window once the ~1.5k system prompt + `_SUMMARY_MAX_TOKENS`
    output reserve are added. The summary call then fails ("prompt too
    long" / dispatch exhausted), which was the root of the proactive-
    compaction dead-end. Size the input to what the model can actually take:
    ``usable - output_reserve`` tokens, converted to chars with a
    conservative ~3 chars/token, and clamp to the historical 200k so large
    windows behave as before.
    """
    try:
        usable = _usable_context(_get_context_limit(task))
    except Exception as e:
        logger.debug('[Compact] usable-context lookup failed, using 96k '
                     'fallback: %s', e)
        usable = 96_000
    input_token_budget = max(4_000, usable - _SUMMARY_MAX_TOKENS - 2_000)
    # Convert token budget → char budget at ~1 char/token. This is the
    # CJK-worst-case ratio (the entropy heuristic counts ~1 token per CJK
    # char), so the char cap is SAFE for Chinese/Japanese input — the exact
    # case that overflowed a 128k window in production (est_input≈122k on a
    # 200k-char summary). For latin-heavy text it trims a bit more than
    # strictly necessary, but the summary is still produced.
    #
    # §10.1 CEILING (owner sign-off 2026-07-18): clamped to _SUMMARY_INPUT_CHAR_CAP
    # (64k), down from the old 200k. The 200k cap was ~3× redundant: a manual
    # /compact's entire wall clock is the single cheap-model summary call
    # (measured ~96% of a 3 MB conv's time), and feeding it up to 200k chars is
    # what made the button slow. 64k still yields a faithful structured
    # working-state receipt while roughly a third of the prompt → a proportionally
    # faster call. On small windows ``usable`` still binds first (unchanged);
    # the cap only bites on large (>=~200k) windows. Elision beyond the cap is
    # MESSAGE-AWARE (see _format_messages_for_summary): every user message is
    # kept, only middle assistant content is dropped.
    return max(20_000, min(summary_input_char_cap(), input_token_budget))
