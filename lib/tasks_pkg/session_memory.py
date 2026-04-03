# HOT_PATH — called after each tool-heavy turn (background thread).
"""Session Memory Extraction — background extraction of session notes.

Inspired by Claude Code's ``SessionMemory/`` system.  After each tool-heavy
turn, a background thread runs a cheap LLM call to extract persistent session
notes.  These notes serve dual purposes:

  1. **Persistent context** — critical decisions, user preferences, file
     paths, and working state survive across compactions.
  2. **Compact summary source** — when force_compact fires, session memory
     notes can seed the summary instead of re-analyzing the full transcript
     (cheaper and faster).

The notes are stored per-conversation in the database as a JSON blob.

Architecture
------------
- Runs as a background thread after each tool-heavy turn.
- Uses a sequential lock to prevent concurrent extractions.
- Threshold-based: only extracts after sufficient new context accumulates.
- Uses ``dispatch_chat`` (cheap capability) for extraction.

Why this pattern is feasible in ChatUI
--------------------------------------
Unlike Claude Code's CacheSafeParams-based forked agent (which shares prompt
cache with the parent — impossible here because ChatUI uses multi-provider
dispatch through a proxy layer that doesn't guarantee cache sharing across
requests), we use a simple background ``dispatch_chat`` call with a focused
extraction prompt.  This is slightly less cache-efficient but architecturally
sound for a web server.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

# Minimum estimated tokens in conversation before first extraction
_MIN_TOKENS_TO_INIT = 15_000

# Minimum new tokens accumulated since last extraction before re-extracting
_MIN_TOKENS_BETWEEN_UPDATES = 10_000

# Minimum tool calls since last extraction
_MIN_TOOL_CALLS_BETWEEN_UPDATES = 5

# Max output tokens for the extraction LLM call
_EXTRACTION_MAX_TOKENS = 2000

# Max session memory size (chars) — prevents unbounded growth
_MAX_MEMORY_CHARS = 8_000


# ═══════════════════════════════════════════════════════════════════════════════
#  State tracking (per-conversation)
# ═══════════════════════════════════════════════════════════════════════════════

_extraction_state: dict[str, dict[str, Any]] = {}
"""Per-conv_id state: last_tokens, last_tool_calls, last_extraction_time"""

_extraction_lock = threading.Lock()
"""Sequential lock — only one extraction runs at a time across all conversations."""


# ═══════════════════════════════════════════════════════════════════════════════
#  Extraction prompt
# ═══════════════════════════════════════════════════════════════════════════════

_EXTRACTION_SYSTEM_PROMPT = """\
You are a session memory extractor for an AI coding assistant.

Your job is to extract KEY INFORMATION from the recent conversation that should \
be remembered across context compactions.  Focus on information that would be \
LOST if the conversation were summarized.

Extract and organize into these categories:

### User Preferences & Instructions
- Explicit style/approach preferences stated by the user
- "Always do X" / "Never do Y" type instructions
- Language preferences, naming conventions

### Working State
- Files currently being edited and their purpose
- Current branch/commit if mentioned
- Build/test status
- Environment details (paths, versions, configs)

### Key Decisions Made
- Architectural choices and WHY they were chosen
- Rejected alternatives and WHY they were rejected
- Trade-offs acknowledged

### Errors & Solutions
- Errors encountered and their root causes (exact error messages)
- Solutions that worked (with specifics)

### Pending Tasks
- What was promised but not yet done
- Known issues to address later
- Next steps discussed

Rules:
- Be CONCISE — aim for 500-1500 chars total
- Include EXACT file paths, function names, error messages
- Skip greetings, chitchat, and resolved topics
- If updating existing notes, MERGE new info (don't repeat)
- Output in the SAME LANGUAGE as the conversation
- Omit empty categories
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  Core logic
# ═══════════════════════════════════════════════════════════════════════════════

def _estimate_tokens(messages: list) -> int:
    """Quick token estimate (1 token ≈ 4 chars)."""
    chars = 0
    for msg in messages:
        for field in ('content', 'reasoning_content'):
            val = msg.get(field)
            if isinstance(val, str):
                chars += len(val)
            elif isinstance(val, list):
                for b in val:
                    if isinstance(b, dict) and b.get('type') == 'text':
                        chars += len(b.get('text', ''))
        for tc in msg.get('tool_calls', []):
            chars += len(tc.get('function', {}).get('arguments', ''))
    return chars // 4


def _count_tool_calls(messages: list) -> int:
    """Count total tool calls in messages."""
    return sum(
        len(msg.get('tool_calls', []))
        for msg in messages
    )


def should_extract_memory(conv_id: str, messages: list) -> bool:
    """Determine if session memory extraction should run.

    Threshold logic (inspired by Claude Code's SessionMemory config):
      - First extraction: after _MIN_TOKENS_TO_INIT tokens
      - Subsequent: after _MIN_TOKENS_BETWEEN_UPDATES new tokens
                    AND _MIN_TOOL_CALLS_BETWEEN_UPDATES new tool calls
    """
    if not conv_id or not messages:
        return False

    current_tokens = _estimate_tokens(messages)
    current_tool_calls = _count_tool_calls(messages)

    state = _extraction_state.get(conv_id)

    if state is None:
        # First extraction — need minimum tokens
        if current_tokens < _MIN_TOKENS_TO_INIT:
            return False
        return True

    # Subsequent — check both thresholds
    tokens_since = current_tokens - state.get('last_tokens', 0)
    tool_calls_since = current_tool_calls - state.get('last_tool_calls', 0)

    if tokens_since < _MIN_TOKENS_BETWEEN_UPDATES:
        return False

    if tool_calls_since < _MIN_TOOL_CALLS_BETWEEN_UPDATES:
        # Exception: if we have a LOT of new tokens but few tool calls
        # (e.g. long text generation), still extract
        if tokens_since < _MIN_TOKENS_BETWEEN_UPDATES * 2:
            return False

    return True


def _get_existing_memory(conv_id: str) -> str:
    """Load existing session memory from DB.

    Stored in conversations.settings JSONB under key 'session_memory'.
    This avoids schema migrations — the settings column already exists.
    """
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        cur = db.cursor()
        cur.execute(
            "SELECT settings FROM conversations WHERE id=%s AND user_id=1",
            (conv_id,)
        )
        row = cur.fetchone()
        if row and row[0]:
            settings = row[0] if isinstance(row[0], dict) else {}
            return settings.get('session_memory', '')
    except Exception as e:
        logger.debug('[SessionMemory] Failed to load existing memory for conv=%s: %s',
                     conv_id[:8], e)
    return ''


def _save_memory(conv_id: str, memory: str):
    """Save session memory to DB.

    Uses PostgreSQL jsonb_set to update settings.session_memory without
    overwriting other settings keys.
    """
    try:
        import json as _json

        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        cur = db.cursor()
        cur.execute(
            """UPDATE conversations
               SET settings = jsonb_set(
                   COALESCE(settings, '{}'::jsonb),
                   '{session_memory}',
                   %s::jsonb
               )
               WHERE id=%s AND user_id=1""",
            (_json.dumps(memory), conv_id)
        )
        db.commit()
        logger.info('[SessionMemory] Saved %d chars for conv=%s',
                    len(memory), conv_id[:8])
    except Exception as e:
        logger.warning('[SessionMemory] Failed to save memory for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)


def _format_recent_messages(messages: list, max_chars: int = 50_000) -> str:
    """Format recent messages for the extraction prompt."""
    parts = []
    total = 0

    # Work backwards from most recent
    for msg in reversed(messages):
        role = msg.get('role', '?')
        if role == 'system':
            continue

        content = msg.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(
                b.get('text', '') for b in content
                if isinstance(b, dict) and b.get('type') == 'text'
            )

        # Truncate long individual messages
        if isinstance(content, str) and len(content) > 2000:
            content = content[:1000] + '\n...[truncated]...\n' + content[-500:]

        tool_info = ''
        for tc in msg.get('tool_calls', []):
            fn = tc.get('function', {})
            tool_info += f'\n  → {fn.get("name", "?")}()'

        line = f'[{role}] {content}{tool_info}'
        total += len(line)

        if total > max_chars:
            break

        parts.append(line)

    parts.reverse()
    return '\n\n'.join(parts)


def extract_session_memory(conv_id: str, messages: list):
    """Extract session memory from recent conversation.

    Called in a background thread after tool-heavy turns.
    Uses a sequential lock to prevent concurrent extractions.
    """
    if not _extraction_lock.acquire(blocking=False):
        logger.debug('[SessionMemory] Extraction already in progress, skipping')
        return

    try:
        existing = _get_existing_memory(conv_id)
        recent_text = _format_recent_messages(messages)

        if not recent_text:
            return

        user_content = ''
        if existing:
            user_content += (
                f'## Existing Session Notes (update/merge, don\'t repeat)\n\n'
                f'{existing}\n\n---\n\n'
            )
        user_content += (
            f'## Recent Conversation to Extract From\n\n{recent_text}'
        )

        from lib.llm_dispatch import dispatch_chat

        content, usage = dispatch_chat(
            [
                {'role': 'system', 'content': _EXTRACTION_SYSTEM_PROMPT},
                {'role': 'user', 'content': user_content},
            ],
            max_tokens=_EXTRACTION_MAX_TOKENS,
            temperature=0,
            capability='cheap',
            log_prefix=f'[SessionMemory:{conv_id[:8]}]',
        )

        if content and content.strip():
            # Truncate if too long
            if len(content) > _MAX_MEMORY_CHARS:
                content = content[:_MAX_MEMORY_CHARS] + '\n\n[...truncated]'

            _save_memory(conv_id, content.strip())

            # Update tracking state
            _extraction_state[conv_id] = {
                'last_tokens': _estimate_tokens(messages),
                'last_tool_calls': _count_tool_calls(messages),
                'last_extraction_time': time.time(),
            }

            in_tok = usage.get('prompt_tokens', 0)
            out_tok = usage.get('completion_tokens', 0)
            logger.info('[SessionMemory] Extracted %d chars for conv=%s '
                        '(in=%d out=%d tokens)',
                        len(content), conv_id[:8], in_tok, out_tok)
        else:
            logger.debug('[SessionMemory] Extraction returned empty for conv=%s',
                         conv_id[:8])

    except Exception as e:
        logger.warning('[SessionMemory] Extraction failed for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)
    finally:
        _extraction_lock.release()


def trigger_memory_extraction(conv_id: str, messages: list,
                               tool_call_happened: bool = False):
    """Check thresholds and trigger background memory extraction if needed.

    Called from the orchestrator after each turn completes.
    Only fires when tool calls happened (indicates substantive work).
    """
    if not tool_call_happened:
        return

    if not should_extract_memory(conv_id, messages):
        return

    logger.info('[SessionMemory] Triggering background extraction for conv=%s',
                conv_id[:8])

    thread = threading.Thread(
        target=extract_session_memory,
        args=(conv_id, list(messages)),  # copy to avoid mutation
        daemon=True,
        name=f'session-memory-{conv_id[:8]}',
    )
    thread.start()


def get_session_memory_for_compact(conv_id: str) -> str | None:
    """Get session memory notes for use as compact summary seed.

    Called by force_compact when generating a summary — if session memory
    exists, it's included as additional context for the summary LLM so
    key information isn't lost during compaction.
    """
    memory = _get_existing_memory(conv_id)
    if memory and memory.strip():
        logger.debug('[SessionMemory] Providing %d chars as compact seed for conv=%s',
                     len(memory), conv_id[:8])
        return memory
    return None


def get_session_memory_for_prompt(conv_id: str) -> str | None:
    """Get session memory notes for injection into the system prompt.

    Returns formatted session memory block for per-turn injection,
    or None if no memory exists.
    """
    memory = _get_existing_memory(conv_id)
    if not memory or not memory.strip():
        return None

    return (
        '\n\n<session-memory>\n'
        '## Session Memory (auto-extracted working notes)\n'
        'These notes were automatically extracted from earlier in this conversation. '
        'They capture key decisions, preferences, and working state.\n\n'
        f'{memory}\n'
        '</session-memory>'
    )


# No DB schema migration needed — session memory is stored in the
# existing conversations.settings JSONB column.
