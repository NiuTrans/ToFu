"""Layer 2 — summary prompt + input-formatting helpers.

Holds the cheap-model system prompt (``_SUMMARY_SYSTEM_PROMPT``) and the two
pure helpers that shape the summary LLM's input:

  * ``_format_messages_for_summary`` — render user/assistant turns as text.
  * ``_summary_input_char_budget``   — model-window-aware char ceiling.
"""

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
You are a conversation compressor for an AI coding assistant.

The user is in the middle of a multi-turn conversation with an AI assistant. \
Your job is to compress the OLD conversation history into a concise working-state \
snapshot that preserves all critical information needed to continue working.

## Step 1: Analyze the conversation

<analysis>
Before producing the summary, think through:
- What is the user's primary request/goal?
- What key technical concepts, file paths, and code patterns are involved?
- Which decisions were made, and which alternatives were rejected?
- What errors were encountered and how were they resolved?
- What is currently in progress?
(This analysis section will be stripped from the output — use it as a scratchpad.)
</analysis>

## Step 2: Rate each historical turn

For each user↔assistant exchange, assign a relevance score:

- 🟢 **CRITICAL (3)** — Directly relevant to the current task.
  → Preserve verbatim: exact file paths, code snippets, error messages, \
decisions, user preferences, architectural choices.

- 🟡 **USEFUL (2)** — Background context that might matter.
  → Compress to 1–3 key sentences.

- 🟠 **TANGENTIAL (1)** — Resolved side-topics, earlier iterations now superseded.
  → One-line mention or drop entirely.

- ⚪ **IRRELEVANT (0)** — Greetings, chitchat, fully superseded work.
  → Drop entirely.

## Step 3: Produce the compressed output in 9 sections

### 1. Primary Request
The user's main objective in 1-2 sentences.

### 2. Key Technical Concepts
Domain-specific terms, APIs, libraries, frameworks, and patterns involved.
Include version numbers, configuration values, and protocol details.

### 3. Files & Code
Files that have been read, modified, or created. For each relevant file:
- Full path
- Key functions/classes/sections touched
- Brief code snippets for critical changes (use ``` blocks)

### 4. Errors & Debugging
Errors encountered, their root causes, and resolutions.
Include: exact error messages, stack traces (abbreviated), and what fixed them.

### 5. Problem-Solving Progress
Approaches tried, what worked, what didn't, and why.
Track the logical chain of investigation.

### 6. All User Messages (MANDATORY)
Reproduce EVERY user message in order (abbreviated if long, but never omitted). \
This is critical — user messages contain instructions, preferences, and context \
that must never be lost.

### 7. Decisions & Preferences
Architectural choices, naming conventions, style preferences, rejected \
alternatives — anything the user explicitly stated they want or don't want.

### 8. Current Working State
What currently works, what's broken, known issues, pending tasks. \
Include the current state of any files being edited.

### 9. Pending / Next Steps
What was about to happen when the context was compressed. \
What the assistant should do next to continue the task.

### Recently Accessed Files
(This section will be auto-appended — do not generate it yourself.)

## Rules
- **Relevance to the CURRENT QUERY is the #1 priority**
- Preserve ALL file paths, function names, variable names, error messages
- Section 6 (All User Messages) is MANDATORY — never skip user messages
- Include actual code snippets (not just descriptions) for critical changes
- Drop verbose tool output details — keep only conclusions and key findings
- When a later turn supersedes an earlier one, keep only the latest version
- Strip the <analysis> section from your final output
- Output in the SAME LANGUAGE as the conversation (Chinese → Chinese)
- Be thorough but concise — aim for 30-50% of original token count
"""


_ELISION_MARKER = '\n\n... [middle of conversation elided for summary] ...\n\n'


def _format_messages_for_summary(messages: list,
                                 char_budget: int | None = None) -> str:
    """Render messages as readable text for the summary LLM.

    INCLUDES user msgs and assistant msgs with non-empty natural-language
    content.  EXCLUDES tool messages and tool-call-only assistant
    messages — they don't help a relevance-rating cheap model.

    When ``char_budget`` is given and the full render would exceed it, the
    input is trimmed MESSAGE-AWARE rather than by a blind string slice:

      * EVERY ``[user]`` part is kept VERBATIM — the summary system prompt's
        section 6 ("All User Messages — MANDATORY") must never lose a user
        instruction, so a middle-slice on the joined string (which could cut a
        user turn in half or drop it entirely) is unacceptable.
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

        parts.append((role, f'[{role}] {text}'))

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
    # User parts are never in ``asst_idx`` → always kept (summary prompt §6).
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
    # what made the button slow. 64k still yields a faithful 9-section
    # working-state summary while roughly a third of the prompt → a proportionally
    # faster call. On small windows ``usable`` still binds first (unchanged);
    # the cap only bites on large (>=~200k) windows. Elision beyond the cap is
    # MESSAGE-AWARE (see _format_messages_for_summary): every user message is
    # kept, only middle assistant content is dropped.
    return max(20_000, min(summary_input_char_cap(), input_token_budget))
