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


def _generate_query_aware_summary(messages: list, current_query: str,
                                   log_prefix: str = '',
                                   conv_id: str = '') -> str | None:
    """Call a cheap model to generate a query-aware summary."""
    from lib.llm_dispatch import dispatch_chat

    formatted = _format_messages_for_summary(messages)
    tag = f'{log_prefix}[Summary]' if log_prefix else '[Summary]'

    logger.info('%s Formatting %d messages for summary (%s), query=%.80s',
                tag, len(messages), _human_size(len(formatted)), current_query)

    if len(formatted) > 200_000:
        original_len = len(formatted)
        formatted = (
            formatted[:50_000]
            + '\n\n... [middle of conversation omitted for summary] ...\n\n'
            + formatted[-100_000:]
        )
        logger.info('%s Input truncated: %s → %s',
                    tag, _human_size(original_len), _human_size(len(formatted)))

    user_content = (
        f'## Current User Query\n{current_query}\n\n'
        f'## Conversation History to Compress\n\n{formatted}'
    )

    try:
        content, usage = dispatch_chat(
            [
                {'role': 'system', 'content': _SUMMARY_SYSTEM_PROMPT},
                {'role': 'user', 'content': user_content},
            ],
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=0,
            capability='cheap',
            log_prefix=tag,
        )

        if content:
            in_tok = usage.get('prompt_tokens', 0)
            out_tok = usage.get('completion_tokens', 0)
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

            if fn_name == 'read_files':
                for spec in args.get('reads', []) or []:
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
                for edit in args['edits'] or []:
                    if isinstance(edit, dict):
                        p = edit.get('path', '')
                        if p and p not in files_set:
                            files_seen.append(p)
                            files_set.add(p)
            elif fn_name in ('insert_content', 'insert_contents') and args.get('edits'):
                for edit in args['edits'] or []:
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

    preserved_turns = sum(
        1 for m in recent_messages if m.get('role') == 'user'
    )

    logger.info('%s [Compact] Summarizing %d old messages, '
                'preserving %d recent (%d turns), query=%.100s',
                pfx, len(old_messages), len(recent_messages),
                preserved_turns, current_query)

    summary_text = _generate_query_aware_summary(
        old_messages, current_query, pfx, conv_id=conv_id
    )

    if not summary_text:
        logger.warning('%s [Compact] Summary generation failed — keeping messages intact', pfx)
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

    new_messages = list(system_msgs) + list(recent_messages)
    messages.clear()
    messages.extend(new_messages)

    tokens_after = _estimate_total_tokens(messages)
    reduction_pct = (1 - tokens_after / max(1, tokens_before)) * 100

    logger.info('%s [Compact] Complete  conv=%s  '
                'tokens: %d → %d (%.0f%% reduction)  '
                'messages: %d → %d  summarized=%d old messages',
                pfx, log_id,
                tokens_before, tokens_after, reduction_pct,
                msg_count_before, len(messages),
                boundary - len(system_msgs))

    if _archive_id is not None:
        try:
            from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
            db2 = get_thread_db(DOMAIN_CHAT)
            db_execute_with_retry(db2,
                'UPDATE transcript_archive SET summary=?, '
                'tokens_after=?, msgs_after=? WHERE id=?',
                ((summary_text or '')[:200_000], int(tokens_after),
                 int(len(messages)), int(_archive_id)),
            )
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

    _trigger = (kwargs.get('_compaction_trigger')
                if isinstance(kwargs, dict) else None) or 'force'
    _reason = (kwargs.get('_compaction_reason')
               if isinstance(kwargs, dict) else None) or ''
    _skip_archive = bool(kwargs.get('_compaction_skip_archive')
                         if isinstance(kwargs, dict) else False)
    compact_result = execute_compact_tool(
        messages, task=task,
        keep_recent_pairs=keep_recent_pairs,
        preserve_budget_tokens=preserve_budget_tokens,
        _compaction_trigger=_trigger,
        _compaction_reason=_reason,
        _compaction_skip_archive=_skip_archive,
    )

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
