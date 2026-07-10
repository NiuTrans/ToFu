"""Layer 2 — query-aware LLM summary with selective turn compression.

Force-injected by the orchestrator only — NOT in the model's tool list.
Triggered when estimated tokens exceed ``_SUMMARY_TRIGGER_RATIO`` of
usable context.

Public surface:
  * ``execute_compact_tool``       — generates the summary, mutates messages
  * ``force_compact_if_needed``    — gates on threshold + injects synthetic pair
  * ``smart_summary_compact``      — legacy alias
  * Boundary helpers:
      - ``_extract_current_query``
      - ``_find_turn_boundary``
      - ``_format_messages_for_summary``
      - ``_generate_query_aware_summary``
      - ``_extract_recently_accessed_files``
  * ``_SUMMARY_SYSTEM_PROMPT`` — the cheap-model system prompt
"""

import json
import re
import time
import uuid

from lib.log import get_logger
from lib.tasks_pkg.compaction._archive import _archive_transcript
from lib.tasks_pkg.compaction._constants import (
    _COMPACT_TOOL_NAME,
    _cooldown_lock,
    _MAX_PRESERVE_TURNS,
    _PRESERVE_BUDGET_RATIO,
    _SUMMARY_MAX_TOKENS,
    _summary_cooldowns,
)
from lib.tasks_pkg.compaction._tokens import (
    _estimate_msg_tokens,
    _estimate_total_tokens,
    _get_context_limit,
    _human_size,
    _should_force_compact,
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Query-aware LLM summary with selective turn compression
# ═══════════════════════════════════════════════════════════════════════════════

def _objective_anchor_index(messages: list) -> int | None:
    """Index of the immutable OBJECTIVE ANCHOR — the first real user message.

    This is the SAME "first real user message" the autopilot objective pin
    (``_get_or_persist_objective`` / ``_extract_objective``) is derived from —
    ONE definition of "the objective", not a parallel one.  Compaction protects
    this message so the original goal survives N successive summaries VERBATIM
    (``execute_compact_tool`` excludes it from the summarized ``old_messages``
    and re-inserts it exactly once; ``_head_truncate`` never drops it).  The
    autopilot pin is a cross-run TEXT cache of the very same message.

    Skips leading ``system`` messages, any VU directive / virtual-user turn
    (defensive — those flags are autopilot-only and absent elsewhere), and the
    synthetic ``_isMeta`` context carriers the builder prepends (CLAUDE.md /
    user-preference profile) — those are a ``user`` message at index 1, BEFORE
    the real user turn, so without this skip the anchor would protect injected
    context instead of the human's goal. Same skip as ``_extract_objective``.
    Returns ``None`` when no real user message exists (compaction then behaves
    exactly as before — no anchor to protect).
    """
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get('role') != 'user':
            continue
        if m.get('_isVuDirective') or m.get('_isVirtualUser') or m.get('_isMeta'):
            continue
        content = m.get('content')
        if isinstance(content, str):
            if content.strip():
                return i
        elif isinstance(content, list):
            if any(isinstance(b, dict) and b.get('type') == 'text'
                   and (b.get('text') or '').strip() for b in content):
                return i
        elif content:  # non-empty non-text (e.g. image-only) — still real
            return i
    return None


def _extract_current_query(messages: list) -> str:
    """Extract the most recent user query from messages."""
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, list):
                text_parts = [
                    b.get('text', '')
                    for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                ]
                return '\n'.join(text_parts)[:500]
            elif isinstance(content, str):
                return content[:500]
    return ''


def _find_turn_boundary(
    messages: list,
    *,
    budget_tokens: float = float('inf'),
    max_turns: int = _MAX_PRESERVE_TURNS,
) -> int:
    """Find the preservation boundary using the turn abstraction.

    A *turn* = ``[user_msg, ...all subsequent non-user messages]``.
    Turns are atomic; the boundary always falls on a ``user`` index.

    Policy:
      • HARD INVARIANT — current (most-recent) turn always preserved.
      • BEST-EFFORT    — older turns added newest → oldest while under
        ``preserved_tokens + turn_tokens <= budget_tokens`` AND total
        preserved turn count stays ``<= max_turns``.
      • REFUSE         — if no ``user`` message exists, returns
        ``len(messages)`` so the caller short-circuits.
    """
    user_idx = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    if not user_idx:
        return len(messages)

    turn_starts = user_idx
    turn_ends = user_idx[1:] + [len(messages)]

    cur_start, cur_end = turn_starts[-1], turn_ends[-1]
    boundary = cur_start
    preserved_tokens = sum(
        _estimate_msg_tokens(m) for m in messages[cur_start:cur_end]
    )
    preserved_turn_count = 1

    for k in range(len(turn_starts) - 2, -1, -1):
        if preserved_turn_count >= max_turns:
            break
        start, end = turn_starts[k], turn_ends[k]
        tt = sum(_estimate_msg_tokens(m) for m in messages[start:end])
        if preserved_tokens + tt > budget_tokens:
            break
        boundary = start
        preserved_tokens += tt
        preserved_turn_count += 1

    return boundary


def _format_messages_for_summary(messages: list) -> str:
    """Render messages as readable text for the summary LLM.

    INCLUDES user msgs and assistant msgs with non-empty natural-language
    content.  EXCLUDES tool messages and tool-call-only assistant
    messages — they don't help a relevance-rating cheap model.
    """
    parts = []
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

        parts.append(f'[{role}] {text}')

    if skipped_tool or skipped_tool_only_assistant:
        logger.debug(
            '[Compact] Relevance-format filter: skipped %d tool results, '
            '%d tool-call-only assistant msgs; kept %d user/assistant turns',
            skipped_tool, skipped_tool_only_assistant, len(parts),
        )

    return '\n\n'.join(parts)


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
    # strictly necessary, but the summary is still produced. Clamp to the
    # historical 200k ceiling, which only binds on large (>=~300k) windows —
    # so 1M-context models are byte-identical to the old fixed cap.
    return max(20_000, min(200_000, input_token_budget))


def _generate_query_aware_summary(messages: list, current_query: str,
                                   log_prefix: str = '',
                                   conv_id: str = '',
                                   task: dict | None = None) -> str | None:
    """Call a cheap model to generate a query-aware summary.

    Degrades gracefully so the proactive path actually works on a
    vanilla/exported deploy: the input is capped to the model's real token
    window (see ``_summary_input_char_budget``), and if the preferred
    ``capability='cheap'`` dispatch fails (no model tagged cheap, or the
    single model is momentarily exhausted) it retries once against any
    text-capable slot before giving up.
    """
    from lib.llm_dispatch import dispatch_chat

    formatted = _format_messages_for_summary(messages)
    tag = f'{log_prefix}[Summary]' if log_prefix else '[Summary]'

    logger.info('%s Formatting %d messages for summary (%s), query=%.80s',
                tag, len(messages), _human_size(len(formatted)), current_query)

    _char_budget = _summary_input_char_budget(task)
    if len(formatted) > _char_budget:
        original_len = len(formatted)
        # Keep the head (early goals/decisions) and a larger tail (recent
        # working state), eliding the middle — 1/3 head, 2/3 tail.
        _head = _char_budget // 3
        _tail = _char_budget - _head
        formatted = (
            formatted[:_head]
            + '\n\n... [middle of conversation omitted for summary] ...\n\n'
            + formatted[-_tail:]
        )
        logger.info('%s Input truncated to model window: %s → %s (budget %s)',
                    tag, _human_size(original_len), _human_size(len(formatted)),
                    _human_size(_char_budget))

    user_content = (
        f'## Current User Query\n{current_query}\n\n'
        f'## Conversation History to Compress\n\n{formatted}'
    )

    def _dispatch(capability: str):
        return dispatch_chat(
            [
                {'role': 'system', 'content': _SUMMARY_SYSTEM_PROMPT},
                {'role': 'user', 'content': user_content},
            ],
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=0,
            capability=capability,
            log_prefix=tag,
        )

    try:
        try:
            content, usage = _dispatch('cheap')
        except Exception as _cheap_e:
            # Preferred cheap tier failed — retry once against ANY text slot
            # (a deploy may have no model tagged 'cheap' at all). The
            # dispatcher already widens 'cheap'→'text' internally when no
            # cheap slot exists, so this mainly covers a transient cheap-slot
            # exhaustion; it's cheap insurance and makes the intent explicit.
            logger.warning('%s cheap-tier summary dispatch failed (%s: %s) — '
                           'retrying on any text-capable slot',
                           tag, type(_cheap_e).__name__, _cheap_e)
            content, usage = _dispatch('text')

        if content:
            in_tok = usage.get('prompt_tokens', 0)
            out_tok = usage.get('completion_tokens', 0)
            # Count this summary call's tokens toward the conversation's
            # compaction cost — otherwise the L2 (chatui 'tofu') summary is
            # invisible in task['usage'] and the arm looks cheaper than it is.
            try:
                from lib.tasks_pkg.compaction._compaction_usage import (
                    record_compaction_usage)
                record_compaction_usage(conv_id, usage, kind='L2')
            except Exception as _ru_e:
                logger.debug('%s record_compaction_usage failed: %s', tag, _ru_e)
            content = re.sub(
                r'<analysis>.*?</analysis>\s*',
                '', content, flags=re.DOTALL,
            )
            logger.info('%s Summary generated: %d chars  in=%d  out=%d tokens',
                        tag, len(content), in_tok, out_tok)
            return content.strip()
        else:
            logger.warning('%s Summary model returned empty content', tag)
            return None

    except Exception as e:
        logger.warning('%s Summary generation failed (will keep messages intact): %s: %s',
                       tag, type(e).__name__, e)
        return None


def _coerce_spec_list(value) -> list:
    """Coerce a tool arg that should be a list-of-specs into a real list.

    Tolerates the observed-in-the-wild case where a streamed / partial
    tool-call recorded the array as a JSON *string* (sometimes truncated)
    instead of a list — e.g. ``reads='[{"path": "a.py", "end_line": 4]'``.
    Iterating such a raw string char-by-char is what produced the notorious
    "one letter per line" modified-files reminder (conv mr4e8pnxbv440z).

    If the string decodes to a list, return it; otherwise return ``[]`` so the
    caller skips it rather than iterating characters and emitting garbage.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError) as e:
            logger.debug('[Compact] _coerce_spec_list: unparseable spec '
                         'string (%s) — dropping', e)
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _extract_recently_accessed_files(messages: list,
                                     max_files: int = 8) -> list[str]:
    """Scan messages newest-first for file paths from read/write tools."""
    files_seen: list[str] = []
    files_set: set[str] = set()

    for msg in reversed(messages):
        for tc in msg.get('tool_calls', []):
            fn = tc.get('function', {})
            fn_name = fn.get('name', '')

            if fn_name not in ('read_files', 'read_file',
                               'write_file', 'apply_diff', 'apply_diffs',
                               'insert_content', 'insert_contents'):
                continue

            try:
                args = json.loads(fn.get('arguments', '{}'))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug('[Compaction] Skipping unparseable tool_call args for %s: %s',
                             fn_name, exc, exc_info=True)
                continue

            if not isinstance(args, dict):
                logger.debug('[Compact] Skipping non-dict tool_call args for %s (type=%s)',
                             fn_name, type(args).__name__)
                continue

            if fn_name == 'read_files':
                # After _coerce_spec_list the container is guaranteed a LIST,
                # so a string ELEMENT is a genuine full path (a documented
                # Claude-Opus shape: reads=["a.py","b.py"]) — NOT a stray char
                # from iterating a string container. Keep both element shapes.
                for spec in _coerce_spec_list(args.get('reads')):
                    if isinstance(spec, dict):
                        p = spec.get('path', '')
                    elif isinstance(spec, str):
                        p = spec.strip()
                    else:
                        logger.debug('[Compact] Skipping non-dict/str read spec type=%s',
                                     type(spec).__name__)
                        continue
                    if p and p not in files_set:
                        files_seen.append(p)
                        files_set.add(p)
            elif fn_name in ('apply_diff', 'apply_diffs') and args.get('edits'):
                for edit in _coerce_spec_list(args.get('edits')):
                    if isinstance(edit, dict):
                        p = edit.get('path', '')
                        if p and p not in files_set:
                            files_seen.append(p)
                            files_set.add(p)
            elif fn_name in ('insert_content', 'insert_contents') and args.get('edits'):
                for edit in _coerce_spec_list(args.get('edits')):
                    if isinstance(edit, dict):
                        p = edit.get('path', '')
                        if p and p not in files_set:
                            files_seen.append(p)
                            files_set.add(p)
            else:
                p = args.get('path', '') if isinstance(args, dict) else ''
                if p and p not in files_set:
                    files_seen.append(p)
                    files_set.add(p)

            if len(files_seen) >= max_files:
                break

    if files_seen:
        logger.debug('[Compact] Found %d recently-accessed files: %s',
                     len(files_seen),
                     ', '.join(files_seen[:4]) + ('...' if len(files_seen) > 4 else ''))

    return files_seen


# ═══════════════════════════════════════════════════════════════════════════════
#  Core: execute_compact_tool — pure LLM summary with selective turn compression
# ═══════════════════════════════════════════════════════════════════════════════

def execute_compact_tool(messages: list, task: dict | None = None, **kwargs) -> str:
    """Execute context compaction — force-injected by the orchestrator only.

    NOT in the model's tool list. The model never calls this voluntarily.
    Triggered when estimated tokens exceed 80% of usable context.

    Pure LLM summary approach with selective turn compression.
    """
    conv_id = task.get('convId', '') if task else ''
    log_id = conv_id[:8] if conv_id else '?'
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    # Optional out-param: caller passes a mutable dict to learn whether
    # messages were actually mutated. Stays False on every early-return
    # failure path; flipped to True only after the message list is
    # replaced.  reactive_compact relies on this so its head-truncate
    # safety net engages when the LLM summary comes back empty.
    _result_meta = kwargs.get('_result_meta') if kwargs else None
    if isinstance(_result_meta, dict):
        _result_meta['compacted'] = False

    tokens_before = _estimate_total_tokens(messages)
    msg_count_before = len(messages)
    context_limit = _get_context_limit(task)
    usable = _usable_context(context_limit)

    budget_override = kwargs.get('preserve_budget_tokens') if kwargs else None
    if budget_override is not None:
        budget_tokens = max(1, int(budget_override))
    else:
        budget_tokens = max(1, int(usable * _PRESERVE_BUDGET_RATIO))

    _krp = kwargs.get('keep_recent_pairs') if kwargs else None
    max_turns = _MAX_PRESERVE_TURNS if _krp is None else max(1, int(_krp))

    logger.info('%s [Compact] Starting  conv=%s  tokens=%d  usable=%d  messages=%d  '
                'budget=%d  max_turns=%d',
                pfx, log_id, tokens_before, usable, msg_count_before,
                budget_tokens, max_turns)

    current_query = _extract_current_query(messages)

    boundary = _find_turn_boundary(
        messages, budget_tokens=budget_tokens, max_turns=max_turns,
    )

    if boundary >= len(messages):
        logger.error(
            '%s [Compact] REFUSING — no user message found to anchor preservation. '
            'msg_count=%d  tokens=%d  model=%s',
            pfx, msg_count_before, tokens_before,
            (task.get('config', {}) or {}).get('model', '?') if task else '?',
        )
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction skipped — no user message found to '
                'anchor preservation. Messages preserved as-is.')

    system_end = 0
    for i, m in enumerate(messages):
        if m.get('role') == 'system':
            system_end = i + 1
        else:
            break

    if boundary >= len(messages) - 0 and boundary <= system_end:
        logger.error(
            '%s [Compact] REFUSING — boundary=%d would preserve 0 live messages '
            '(system_end=%d, total=%d)',
            pfx, boundary, system_end, msg_count_before,
        )
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction skipped — boundary calculation would '
                'preserve no live messages. Bailing out to prevent data loss.')

    with _cooldown_lock:
        _summary_cooldowns[conv_id] = time.time()

    _archive_id: int | None = None
    if not kwargs.get('_compaction_skip_archive'):
        _archive_id = _archive_transcript(
            conv_id, messages,
            trigger=kwargs.get('_compaction_trigger') or 'force',
            task=task,
            round_num=int((task.get('round_num') if task else 0) or 0),
            tokens_before=int(tokens_before or 0),
            msgs_before=int(msg_count_before or 0),
            reason=kwargs.get('_compaction_reason') or '',
            emit_event=True,
        )

    old_messages = messages[:boundary]
    recent_messages = messages[boundary:]

    # ★ OBJECTIVE ANCHOR — the first real user message is the north-star
    #   objective.  If it falls in the to-be-summarized ``old_messages`` it
    #   would be lossily paraphrased (and re-paraphrased every subsequent
    #   compaction → unbounded drift), so we PULL IT OUT and re-insert it
    #   verbatim exactly once, right after the system messages.  If it is
    #   already in ``recent_messages`` (short conversation) there is nothing to
    #   do — it's preserved as-is.  Because the anchor is a genuine existing
    #   message (not a synthesized prepend), a subsequent compaction finds the
    #   SAME message already at the front of ``recent_messages`` and never
    #   duplicates it — idempotent, byte-identical, cache-prefix-stable.
    anchor_idx = _objective_anchor_index(messages)
    anchor_msg = None
    if anchor_idx is not None and anchor_idx < boundary:
        anchor_msg = messages[anchor_idx]
        # Summarize everything old EXCEPT the anchor.
        old_messages = [m for k, m in enumerate(messages[:boundary])
                        if k != anchor_idx]
        logger.info('%s [Compact] Preserving objective anchor verbatim '
                    '(msg idx=%d) across summary', pfx, anchor_idx)

    preserved_turns = sum(
        1 for m in recent_messages if m.get('role') == 'user'
    )

    logger.info('%s [Compact] Summarizing %d old messages, '
                'preserving %d recent (%d turns), query=%.100s',
                pfx, len(old_messages), len(recent_messages),
                preserved_turns, current_query)

    summary_text = _generate_query_aware_summary(
        old_messages, current_query, pfx, conv_id=conv_id, task=task
    )

    if not summary_text:
        logger.warning('%s [Compact] Summary generation failed — keeping messages intact', pfx)
        if isinstance(_result_meta, dict):
            _result_meta['compacted'] = False
        return ('Context compaction attempted but summary generation failed. '
                'Messages preserved as-is.')

    recent_files = _extract_recently_accessed_files(messages)
    if recent_files:
        file_list = '\n'.join(f'  - {f}' for f in recent_files)
        summary_text += (
            f'\n\n### Recently Accessed Files\n'
            f'Use read_files to review current state if needed:\n'
            f'{file_list}'
        )

    system_msgs = []
    for msg in old_messages:
        if msg.get('role') == 'system':
            system_msgs.append(msg)
        else:
            break

    # Rebuild: system → [objective anchor, if it was in the summarized region]
    # → recent.  The anchor is placed immediately after the system block so the
    # model always sees the original goal at a stable position, and exactly
    # once (it was removed from ``old_messages`` above, so it isn't also inside
    # the summary text's source, and it is NOT in ``recent_messages`` because
    # anchor_idx < boundary).
    anchor_block = [anchor_msg] if anchor_msg is not None else []
    new_messages = list(system_msgs) + anchor_block + list(recent_messages)
    messages.clear()
    messages.extend(new_messages)

    if isinstance(_result_meta, dict):
        _result_meta['compacted'] = True

    tokens_after = _estimate_total_tokens(messages)
    reduction_pct = (1 - tokens_after / max(1, tokens_before)) * 100

    logger.info('%s [Compact] Complete  conv=%s  '
                'tokens: %d → %d (%.0f%% reduction)  '
                'messages: %d → %d  summarized=%d old messages',
                pfx, log_id,
                tokens_before, tokens_after, reduction_pct,
                msg_count_before, len(messages),
                boundary - len(system_msgs))

    # ── Phase-C: record the 'saved' half of this L2 event's cache ROI ──
    # The following round's detect_cache_break completes it with the re-billed
    # cache_write. Best-effort; never let instrumentation break compaction.
    if conv_id:
        try:
            from lib.tasks_pkg.cache_tracking import record_l2_compaction
            record_l2_compaction(
                conv_id, tokens_before=int(tokens_before),
                tokens_after=int(tokens_after),
                msgs_before=int(msg_count_before), msgs_after=int(len(messages)))
        except Exception as _roi_e:
            logger.debug('%s [Compact] record_l2_compaction failed: %s', pfx, _roi_e)

    if _archive_id is not None:
        try:
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().update_archive_summary(
                _archive_id, summary_text or '', int(tokens_after), int(len(messages)))
        except Exception as _upd_e:
            logger.debug('[Compact] archive row update failed: %s', _upd_e)

        if task is not None:
            try:
                from lib.agent_core.events import EventType, build_event
                from lib.tasks_pkg.manager import append_event
                append_event(task, build_event(
                    EventType.COMPACTION_DONE,
                    archiveId=int(_archive_id),
                    convId=conv_id,
                    tokensAfter=int(tokens_after),
                    msgsAfter=int(len(messages)),
                    reductionPct=round(reduction_pct, 1),
                ))
            except Exception as _ev_e:
                logger.debug('[Compact] compaction_done emit failed: %s', _ev_e)

    result_parts = [
        '## Context Compacted — Selective Summary\n',
        f'Compressed {boundary - len(system_msgs)} historical messages '
        f'({tokens_before:,} → {tokens_after:,} tokens, '
        f'{reduction_pct:.0f}% reduction)\n',
        summary_text,
    ]

    return '\n'.join(result_parts)


# ═══════════════════════════════════════════════════════════════════════════════
#  Force compact: inject context_compact tool call when over threshold
# ═══════════════════════════════════════════════════════════════════════════════

def force_compact_if_needed(messages: list, task: dict | None = None,
                            keep_recent_pairs: int | None = None,
                            preserve_budget_tokens: int | None = None,
                            *, force: bool = False,
                            **kwargs) -> bool:
    """Check token usage and force-inject a context_compact tool round if needed.

    Args:
        keep_recent_pairs: Legacy knob mapped to ``max_turns`` (turn-count cap).
        preserve_budget_tokens: Token budget for verbatim preservation.
        force: Skip the ``_should_force_compact`` threshold gate.

    Returns True if compaction was performed, False otherwise.
    """
    if not force and not _should_force_compact(messages, task):
        return False

    conv_id = task.get('convId', '') if task else ''
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    logger.info('%s [ForceCompact] Injecting context_compact for conv=%s',
                pfx, conv_id[:8] if conv_id else '?')

    # ★ Surface the L2 summary as a live phase. Without this, the front-end
    #   spinner stays frozen on "Analyzing results…" for the several seconds
    #   the cheap-model summary call takes — the user can't tell the harness
    #   is busy compressing context rather than hung.
    if task is not None:
        try:
            from lib.agent_core.events import EventType, build_event
            from lib.tasks_pkg.manager import append_event
            append_event(task, build_event(
                EventType.PHASE, phase='compacting',
                detail='Compressing earlier context to fit the window…'))
        except Exception as _ph_e:
            logger.debug('%s [ForceCompact] phase emit failed: %s', pfx, _ph_e)

    _trigger = (kwargs.get('_compaction_trigger')
                if isinstance(kwargs, dict) else None) or 'force'
    _reason = (kwargs.get('_compaction_reason')
               if isinstance(kwargs, dict) else None) or ''
    _skip_archive = bool(kwargs.get('_compaction_skip_archive')
                         if isinstance(kwargs, dict) else False)
    _meta: dict = {}
    compact_result = execute_compact_tool(
        messages, task=task,
        keep_recent_pairs=keep_recent_pairs,
        preserve_budget_tokens=preserve_budget_tokens,
        _compaction_trigger=_trigger,
        _compaction_reason=_reason,
        _compaction_skip_archive=_skip_archive,
        _result_meta=_meta,
    )

    # If the summary LLM returned empty / compaction refused, the message
    # list was NOT mutated. Injecting a synthetic context_compact
    # tool-pair here would only grow the context and — worse — make the
    # caller (reactive_compact) believe compaction succeeded, skipping its
    # head-truncate safety net and looping the same oversized prompt back
    # to the API. Report failure so the caller can fall through.
    if not _meta.get('compacted'):
        # ★ Deterministic proactive safety net (fix for the OOM fatal loop).
        #   The summary LLM is the ONLY mechanism the proactive path had; on
        #   a vanilla/exported deploy the cheap-model dispatch can fail
        #   outright (no model tagged 'cheap', saturated single model,
        #   summary input itself too big). Historically force-compact then
        #   returned False and did nothing, so the context stayed pinned near
        #   the window every round — and the reactive head-truncate net never
        #   fired because the max_tokens clamp keeps the request just under
        #   the hard ceiling (no API rejection). Nothing bounded the context
        #   → unbounded re-send → OOM (SIGKILL).
        #
        #   So when the proactive pipeline opts in (_allow_head_truncate_fallback)
        #   AND we are genuinely over the usable window, fall through to the
        #   same last-resort _head_truncate the reactive path already trusts,
        #   right here. This is bounded, logged (audit_log
        #   'proactive_head_truncate') context loss — strictly better than a
        #   process death. The empty-summary→False contract is preserved for
        #   the NON-critical case (still headroom before the window): we only
        #   head-truncate when estimated input >= usable window.
        _allow_ht = bool(kwargs.get('_allow_head_truncate_fallback')
                         if isinstance(kwargs, dict) else False)
        if _allow_ht:
            try:
                from lib.tasks_pkg.compaction._tokens import (
                    _count_tokens_authoritative)
                _est_tokens, _tok_method = _count_tokens_authoritative(
                    messages, task)
            except Exception as _ce:
                logger.debug('%s [ForceCompact] authoritative count failed, '
                             'using heuristic: %s', pfx, _ce)
                _est_tokens = _estimate_total_tokens(messages)
                _tok_method = 'heuristic'
            _usable = _usable_context(_get_context_limit(task))
            if _est_tokens >= _usable:
                logger.warning(
                    '%s [ForceCompact] Summary failed AND context critically '
                    'over budget (est=%d via %s >= usable=%d) — falling back '
                    'to deterministic head-truncate so the context is bounded '
                    'without depending on the summary LLM',
                    pfx, _est_tokens, _tok_method, _usable)
                from lib.tasks_pkg.compaction._reactive import _head_truncate
                _dropped = _head_truncate(
                    messages, task,
                    reported_token_count=_est_tokens,
                    event_name='proactive_head_truncate')
                if _dropped:
                    # Context was bounded — surface as a real compaction so
                    # the pipeline notifies the cache tracker (prefix changed)
                    # and the round proceeds with a smaller prompt.
                    return True
                logger.warning(
                    '%s [ForceCompact] Head-truncate dropped 0 messages '
                    '(too few to shed) — reporting failure', pfx)
        logger.warning('%s [ForceCompact] Compaction did not mutate messages '
                       '(summary empty or refused) — reporting failure so the '
                       'caller can fall back', pfx)
        return False

    compact_call_id = f'compact_{uuid.uuid4().hex[:12]}'

    messages.append({
        'role': 'assistant',
        'content': None,
        'tool_calls': [{
            'id': compact_call_id,
            'type': 'function',
            'function': {
                'name': _COMPACT_TOOL_NAME,
                'arguments': '{}',
            },
        }],
    })

    messages.append({
        'role': 'tool',
        'tool_call_id': compact_call_id,
        'name': _COMPACT_TOOL_NAME,
        'content': compact_result,
    })

    return True


def smart_summary_compact(messages: list, task: dict | None = None):
    """Legacy entry point — now delegates to force_compact_if_needed."""
    force_compact_if_needed(messages, task=task)
