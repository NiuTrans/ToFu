#!/usr/bin/env python3
"""A/B Test: Timestamp Placement for Cache Efficiency.

Tests 4 strategies for injecting the "current date/time" context into the
prompt, measuring real cache hit/miss and cost impact over multi-round
tool conversations.

Arms:
  A. USER_DATETIME — Current behavior: "Current date and time: 2026-04-06 15:45 UTC"
     injected into last user message on every round (strip+re-add).
  B. USER_ROUND0  — Inject into last user message on round 0 ONLY.
     Subsequent rounds keep the R0 timestamp (preserves cached prefix).
  C. SYS_DATE     — "Current date: 2026-04-06" in the system prompt tail.
     Date-only changes once per day. No user message modification.
  D. SYS_DATETIME — "Current date and time: 2026-04-06 15:45 UTC" in
     system prompt tail. More precise but changes every minute.

Each arm runs the SAME multi-round tool conversation.
Arms MUST use unique system prompt suffixes to prevent cross-arm cache sharing.

Test design:
  Phase 1: "Within-task" — 6 tool rounds in a single continuous conversation.
     Measures intra-task cache stability.
  Phase 2: "Cross-task" — Simulates a new task by resetting the timestamp
     (e.g. 5 min later) and adding a new user message while keeping history.
     Measures inter-task cache resilience (where old user messages are "clean").

Usage:
    python debug/test_timestamp_placement_ab.py                       # Full test
    python debug/test_timestamp_placement_ab.py --arms A,C            # Compare just two
    python debug/test_timestamp_placement_ab.py --dry-run             # Preview logic
    python debug/test_timestamp_placement_ab.py --rounds 4            # Quick test
    python debug/test_timestamp_placement_ab.py --model MODEL_ID      # Custom model
"""

import argparse
import copy
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_client import add_cache_breakpoints, build_body, stream_chat
from lib.llm_dispatch.api import dispatch_stream
from lib.model_info import is_claude

# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 6

# Base system prompt — MUST exceed 4096 tokens for Opus cache eligibility.
# Read CLAUDE.md at import time for a realistic, production-sized prompt.
# Each arm appends a unique suffix to prevent cross-arm cache sharing.
_CLAUDE_MD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'CLAUDE.md')
if os.path.isfile(_CLAUDE_MD_PATH):
    with open(_CLAUDE_MD_PATH, 'r', encoding='utf-8') as f:
        _CLAUDE_MD_CONTENT = f.read()
else:
    _CLAUDE_MD_CONTENT = ''

BASE_SYSTEM_PROMPT = """You are an AI coding assistant called Tofu (豆腐). You help users with programming tasks by using project tools to explore and modify code.

## Core Rules
1. Always write clean, well-documented code.
2. Follow the project's coding conventions strictly.
3. Test your changes before suggesting them to the user.
4. Use the project tools to explore and modify code — never guess file contents.
5. Never modify files without reading them first — always understand existing code.
6. When making multiple edits, prefer batch apply_diff over separate calls.
7. Read WIDE, not narrow — read 200+ lines in one shot for function/class context.
8. Prefer reading the WHOLE file for files under 500 lines.

## Project Context — Tofu Self-Hosted AI Assistant
This is a Python Flask web application with a vanilla JS frontend.
The project uses PostgreSQL for persistence and SSE for streaming.

### Architecture
- **Flask Blueprint registration**: All routes live in `routes/*.py` as Blueprints.
- **Task lifecycle (SSE streaming)**: POST /api/chat/start → background thread → SSE events → persist.
- **LLM client flow**: build_body() → stream_chat() with retry logic.
- **Tool execution**: Tools defined in lib/tools.py, executed in lib/tasks_pkg/executor.py.

### Error Handling Patterns
```python
try:
    resp = requests.get(url, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
except requests.Timeout:
    logger.warning('[Fetch] Timeout after %ds: %s', FETCH_TIMEOUT, url)
    return ''
except requests.RequestException as e:
    logger.warning('[Fetch] Request failed for %s: %s', url, e)
    return ''
```

```python
try:
    data = json.loads(raw)
except (json.JSONDecodeError, TypeError) as e:
    logger.warning('Invalid JSON (len=%d): %s — preview: %.200s', len(raw), e, raw)
    data = {}
```

### Logging Discipline
Every code path that can fail MUST leave a trace in the log file. Silent failures are the enemy.
- Every except block logs something (debug at minimum).
- Use %-style formatting for lazy evaluation: `logger.info('x=%s', x)`.
- Sanitize secrets: never log API keys, tokens, or full request bodies with credentials.
- Truncate large data: `logger.debug('Response preview: %.500s', body)`.

### Code Style & Conventions
- Imports: stdlib → third-party → lib.* → routes.*, blank line between groups.
- Logger init: from lib.log import get_logger; logger = get_logger(__name__)
- Type hints: encouraged on public functions; optional on internal helpers.
- Docstrings: Google-style on modules and public functions.
- Constants: UPPER_SNAKE_CASE at module level. Private helpers: prefix with _.

### File Modification Checklist
Before submitting any code change, verify:
- Logger present: File has from lib.log import get_logger; logger = get_logger(__name__).
- No silent catches: Every except block logs something (debug at minimum).
- Context in logs: Log messages include relevant IDs (conv_id, task_id, url, model).
- Tracebacks on errors: exc_info=True on logger.error() for unexpected exceptions.
- No f-strings in log calls: Use logger.info('x=%s', x) not logger.info(f'x={x}').
- Secrets not logged: API keys, tokens, passwords never appear in log output.
- Large data truncated: Use %.500s or [:500] to cap logged payloads.

### Output Guidelines
- Be concise and direct. Lead with the answer or action, not the reasoning.
- Show exact code with file paths and line numbers.
- Use apply_diff for small targeted edits, write_file for new files or major rewrites.
- Keep text output brief and direct. Skip filler words, preamble.
- Focus on decisions needing user input, status updates, errors/blockers.

### Additional Tool Guidance
When using tools, follow these patterns:
- list_dir: Use for initial project exploration. Shows files with line counts and sizes.
- read_files: Batch multiple paths into ONE call. Files under 40KB auto-expand.
- grep_search: Case-insensitive regex. Use short patterns.
- write_file: Creates the file if it doesn't exist. Overwrites the entire file.
- apply_diff: Search string must match EXACTLY including whitespace/indentation.
- run_command: Execute shell command, returns stdout+stderr. Avoid interactive commands.
- web_search: Search the web. Prefer fewer, targeted searches.
- fetch_url: Fetch full page content. Use after web_search for promising URLs.

### Database Operations
```python
try:
    db.execute(sql, params)
    db.commit()
except Exception as e:
    logger.error('DB write failed: %s — sql=%.200s params=%s', e, sql, params, exc_info=True)
    db.rollback()
    raise
```

### Background Thread Pattern
Background threads MUST wrap their entire run loop in try/except to prevent silent death:
```python
def _worker_loop():
    logger.info('[Worker] Started')
    while running:
        try:
            _do_one_cycle()
        except Exception as e:
            logger.error('[Worker] Cycle failed: %s', e, exc_info=True)
            time.sleep(60)
    logger.info('[Worker] Stopped')
```

### Security & Sensitive Data
- No hardcoded paths, hostnames, cluster names, or datacenter IDs.
- Use env vars or config files for deployment-specific values.
- Provide sensible defaults that work on a vanilla machine.
- Probe/auto-detect at runtime where possible.

### Token-Saving Mechanisms
Two mechanisms avoid re-generating content that already exists as tool results:
- emit_to_user(tool_round, comment): Terminal tool, ends turn by referencing existing output.
- content_ref on write_file: References previous tool result content for file writes.

### Compaction System
Two-layer compaction pipeline:
- L1: micro-compact cold tool results (every round, zero LLM cost)
- L2: smart summary as synthetic tool result (on context overflow)
Compaction respects the cache prefix — only compacts messages outside the cached region.

### Cache Breakpoint Strategy
4 breakpoints (BP1-BP4) with mixed TTL:
- BP1-BP2: System message blocks (1h TTL for stability)
- BP3: Last tool definition (1h TTL)
- BP4: Conversation tail message (5m TTL, changes each round)

The system message is structured as multiple blocks for optimal caching:
1. Project CLAUDE.md (prepended, large, rarely changes)
2. Static guidance (FRC, tool usage, output) — SEPARATE BLOCK
3. Compact skill instructions
4. Session memory (most dynamic, injected last)

### Multi-Agent System (Swarm)
spawn_agents tool for parallel sub-task execution.
Each sub-task runs with full tool access in isolation.
Results aggregated for final synthesis.

### Cross-Platform Compatibility
- Platform-specific code in lib/compat.py
- PG binary lookup via _find_pg_binary()
- FS keepalive Linux-only, graceful no-op elsewhere
- DANGEROUS_PATTERNS include both Unix and Windows variants
"""

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List contents of a directory.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read one or more files.", "parameters": {"type": "object", "properties": {"reads": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}, "required": ["reads"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Search for a pattern across files.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute a shell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch URL content.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
]

TOOL_RESULTS = {
    'list_dir': (
        "Directory: .\n\nFiles:\n"
        "  server.py (245L, 8.2KB)\n  bootstrap.py (189L, 6.1KB)\n"
        "  export.py (1120L, 42.3KB)\n\nSubdirectories:\n"
        "  lib/ (42 items)\n  routes/ (18 items)\n  static/ (31 items)\n"
    ),
    'read_files': (
        "File: lib/llm_client.py (lines 764-850 of 1736)\n"
        "────────────────────────────────────────\n"
        "764 │ def add_cache_breakpoints(body, log_prefix=''):\n"
        '765 │     """Add Anthropic-style ephemeral cache breakpoints.\n'
        "766 │     Annotates up to 4 content blocks with cache_control.\n"
        '767 │     """\n'
        "768 │     model = body.get('model', '')\n"
        "769 │     if not is_claude(model): return\n"
    ),
    'grep_search': (
        'grep "cache" — 8 matches:\n'
        'lib/llm_client.py:764:def add_cache_breakpoints\n'
        'lib/tasks_pkg/cache_tracking.py:26:  add_cache_breakpoints()\n'
        'tests/test_cache_breakpoints.py:35:from lib.llm_client import\n'
    ),
    'web_search': "Search results: 1. Anthropic Docs — Prompt Caching\n",
    'fetch_url': "# Anthropic Prompt Caching\nCache breakpoints optimize API usage.\n",
    'run_command': "$ wc -l lib/llm_client.py\n1736 lib/llm_client.py\n",
    'write_file': "✅ Wrote 42 lines to debug/test_output.py",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Arm Configuration
# ═══════════════════════════════════════════════════════════════════════════════

_TS_PREFIX = 'Current date and time: '
_DATE_PREFIX = 'Current date: '


def _make_timestamp(minute_offset: int = 0) -> str:
    """Generate a timestamp string, optionally offset by minutes."""
    now = datetime.now(timezone.utc) + timedelta(minutes=minute_offset)
    return now.strftime('%Y-%m-%d %H:%M UTC')


def _make_date_only() -> str:
    """Generate a date-only string (no time)."""
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%d')


def _strip_timestamp_from_text(text: str) -> str:
    """Remove injected timestamp/date lines from text."""
    lines = text.split('\n')
    cleaned = [ln for ln in lines
               if not ln.strip().startswith(_TS_PREFIX)
               and not ln.strip().startswith(_DATE_PREFIX)]
    return '\n'.join(cleaned).rstrip()


def _inject_into_last_user(messages: list, line: str):
    """Inject a text line into the last user message."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str):
                content = _strip_timestamp_from_text(content)
                messages[i]['content'] = content + '\n\n' + line
            elif isinstance(content, list):
                # Remove old timestamp blocks
                messages[i]['content'] = [
                    b for b in messages[i]['content']
                    if not (isinstance(b, dict) and b.get('type') == 'text'
                            and (b.get('text', '').strip().startswith(_TS_PREFIX)
                                 or b.get('text', '').strip().startswith(_DATE_PREFIX)))
                ]
                messages[i]['content'].append({'type': 'text', 'text': '\n\n' + line})
            return


def _inject_into_system_tail(messages: list, line: str):
    """Inject a text line into the tail of the system message."""
    if not messages or messages[0].get('role') != 'system':
        return
    content = messages[0].get('content', '')
    # Strip old timestamp/date from system message
    if isinstance(content, str):
        content = _strip_timestamp_from_text(content)
        messages[0]['content'] = content + '\n\n' + line
    elif isinstance(content, list):
        # Remove old timestamp blocks
        messages[0]['content'] = [
            b for b in content
            if not (isinstance(b, dict) and b.get('type') == 'text'
                    and (b.get('text', '').strip().startswith(_TS_PREFIX)
                         or b.get('text', '').strip().startswith(_DATE_PREFIX)))
        ]
        messages[0]['content'].append({'type': 'text', 'text': '\n\n' + line})


class ArmConfig:
    """Configuration for one arm of the A/B test."""

    def __init__(self, key: str, desc: str, suffix: str):
        self.key = key
        self.desc = desc
        # Unique suffix prevents cross-arm cache sharing
        self.suffix = suffix

    def prepare_system_prompt(self) -> str:
        """Build system prompt with CLAUDE.md + arm-unique suffix.

        Uses the actual CLAUDE.md to exceed the 4096-token Opus threshold.
        The arm-unique suffix prevents cross-arm cache sharing.
        """
        parts = [BASE_SYSTEM_PROMPT]
        if _CLAUDE_MD_CONTENT:
            parts.append('\n\n' + _CLAUDE_MD_CONTENT)
        parts.append(f'\n\n<!-- arm-seed: {self.suffix} -->')
        return ''.join(parts)

    def inject_timestamp(self, messages: list, round_num: int, minute_offset: int = 0):
        """Apply this arm's timestamp injection strategy."""
        raise NotImplementedError


class ArmA_UserDatetime(ArmConfig):
    """Arm A: Full datetime in user message, EVERY round (old behavior)."""

    def __init__(self):
        super().__init__('A', 'USER_DATETIME — full timestamp in user msg every round',
                         'ts-ab-arm-a-user-datetime-2026')

    def inject_timestamp(self, messages, round_num, minute_offset=0):
        ts = _make_timestamp(minute_offset)
        _inject_into_last_user(messages, f'{_TS_PREFIX}{ts}')


class ArmB_UserRound0(ArmConfig):
    """Arm B: Full datetime in user message, round 0 only."""

    def __init__(self):
        super().__init__('B', 'USER_ROUND0 — full timestamp in user msg R0 only',
                         'ts-ab-arm-b-user-round0-2026')

    def inject_timestamp(self, messages, round_num, minute_offset=0):
        if round_num == 0:
            ts = _make_timestamp(minute_offset)
            _inject_into_last_user(messages, f'{_TS_PREFIX}{ts}')
        # round > 0: skip — preserve cached prefix


class ArmC_SysDate(ArmConfig):
    """Arm C: Date-only in system prompt tail. Changes once per day."""

    def __init__(self):
        super().__init__('C', 'SYS_DATE — date-only in system prompt (changes 1x/day)',
                         'ts-ab-arm-c-sys-date-2026')

    def inject_timestamp(self, messages, round_num, minute_offset=0):
        # Date-only: doesn't change within a test run
        date = _make_date_only()
        _inject_into_system_tail(messages, f'{_DATE_PREFIX}{date}')


class ArmD_SysDatetime(ArmConfig):
    """Arm D: Full datetime in system prompt tail. Changes every minute."""

    def __init__(self):
        super().__init__('D', 'SYS_DATETIME — full timestamp in system prompt',
                         'ts-ab-arm-d-sys-datetime-2026')

    def inject_timestamp(self, messages, round_num, minute_offset=0):
        ts = _make_timestamp(minute_offset)
        _inject_into_system_tail(messages, f'{_TS_PREFIX}{ts}')


ALL_ARMS = {
    'A': ArmA_UserDatetime,
    'B': ArmB_UserRound0,
    'C': ArmC_SysDate,
    'D': ArmD_SysDatetime,
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RoundResult:
    round_num: int
    phase: str = 'task1'  # 'task1' or 'task2'
    prompt_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    total_input: int = 0
    elapsed: float = 0.0
    tool_calls: int = 0
    finish_reason: str = ''
    error: str = ''

    @property
    def cache_pct(self) -> float:
        total = self.prompt_tokens + self.cache_read + self.cache_write
        if total == 0:
            return 0.0
        return self.cache_read / total * 100

    @property
    def cost_input(self) -> float:
        """Estimated input cost at Opus pricing ($15/M input, $1.5/M cache read, $18.75/M cache write)."""
        return (self.prompt_tokens * 15 + self.cache_read * 1.5 +
                self.cache_write * 18.75) / 1_000_000


@dataclass
class ArmResult:
    arm_key: str
    desc: str
    rounds: list = field(default_factory=list)

    @property
    def total_prompt(self):
        return sum(r.prompt_tokens for r in self.rounds if not r.error)

    @property
    def total_cache_read(self):
        return sum(r.cache_read for r in self.rounds if not r.error)

    @property
    def total_cache_write(self):
        return sum(r.cache_write for r in self.rounds if not r.error)

    @property
    def total_cost(self):
        return sum(r.cost_input for r in self.rounds if not r.error)

    @property
    def avg_cache_pct(self):
        valid = [r for r in self.rounds if not r.error]
        if not valid:
            return 0.0
        return sum(r.cache_pct for r in valid) / len(valid)

    def task1_rounds(self):
        return [r for r in self.rounds if r.phase == 'task1' and not r.error]

    def task2_rounds(self):
        return [r for r in self.rounds if r.phase == 'task2' and not r.error]


# ═══════════════════════════════════════════════════════════════════════════════
#  Conversation runner
# ═══════════════════════════════════════════════════════════════════════════════

def _run_one_round(model, messages, tools, round_num, dry_run=False):
    """Make one API call and collect usage stats.

    Returns (RoundResult, tool_calls_list, content_text) on success,
    or just RoundResult on error.
    """
    rr = RoundResult(round_num=round_num)

    if dry_run:
        # Simulate cache behavior
        rr.prompt_tokens = 100
        rr.cache_write = 5000 if round_num == 0 else 500
        rr.cache_read = 0 if round_num == 0 else 4500
        rr.output_tokens = 200
        rr.tool_calls = 1
        rr.finish_reason = 'tool_calls' if round_num < 5 else 'stop'
        return rr

    body = build_body(model, messages, tools=tools, max_tokens=1024, stream=True)

    t0 = time.time()

    try:
        # Use dispatch_stream for proper provider routing (extra_headers, api_key)
        msg, finish_reason, usage = dispatch_stream(
            body,
            prefer_model=model,
            strict_model=True,
            log_prefix=f'[AB-R{round_num}]',
        )
    except Exception as e:
        rr.error = str(e)
        rr.elapsed = time.time() - t0
        print(f"    ❌ Error: {e}")
        return rr

    rr.elapsed = time.time() - t0
    u = usage or {}

    # Extract cache tokens — try all known key names (proxy vs native Anthropic)
    rr.cache_read = (u.get('cache_read_tokens')
                     or u.get('cache_read_input_tokens')
                     or u.get('cached_tokens')
                     or u.get('prompt_tokens_details', {}).get('cached_tokens', 0))
    rr.cache_write = (u.get('cache_creation_input_tokens')
                      or u.get('cache_write_tokens') or 0)
    rr.prompt_tokens = u.get('prompt_tokens', 0)
    rr.output_tokens = u.get('completion_tokens', 0)

    # Anthropic convention: prompt_tokens is uncached only
    inp = rr.prompt_tokens
    if inp <= rr.cache_write + rr.cache_read:
        rr.total_input = inp + rr.cache_write + rr.cache_read
    else:
        rr.total_input = inp

    tool_calls = msg.get('tool_calls', []) if msg else []
    content = msg.get('content', '') if msg else ''
    rr.tool_calls = len(tool_calls)
    rr.finish_reason = finish_reason or 'stop'

    # Print round summary with raw usage for debugging
    cache_pct = rr.cache_pct
    indicator = '🟢' if cache_pct > 50 else '🟡' if cache_pct > 10 else '🔴'
    print(f"    R{round_num+1}: {indicator} cache={cache_pct:5.1f}%  "
          f"CR={rr.cache_read:>7,}  CW={rr.cache_write:>7,}  "
          f"inp={rr.prompt_tokens:>5,}  out={rr.output_tokens:>5,}  "
          f"tools={rr.tool_calls}  ${rr.cost_input:.4f}  "
          f"{rr.elapsed:.1f}s")
    # Debug: print raw usage if no cache activity detected
    if rr.cache_read == 0 and rr.cache_write == 0 and round_num < 2:
        _usage_keys = {k: v for k, v in u.items() if v and k != '_model_limit_learned'}
        print(f"           raw_usage={_usage_keys}")

    return rr, tool_calls, content


def run_arm(arm: ArmConfig, model: str, num_rounds: int,
            dry_run: bool = False) -> ArmResult:
    """Run one full arm: Task 1 (multi-round) + Task 2 (new user message)."""

    print(f"\n{'═'*70}")
    print(f"  ARM {arm.key}: {arm.desc}")
    print(f"{'═'*70}")

    result = ArmResult(arm_key=arm.key, desc=arm.desc)

    # ── Phase 1: Task 1 (multi-round tool conversation) ──
    print(f"\n  ── Phase 1: Task 1 ({num_rounds} rounds) ──")

    system_prompt = arm.prepare_system_prompt()
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': (
            'I want to understand the cache breakpoint system. '
            'Please: (1) find the add_cache_breakpoints function, '
            '(2) read the cache_tracking module, (3) check orchestrator usage, '
            'and (4) explain the full flow.'
        )},
    ]

    for round_num in range(num_rounds):
        # Apply arm's timestamp injection strategy
        arm.inject_timestamp(messages, round_num, minute_offset=round_num)

        if dry_run:
            rr = _run_one_round(model, messages, TOOLS, round_num, dry_run=True)
            rr.phase = 'task1'
            result.rounds.append(rr)

            # Simulate tool call response
            fn = ['list_dir', 'read_files', 'grep_search', 'run_command',
                  'web_search', 'fetch_url'][round_num % 6]
            tc_id = f'tc_{arm.key}_{round_num}'
            messages.append({
                'role': 'assistant',
                'content': f'Let me check that...',
                'tool_calls': [{'id': tc_id, 'type': 'function',
                                'function': {'name': fn, 'arguments': '{}'}}],
            })
            messages.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': TOOL_RESULTS.get(fn, 'OK'),
            })
            continue

        # Real API call
        out = _run_one_round(model, messages, TOOLS, round_num)
        if isinstance(out, tuple):
            rr, tool_calls, content = out
        else:
            rr = out
            tool_calls, content = [], ''

        rr.phase = 'task1'
        result.rounds.append(rr)

        if rr.error:
            print(f"    ⚠️  Stopping arm {arm.key} due to error")
            break

        # Build assistant message from response
        asst_msg = {'role': 'assistant', 'content': content or ''}
        if tool_calls:
            asst_msg['tool_calls'] = tool_calls
        messages.append(asst_msg)

        # If model made tool calls, add tool results
        if tool_calls:
            for tc in tool_calls:
                fn_name = tc.get('function', {}).get('name', 'unknown')
                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id', f'tc_{round_num}'),
                    'content': TOOL_RESULTS.get(fn_name, f'{fn_name} result OK'),
                })
        else:
            # Model stopped — no more rounds
            print(f"    ℹ️  Model stopped at round {round_num+1}")
            break

    # ── Phase 2: Task 2 (new user message, simulating inter-task) ──
    # Simulate what happens when a new task starts:
    # - Old messages come from frontend (CLEAN — no injected timestamps)
    # - New user message is appended
    # - Timestamp re-injected into the new user message
    print(f"\n  ── Phase 2: Task 2 (inter-task, +5 min) ──")

    # Strip timestamps from ALL user messages (simulating clean frontend messages)
    task2_messages = []
    for msg in messages:
        msg_copy = copy.deepcopy(msg)
        if msg_copy.get('role') == 'user':
            content = msg_copy.get('content', '')
            if isinstance(content, str):
                msg_copy['content'] = _strip_timestamp_from_text(content)
        task2_messages.append(msg_copy)

    # Replace system prompt (fresh — same content but clean)
    task2_messages[0] = {'role': 'system', 'content': arm.prepare_system_prompt()}

    # Add new user message
    task2_messages.append({
        'role': 'user',
        'content': 'Now explain how the cache statistics are tracked and reported.',
    })

    # Run 2 rounds in Task 2
    for round_num in range(2):
        global_round = num_rounds + round_num

        # Apply arm's timestamp injection (with 5-minute offset to simulate time passing)
        arm.inject_timestamp(task2_messages, round_num, minute_offset=5 + round_num)

        if dry_run:
            rr = _run_one_round(model, task2_messages, TOOLS, global_round, dry_run=True)
            rr.phase = 'task2'
            rr.cache_read = 3000 if arm.key in ('B', 'C') else 0  # simulate better cache for B,C
            result.rounds.append(rr)

            fn = ['grep_search', 'read_files'][round_num % 2]
            tc_id = f'tc_{arm.key}_t2_{round_num}'
            task2_messages.append({
                'role': 'assistant', 'content': 'Checking...',
                'tool_calls': [{'id': tc_id, 'type': 'function',
                                'function': {'name': fn, 'arguments': '{}'}}],
            })
            task2_messages.append({
                'role': 'tool', 'tool_call_id': tc_id,
                'content': TOOL_RESULTS.get(fn, 'OK'),
            })
            continue

        out = _run_one_round(model, task2_messages, TOOLS, global_round)
        if isinstance(out, tuple):
            rr, tool_calls, content = out
        else:
            rr = out
            tool_calls, content = [], ''

        rr.phase = 'task2'
        result.rounds.append(rr)

        if rr.error:
            break

        asst_msg = {'role': 'assistant', 'content': content or ''}
        if tool_calls:
            asst_msg['tool_calls'] = tool_calls
        task2_messages.append(asst_msg)

        if tool_calls:
            for tc in tool_calls:
                fn_name = tc.get('function', {}).get('name', 'unknown')
                task2_messages.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id', f'tc_t2_{round_num}'),
                    'content': TOOL_RESULTS.get(fn_name, f'{fn_name} result OK'),
                })
        else:
            break

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Reporting
# ═══════════════════════════════════════════════════════════════════════════════

def print_report(results: list[ArmResult]):
    """Print comparison report across all arms."""

    print(f"\n\n{'='*80}")
    print(f"  TIMESTAMP PLACEMENT A/B TEST RESULTS")
    print(f"{'='*80}")

    # ── Per-arm summary ──
    for r in results:
        t1 = r.task1_rounds()
        t2 = r.task2_rounds()
        t1_cr = sum(x.cache_read for x in t1)
        t1_cw = sum(x.cache_write for x in t1)
        t2_cr = sum(x.cache_read for x in t2)
        t2_cw = sum(x.cache_write for x in t2)
        t1_cost = sum(x.cost_input for x in t1)
        t2_cost = sum(x.cost_input for x in t2)

        print(f"\n  Arm {r.arm_key}: {r.desc}")
        print(f"  {'─'*66}")
        print(f"    Task 1 ({len(t1)} rounds):")
        print(f"      Cache Read:  {t1_cr:>10,} tokens")
        print(f"      Cache Write: {t1_cw:>10,} tokens")
        print(f"      Avg Cache%:  {sum(x.cache_pct for x in t1)/max(len(t1),1):>9.1f}%")
        print(f"      Input Cost:  ${t1_cost:>10.4f}")
        if t2:
            print(f"    Task 2 ({len(t2)} rounds):")
            print(f"      Cache Read:  {t2_cr:>10,} tokens")
            print(f"      Cache Write: {t2_cw:>10,} tokens")
            print(f"      Avg Cache%:  {sum(x.cache_pct for x in t2)/max(len(t2),1):>9.1f}%")
            print(f"      Input Cost:  ${t2_cost:>10.4f}")
        print(f"    TOTAL Cost:    ${r.total_cost:>10.4f}")

    # ── Comparison table ──
    print(f"\n\n  {'─'*70}")
    print(f"  COMPARISON TABLE")
    print(f"  {'─'*70}")
    print(f"  {'Arm':<4} {'Strategy':<45} {'Cost':>10} {'CR%':>6} {'Savings':>8}")
    print(f"  {'─'*4} {'─'*45} {'─'*10} {'─'*6} {'─'*8}")

    # Sort by cost
    sorted_results = sorted(results, key=lambda r: r.total_cost)
    baseline_cost = sorted_results[-1].total_cost if sorted_results else 1

    for i, r in enumerate(sorted_results):
        savings = (1 - r.total_cost / baseline_cost) * 100 if baseline_cost > 0 else 0
        medal = ['🥇', '🥈', '🥉', '  '][min(i, 3)]
        print(f"  {medal}{r.arm_key:<3} {r.desc[:45]:<45} "
              f"${r.total_cost:>9.4f} {r.avg_cache_pct:>5.1f}% {savings:>+7.1f}%")

    # ── Recommendations ──
    winner = sorted_results[0] if sorted_results else None
    if winner:
        print(f"\n  ★ WINNER: Arm {winner.arm_key} — {winner.desc}")
        if winner.arm_key == 'C':
            print(f"    Recommendation: Move timestamp to system prompt, date-only format.")
            print(f"    Date changes once per day → system prompt cache stays stable.")
        elif winner.arm_key == 'B':
            print(f"    Recommendation: Keep timestamp in user msg but inject only on round 0.")
            print(f"    Intra-task cache preserved. Inter-task still has prefix mismatch.")
        elif winner.arm_key == 'A':
            print(f"    Note: Current behavior won. No change needed.")
        elif winner.arm_key == 'D':
            print(f"    Note: System prompt datetime won but changes every minute.")
            print(f"    Consider: system prompt changes break BP1-BP2 cache.")

    print()


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='A/B test: timestamp placement for cache efficiency')
    parser.add_argument('--model', default=DEFAULT_MODEL, help='Model ID')
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS, help='Rounds per task')
    parser.add_argument('--arms', default='A,B,C,D', help='Comma-separated arm keys (A,B,C,D)')
    parser.add_argument('--dry-run', action='store_true', help='Preview logic without API calls')
    args = parser.parse_args()

    arm_keys = [k.strip().upper() for k in args.arms.split(',')]
    arms = []
    for k in arm_keys:
        if k not in ALL_ARMS:
            print(f"Unknown arm: {k}. Available: {', '.join(ALL_ARMS.keys())}")
            sys.exit(1)
        arms.append(ALL_ARMS[k]())

    print(f"Timestamp Placement A/B Test")
    print(f"Model: {args.model}")
    print(f"Rounds: {args.rounds} per task + 2 inter-task")
    print(f"Arms: {', '.join(arm_keys)}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE API'}")

    results = []
    for arm in arms:
        r = run_arm(arm, args.model, args.rounds, dry_run=args.dry_run)
        results.append(r)
        # Brief pause between arms to avoid rate limiting
        if not args.dry_run and arm != arms[-1]:
            print(f"\n  ⏳ Waiting 5s between arms...")
            time.sleep(5)

    print_report(results)


if __name__ == '__main__':
    main()
