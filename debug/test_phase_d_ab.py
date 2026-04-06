#!/usr/bin/env python3
"""A/B test: Phase D assistant content compaction — real API validation.

Tests whether compacting cold assistant message content reduces total tokens
sent per round, thereby reducing cache write costs.

ARM A (BASELINE): micro_compact with Phase D disabled (monkey-patched out)
ARM B (PHASE_D):  micro_compact with Phase D enabled (production behavior)

The test builds a realistic multi-round conversation with tool calls,
then measures the actual token usage from the API for each arm.

Key metric: total prompt + cache_write tokens across rounds.
Phase D should reduce this by compacting verbose assistant responses
from early rounds that are outside the hot tail.

Usage:
    python debug/test_phase_d_ab.py                    # Live test
    python debug/test_phase_d_ab.py --dry-run           # Validate logic only
    python debug/test_phase_d_ab.py --model aws.claude-sonnet-4.6  # Use Sonnet
    python debug/test_phase_d_ab.py --rounds 6          # Fewer rounds
"""

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger
from lib.llm_client import build_body, stream_chat
from lib.model_info import is_claude

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-sonnet-4.6'  # Sonnet: cheaper, 1024 min cache
DEFAULT_ROUNDS = 6

# Realistic system prompt (~5000 tokens to exceed Opus 4096 minimum)
SYSTEM_PROMPT = """You are an AI coding assistant called Tofu. You help users with programming tasks.

## Core Rules
1. Always write clean, well-documented code.
2. Follow the project's coding conventions strictly.
3. Test your changes before suggesting them to the user.
4. Use the project tools to explore and modify code.
5. Never modify files without reading them first.
6. When making multiple edits, prefer batch apply_diff.
7. Read WIDE, not narrow — read 200+ lines in one shot.

## Architecture
- Flask Blueprint registration: All routes in routes/*.py as Blueprints.
- Task lifecycle (SSE streaming): Client POSTs /api/chat/start → background thread.
- LLM client: lib/llm_client.py → build_body(), stream_chat().
- Tool execution: lib/tools.py → lib/tasks_pkg/executor.py.

## Error Handling
```python
try:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
except requests.Timeout:
    logger.warning('[Fetch] Timeout: %s', url)
    return ''
except requests.RequestException as e:
    logger.warning('[Fetch] Failed %s: %s', url, e)
    return ''
```

## Logging Discipline
Every code path that can fail MUST leave a trace. Silent failures are the enemy.
Use %-style formatting: logger.info('x=%s', x). Truncate large data: %.500s.
Include context: conv_id, task_id, model name. Structured prefix: [Module].

## Key Files
| Need to… | Look at… |
|---|---|
| Change LLM behavior | lib/llm_client.py, lib/llm_dispatch.py |
| Add a new tool | lib/tools.py → lib/tasks_pkg/executor.py |
| Add API endpoint | routes/ → routes/__init__.py |
| Fix streaming | lib/llm_client.py → routes/chat.py |
| Debug task flow | lib/tasks_pkg/orchestrator.py |
| Project tools | lib/project_mod/tools.py |
| Skills | lib/skills.py |

## Environment
Python 3.10+, Flask, PostgreSQL 18+, multi-file logging.

## Prompt Caching Strategy
4 cache breakpoints for Claude (system, tools, conversation tail).
Mixed TTL: 1h for stable prefix (BP1-BP3), 5m for tail (BP4).
Cache-aware microcompact: skip editing messages in cached prefix.
Minimum cacheable: Opus/Haiku 4096 tokens, Sonnet 1024 tokens.

## Tool Usage Guidelines
- list_dir: Show files with line counts and sizes
- read_files: Read one or more files, max 20 per batch
- grep_search: Case-insensitive pattern search across files
- run_command: Execute shell command, return stdout+stderr
- apply_diff: Targeted search-and-replace edits
- write_file: Write/create files, supports content_ref
- web_search: Search the web with targeted queries
- fetch_url: Fetch full content of a URL
- find_files: Find files by glob pattern

## Code Style
Imports: stdlib → third-party → lib.* → routes.*
Logger init: from lib.log import get_logger; logger = get_logger(__name__)
Type hints on public functions, Google-style docstrings.
Constants: UPPER_SNAKE_CASE. Private: prefix with _.
"""

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List directory contents.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read one or more files.", "parameters": {"type": "object", "properties": {"reads": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}, "required": ["reads"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Search pattern across files.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute a shell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "apply_diff", "description": "Apply targeted edits.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}}, "required": ["path", "search", "replace"]}}},
]

# Realistic tool results of varying sizes
ASSISTANT_RESPONSES = [
    # Round 0: Initial analysis (long)
    (
        "Let me analyze the project structure first. I'll start by listing the root directory "
        "and then examining the key files you mentioned.\n\n"
        "Looking at the project, I can see it's a Flask-based application with:\n"
        "1. **Backend**: `lib/` contains the core business logic including LLM client, tools, "
        "task orchestration, and compaction\n"
        "2. **Routes**: `routes/` has Flask Blueprints for different API endpoints\n"
        "3. **Frontend**: `static/js/` has vanilla JS files\n"
        "4. **Tests**: `tests/` and `debug/` for testing\n\n"
        "The main areas of interest for cache optimization are:\n"
        "- `lib/tasks_pkg/cache_tracking.py` — cache break detection\n"
        "- `lib/llm_client.py` — cache breakpoint placement\n"
        "- `lib/tasks_pkg/compaction.py` — context compaction pipeline\n\n"
        "Let me read the cache tracking module first to understand the current implementation."
    ),
    # Round 1: Deep code analysis (very long)
    (
        "I've read through the cache tracking module. Here's what I found:\n\n"
        "## Cache Break Detection (detect_cache_break)\n\n"
        "The detection uses a two-phase approach:\n"
        "- **Phase 1 (pre-call)**: Hashes system prompt, tools, and message count to detect "
        "client-side changes that WOULD break the cache\n"
        "- **Phase 2 (post-call)**: Checks API-reported cache_read_tokens to confirm whether "
        "a break actually occurred\n\n"
        "Key design decisions:\n"
        "- Does NOT hash message content (avoids false positives from micro-compact mutations)\n"
        "- Only tracks system prompt, tools, model, and message count\n"
        "- Uses a minimum threshold (_MIN_CACHE_MISS_TOKENS = 2000) to ignore small drops\n\n"
        "## CacheState Class\n"
        "```python\n"
        "class CacheState:\n"
        "    system_hash: str = ''\n"
        "    tools_hash: str = ''\n"
        "    model: str = ''\n"
        "    message_count: int = 0\n"
        "    last_cache_read_tokens: int = 0\n"
        "    last_update_time: float = 0.0\n"
        "    call_count: int = 0\n"
        "    compaction_pending: bool = False\n"
        "```\n\n"
        "## Micro-Compact Cache Awareness\n"
        "The `get_cache_prefix_count()` function returns how many messages are in the cached "
        "prefix. Micro-compact skips these messages to keep cached content byte-identical.\n\n"
        "## Concurrent Conversation Tracking\n"
        "The `_count_active_on_model()` function tracks conversations sharing the same model "
        "within the last 60 seconds. When a cache break occurs with no client-side changes, "
        "it checks for contention from concurrent conversations.\n\n"
        "Now let me look at the breakpoint placement in llm_client.py."
    ),
    # Round 2: Implementation plan (medium)
    (
        "Based on my analysis, here's the implementation plan for the improvements:\n\n"
        "### Priority 1: Fix the elapsed time bug\n"
        "Line 208 sets `last_update_time = now` BEFORE the elapsed check. This means "
        "`elapsed` is always 0, making the TTL expiry detection dead code.\n\n"
        "### Priority 2: Add per-round cache stats at INFO\n"
        "Currently only logged at DEBUG. Moving to INFO gives production visibility.\n\n"
        "### Priority 3: Session-stable TTL latch\n"
        "Prevents mid-session settings changes from shifting the beta header.\n\n"
        "Let me start implementing these changes."
    ),
    # Round 3: Code changes (long with code)
    (
        "I've made the following changes:\n\n"
        "## 1. Fixed elapsed time bug (cache_tracking.py)\n"
        "```python\n"
        "# BEFORE (buggy):\n"
        "prev.last_update_time = now  # ← set first\n"
        "elapsed = now - prev.last_update_time  # ← always 0!\n\n"
        "# AFTER (fixed):\n"
        "elapsed = now - prev.last_update_time  # ← compute first\n"
        "prev.last_update_time = now  # ← then update\n"
        "```\n\n"
        "## 2. Added log_round_cache_stats()\n"
        "```python\n"
        "def log_round_cache_stats(conv_id, round_num, usage, model, tid=''):\n"
        "    cache_write = usage.get('cache_write_tokens', 0)\n"
        "    cache_read = usage.get('cache_read_tokens', 0)\n"
        "    prompt_tokens = usage.get('prompt_tokens', 0)\n"
        "    total_input = prompt_tokens + cache_write + cache_read\n"
        "    hit_pct = round(cache_read / max(total_input, 1) * 100)\n"
        "    logger.info('[CacheStats] conv=%s R%d cache_w=%d cache_r=%d hit=%d%%',\n"
        "               conv_id[:8], round_num + 1, cache_write, cache_read, hit_pct)\n"
        "```\n\n"
        "## 3. Added TTL latch\n"
        "```python\n"
        "_ttl_latch: dict[str, bool] = {}\n\n"
        "def latch_extended_ttl(task_id: str) -> bool:\n"
        "    with _ttl_latch_lock:\n"
        "        if task_id in _ttl_latch:\n"
        "            return _ttl_latch[task_id]\n"
        "        decision = getattr(_lib, 'CACHE_EXTENDED_TTL', False)\n"
        "        _ttl_latch[task_id] = decision\n"
        "        return decision\n"
        "```\n\n"
        "All tests pass. Let me now write the A/B test for these features."
    ),
    # Round 4: Test results discussion (medium)
    (
        "The A/B test results show that the TTL latch and tool ordering have no measurable "
        "benefit on Claude. The key discovery is that Anthropic's cache key is based solely "
        "on prompt prefix bytes — headers, TTL values, and tool result order don't affect it.\n\n"
        "This means:\n"
        "- TTL latch: neutral (doesn't prevent eviction since TTL doesn't change cache key)\n"
        "- Tool ordering: neutral on Claude (explicit breakpoints, not automatic prefix)\n"
        "- The diagnostic features (TTL detection fix, concurrent tracking, INFO stats) "
        "are the real winners from this optimization round."
    ),
    # Round 5-9: Follow-up rounds (varying lengths)
    "Now let me check if there are any remaining issues in the test suite.",
    (
        "I found one test failure in test_cc_alignment.py — it needs a `_disable_extended_ttl` "
        "fixture since CACHE_EXTENDED_TTL defaults to True. The fix is straightforward: add "
        "the same autouse fixture that test_cache_breakpoints.py uses.\n\n"
        "```python\n"
        "@pytest.fixture(autouse=True)\n"
        "def _disable_extended_ttl():\n"
        "    import lib as _lib\n"
        "    old = getattr(_lib, 'CACHE_EXTENDED_TTL', False)\n"
        "    _lib.CACHE_EXTENDED_TTL = False\n"
        "    yield\n"
        "    _lib.CACHE_EXTENDED_TTL = old\n"
        "```"
    ),
    "All 497 tests pass now. The implementation is complete.",
    (
        "Let me summarize the full set of changes:\n\n"
        "1. **TTL detection bug fix** — elapsed computed before update\n"
        "2. **Concurrent conversation tracking** — detects cache contention\n"
        "3. **Per-round cache stats at INFO** — production visibility\n"
        "4. **Session-stable TTL latch** — prevents mid-session changes\n"
        "5. **Tool result ordering** — deterministic prefix\n"
        "6. **Memory cleanup** — cleanup_cache_state(), release_ttl_latch()\n\n"
        "The A/B test confirmed that features 4 and 5 are neutral on Claude, "
        "while features 1-3 and 6 provide clear diagnostic and operational value."
    ),
    "Ready for the next optimization task. What would you like me to work on?",
]

TOOL_RESULTS = {
    'list_dir': (
        "Directory: .\n\nFiles:\n"
        "  server.py (245L, 8.2KB)\n  bootstrap.py (189L, 6.1KB)\n"
        "  export.py (1120L, 42.3KB)\n  CLAUDE.md (380L, 14.8KB)\n\n"
        "Subdirectories:\n"
        "  lib/ (42 items)\n  routes/ (18 items)\n  static/ (31 items)\n"
        "  debug/ (15 items)\n  tests/ (22 items)\n"
    ),
    'read_files': (
        "File: lib/tasks_pkg/cache_tracking.py (497 lines)\n"
        "────────────────────────────────────────\n"
        "1 │ # HOT_PATH — called every round.\n"
        '2 │ """Prompt Cache Break Detection."""\n'
        "3 │ from __future__ import annotations\n"
        "4 │ import hashlib, json, threading, time\n"
        "5 │ from lib.log import get_logger\n"
        "6 │ logger = get_logger(__name__)\n"
        "7 │ class CacheState:\n"
        "8 │     __slots__ = ('system_hash', 'tools_hash', 'model',\n"
        "9 │         'message_count', 'last_cache_read_tokens',\n"
        "10 │         'last_update_time', 'call_count', 'compaction_pending')\n"
        + "".join(f"{i} │ # line {i}\n" for i in range(11, 100))
    ),
    'grep_search': (
        'grep "detect_cache_break" — 8 matches:\n\n'
        'lib/tasks_pkg/cache_tracking.py:155:def detect_cache_break(\n'
        'lib/tasks_pkg/orchestrator.py:661:    cache_break = detect_cache_break(\n'
        'tests/test_cache_improvements.py:45:    result = detect_cache_break(\n'
    ),
    'run_command': "$ pytest tests/ -q\n572 passed in 37.84s\n",
    'apply_diff': "✅ Applied 1 edit to lib/tasks_pkg/cache_tracking.py\n",
}

TOOL_NAMES_PER_ROUND = [
    ['list_dir', 'read_files'],
    ['read_files', 'grep_search'],
    ['read_files'],
    ['apply_diff', 'run_command'],
    ['grep_search', 'read_files'],
    ['run_command'],
    ['read_files', 'grep_search'],
    ['apply_diff'],
    ['run_command'],
    ['read_files'],
]


# ═══════════════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RoundResult:
    round_num: int
    prompt_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    total_input: int = 0
    elapsed: float = 0.0
    msg_count: int = 0
    est_tokens_before_compact: int = 0
    est_tokens_after_compact: int = 0
    tokens_saved_by_compact: int = 0
    status: str = ''
    error: str = ''


@dataclass
class ArmResult:
    label: str
    model: str
    rounds: list = field(default_factory=list)

    @property
    def valid_rounds(self):
        return [r for r in self.rounds if not r.error]

    @property
    def total_prompt(self):
        return sum(r.prompt_tokens for r in self.valid_rounds)

    @property
    def total_cache_read(self):
        return sum(r.cache_read for r in self.valid_rounds)

    @property
    def total_cache_write(self):
        return sum(r.cache_write for r in self.valid_rounds)

    @property
    def total_output(self):
        return sum(r.output_tokens for r in self.valid_rounds)

    @property
    def total_input(self):
        return self.total_prompt + self.total_cache_read + self.total_cache_write

    @property
    def total_tokens_saved(self):
        return sum(r.tokens_saved_by_compact for r in self.valid_rounds)


# ═══════════════════════════════════════════════════════════════════════════════
#  Build realistic conversation
# ═══════════════════════════════════════════════════════════════════════════════

def _build_conversation(num_rounds: int, arm_seed: str = '') -> list:
    """Build a realistic multi-round agentic conversation.

    Returns a messages list with the specified number of rounds of
    user → assistant(tool_calls) → tool_results → user → ...
    """
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'Analyze the cache optimization system in this project. '
            'Start by reading the cache_tracking module, then the breakpoint '
            'placement code, and suggest improvements.'
        )},
    ]

    tc_counter = 0
    for round_idx in range(num_rounds):
        # Assistant response
        asst_content = ASSISTANT_RESPONSES[round_idx % len(ASSISTANT_RESPONSES)]
        tool_names = TOOL_NAMES_PER_ROUND[round_idx % len(TOOL_NAMES_PER_ROUND)]

        tool_calls = []
        for tn in tool_names:
            tc_id = f'call_{tc_counter:04d}'
            tool_calls.append({
                'id': tc_id,
                'type': 'function',
                'function': {'name': tn, 'arguments': '{}'},
            })
            tc_counter += 1

        messages.append({
            'role': 'assistant',
            'content': asst_content,
            'tool_calls': tool_calls,
        })

        # Tool results
        for tc in tool_calls:
            fn_name = tc['function']['name']
            messages.append({
                'role': 'tool',
                'tool_call_id': tc['id'],
                'name': fn_name,
                'content': TOOL_RESULTS.get(fn_name, f'Result for {fn_name}'),
            })

        # Follow-up user message
        if round_idx < num_rounds - 1:
            messages.append({
                'role': 'user',
                'content': f'Good, now continue with step {round_idx + 2}.',
            })

    return messages


# ═══════════════════════════════════════════════════════════════════════════════
#  Token estimation
# ═══════════════════════════════════════════════════════════════════════════════

def _estimate_tokens(messages: list) -> int:
    """Rough token estimate (1 token ≈ 4 chars)."""
    total = 0
    for msg in messages:
        for field in ('content', 'reasoning_content'):
            val = msg.get(field)
            if isinstance(val, str):
                total += len(val)
            elif isinstance(val, list):
                for blk in val:
                    if isinstance(blk, dict) and blk.get('type') == 'text':
                        total += len(blk.get('text', ''))
        for tc in msg.get('tool_calls', []):
            total += len(tc.get('function', {}).get('arguments', ''))
    return total // 4


# ═══════════════════════════════════════════════════════════════════════════════
#  Run one arm
# ═══════════════════════════════════════════════════════════════════════════════

def _run_arm(
    model: str,
    num_rounds: int,
    label: str,
    enable_phase_d: bool,
    dry_run: bool = False,
) -> ArmResult:
    """Run one arm of the A/B test.

    For each 'API round', we:
    1. Build the conversation up to that round
    2. Apply micro_compact (with or without Phase D)
    3. Send to the API and record cache stats
    """
    from lib.tasks_pkg.compaction import micro_compact

    arm_seed = f'\n\n<!-- arm={label} seed={time.time():.0f} -->'
    result = ArmResult(label=label, model=model)

    print(f"\n  {'═'*60}")
    print(f"  {label} — Phase D {'ENABLED' if enable_phase_d else 'DISABLED'}")
    print(f"  {'═'*60}")

    # Build the FULL conversation first (all rounds pre-populated)
    full_messages = _build_conversation(num_rounds, arm_seed=arm_seed)

    # Process each round incrementally
    # For each "API call round", we take the first N messages
    # and apply compaction, then send to the API
    round_boundaries = []
    msg_idx = 0
    for msg in full_messages:
        msg_idx += 1
        # A "round boundary" is right before a user message (except the first)
        # or at the end of the conversation
    # Find boundaries: after each set of tool results, before next user msg
    boundaries = [len(full_messages)]  # last round = full conversation
    for i in range(len(full_messages) - 1, 0, -1):
        if full_messages[i].get('role') == 'user' and i > 1:
            boundaries.append(i)
    boundaries = sorted(set(boundaries))

    # Take the last num_rounds boundaries
    if len(boundaries) > num_rounds:
        boundaries = boundaries[-num_rounds:]

    for round_idx, boundary in enumerate(boundaries):
        # Take messages up to this boundary
        messages = copy.deepcopy(full_messages[:boundary])

        # Estimate tokens before compaction
        est_before = _estimate_tokens(messages)

        # Apply micro_compact
        if enable_phase_d:
            tokens_saved = micro_compact(messages, conv_id=f'ab_{label}')
        else:
            # Disable Phase D by monkey-patching: save & restore
            # We'll do this by temporarily setting _ASSISTANT_HOT_TAIL very high
            import lib.tasks_pkg.compaction as _comp_mod
            # Run micro_compact but skip Phase D by making hot tail = 9999
            _orig_code = None  # We can't easily monkey-patch Phase D out...
            # Instead, run normal micro_compact but then UN-compact assistants
            tokens_saved = micro_compact(messages, conv_id=f'ab_{label}')
            # Undo Phase D effects: this is tricky. Better approach: use a flag.
            # Actually, the simplest approach: for ARM A, restore the original
            # assistant content from full_messages after micro_compact
            for i, msg in enumerate(messages):
                if (msg.get('role') == 'assistant'
                        and isinstance(msg.get('content'), str)
                        and msg['content'].startswith('[Assistant response compacted')):
                    # Restore original content
                    if i < len(full_messages):
                        messages[i] = copy.deepcopy(full_messages[i])

        est_after = _estimate_tokens(messages)

        print(f"\n  ── R{round_idx+1}/{len(boundaries)} ── "
              f"msgs={len(messages)} tokens={est_before}→{est_after} "
              f"(saved ~{est_before - est_after})")

        if dry_run:
            rr = RoundResult(
                round_num=round_idx + 1,
                msg_count=len(messages),
                est_tokens_before_compact=est_before,
                est_tokens_after_compact=est_after,
                tokens_saved_by_compact=est_before - est_after,
                prompt_tokens=max(1, est_after - 5000),  # simulate
                cache_read=min(5000 + round_idx * 3000, est_after),
                cache_write=max(500, 2000 - round_idx * 200),
                output_tokens=200,
                status='SIM',
            )
            rr.total_input = rr.prompt_tokens + rr.cache_read + rr.cache_write
            result.rounds.append(rr)
            continue

        # Build API body
        body = build_body(
            model, messages,
            max_tokens=512,   # short output to save cost
            temperature=1.0,
            thinking_enabled=True,
            preset='low',
            thinking_depth='low',
            tools=TOOLS,
            stream=True,
        )

        # Make API call with retry on 429
        t0 = time.time()
        assistant_msg = None
        finish_reason = ''
        usage = {}
        for _attempt in range(5):
            try:
                assistant_msg, finish_reason, usage = stream_chat(
                    body,
                    on_content=lambda _: None,
                    on_thinking=lambda _: None,
                    log_prefix=f'[{label} R{round_idx+1}]',
                )
                break  # success
            except Exception as e:
                if '429' in str(e):
                    wait = 15 * (_attempt + 1)
                    print(f"    ⏳ Rate limited, waiting {wait}s (attempt {_attempt+1}/5)...")
                    time.sleep(wait)
                    # Rebuild body since stream_chat may have consumed it
                    body = build_body(
                        model, messages,
                        max_tokens=512,
                        temperature=1.0,
                        thinking_enabled=True,
                        preset='low',
                        thinking_depth='low',
                        tools=TOOLS,
                        stream=True,
                    )
                    continue
                logger.warning('[AB] API error in %s R%d: %s', label, round_idx+1, e)
                result.rounds.append(RoundResult(
                    round_num=round_idx + 1, error=str(e)))
                break
        else:
            # All retries exhausted
            result.rounds.append(RoundResult(
                round_num=round_idx + 1, error='429 after 5 retries'))
            continue

        if assistant_msg is None:
            continue

        elapsed = time.time() - t0
        u = usage or {}

        cache_read = (u.get('cache_read_tokens')
                      or u.get('cache_read_input_tokens') or 0)
        cache_write = (u.get('cache_creation_input_tokens')
                       or u.get('cache_write_tokens') or 0)
        prompt_tokens = u.get('prompt_tokens', 0)
        output_tokens = u.get('completion_tokens', 0)

        if cache_write > 500 and cache_read > 500:
            status = "HIT+WRITE"
        elif cache_read > 500:
            status = "HIT"
        elif cache_write > 500:
            status = "WRITE"
        else:
            status = "MISS"

        rr = RoundResult(
            round_num=round_idx + 1,
            prompt_tokens=prompt_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            output_tokens=output_tokens,
            total_input=prompt_tokens + cache_read + cache_write,
            elapsed=elapsed,
            msg_count=len(messages),
            est_tokens_before_compact=est_before,
            est_tokens_after_compact=est_after,
            tokens_saved_by_compact=est_before - est_after,
            status=status,
        )
        result.rounds.append(rr)

        print(f"    ⏱ {elapsed:.1f}s | {status}")
        print(f"    📊 pt={prompt_tokens:,}  cr={cache_read:,}  "
              f"cw={cache_write:,}  out={output_tokens:,}")

        # Delay between rounds to avoid rate limiting
        if round_idx < len(boundaries) - 1:
            time.sleep(5)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Cost computation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_cost(cr: ArmResult, *, input_price=None, output_price=None,
                 cw_mul=1.25, cr_mul=0.10):
    """Compute cost. Default to Sonnet pricing."""
    # Auto-detect pricing
    if input_price is None:
        model = cr.model.lower()
        if 'opus' in model:
            input_price, output_price = 15.0, 75.0
        elif 'sonnet' in model:
            input_price, output_price = 3.0, 15.0
        else:
            input_price, output_price = 3.0, 15.0

    tp = cr.total_prompt
    tr = cr.total_cache_read
    tw = cr.total_cache_write
    to = cr.total_output
    ti = cr.total_input

    cost_prompt = tp * input_price / 1_000_000
    cost_read = tr * input_price * cr_mul / 1_000_000
    cost_write = tw * input_price * cw_mul / 1_000_000
    cost_output = to * output_price / 1_000_000
    total = cost_prompt + cost_read + cost_write + cost_output

    cost_no_cache = ti * input_price / 1_000_000 + cost_output
    savings = cost_no_cache - total
    savings_pct = savings / cost_no_cache * 100 if cost_no_cache > 0 else 0

    return {
        'total_prompt': tp, 'total_read': tr, 'total_write': tw,
        'total_output': to, 'total_input': ti,
        'cost_prompt': cost_prompt, 'cost_read': cost_read,
        'cost_write': cost_write, 'cost_output': cost_output,
        'total': total, 'cost_no_cache': cost_no_cache,
        'savings': savings, 'savings_pct': savings_pct,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Report
# ═══════════════════════════════════════════════════════════════════════════════

def print_comparison(arm_a: ArmResult, arm_b: ArmResult):
    """Print comparison report."""
    cost_a = compute_cost(arm_a)
    cost_b = compute_cost(arm_b)

    print(f"\n\n  {'▓'*60}")
    print(f"  Phase D Assistant Compaction — A/B COMPARISON")
    print(f"  {'▓'*60}")

    for arm in [arm_a, arm_b]:
        print(f"\n  ┌─ {arm.label} ─────────────────────────┐")
        print(f"  {'Rnd':>3} │ {'Msgs':>4} │ {'EstSaved':>8} │ {'Prompt':>8} │ "
              f"{'CacheRd':>8} │ {'CacheWr':>8} │ {'Output':>7} │ {'Status':>10}")
        print(f"  {'─'*3}─┼─{'─'*4}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*8}─┼─{'─'*8}─┼─"
              f"{'─'*7}─┼─{'─'*10}")
        for r in arm.rounds:
            if r.error:
                print(f"  {r.round_num:>3} │ {'ERROR':>4} │ {r.error[:50]}")
                continue
            print(f"  {r.round_num:>3} │ {r.msg_count:>4} │ {r.tokens_saved_by_compact:>8,} │ "
                  f"{r.prompt_tokens:>8,} │ {r.cache_read:>8,} │ {r.cache_write:>8,} │ "
                  f"{r.output_tokens:>7,} │ {r.status:>10}")

    print(f"\n  {'─'*60}")

    def _delta(old_v, new_v, lower_better=True):
        if old_v == 0:
            return "N/A"
        pct = (new_v - old_v) / old_v * 100
        better = pct < 0 if lower_better else pct > 0
        sym = "✅" if better else ("⚠️" if abs(pct) > 5 else "➖")
        return f"{pct:+.1f}% {sym}"

    print(f"\n  {'Metric':<35} │ {'BASELINE':>12} │ {'PHASE_D':>12} │ {'Delta':>12}")
    print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")

    metrics = [
        ("Est tokens saved by compact", arm_a.total_tokens_saved, arm_b.total_tokens_saved, False),
        ("Uncached prompt (tokens)", arm_a.total_prompt, arm_b.total_prompt, True),
        ("Cache reads (tokens)", arm_a.total_cache_read, arm_b.total_cache_read, False),
        ("Cache writes (tokens)", arm_a.total_cache_write, arm_b.total_cache_write, True),
        ("Output (tokens)", arm_a.total_output, arm_b.total_output, True),
    ]
    for name, a, b, lb in metrics:
        print(f"  {name:<35} │ {a:>12,} │ {b:>12,} │ {_delta(a, b, lb):>12}")

    print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")
    for name, a, b, lb in [
        ("TOTAL COST", cost_a['total'], cost_b['total'], True),
        ("Cache savings (%)", cost_a['savings_pct'], cost_b['savings_pct'], False),
    ]:
        if '%' in name:
            print(f"  {name:<35} │ {a:>11.1f}% │ {b:>11.1f}% │ {_delta(a, b, lb):>12}")
        else:
            print(f"  {name:<35} │ ${a:>11.4f} │ ${b:>11.4f} │ {_delta(a, b, lb):>12}")

    # Verdict
    diff = cost_a['total'] - cost_b['total']
    if abs(diff) < 0.0005:
        verdict = "NEUTRAL — no significant difference"
    elif diff > 0:
        verdict = f"PHASE_D saves ${diff:.4f} ({diff/max(cost_a['total'],0.0001)*100:.1f}%)"
    else:
        verdict = f"BASELINE is ${-diff:.4f} ({-diff/max(cost_b['total'],0.0001)*100:.1f}%) cheaper"

    print(f"\n  💰 {verdict}")

    # Phase D specific validation
    print(f"\n  📋 Phase D Validation:")
    if arm_b.total_tokens_saved > arm_a.total_tokens_saved:
        extra = arm_b.total_tokens_saved - arm_a.total_tokens_saved
        print(f"     ✅ Phase D saved {extra:,} MORE tokens than baseline")
    else:
        print(f"     ⚠️ Phase D did not save more tokens than baseline")

    if cost_b['total_write'] < cost_a['total_write']:
        pct = (cost_a['total_write'] - cost_b['total_write']) / max(cost_a['total_write'], 1) * 100
        print(f"     ✅ Phase D reduced cache writes by {pct:.1f}%")
    else:
        print(f"     ⚠️ Phase D did not reduce cache writes")

    return cost_a, cost_b


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='A/B test: Phase D assistant content compaction')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--wait', type=int, default=15)
    args = parser.parse_args()

    print(f"\n{'█'*70}")
    print(f"  PHASE D ASSISTANT COMPACTION — A/B TEST")
    print(f"  Model: {args.model}")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"  Rounds: {args.rounds}")
    print(f"  Dry run: {args.dry_run}")
    print(f"{'█'*70}")

    # ARM A: BASELINE (Phase D disabled)
    arm_a = _run_arm(
        args.model, args.rounds,
        'BASELINE', enable_phase_d=False, dry_run=args.dry_run)

    if not args.dry_run:
        print(f"\n  ⏳ Waiting {args.wait}s between arms...")
        time.sleep(args.wait)

    # ARM B: PHASE_D (Phase D enabled)
    arm_b = _run_arm(
        args.model, args.rounds,
        'PHASE_D', enable_phase_d=True, dry_run=args.dry_run)

    cost_a, cost_b = print_comparison(arm_a, arm_b)

    # Save results
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    output_path = f"debug/phase_d_ab_{timestamp}.json"
    try:
        output = {
            'arm_a': {
                'label': arm_a.label,
                'rounds': [r.__dict__ for r in arm_a.rounds],
                'cost': cost_a,
            },
            'arm_b': {
                'label': arm_b.label,
                'rounds': [r.__dict__ for r in arm_b.rounds],
                'cost': cost_b,
            },
        }
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n📁 Results saved to: {output_path}")
    except Exception as e:
        print(f"⚠️ Could not save: {e}")

    print(f"{'█'*70}\n")


if __name__ == '__main__':
    main()
