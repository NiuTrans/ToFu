# HOT_PATH
"""
Context compaction — two-layer progressive compression pipeline.

Layer 1 — Micro-compaction (runs before every LLM call, zero LLM cost):

    Keeps a "hot tail" of the N most recent tool results untouched.
    Tool results that fall outside the hot tail are replaced in the
    messages list with a short placeholder that tells the model the
    result was compacted and it can re-call the tool if needed.

    This layer runs every round, is idempotent (skips already-compacted
    results), and requires no LLM calls.

Layer 2 — Context compact (force-triggered by orchestrator only):

    NOT in the model's tool list — the model never calls this voluntarily.
    Force-injected by the orchestrator when estimated token count exceeds
    80% of usable context window.

    Pure LLM summary with selective turn compression:
      - A cheap model evaluates each historical user↔assistant turn
        for relevance to the current query
      - Critical turns (score 3) preserved verbatim
      - Useful turns (score 2) compressed to key sentences
      - Tangential turns (score 1) reduced to one-line mentions
      - Irrelevant turns (score 0) dropped entirely

    The summary is injected as a synthetic tool_call + tool_result pair.
    Old messages before the boundary are replaced.

Concurrency safety:
    All persistent state is keyed by conv_id.  Multiple conversations
    can compact concurrently without interference.  No filesystem
    artifacts — everything goes through the database.
"""

import json
import os
import re
import threading
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════════════════

# ── Layer 1 ──────────────────────────────────────────────────────────────────

MICRO_HOT_TAIL = 30
"""Number of most-recent tool results to keep uncompressed.
Everything older is archived to DB and replaced with a placeholder."""

MICRO_COMPACT_THRESHOLD = 500
"""Minimum character count before a tool result is worth compacting.
Results shorter than this are left in place even outside the hot tail."""

# ── Layer 2 / Force compact ──────────────────────────────────────────────────

_SUMMARY_TRIGGER_RATIO = 0.80
"""Trigger force-compact when tokens exceed this fraction of usable context."""

_SUMMARY_MAX_TOKENS = 3000
"""Maximum output tokens for the summary LLM call."""

_SUMMARY_COOLDOWN = 30.0
"""Seconds between consecutive summary attempts for the same conv_id.
Prevents rapid re-triggering when the model generates a long response
right after a summary."""

_DEFAULT_CONTEXT_LIMIT = 1_000_000
"""Fallback context limit when the model name is not recognized.
Raised to 1M since primary models (Claude 4.6) support 1M context (GA 2026-03-13)."""

_OUTPUT_RESERVE = 32_000
"""Tokens reserved for model output generation."""

_COMPACTION_RESERVE = 8_000
"""Tokens reserved for the compaction LLM call itself."""

_COMPACT_TOOL_NAME = 'context_compact'
"""Tool name for the synthetic compact tool pair."""

_KEEP_RECENT_PAIRS = 4
"""Minimum number of user-assistant message pairs to always preserve verbatim."""


# ── Internal state ───────────────────────────────────────────────────────────

_summary_cooldowns: dict[str, float] = {}
"""Mapping of conv_id → timestamp of last summary attempt."""

_cooldown_lock = threading.Lock()
"""Protects concurrent access to _summary_cooldowns."""


# ═══════════════════════════════════════════════════════════════════════════════
#  Summary prompt (Phase 2 — query-aware)
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
#  DB helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_compaction_tables():
    """Create compaction-related tables if they don't already exist."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS transcript_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ta_conv ON transcript_archive(conv_id);
    ''')


_tables_initialized = False
_tables_lock = threading.Lock()


def _init_tables():
    """Lazy, one-time table creation.  Thread-safe via double-checked lock."""
    global _tables_initialized
    if _tables_initialized:
        return
    with _tables_lock:
        if _tables_initialized:
            return
        try:
            _ensure_compaction_tables()
            _tables_initialized = True
            logger.debug('[Compaction] DB tables initialized')
        except Exception as e:
            logger.error('[Compaction] Failed to initialize DB tables: %s',
                         e, exc_info=True)


def _archive_transcript(conv_id: str, messages: list, summary: str = ''):
    """Archive the full message list to DB before summarization."""
    _init_tables()
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        from lib.database import json_dumps_pg
        messages_json = json_dumps_pg(messages, default=str)
        db_execute_with_retry(db,
            'INSERT INTO transcript_archive '
            '(conv_id, messages_json, summary) VALUES (?,?,?)',
            (conv_id, messages_json, summary),
        )
        logger.info('[Compact] Transcript archived conv=%s  '
                    'messages=%d  size=%s',
                    conv_id[:8] if conv_id else '?',
                    len(messages),
                    _human_size(len(messages_json)))
    except Exception as e:
        logger.warning('[Compact] Transcript archive failed conv=%s: %s',
                       conv_id[:8] if conv_id else '?', e, exc_info=True)


def cleanup_compaction_data(conv_id: str):
    """Delete all compaction artifacts for a conversation."""
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM transcript_archive WHERE conv_id=?', (conv_id,))
        logger.debug('[Compaction] Cleaned up artifacts for conv=%s',
                     conv_id[:8] if conv_id else '?')
    except Exception as e:
        logger.debug('[Compaction] Cleanup artifacts failed for conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 0 — Tool Result Budgeting (inline, per-result cap)
#  Inspired by Claude Code's toolResultStorage — large tool results are
#  truncated IMMEDIATELY when they enter context, not deferred to later.
# ═══════════════════════════════════════════════════════════════════════════════

# Per-tool maximum result chars.  Results exceeding this are truncated to a
# preview + tail immediately upon entry.  This prevents a single grep_search
# or read_files from eating the entire context window.
# Tools whose results should NEVER be truncated by budget_tool_result.
# Like Claude Code's Read tool (maxResultSizeChars = Infinity), truncating
# read results is counterproductive — the model will just re-call the tool,
# wasting tokens and time.  These tools already have their own internal
# limits (MAX_READ_CHARS=100K per file, BATCH_CHAR_BUDGET=200K).
# micro_compact (Layer 1) will compress them later when they become cold.
_BUDGET_EXEMPT_TOOLS = frozenset({
    'read_files',
    'read_local_file',
})

TOOL_RESULT_MAX_CHARS: dict[str, int] = {
    'read_files':    0,          # exempt — see _BUDGET_EXEMPT_TOOLS
    'grep_search':   30_000,
    'find_files':    20_000,
    'list_dir':      15_000,
    'run_command':   40_000,
    'fetch_url':     50_000,
    'web_search':    30_000,
    'browser_read_tab': 40_000,
    'browser_get_interactive_elements': 30_000,
    'browser_execute_js': 30_000,
    'browser_get_app_state': 30_000,
    'check_error_logs': 30_000,
    'read_local_file': 0,        # exempt — see _BUDGET_EXEMPT_TOOLS
}
_DEFAULT_TOOL_RESULT_MAX = 60_000
"""Default budget for tools not listed above."""

# ── Disk persistence for oversized results ──────────────────────────────
# Instead of irreversibly truncating large tool results (head+tail),
# write the full content to a temp file and return a preview + file path.
# The model can later use read_local_file to access the full content.
# Inspired by Claude Code's toolResultStorage.ts persistence mechanism.

_PERSIST_DIR_BASE = '/tmp/chatui-tool-results'
_PERSIST_PREVIEW_CHARS = 2000
"""Preview size for persisted results (truncated at newline boundary)."""

# ── Per-round aggregate budget ──────────────────────────────────────────
# Prevents context explosion from parallel tool calls.
# If total tool result chars in one round exceed this, the largest
# non-exempt results are persisted to disk.
MAX_ROUND_TOOL_RESULTS_CHARS = 300_000


def _persist_to_disk(content: str, tool_name: str, tool_use_id: str = '',
                     conv_id: str = '') -> str:
    """Write full content to disk and return a preview + file path.

    The model receives the file path and can use read_local_file to
    access the full content later.  Information is never lost.

    Args:
        content:     Full tool result string.
        tool_name:   Name of the tool that produced the result.
        tool_use_id: Tool call ID (used for filename uniqueness).
        conv_id:     Conversation ID (used for directory grouping).

    Returns:
        A formatted string with file path + preview.
    """
    # Build directory: /tmp/chatui-tool-results/{conv_id_prefix}/
    dir_name = conv_id[:12] if conv_id else 'default'
    persist_dir = os.path.join(_PERSIST_DIR_BASE, dir_name)
    os.makedirs(persist_dir, exist_ok=True)

    # Filename: {tool_name}_{tool_use_id}.txt
    safe_id = (tool_use_id or uuid.uuid4().hex[:12]).replace('/', '_')
    filename = f'{tool_name}_{safe_id}.txt'
    filepath = os.path.join(persist_dir, filename)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        logger.warning('[Persist] Failed to write %s: %s', filepath, e,
                       exc_info=True)
        # Fall back to old head+tail truncation
        return _truncate_head_tail(content, tool_name,
                                   TOOL_RESULT_MAX_CHARS.get(tool_name, _DEFAULT_TOOL_RESULT_MAX))

    # Generate preview truncated at newline boundary
    preview = content[:_PERSIST_PREVIEW_CHARS]
    last_nl = preview.rfind('\n')
    if last_nl > _PERSIST_PREVIEW_CHARS // 2:
        preview = preview[:last_nl]

    logger.info('[Persist] %s result persisted to disk: %s (%s)',
                tool_name, filepath, _human_size(len(content)))

    return (
        f'[Persisted to: {filepath}]\n'
        f'Output too large ({_human_size(len(content))}). '
        f'Full output saved to: {filepath}\n'
        f'Use read_local_file to access the full content if needed.\n\n'
        f'Preview (first ~{_human_size(len(preview))}):\n'
        f'{preview}\n...'
    )


def _truncate_head_tail(content: str, tool_name: str, max_chars: int) -> str:
    """Legacy head+tail truncation fallback.

    Used only when disk persistence fails (e.g. permission errors).
    """
    original_len = len(content)
    head_budget = int(max_chars * 0.70)
    tail_budget = int(max_chars * 0.25)

    head = content[:head_budget]
    tail = content[-tail_budget:]

    truncation_note = (
        f'\n\n... [{original_len - head_budget - tail_budget:,} chars truncated — '
        f'result was {original_len:,} chars, budget is {max_chars:,}] ...\n\n'
    )

    logger.info('[Budget] %s result truncated (fallback): %s → %s (budget %s)',
                tool_name, _human_size(original_len),
                _human_size(head_budget + tail_budget),
                _human_size(max_chars))

    return head + truncation_note + tail


def budget_tool_result(tool_name: str, content: str,
                       tool_use_id: str = '', conv_id: str = '') -> str:
    """Budget a tool result — persist to disk or pass through.

    For exempt tools (read_files, read_local_file): always pass through
    unchanged.  These tools have their own internal limits and truncating
    them is counterproductive (the model would just re-call).

    For other tools: if the content exceeds the per-tool budget, persist
    the full content to disk and return a preview + file path.  The model
    can later use read_local_file to access the full content.

    Args:
        tool_name:   Name of the tool that produced the result.
        content:     Raw result string.
        tool_use_id: Tool call ID (for persistence filename).
        conv_id:     Conversation ID (for persistence directory).

    Returns:
        Original content if within budget or exempt, or persisted
        preview+path string.
    """
    if not isinstance(content, str):
        return content

    # Exempt tools: never truncated (like Claude Code's Read with Infinity)
    if tool_name in _BUDGET_EXEMPT_TOOLS:
        return content

    max_chars = TOOL_RESULT_MAX_CHARS.get(tool_name, _DEFAULT_TOOL_RESULT_MAX)
    if len(content) <= max_chars:
        return content

    # Persist to disk instead of irreversibly truncating
    return _persist_to_disk(content, tool_name, tool_use_id, conv_id)


def enforce_round_aggregate_budget(
    tool_results: dict[str, tuple[str, str, str]],
    conv_id: str = '',
) -> dict[str, tuple[str, str, str]]:
    """Enforce per-round aggregate budget on tool results.

    If the total chars of all tool results in one round exceed
    MAX_ROUND_TOOL_RESULTS_CHARS, persist the largest non-exempt results
    to disk until under budget.

    Args:
        tool_results: dict of tc_id → (content, tool_name, tool_use_id)
        conv_id:      Conversation ID for persistence directory.

    Returns:
        Updated tool_results dict (modified in place and returned).
    """
    total_chars = sum(
        len(content) for content, _, _ in tool_results.values()
        if isinstance(content, str)
    )

    if total_chars <= MAX_ROUND_TOOL_RESULTS_CHARS:
        return tool_results

    logger.info('[AggregateBudget] Round total %s exceeds budget %s, '
                'persisting largest results',
                _human_size(total_chars),
                _human_size(MAX_ROUND_TOOL_RESULTS_CHARS))

    # Sort by size descending, persist largest non-exempt results first
    candidates = [
        (tc_id, content, tool_name, tool_use_id)
        for tc_id, (content, tool_name, tool_use_id) in tool_results.items()
        if isinstance(content, str)
        and tool_name not in _BUDGET_EXEMPT_TOOLS
        and not content.startswith('[Persisted to:')  # already persisted
    ]
    candidates.sort(key=lambda x: len(x[1]), reverse=True)

    for tc_id, content, tool_name, tool_use_id in candidates:
        if total_chars <= MAX_ROUND_TOOL_RESULTS_CHARS:
            break
        persisted = _persist_to_disk(content, tool_name, tool_use_id, conv_id)
        saved = len(content) - len(persisted)
        total_chars -= saved
        tool_results[tc_id] = (persisted, tool_name, tool_use_id)
        logger.info('[AggregateBudget] Persisted %s result (%s saved), '
                    'new total %s',
                    tool_name, _human_size(saved), _human_size(total_chars))

    return tool_results


def mark_empty_result(tool_name: str, content: str) -> str:
    """Replace empty/whitespace-only tool results with a descriptive marker.

    Inspired by Claude Code's empty result handling which prevents models
    from misinterpreting empty results as conversation end.

    Args:
        tool_name: Name of the tool.
        content:   Tool result content.

    Returns:
        Original content if non-empty, or a marker string.
    """
    if isinstance(content, str) and not content.strip():
        return f'({tool_name} completed with no output)'
    return content


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 1 — Micro-compaction (extended)
#  Now processes ALL message types, not just tool results:
#    - Strips old thinking/reasoning_content from cold assistant messages
#    - Compresses cold tool results (original behaviour)
#  Inspired by Claude Code's microcompact which edits all message types.
# ═══════════════════════════════════════════════════════════════════════════════

# How many recent assistant messages keep their thinking blocks intact.
_THINKING_HOT_TAIL = 4


def micro_compact(messages: list, conv_id: str = '') -> int:
    """Compress cold tool results AND strip old thinking blocks.

    Extended beyond the original tool-result-only compaction:
      1. Strip reasoning_content from cold assistant messages (saves huge
         amounts — thinking blocks can be 10K+ chars each).
      2. Compress cold tool results outside the hot tail (original behaviour).

    Cache-aware: if prompt cache is active (tracked by cache_tracking.py),
    messages in the cache prefix are left byte-identical to avoid
    invalidating the cache.  Inspired by Claude Code's microcompact which
    only edits messages OUTSIDE the cache prefix window.

    Args:
        messages: The live messages list.  Mutated in place.
        conv_id:  Conversation ID for logging.

    Returns:
        Estimated number of tokens saved.
    """
    tokens_saved = 0

    # ── Cache-aware: determine which messages are in the cache prefix ──
    # Messages in the cache prefix are skipped to maintain byte-identical
    # content for prompt cache stability.
    _cache_prefix_count = 0
    if conv_id:
        try:
            from lib.tasks_pkg.cache_tracking import get_cache_prefix_count
            _cache_prefix_count = get_cache_prefix_count(conv_id)
        except Exception as e:
            logger.debug('[Compaction] cache_tracking not available: %s', e)

    # ── Phase A: Strip old thinking/reasoning_content ──────────────────
    # Keep only the N most recent assistant messages' thinking intact.
    assistant_indices = [
        i for i, m in enumerate(messages)
        if m.get('role') == 'assistant' and m.get('reasoning_content')
    ]
    thinking_stripped = 0
    if len(assistant_indices) > _THINKING_HOT_TAIL:
        cold_thinking = assistant_indices[:-_THINKING_HOT_TAIL]
        for idx in cold_thinking:
            # Cache-aware: skip messages in the cache prefix
            if idx < _cache_prefix_count:
                continue
            msg = messages[idx]
            rc = msg.get('reasoning_content', '')
            if not rc:
                continue
            rc_len = len(rc) if isinstance(rc, str) else 0
            if rc_len > 0:
                tokens_saved += rc_len // 4
                msg['reasoning_content'] = ''
                thinking_stripped += 1

    if thinking_stripped > 0:
        logger.info('[L1-think] conv=%s  stripped reasoning_content from %d '
                    'cold assistant messages (~%d tokens saved)',
                    conv_id[:8] if conv_id else '?',
                    thinking_stripped, tokens_saved)

    # ── Phase B: Compress cold tool results (original logic) ──────────
    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']

    if len(tool_indices) <= MICRO_HOT_TAIL:
        logger.debug('[L1] %d tool results ≤ hot-tail size %d, nothing to do',
                     len(tool_indices), MICRO_HOT_TAIL)
        return tokens_saved

    cold_indices = tool_indices[:-MICRO_HOT_TAIL]
    compacted_count = 0
    skipped_short = 0
    skipped_already = 0
    tool_tokens_saved = 0

    for idx in cold_indices:
        # Cache-aware: skip messages in the cache prefix
        if idx < _cache_prefix_count:
            skipped_already += 1
            continue

        msg = messages[idx]
        content = msg.get('content', '')
        tool_name = msg.get('name', 'tool')

        # ── Handle multimodal content (list of content blocks) ──
        if isinstance(content, list):
            text_parts = [
                b.get('text', '')
                for b in content
                if isinstance(b, dict) and b.get('type') == 'text'
            ]
            text_len = sum(len(t) for t in text_parts)
            if text_len <= MICRO_COMPACT_THRESHOLD:
                skipped_short += 1
                continue

            msg['content'] = (
                f'[{tool_name} result compacted — was {text_len:,} chars'
                f' — re-call tool if full content needed]'
            )
            tool_tokens_saved += text_len // 4
            compacted_count += 1
            continue

        # ── Handle plain-string content ──
        if not isinstance(content, str):
            continue

        if content.startswith('[') and 'compacted' in content[:80]:
            skipped_already += 1
            continue

        # Skip persisted-output markers (already compressed)
        if content.startswith('[Persisted to:'):
            skipped_already += 1
            continue

        if len(content) <= MICRO_COMPACT_THRESHOLD:
            skipped_short += 1
            continue

        old_len = len(content)
        first_two = '\n'.join(content.split('\n')[:2])
        if len(first_two) > 120:
            first_two = first_two[:120] + '…'

        placeholder = (
            f'[{tool_name} result compacted — was {old_len:,} chars]\n'
            f'Preview: {first_two}\n'
            f'[Re-call tool if full content needed]'
        )
        msg['content'] = placeholder
        tool_tokens_saved += (old_len - len(placeholder)) // 4
        compacted_count += 1

    tokens_saved += tool_tokens_saved

    logger.info('[L1] conv=%s  cold=%d  compacted=%d  '
                'skipped_short=%d  skipped_already=%d  '
                'thinking_stripped=%d  ~%d tokens saved',
                conv_id[:8] if conv_id else '?',
                len(cold_indices), compacted_count,
                skipped_short, skipped_already,
                thinking_stripped, tokens_saved)

    return tokens_saved


# ═══════════════════════════════════════════════════════════════════════════════
#  Token estimation helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _estimate_msg_tokens(msg: dict) -> int:
    """Rough token estimate for a single message (1 token ≈ 4 chars)."""
    chars = 0
    for field in ('content', 'reasoning_content'):
        val = msg.get(field)
        if not val:
            continue
        if isinstance(val, str):
            chars += len(val)
        elif isinstance(val, list):
            for block in val:
                if isinstance(block, dict):
                    if block.get('type') == 'text':
                        chars += len(block.get('text', ''))
                    elif block.get('type') == 'image_url':
                        chars += len(str(block.get('image_url', {})
                                         .get('url', '')))
    for tc in msg.get('tool_calls', []):
        chars += len(tc.get('function', {}).get('arguments', ''))
    return chars // 4


def _estimate_total_tokens(messages: list) -> int:
    """Sum token estimates across all messages."""
    return sum(_estimate_msg_tokens(m) for m in messages)


def _human_size(byte_count: int) -> str:
    """Format a byte/char count as a human-readable string."""
    if byte_count < 1024:
        return f'{byte_count}B'
    elif byte_count < 1024 * 1024:
        return f'{byte_count / 1024:.1f}KB'
    else:
        return f'{byte_count / (1024 * 1024):.1f}MB'


# ═══════════════════════════════════════════════════════════════════════════════
#  Context limit helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_context_limit(task: dict | None = None) -> int:
    """Look up the model's context window size in tokens."""
    if task:
        model = task.get('config', {}).get('model', '').lower()
        limits = {
            # Claude 4.6 (Opus/Sonnet): 1M context GA since 2026-03-13
            'claude-opus-4.6':   1_000_000,
            'claude-sonnet-4.6': 1_000_000,
            'claude':   200_000,   # older Claude models fallback
            'gpt-4':    128_000,
            'gpt-4o':   128_000,
            'o1':       200_000,
            'o3':       200_000,
            'o4':       200_000,
            'gemini':   1_000_000,
            'qwen':     128_000,
            'deepseek': 128_000,
            'doubao':   128_000,
            'minimax':  1_000_000,
        }
        for key, limit in limits.items():
            if key in model:
                return limit
    return _DEFAULT_CONTEXT_LIMIT


def _should_force_compact(messages: list, task: dict | None = None) -> bool:
    """Decide whether force-compact should fire.

    Returns True when estimated token count exceeds
    ``_SUMMARY_TRIGGER_RATIO`` of usable context.
    """
    conv_id = task.get('convId', '') if task else ''
    log_id = conv_id[:8] if conv_id else '?'

    # Check cooldown
    with _cooldown_lock:
        last = _summary_cooldowns.get(conv_id, 0)
        elapsed = time.time() - last
        if elapsed < _SUMMARY_COOLDOWN:
            logger.debug('[Compact] conv=%s  cooldown active (%.0fs remaining)',
                         log_id, _SUMMARY_COOLDOWN - elapsed)
            return False

    context_limit = _get_context_limit(task)
    usable = context_limit - _OUTPUT_RESERVE - _COMPACTION_RESERVE
    trigger_threshold = int(usable * _SUMMARY_TRIGGER_RATIO)
    total_tokens = _estimate_total_tokens(messages)

    logger.debug('[Compact] conv=%s  tokens=%d  threshold=%d  '
                 'limit=%d  usable=%d',
                 log_id, total_tokens, trigger_threshold,
                 context_limit, usable)

    if total_tokens > trigger_threshold:
        logger.info('[Compact] Force-compact TRIGGERED  conv=%s  '
                    'tokens=%d > threshold=%d  '
                    '(limit=%d, usable=%d, ratio=%.0f%%)',
                    log_id, total_tokens, trigger_threshold,
                    context_limit, usable,
                    _SUMMARY_TRIGGER_RATIO * 100)
        return True

    return False


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


def _find_pair_boundary(messages: list, keep_recent: int | None = None) -> int:
    """Find the boundary for Phase 2 summarization.

    Preserves system messages + recent N user-assistant pairs.
    Returns the index where old messages end (everything before this
    will be summarized).

    Args:
        messages: Conversation messages.
        keep_recent: Override for _KEEP_RECENT_PAIRS (thread-safe).
                     Defaults to the module-level constant.
    """
    _keep = keep_recent if keep_recent is not None else _KEEP_RECENT_PAIRS
    # Count user-assistant pairs from the end
    pairs_found = 0
    boundary = len(messages)

    i = len(messages) - 1
    while i >= 0:
        msg = messages[i]
        role = msg.get('role')

        if role == 'user':
            pairs_found += 1
            if pairs_found >= _keep:
                boundary = i
                break
        i -= 1

    # Walk backward from boundary to avoid splitting tool rounds
    while 1 < boundary < len(messages) and messages[boundary].get('role') == 'tool':
        boundary -= 1

    # Also don't split an assistant with tool_calls from its results
    if 1 < boundary < len(messages) and messages[boundary - 1].get('role') == 'assistant' and messages[boundary - 1].get('tool_calls'):
        boundary -= 1

    # Ensure boundary is past system messages
    while boundary < len(messages) and messages[boundary].get('role') == 'system':
        boundary += 1

    return boundary


def _format_messages_for_summary(messages: list) -> str:
    """Render messages as readable text for the summary LLM."""
    parts = []

    for msg in messages:
        role = msg.get('role', '?')
        content = msg.get('content', '')

        if isinstance(content, list):
            content = '\n'.join(
                b.get('text', '') for b in content
                if isinstance(b, dict) and b.get('type') == 'text'
            )

        if isinstance(content, str) and len(content) > 3000:
            content = (content[:1500]
                       + '\n...[truncated]...\n'
                       + content[-1000:])

        tool_info = ''
        for tc in msg.get('tool_calls', []):
            fn = tc.get('function', {})
            fn_name = fn.get('name', '?')
            args_raw = fn.get('arguments', '')
            try:
                args = (json.loads(args_raw) if isinstance(args_raw, str)
                        else args_raw)
                if isinstance(args, dict):
                    brief = {}
                    for k, v in args.items():
                        vs = str(v)
                        brief[k] = (vs[:100] + f'...({len(vs)} chars)'
                                    if len(vs) > 200 else v)
                    tool_info += (
                        f'\n  → {fn_name}('
                        + json.dumps(brief, ensure_ascii=False)[:300]
                        + ')')
                else:
                    tool_info += f'\n  → {fn_name}()'
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug('[Compact] Failed to parse tool_call args for %s: %s',
                             fn_name, exc, exc_info=True)
                tool_info += f'\n  → {fn_name}()'

        line = f'[{role}] {content}'
        if tool_info:
            line += tool_info
        parts.append(line)

    return '\n\n'.join(parts)


def _generate_query_aware_summary(messages: list, current_query: str,
                                   log_prefix: str = '',
                                   conv_id: str = '') -> str | None:
    """Call a cheap model to generate a query-aware summary.

    The current user query is provided so the summary prioritizes
    information relevant to what the user is currently working on.
    """
    from lib.llm_dispatch import dispatch_chat

    formatted = _format_messages_for_summary(messages)
    tag = f'{log_prefix}[Summary]' if log_prefix else '[Summary]'

    logger.info('%s Formatting %d messages for summary (%s), query=%.80s',
                tag, len(messages), _human_size(len(formatted)), current_query)

    # Cap input
    if len(formatted) > 200_000:
        original_len = len(formatted)
        formatted = (
            formatted[:50_000]
            + '\n\n... [middle of conversation omitted for summary] ...\n\n'
            + formatted[-100_000:]
        )
        logger.info('%s Input truncated: %s → %s',
                    tag, _human_size(original_len), _human_size(len(formatted)))

    # ★ Session memory as compact seed: if session memory notes exist,
    #   include them so the summary model preserves key decisions.
    #   Inspired by Claude Code's sessionMemoryCompact.ts which uses
    #   session memory as the compaction summary source.
    _session_seed = ''
    if conv_id:
        try:
            from lib.tasks_pkg.session_memory import get_session_memory_for_compact
            _seed = get_session_memory_for_compact(conv_id)
            if _seed:
                _session_seed = (
                    f'\n\n## Existing Session Notes (incorporate into summary)\n\n'
                    f'{_seed}\n\n'
                )
        except Exception as e:
            logger.debug('[Compaction] session seed extraction failed: %s', e)

    user_content = (
        f'## Current User Query\n{current_query}\n\n'
        f'{_session_seed}'
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
            # Strip the <analysis> scratchpad if the model included it
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
        logger.error('%s Summary generation failed: %s', tag, e, exc_info=True)
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
                               'write_file', 'apply_diff'):
                continue

            try:
                args = json.loads(fn.get('arguments', '{}'))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug('[Compaction] Skipping unparseable tool_call args for %s: %s',
                             fn_name, exc, exc_info=True)
                continue

            if fn_name == 'read_files':
                for spec in args.get('reads', []):
                    p = spec.get('path', '')
                    if p and p not in files_set:
                        files_seen.append(p)
                        files_set.add(p)
            elif fn_name == 'apply_diff' and args.get('edits'):
                # Batch apply_diff: paths are inside edits[i].path
                for edit in args['edits']:
                    if isinstance(edit, dict):
                        p = edit.get('path', '')
                        if p and p not in files_set:
                            files_seen.append(p)
                            files_set.add(p)
            else:
                p = args.get('path', '')
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

    Keyword Args:
        keep_recent_pairs: Override for _KEEP_RECENT_PAIRS (thread-safe,
            used by reactive_compact to request more aggressive compaction).

    Pure LLM summary approach:
      - Finds a boundary (system msgs + last N user-assistant pairs preserved)
      - Sends everything before the boundary to a cheap model
      - The cheap model rates each turn's relevance to the current query
        and selectively compresses/drops turns accordingly
      - Old messages are replaced with system msgs + recent msgs
      - The summary becomes the tool result in a synthetic tool pair

    Args:
        messages: Live messages list — mutated in place.
        task: Task dict for context (conv_id, model, etc.).

    Returns:
        A string to be used as the tool result content, containing
        the selective summary of old conversation history.
    """
    conv_id = task.get('convId', '') if task else ''
    log_id = conv_id[:8] if conv_id else '?'
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    # Record cooldown
    with _cooldown_lock:
        _summary_cooldowns[conv_id] = time.time()

    tokens_before = _estimate_total_tokens(messages)
    msg_count_before = len(messages)
    context_limit = _get_context_limit(task)
    usable = context_limit - _OUTPUT_RESERVE

    logger.info('%s [Compact] Starting  conv=%s  tokens=%d  usable=%d  messages=%d',
                pfx, log_id, tokens_before, usable, msg_count_before)

    # Archive full transcript first (safety net for recovery)
    _archive_transcript(conv_id, messages)

    # Extract current query for query-aware summarization
    current_query = _extract_current_query(messages)

    # Find boundary: preserve system msgs + recent N user-assistant pairs
    # keep_recent_pairs is threaded through from reactive_compact for
    # thread-safe override of the default _KEEP_RECENT_PAIRS.
    _krp = kwargs.get('keep_recent_pairs') if kwargs else None
    boundary = _find_pair_boundary(messages, keep_recent=_krp)

    if boundary <= 1:
        logger.info('%s [Compact] Boundary=%d too early — not enough old messages '
                    'to summarize, skipping', pfx, boundary)
        return ('Context compaction skipped — not enough historical messages '
                'to summarize. Only recent messages exist.')

    old_messages = messages[:boundary]
    recent_messages = messages[boundary:]

    logger.info('%s [Compact] Summarizing %d old messages, '
                'preserving %d recent, query=%.100s',
                pfx, len(old_messages), len(recent_messages), current_query)

    # Generate query-aware selective summary via cheap model
    summary_text = _generate_query_aware_summary(
        old_messages, current_query, pfx, conv_id=conv_id
    )

    if not summary_text:
        logger.warning('%s [Compact] Summary generation failed — keeping messages intact', pfx)
        return ('Context compaction attempted but summary generation failed. '
                'Messages preserved as-is.')

    # Add recently-accessed file hints
    recent_files = _extract_recently_accessed_files(messages)
    if recent_files:
        file_list = '\n'.join(f'  - {f}' for f in recent_files)
        summary_text += (
            f'\n\n### Recently Accessed Files\n'
            f'Use read_files to review current state if needed:\n'
            f'{file_list}'
        )

    # Rebuild messages: system msgs + recent (old msgs replaced by summary)
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

    # Build the tool result — this is what the model sees as the
    # context_compact tool response
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
                            keep_recent_pairs: int | None = None) -> bool:
    """Check token usage and force-inject a context_compact tool round if needed.

    This replaces the old ``smart_summary_compact()`` — instead of
    silently replacing messages, it injects a proper tool_call + tool_result
    pair so the model sees the compaction results naturally.

    Called from the orchestrator before each LLM call.

    Args:
        keep_recent_pairs: Override for _KEEP_RECENT_PAIRS (thread-safe).
            Used by reactive_compact to request more aggressive compaction
            without mutating module-level state.

    Returns True if compaction was performed, False otherwise.
    """
    if not _should_force_compact(messages, task):
        return False

    conv_id = task.get('convId', '') if task else ''
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    logger.info('%s [ForceCompact] Injecting context_compact for conv=%s',
                pfx, conv_id[:8] if conv_id else '?')

    # Execute the compact logic, passing through keep_recent_pairs
    compact_result = execute_compact_tool(
        messages, task=task, keep_recent_pairs=keep_recent_pairs)

    # Inject the tool_call + tool_result pair at the end of messages
    # so the model sees it as a normal tool round.
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Legacy entry points (kept for backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════════

def smart_summary_compact(messages: list, task: dict | None = None):
    """Legacy entry point — now delegates to force_compact_if_needed."""
    force_compact_if_needed(messages, task=task)


# ═══════════════════════════════════════════════════════════════════════════════
#  Reactive compact — emergency compaction on API context-length rejection
#  Inspired by Claude Code's reactive compact: when the API returns 400 with
#  a "prompt too long" error, automatically compact and retry.
# ═══════════════════════════════════════════════════════════════════════════════

def reactive_compact(messages: list, task: dict | None = None) -> bool:
    """Emergency compaction triggered when the API rejects a request as too long.

    Unlike force_compact_if_needed, this:
      1. Ignores the cooldown timer (we MUST compact NOW)
      2. Increases _KEEP_RECENT_PAIRS to 2 (more aggressive)
      3. Runs micro_compact first to squeeze out maximum space

    Called from the orchestrator when a 400/prompt_too_long error is received.

    Returns True if compaction was performed, False otherwise.
    """
    conv_id = task.get('convId', '') if task else ''
    task_id = task.get('id', '')[:8] if task else '?'
    pfx = f'[Task {task_id}]'

    logger.warning('%s [ReactiveCompact] Emergency compaction triggered for conv=%s '
                   '(API rejected request as too long)',
                   pfx, conv_id[:8] if conv_id else '?')

    # Phase 1: Aggressive micro-compact
    micro_compact(messages, conv_id=conv_id)

    # Phase 2: Force-reset the cooldown so compaction can fire
    with _cooldown_lock:
        _summary_cooldowns.pop(conv_id, None)

    # Phase 3: Force compact with reduced recent-pairs (thread-safe via parameter)
    compacted = force_compact_if_needed(
        messages, task=task, keep_recent_pairs=2)

    if not compacted:
        # Even force_compact didn't think it was needed — try head truncation
        logger.warning('%s [ReactiveCompact] Force compact did not trigger — '
                       'attempting head truncation', pfx)
        _head_truncate(messages, task)
        compacted = True

    tokens_after = _estimate_total_tokens(messages)
    logger.info('%s [ReactiveCompact] Complete — %d messages, ~%d tokens remaining',
                pfx, len(messages), tokens_after)

    return compacted


def _head_truncate(messages: list, task: dict | None = None):
    """Last-resort head truncation: drop the oldest non-system messages.

    Only called when reactive_compact's force_compact fails to trigger.
    Drops messages from the front until we're under 60% of context.
    """
    context_limit = _get_context_limit(task)
    target = int(context_limit * 0.60)

    # Preserve system messages
    system_end = 0
    for i, msg in enumerate(messages):
        if msg.get('role') == 'system':
            system_end = i + 1
        else:
            break

    # Drop oldest non-system messages
    dropped = 0
    while _estimate_total_tokens(messages) > target and len(messages) > system_end + 4:
        messages.pop(system_end)
        dropped += 1

    if dropped:
        logger.warning('[HeadTruncate] Dropped %d oldest messages to fit context '
                       '(tokens now ~%d, target %d)',
                       dropped, _estimate_total_tokens(messages), target)


# ═══════════════════════════════════════════════════════════════════════════════
#  Post-compact context re-injection
#  Inspired by Claude Code: after compaction replaces old messages, the system
#  context (project context, skills, swarm prompt) is re-injected to ensure
#  the model doesn't lose critical instructions.
# ═══════════════════════════════════════════════════════════════════════════════

def _reinject_system_contexts_after_compact(messages: list, task: dict | None = None):
    """Re-inject system contexts after compaction.

    After force_compact replaces old messages, the system message may have
    been rebuilt from only the archived system messages.  This ensures
    project context, skills, and swarm prompts are still present.

    Only runs if the task has the necessary config to re-inject.
    """
    if not task:
        return

    cfg = task.get('config', {})
    project_path = cfg.get('projectPath', '')
    project_enabled = bool(project_path)
    skills_enabled = cfg.get('skillsEnabled', True)
    search_enabled = cfg.get('searchMode', '') in ('single', 'multi')
    swarm_enabled = cfg.get('swarmEnabled', False)

    # Check if system contexts are already present (avoid double-injection)
    if messages and messages[0].get('role') == 'system':
        sys_content = messages[0].get('content', '')
        if isinstance(sys_content, list):
            sys_text = ''.join(
                b.get('text', '') for b in sys_content
                if isinstance(b, dict) and b.get('type') == 'text'
            )
        else:
            sys_text = sys_content or ''

        # If project context is expected but missing, re-inject
        if project_enabled and '[PROJECT CO-PILOT MODE]' not in sys_text:
            from lib.tasks_pkg.system_context import _inject_system_contexts
            # Re-inject from scratch — the system_context module handles dedup
            _inject_system_contexts(
                messages, project_path, project_enabled,
                skills_enabled, search_enabled, swarm_enabled,
                has_real_tools=True,
            )
            logger.info('[PostCompact] Re-injected system contexts after compaction')


# ═══════════════════════════════════════════════════════════════════════════════
#  Pipeline entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run_compaction_pipeline(messages: list, current_round: int,
                            task: dict | None = None):
    """Run the compaction pipeline.

    Called from the orchestrator before each LLM API call.

    Layer 0 (budget_tool_result):
        Applied at tool-result entry time (in tool_dispatch.py).
        Truncates oversized results immediately.  Zero LLM cost.

    Layer 1 (micro_compact):
        Archives and compacts cold tool results every round.
        Also strips old thinking/reasoning_content.
        Zero LLM cost.  Runs unconditionally.

    Force compact (force_compact_if_needed):
        Fires only when estimated tokens approach the context limit.
        Injects a context_compact tool_call/result pair.
        After compaction, re-injects system contexts if needed.

    Layer 3 (reactive_compact):
        Emergency compaction — called from orchestrator on API 400
        prompt_too_long errors.  Not called here (called from
        llm_fallback.py on error).
    """
    conv_id = task.get('convId', '') if task else ''

    logger.debug('[Pipeline] round=%d  conv=%s  messages=%d',
                 current_round, conv_id[:8] if conv_id else '?',
                 len(messages))

    # Layer 1: compact cold tool results + strip old thinking
    saved = micro_compact(messages, conv_id=conv_id)

    if saved > 0:
        logger.debug('[Pipeline] L1 saved ~%d tokens, now %d messages',
                     saved, len(messages))

    # Force compact if context near capacity
    compacted = force_compact_if_needed(messages, task=task)

    # Post-compact: re-inject system contexts if compaction dropped them
    if compacted:
        _reinject_system_contexts_after_compact(messages, task=task)

    # Notify cache tracker that compaction occurred so the expected
    # cache_read token drop isn't flagged as a cache break.
    if (saved > 0 or compacted) and conv_id:
        try:
            from lib.tasks_pkg.cache_tracking import notify_compaction
            notify_compaction(conv_id)
        except Exception as e:
            logger.debug('[Pipeline] notify_compaction failed: %s', e)
