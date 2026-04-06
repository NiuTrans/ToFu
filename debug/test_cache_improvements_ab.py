#!/usr/bin/env python3
"""A/B test: validate cache improvement features with live API calls.

Tests two behavior-altering features from the 2026-04-06 cache optimization:

  1. **Session-stable TTL latch** — latches CACHE_EXTENDED_TTL once per task
     to prevent mid-session beta header changes that shift the cache key.
     ARM: LATCH_ON vs LATCH_OFF (simulates mid-session TTL toggle)

  2. **Tool result ordering** — sorts consecutive tool results by tool_call_id
     for deterministic prefix (important for OpenAI/Qwen automatic prefix caching).
     ARM: SORTED vs UNSORTED (randomized tool_call_id order)

For feature 1, the test simulates the scenario where CACHE_EXTENDED_TTL changes
mid-session (e.g., user toggles it in Settings while a task is running).  Without
the latch, the beta header changes → cache key changes → full eviction.

For feature 2, the test uses parallel tool calls with varying arrival order.
Without sorting, the prefix differs between rounds → cache miss.

Usage:
    python debug/test_cache_improvements_ab.py [--model MODEL] [--rounds N]
    python debug/test_cache_improvements_ab.py --feature ttl_latch
    python debug/test_cache_improvements_ab.py --feature tool_order
    python debug/test_cache_improvements_ab.py --dry-run
"""

import argparse
import copy
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger
from lib.llm_client import build_body, stream_chat, add_cache_breakpoints
from lib.model_info import is_claude

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 8

# ── System prompt: must exceed Opus 4096-token minimum cacheable segment ──
# Production system prompt is ~14K tokens.  We need at least ~5000 tokens
# so that system+tools exceeds 4096 from round 1, enabling prompt caching.
# Previous version was only ~1040 tokens → cache never activated in tests!
SYSTEM_PROMPT = """You are an AI coding assistant called Tofu (豆腐). You help users with programming tasks by using project tools to explore and modify code.

## Core Rules
1. Always write clean, well-documented code.
2. Follow the project's coding conventions strictly.
3. Test your changes before suggesting them to the user.
4. Use the project tools to explore and modify code — never guess file contents.
5. Never modify files without reading them first — always understand existing code.
6. When making multiple edits, prefer batch apply_diff over separate calls.
7. Read WIDE, not narrow — read 200+ lines in one shot for function/class context.
8. Prefer reading the WHOLE file for files under 500 lines.
9. Use grep_search for finding code patterns (built-in fuzzy hints, case-insensitive).
10. Use read_files for understanding code (returns with line numbers, supports batch reads).
11. Prefer run_command for shell operations (counting, testing, building).

## Project Context — Tofu Self-Hosted AI Assistant
This is a Python Flask web application with a vanilla JS frontend.
The project uses PostgreSQL for persistence and SSE for streaming.

### Architecture
- **Flask Blueprint registration**: All routes live in `routes/*.py` as Blueprints. `routes/__init__.py` → `register_all(app)` wires them.
- **Task lifecycle (SSE streaming)**: Client POSTs to `/api/chat/start` → creates task dict in memory → Background thread runs `orchestrator.run_task(task)` → Task appends SSE events via `append_event(task, ...)` → Client polls `/api/chat/stream/<id>` for SSE events → On completion, result persisted to SQLite via `persist_task_result()`.
- **LLM client flow**: `lib/llm_client.py` → `build_body()` constructs model-specific payloads. `stream_chat()` handles SSE streaming with retry logic.
- **Tool execution**: Tools defined in `lib/tools.py`, executed in `lib/tasks_pkg/executor.py`.
- **Token-saving tools**: `emit_to_user` and `content_ref` avoid re-generating content.

### Key Directories
```
lib/                   — Core business logic (LLM client, tools, fetch, trading, swarm)
  llm_client.py        — All LLM API communication
  llm_dispatch.py      — Dynamic model routing / load balancing
  tasks_pkg/           — Task orchestration, compaction, execution
  project_mod/         — Project file tools (list/read/write/grep/run)
  swarm/               — Multi-agent orchestration
  skills.py            — Skill accumulation system
routes/                — Flask Blueprints (chat, common, browser, fund_*)
static/js/             — Frontend (core.js, main.js, ui.js, trading/*.js)
static/                — CSS
debug/                 — Standalone test/benchmark scripts
logs/
  app.log              — Business logic only
  access.log           — HTTP request log
  error.log            — WARNING/ERROR/CRITICAL
  vendor.log           — Third-party libraries WARNING+
  audit.log            — Structured JSON audit trail
```

## Error Handling Patterns
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
    logger.warning('Invalid JSON (len=%d): %s', len(raw), e)
    data = {}
```

```python
try:
    db.execute(sql, params)
    db.commit()
except Exception as e:
    logger.error('DB write failed: %s — sql=%.200s params=%s', e, sql, params, exc_info=True)
    db.rollback()
    raise
```

## Logging Discipline
Every code path that can fail MUST leave a trace in the log file. Silent failures are the enemy.
- Every except block logs something (debug at minimum).
- Use %-style formatting for lazy evaluation: `logger.info('x=%s', x)`.
- Sanitize secrets: never log API keys, tokens, or full request bodies with credentials.
- Truncate large data: `logger.debug('Response preview: %.500s', body)`.
- Include context: conv_id, task_id, model name, URL, file path — whatever helps grep.
- Structured prefix: Use [Module] or [op:name] prefix for easy grepping.
- ZERO silent catches: Every `except` block logs something (debug at minimum).

| Scenario | Level | exc_info | Example |
|---|---|---|---|
| Expected / harmless fallback | debug | optional | Parse int, optional file |
| Unexpected but recoverable | warning | False | API timeout, retry |
| Unexpected, degraded behavior | error | True | Tool execution failure |
| Fatal / unrecoverable | critical | True | DB corruption |
| Retry loop (each attempt) | warning | False | Stream retry |
| Retry loop (final failure) | error | True | All retries exhausted |

## Code Style & Conventions
- **Imports**: stdlib → third-party → `lib.*` → `routes.*`, blank line between groups.
- **Logger init**: Always `from lib.log import get_logger; logger = get_logger(__name__)`.
- **Type hints**: Encouraged on public functions; optional on internal helpers.
- **Docstrings**: Google-style on modules and public functions.
- **Constants**: UPPER_SNAKE_CASE at module level.
- **Private helpers**: Prefix with `_`.

## Output Guidelines
- Be concise and direct. Lead with the answer or action, not the reasoning.
- Show exact code with file paths and line numbers.
- If you can say it in one sentence, don't use three.

## Change Approval Requirements
The following categories of changes require explicit user approval:
- LLM parameters: temperature, top_p, top_k, max_tokens, frequency_penalty
- Retry & timeout settings: retry counts, backoff multipliers, request timeouts
- Token budgets: context window sizes, compaction thresholds, layer boundaries
- Rate limiter settings: RPM, TPM, concurrency caps, cooldown periods
- Batch/queue sizes: thread pool sizes, chunk sizes, polling intervals
- Model routing & dispatch logic: default model assignments, API key rotation
- Database schema changes: ALTER TABLE, new tables, new indexes
- Security-sensitive changes: authentication, CORS, proxy configurations

## File Modification Checklist
Before submitting any code change, verify:
- Logger present: File has `from lib.log import get_logger; logger = get_logger(__name__)`.
- No silent catches: Every `except` block logs something.
- Context in logs: Log messages include relevant IDs (conv_id, task_id, url, model, etc.).
- Tracebacks on errors: `exc_info=True` on `logger.error()` for unexpected exceptions.
- `log_context` for slow ops: Operations > 1s use `with log_context(...)`.
- No f-strings in log calls: Use `logger.info('x=%s', x)` not `logger.info(f'x={x}')`.
- Secrets not logged: API keys, tokens, passwords never appear in log output.
- Large data truncated: Use `%.500s` or `[:500]` to cap logged payloads.
- Export sync: If change adds secrets/endpoints → update export.py.

## Environment
- Python 3.10+, Flask with flask-compress
- PostgreSQL 18+ (auto-bootstraps on first run)
- Multi-file logging architecture (app.log, access.log, error.log, vendor.log, audit.log)
- Per-project config isolation in data/config/
- Key env vars: LLM_API_KEYS, LLM_BASE_URL, LLM_MODEL, PROXY_BYPASS_DOMAINS
- Cross-platform support (Linux, macOS, Windows) via lib/compat.py

## Key Files Quick Reference
| Need to… | Look at… |
|---|---|
| Change LLM behavior | lib/llm_client.py, lib/llm_dispatch.py |
| Add a new tool | lib/tools.py → lib/tasks_pkg/executor.py |
| Add a new API endpoint | routes/ → routes/__init__.py |
| Fix streaming issues | lib/llm_client.py → routes/chat.py |
| Debug task flow | lib/tasks_pkg/orchestrator.py |
| Change project file tools | lib/project_mod/tools.py |
| Read local files (images/PDF) | lib/file_reader.py → lib/tools/project.py |
| Add/edit skills | lib/skills.py |
| Modify trading features | lib/trading.py, routes/trading_*.py |
| Check recent errors | lib/project_error_tracker.py |
| Export / sanitize project | export.py |
| Cross-platform compat | lib/compat.py |

## Testing
- Test scripts live in debug/ (e.g., debug/test_build_body.py)
- Unit tests in tests/ with pytest
- Run specific test: python debug/test_swarm.py
- Auto-fix silent catches: _fix_silent_catches.py

## Security Patterns
- Never log API keys, tokens, or passwords
- Use lib/proxy.py for all external HTTP requests
- Validate file paths in project_mod/ to prevent traversal
- Sanitize user input before passing to shell commands in run_command
- CORS configuration in server.py middleware
- Rate limiting on API endpoints

## Database Patterns
- PostgreSQL with auto-bootstrap (lib/database/_bootstrap.py)
- Free port detection starting from 15432
- Schema migrations in lib/database/schema.py
- Connection pooling via psycopg2
- Always use parameterized queries — never string concatenation for SQL
- Wrap writes in try/except with rollback on failure

## Prompt Caching Strategy
- 4 cache breakpoints for Claude (system, tools, conversation tail)
- Mixed TTL: 1h for stable prefix (BP1-BP3), 5m for tail (BP4)
- Cache-aware microcompact: skip editing messages in cached prefix
- Tool result ordering for deterministic prefix (automatic caching providers)
- Session-stable TTL latch prevents mid-session cache key shift
- Minimum cacheable segment sizes: Opus/Haiku 4096 tokens, Sonnet 1024 tokens
- Cache write pricing: 1.25x (5m TTL) or 2.0x (1h TTL) base input price
- Cache read pricing: 0.1x base input price (same for all TTLs)

## Tool Usage Guidelines

### list_dir(path)
List directory contents. Shows files with line counts and sizes, subdirectories with item counts.
Use this to understand project structure before reading specific files.
Always start exploration with `list_dir('.')` to get the root structure.

### read_files(reads)
Read one or more files in a single call. Each entry has 'path' (required), 'start_line' and 'end_line' (optional).
Maximum 20 files per batch. Files under ~40KB auto-expand to whole-file regardless of range.
IMPORTANT: When reading a function or class, read 200+ lines in one shot — don't fragment.
Prefer reading the WHOLE file for files under 500 lines.

### grep_search(pattern, path?, include?)
Search for a pattern across project files. Returns matching lines with file paths and line numbers.
Search is case-insensitive. Use simple, short patterns for best results.
Prefer grep_search over read_files when looking for specific patterns across many files.

### run_command(command, timeout?, working_dir?)
Execute a shell command and return stdout + stderr. Use for counting, testing, building, git operations.
Commands run with project root as working directory. Avoid interactive commands requiring stdin.
Use for: `wc -l`, `sort`, `uniq`, `npm test`, `pytest`, `git status`, etc.

### find_files(pattern, path?)
Find files by name pattern (glob). Useful for discovering test files, configs, specific file types.
Examples: `*.test.py`, `Dockerfile`, `*.config.*`, `package.json`

### apply_diff(path, search, replace, replace_all?)
Apply targeted search-and-replace edit(s). The 'search' string must match EXACTLY (including whitespace).
For MULTIPLE edits, pass an 'edits' array. Edits are applied sequentially.
Set replace_all=true to replace ALL occurrences (default: errors on multiple matches).
Always read_files first to get exact content before using apply_diff.

### write_file(path, content, description?)
Write content to a file. Creates new files or overwrites existing ones.
Use for new files or major rewrites. Use apply_diff for small targeted edits.
Supports content_ref to reference previous tool result content.

### web_search(query)
Search the web. Use targeted, specific queries. Review summaries before fetching URLs.
Prefer fewer, targeted searches over many broad ones.

### fetch_url(url)
Fetch and read full content of a URL (HTML, PDF, plain text).
Use after web_search to deeply read the most promising pages.

## Multi-Agent (Swarm) Architecture
The swarm system enables multi-agent orchestration for complex tasks:
- Planner agent: breaks down complex tasks into subtasks
- Worker agents: execute individual subtasks in parallel
- Scheduler: manages agent lifecycle and resource allocation
- Each agent runs its own orchestrator loop with shared tool access
- Communication via structured message passing through the scheduler

## Trading Module (Optional)
Disabled by default (TRADING_ENABLED=0). When enabled:
- Fund intelligence crawler with multi-source data aggregation
- Quantitative signal analysis (momentum, value, sentiment)
- Risk management with portfolio-level constraints
- Backtesting engine with time-locked historical data
- Real-time market data integration
"""

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List contents of a directory. Shows files with line counts and sizes.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read one or more files. Each entry has 'path', optional 'start_line' and 'end_line'. Max 20 files.", "parameters": {"type": "object", "properties": {"reads": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}}, "required": ["reads"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Search for a pattern across project files. Case-insensitive.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute a shell command and return stdout + stderr.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch and read the full content of a URL.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "find_files", "description": "Find files by name pattern (glob).", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "apply_diff", "description": "Apply targeted search-and-replace edit(s).", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}}, "required": ["path", "search", "replace"]}}},
]

# Tool results of varying sizes for realistic simulation.
# Results should be large enough that system_prompt + tools + a few tool results
# comfortably exceeds 4096 tokens (Opus minimum cacheable segment).
TOOL_RESULTS = {
    'list_dir': (
        "Directory: .\n\nFiles:\n"
        "  📄 server.py (245L, 8.2KB)\n  📄 bootstrap.py (189L, 6.1KB)\n"
        "  📄 export.py (1120L, 42.3KB)\n  📄 CLAUDE.md (380L, 14.8KB)\n"
        "  📄 requirements.txt (28L, 0.6KB)\n  📄 README.md (120L, 4.8KB)\n"
        "  📄 pyproject.toml (45L, 1.2KB)\n  📄 .env.example (15L, 0.4KB)\n\n"
        "Subdirectories:\n"
        "  📁 lib/ (42 items)\n  📁 routes/ (18 items)\n  📁 static/ (31 items)\n"
        "  📁 debug/ (15 items)\n  📁 tests/ (22 items)\n  📁 data/ (5 items)\n"
        "  📁 uploads/ (3 items)\n  📁 logs/ (6 items)\n  📁 docs/ (8 items)\n"
    ),
    'read_files': (
        "File: lib/tasks_pkg/cache_tracking.py (lines 1-120 of 497)\n"
        "────────────────────────────────────────\n"
        "1 │ # HOT_PATH — called every round in the orchestrator.\n"
        '2 │ """Prompt Cache Break Detection & Cache-Aware Microcompact.\n'
        "3 │ \n"
        "4 │ Inspired by Claude Code's promptCacheBreakDetection.ts (727 lines).\n"
        "5 │ \n"
        "6 │ Features:\n"
        "7 │   1. **Cache break detection**: two-phase approach (like Claude Code):\n"
        "8 │      - Phase 1 (pre-call): hash system prompt, tools, message count.\n"
        "9 │      - Phase 2 (post-call): check API-reported cache_read_tokens.\n"
        "10 │   2. **Cache-aware microcompact**: skip editing cached prefix.\n"
        "11 │   3. **Concurrent conversation tracking**: detect cache contention.\n"
        "12 │   4. **Session-stable TTL latch**: prevent mid-session cache key shift.\n"
        "13 │   5. **Cache-aware tool result ordering**: deterministic prefix.\n"
        '14 │ """\n'
        "15 │ \n"
        "16 │ from __future__ import annotations\n"
        "17 │ \n"
        "18 │ import hashlib\n"
        "19 │ import json\n"
        "20 │ import threading\n"
        "21 │ import time\n"
        "22 │ from typing import Any\n"
        "23 │ \n"
        "24 │ from lib.log import get_logger\n"
        "25 │ \n"
        "26 │ logger = get_logger(__name__)\n"
        "27 │ \n"
        "28 │ class CacheState:\n"
        '29 │     """Tracks the state of the prompt cache for a conversation."""\n'
        "30 │     __slots__ = (\n"
        "31 │         'system_hash', 'tools_hash', 'model',\n"
        "32 │         'message_count', 'last_cache_read_tokens',\n"
        "33 │         'last_update_time', 'call_count',\n"
        "34 │         'compaction_pending',\n"
        "35 │     )\n"
        "36 │ \n"
        "37 │     def __init__(self):\n"
        "38 │         self.system_hash: str = ''\n"
        "39 │         self.tools_hash: str = ''\n"
        "40 │         self.model: str = ''\n"
        "41 │         self.message_count: int = 0\n"
        "42 │         self.last_cache_read_tokens: int = 0\n"
        "43 │         self.last_update_time: float = 0.0\n"
        "44 │         self.call_count: int = 0\n"
        "45 │         self.compaction_pending: bool = False\n"
        "46 │ \n"
        "47 │ _cache_states: dict[str, CacheState] = {}\n"
        "48 │ _cache_lock = threading.Lock()\n"
        "49 │ \n"
        "50 │ _MIN_CACHE_MISS_TOKENS = 2000\n"
        "51 │ \n"
        "52 │ def detect_cache_break(conv_id, messages, tools, model, usage=None):\n"
        '53 │     """Two-phase cache break detection."""\n'
        "54 │     if not conv_id:\n"
        "55 │         return None\n"
        "56 │     now = time.time()\n"
        "57 │     with _cache_lock:\n"
        "58 │         prev = _cache_states.get(conv_id)\n"
        "59 │         if prev is None:\n"
        "60 │             prev = CacheState()\n"
        "61 │             _cache_states[conv_id] = prev\n"
        "62 │         sys_hash = _hash_system_prompt(messages)\n"
        "63 │         tools_hash = _hash_tools(tools)\n"
        "64 │         # Phase 1: detect client-side changes\n"
        "65 │         client_changes = {}\n"
        "66 │         if prev.call_count > 0:\n"
        "67 │             if sys_hash != prev.system_hash:\n"
        "68 │                 client_changes['system_prompt'] = 'changed'\n"
        "69 │             if tools_hash != prev.tools_hash:\n"
        "70 │                 client_changes['tools'] = 'changed'\n"
    ),
    'grep_search': (
        'grep "detect_cache_break" — 12 matches:\n\n'
        'lib/tasks_pkg/cache_tracking.py:155:def detect_cache_break(\n'
        'lib/tasks_pkg/cache_tracking.py:156:    conv_id: str,\n'
        'lib/tasks_pkg/cache_tracking.py:157:    messages: list,\n'
        'lib/tasks_pkg/cache_tracking.py:158:    tools: list | None,\n'
        'lib/tasks_pkg/cache_tracking.py:159:    model: str,\n'
        'lib/tasks_pkg/cache_tracking.py:160:    usage: dict | None = None,\n'
        'lib/tasks_pkg/orchestrator.py:661:    cache_break = detect_cache_break(\n'
        'lib/tasks_pkg/orchestrator.py:662:        task["convId"], messages, tools,\n'
        'lib/tasks_pkg/orchestrator.py:663:        model=model, usage=last_usage,\n'
        'tests/test_cache_improvements.py:45:    result = detect_cache_break(\n'
        'tests/test_cache_breakpoints.py:112:    # verify no break on first call\n'
        'tests/test_cache_breakpoints.py:118:    detect_cache_break(conv, msgs, None, model, usage)\n'
    ),
    'run_command': (
        "$ find . -name '*.py' -path '*/tasks_pkg/*' | wc -l\n12\n\n"
        "$ wc -l lib/tasks_pkg/*.py\n"
        "   497 lib/tasks_pkg/cache_tracking.py\n"
        "   789 lib/tasks_pkg/orchestrator.py\n"
        "   456 lib/tasks_pkg/executor.py\n"
        "   234 lib/tasks_pkg/compaction.py\n"
        "   123 lib/tasks_pkg/__init__.py\n"
        "  2099 total\n"
    ),
    'web_search': (
        "Search results:\n\n"
        "1. [Anthropic Docs] Prompt Caching — https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching\n"
        "   Prompt caching reduces costs by up to 90% and latency by up to 85%.\n"
        "   Cache lifetime: 5 minutes default, 1 hour with extended-cache-ttl beta.\n"
        "   Minimum cacheable: 1024 tokens (Sonnet), 4096 tokens (Opus/Haiku).\n\n"
        "2. [Blog] Optimizing LLM Costs with Caching — https://blog.langchain.dev/prompt-caching\n"
        "   Best practices for prompt caching across providers.\n\n"
        "3. [GitHub] anthropics/anthropic-sdk — https://github.com/anthropics/anthropic-sdk-python\n"
        "   Official Python SDK with cache_control support.\n"
    ),
    'fetch_url': (
        "# Anthropic Prompt Caching\n\n"
        "## Overview\nPrompt caching optimizes your API usage by allowing you to\n"
        "reference previously sent content without resending it. This reduces\n"
        "costs by up to 90% and latency by up to 85%.\n\n"
        "## How it works\n"
        "1. Mark content blocks with `cache_control: {type: 'ephemeral'}`\n"
        "2. On first request, content is cached (cache write at 1.25x base price)\n"
        "3. On subsequent requests, cached content is read (at 0.1x base price)\n\n"
        "## Cache lifetime\n- Default TTL: 5 minutes from last access\n"
        "- Extended TTL: 1 hour (requires `anthropic-beta: extended-cache-ttl-2025-04-11` header)\n"
        "- Extended TTL pricing: 2x base price for writes, 0.1x for reads\n\n"
        "## Minimum cacheable tokens\n"
        "- Claude Sonnet: 1,024 tokens\n"
        "- Claude Opus / Haiku 4.5: 4,096 tokens\n\n"
        "## Best practices\n"
        "- Place cache breakpoints on stable content (system prompts, tool definitions)\n"
        "- Use up to 4 cache breakpoints per request\n"
        "- Order breakpoints with longer TTL before shorter TTL\n"
    ),
    'find_files': (
        "Found 8 files matching '*.py' in lib/tasks_pkg/:\n"
        "  lib/tasks_pkg/__init__.py\n"
        "  lib/tasks_pkg/cache_tracking.py\n"
        "  lib/tasks_pkg/orchestrator.py\n"
        "  lib/tasks_pkg/executor.py\n"
        "  lib/tasks_pkg/compaction.py\n"
        "  lib/tasks_pkg/attachments.py\n"
        "  lib/tasks_pkg/reactive_compact.py\n"
        "  lib/tasks_pkg/stream_handler.py\n"
    ),
    'apply_diff': "✅ Applied 1 edit to lib/example.py\n\nDiff preview:\n  - old_code = something\n  + new_code = something_better",
}


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
    ttft: float = 0.0
    tool_calls: int = 0
    finish_reason: str = ''
    status: str = ''
    error: str = ''


@dataclass
class ConvResult:
    label: str
    feature: str
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Feature 1: TTL Latch A/B Test
# ═══════════════════════════════════════════════════════════════════════════════

def _run_ttl_latch_test(
    model: str,
    num_rounds: int,
    label: str,
    use_latch: bool,
    dry_run: bool = False,
) -> ConvResult:
    """Test TTL latch: simulate mid-session CACHE_EXTENDED_TTL toggle.

    With latch OFF, we toggle lib.CACHE_EXTENDED_TTL mid-session at round 4.
    This changes the beta header → different cache key → full eviction.

    With latch ON, the decision is frozen at task start, so the mid-session
    toggle has no effect on the running task.

    The test uses a fake task_id when latch is ON, so latch_extended_ttl()
    latches the initial value.  When latch is OFF, no task_id is set in body,
    so add_cache_breakpoints reads the live lib.CACHE_EXTENDED_TTL each round.
    """
    import lib as _lib
    from lib.tasks_pkg.cache_tracking import (
        latch_extended_ttl,
        release_ttl_latch,
    )

    # Start with CACHE_EXTENDED_TTL = True
    original_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
    _lib.CACHE_EXTENDED_TTL = True

    fake_task_id = f'ab_ttl_latch_{time.time():.0f}'

    # If using latch, latch the initial value NOW (True)
    if use_latch:
        latch_extended_ttl(fake_task_id)

    print(f"\n  {'═'*60}")
    print(f"  {label} — TTL Latch {'ON' if use_latch else 'OFF'}")
    print(f"  Initial CACHE_EXTENDED_TTL=True, toggles to False at round 4")
    print(f"  {'═'*60}")

    result = ConvResult(label=label, feature='ttl_latch', model=model)
    # ★ Add unique arm identifier to system prompt to prevent cross-arm
    # cache sharing.  Without this, the second arm always benefits from
    # the first arm's warm cache, confounding the A/B comparison.
    arm_seed = f'\n\n<!-- arm={label} seed={time.time():.0f} -->'
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'Explain the cache breakpoint system in this project. '
            'Find add_cache_breakpoints and cache_tracking.py, then explain the flow.'
        )},
    ]
    tc_counter = 0

    try:
        for round_num in range(num_rounds):
            # ★ Simulate mid-session settings change at round 4
            if round_num == 4:
                _lib.CACHE_EXTENDED_TTL = False
                print(f"\n  ⚡ TOGGLED CACHE_EXTENDED_TTL → False at round {round_num + 1}")
                if use_latch:
                    print(f"     (latch is ON → task still uses True)")
                else:
                    print(f"     (latch is OFF → beta header changes → cache key shift!)")

            print(f"\n  ── R{round_num+1}/{num_rounds} ── msgs={len(messages)}")

            if dry_run:
                rr = _dry_run_round(round_num, latch_active=use_latch)
                result.rounds.append(rr)
                _append_simulated_tool_round(messages, tc_counter)
                tc_counter += 1
                continue

            body = build_body(
                model, messages,
                max_tokens=2048,
                temperature=1.0,
                thinking_enabled=True,
                preset='medium',
                thinking_depth='medium',
                tools=TOOLS,
                stream=True,
            )

            # ★ KEY DIFFERENCE: set _task_id only when latch is ON
            if use_latch:
                body['_task_id'] = fake_task_id

            rr = _run_api_round(body, round_num, label, messages)
            result.rounds.append(rr)
            if rr.error:
                break

            # Simulate tool execution
            if not _append_real_tool_round(messages, rr, tc_counter):
                break
            tc_counter += rr.tool_calls

    finally:
        _lib.CACHE_EXTENDED_TTL = original_ttl
        if use_latch:
            release_ttl_latch(fake_task_id)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Feature 2: Tool Result Ordering A/B Test
# ═══════════════════════════════════════════════════════════════════════════════

def _run_tool_order_test(
    model: str,
    num_rounds: int,
    label: str,
    use_sorting: bool,
    dry_run: bool = False,
) -> ConvResult:
    """Test tool result ordering: sorted vs random order for parallel calls.

    Each round simulates 2-3 parallel tool calls.  Without sorting, tool results
    are appended in random order (simulating async completion times).  With sorting,
    sort_tool_results() is called before build_body to ensure deterministic order.

    For Anthropic explicit breakpoints, the order doesn't matter much (breakpoints
    mark exact positions).  But for OpenAI/Qwen automatic prefix caching, different
    orders = different prefix = cache miss.

    Note: This test is most meaningful for OpenAI/Qwen models with automatic prefix
    caching.  For Claude, the effect is subtle — only the raw bytes of the prefix
    change, which matters if the server hashes the full prefix.
    """
    from lib.tasks_pkg.cache_tracking import sort_tool_results

    print(f"\n  {'═'*60}")
    print(f"  {label} — Tool Order {'SORTED' if use_sorting else 'RANDOM'}")
    print(f"  {'═'*60}")

    result = ConvResult(label=label, feature='tool_order', model=model)
    # ★ Unique arm seed to prevent cross-arm cache sharing
    arm_seed = f'\n\n<!-- arm={label} seed={time.time():.0f} -->'
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'I need you to do several things at once:\n'
            '1. List the root directory\n'
            '2. Find all Python test files\n'
            '3. Search for cache_tracking imports\n'
            'Do all of these in parallel.'
        )},
    ]
    tc_counter = 0

    # Pre-defined parallel tool call sets (2-3 calls per round)
    parallel_tool_sets = [
        ['list_dir', 'find_files', 'grep_search'],
        ['read_files', 'grep_search'],
        ['list_dir', 'run_command', 'find_files'],
        ['read_files', 'grep_search', 'find_files'],
        ['run_command', 'read_files'],
        ['grep_search', 'list_dir', 'find_files'],
        ['read_files', 'run_command'],
        ['find_files', 'grep_search', 'list_dir'],
    ]

    for round_num in range(num_rounds):
        print(f"\n  ── R{round_num+1}/{num_rounds} ── msgs={len(messages)}")

        # Generate parallel tool calls for this round
        tool_names = parallel_tool_sets[round_num % len(parallel_tool_sets)]

        if dry_run:
            rr = _dry_run_round(round_num, latch_active=True)
            result.rounds.append(rr)
            _append_parallel_tools(messages, tool_names, tc_counter,
                                   randomize=not use_sorting)
            if use_sorting:
                sort_tool_results(messages)
            tc_counter += len(tool_names)
            continue

        # Append assistant with parallel tool_calls
        tool_calls = []
        for i, fn_name in enumerate(tool_names):
            tc_id = f'call_{tc_counter + i:04d}'
            tool_calls.append({
                'id': tc_id,
                'type': 'function',
                'function': {'name': fn_name, 'arguments': '{}'},
            })

        messages.append({
            'role': 'assistant',
            'content': '',
            'tool_calls': tool_calls,
        })

        # Append tool results in random or sorted order
        result_order = list(range(len(tool_calls)))
        if not use_sorting:
            random.shuffle(result_order)

        for idx in result_order:
            tc = tool_calls[idx]
            fn_name = tc['function']['name']
            messages.append({
                'role': 'tool',
                'tool_call_id': tc['id'],
                'content': TOOL_RESULTS.get(fn_name, f"Result for {fn_name}"),
            })

        # Apply sorting if enabled
        if use_sorting:
            sort_tool_results(messages)

        # Add follow-up user message to trigger model response
        if round_num < num_rounds - 1:
            messages.append({
                'role': 'user',
                'content': f'Now do step {round_num + 2}: look deeper into the results.',
            })

        body = build_body(
            model, messages,
            max_tokens=2048,
            temperature=1.0,
            thinking_enabled=True,
            preset='medium',
            thinking_depth='medium',
            tools=TOOLS,
            stream=True,
        )

        rr = _run_api_round(body, round_num, label, messages)
        result.rounds.append(rr)
        if rr.error:
            break

        tc_counter += len(tool_names)

        # Append assistant response for next round
        if not rr.error and round_num < num_rounds - 1:
            # Need to simulate adding the assistant's response back
            # In a real conversation the model's response would be here
            pass

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

_last_assistant_msg = None  # stashed from _run_api_round for tool append


def _run_api_round(body, round_num, label, messages):
    """Make one API call and return a RoundResult."""
    t0 = time.time()
    ttft = None
    content_buf = []
    thinking_buf = []

    def on_content(cd):
        nonlocal ttft
        if ttft is None:
            ttft = time.time() - t0
        content_buf.append(cd)

    def on_thinking(td):
        thinking_buf.append(td)

    try:
        assistant_msg, finish_reason, usage = stream_chat(
            body,
            on_content=on_content,
            on_thinking=on_thinking,
            log_prefix=f'[{label} R{round_num+1}]',
        )
    except Exception as e:
        logger.warning('[AB] API error in %s R%d: %s', label, round_num+1, e)
        return RoundResult(round_num=round_num+1, error=str(e))

    global _last_assistant_msg
    _last_assistant_msg = assistant_msg

    elapsed = time.time() - t0
    u = usage or {}

    cache_read = (u.get('cache_read_tokens')
                  or u.get('cache_read_input_tokens')
                  or u.get('cached_tokens') or 0)
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
        round_num=round_num+1,
        prompt_tokens=prompt_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        output_tokens=output_tokens,
        total_input=prompt_tokens + cache_read + cache_write,
        elapsed=elapsed,
        ttft=ttft or elapsed,
        tool_calls=len(assistant_msg.get('tool_calls', [])),
        finish_reason=finish_reason,
        status=status,
    )

    print(f"    ⏱ {elapsed:.1f}s (TTFT: {rr.ttft:.1f}s) | {status}")
    print(f"    📊 pt={prompt_tokens:,}  cr={cache_read:,}  "
          f"cw={cache_write:,}  out={output_tokens:,}")
    print(f"    🔧 tools={rr.tool_calls}  finish={finish_reason}")

    return rr


def _append_real_tool_round(messages, rr, tc_counter):
    """Append assistant + tool results from a real API call."""
    global _last_assistant_msg
    if _last_assistant_msg is None:
        return False
    tool_calls = _last_assistant_msg.get('tool_calls', [])
    if not tool_calls:
        print(f"    ✅ Complete (no tool calls)")
        return False

    clean_msg = {
        'role': 'assistant',
        'tool_calls': tool_calls,
        'content': _last_assistant_msg.get('content') or '',
    }
    messages.append(clean_msg)

    for tc in tool_calls:
        fn_name = tc.get('function', {}).get('name', 'unknown')
        tool_result = TOOL_RESULTS.get(fn_name, f"Tool {fn_name} executed successfully.")
        messages.append({
            'role': 'tool',
            'tool_call_id': tc.get('id', f'call_{tc_counter}'),
            'content': tool_result,
        })
        tc_counter += 1
        print(f"       → {fn_name}: {len(tool_result)} chars")

    _last_assistant_msg = None
    return True


def _append_simulated_tool_round(messages, tc_counter):
    """Append a simulated tool round for dry-run mode."""
    tc_id = f'call_{tc_counter:04d}'
    messages.append({
        'role': 'assistant',
        'content': '',
        'tool_calls': [{'id': tc_id, 'type': 'function',
                        'function': {'name': 'read_files', 'arguments': '{}'}}],
    })
    messages.append({
        'role': 'tool',
        'tool_call_id': tc_id,
        'content': TOOL_RESULTS['read_files'],
    })


def _append_parallel_tools(messages, tool_names, tc_counter, randomize=False):
    """Append parallel tool calls and results."""
    tool_calls = []
    for i, fn_name in enumerate(tool_names):
        tc_id = f'call_{tc_counter + i:04d}'
        tool_calls.append({
            'id': tc_id,
            'type': 'function',
            'function': {'name': fn_name, 'arguments': '{}'},
        })

    messages.append({
        'role': 'assistant',
        'content': '',
        'tool_calls': tool_calls,
    })

    # Vary insertion order
    result_indices = list(range(len(tool_calls)))
    if randomize:
        random.shuffle(result_indices)

    for idx in result_indices:
        tc = tool_calls[idx]
        fn_name = tc['function']['name']
        messages.append({
            'role': 'tool',
            'tool_call_id': tc['id'],
            'content': TOOL_RESULTS.get(fn_name, f"Result for {fn_name}"),
        })


def _dry_run_round(round_num, latch_active=True):
    """Simulate a round for dry-run mode."""
    # Simulate: latch_active keeps cache stable, no-latch causes drop at R5
    if not latch_active and round_num >= 4:
        # Simulate cache eviction from mid-session TTL toggle
        cache_read = max(0, 5000 - (round_num - 3) * 2000)
        cache_write = 6000  # full re-cache
        prompt_tokens = 500
    else:
        cache_read = min(50000, 5000 + round_num * 4000)
        cache_write = max(500, 3000 - round_num * 200)
        prompt_tokens = max(1, 20 - round_num)

    return RoundResult(
        round_num=round_num+1,
        prompt_tokens=prompt_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        output_tokens=200 + round_num * 30,
        total_input=prompt_tokens + cache_read + cache_write,
        elapsed=3.5 + round_num * 0.2,
        ttft=2.0 + round_num * 0.1,
        tool_calls=2,
        finish_reason='tool_calls',
        status='MISS' if cache_read < 500 else ('HIT+WRITE' if cache_write > 500 else 'HIT'),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Cost computation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_cost(cr: ConvResult, *, input_price=15.0, output_price=75.0,
                 cw_mul=1.25, cr_mul=0.10):
    """Compute cost breakdown. Opus 4.6: $15/M in, $75/M out."""
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
#  Report generation
# ═══════════════════════════════════════════════════════════════════════════════

def _print_round_table(cr: ConvResult):
    """Print per-round results."""
    print(f"\n  {'Rnd':>3} │ {'Prompt':>8} │ {'CacheRead':>10} │ {'CacheWrite':>11} │ "
          f"{'Output':>7} │ {'Status':>10} │ {'TTFT':>5} │ {'Total':>5}")
    print(f"  {'─'*3}─┼─{'─'*8}─┼─{'─'*10}─┼─{'─'*11}─┼─"
          f"{'─'*7}─┼─{'─'*10}─┼─{'─'*5}─┼─{'─'*5}")
    for r in cr.rounds:
        if r.error:
            print(f"  {r.round_num:>3} │ {'ERROR':>8} │ {r.error[:40]:>40}")
            continue
        print(f"  {r.round_num:>3} │ {r.prompt_tokens:>8,} │ {r.cache_read:>10,} │ "
              f"{r.cache_write:>11,} │ {r.output_tokens:>7,} │ {r.status:>10} │ "
              f"{r.ttft:>5.1f}s │ {r.elapsed:>5.1f}s")


def print_comparison(arm_a: ConvResult, arm_b: ConvResult, feature: str):
    """Print A/B comparison report."""
    cost_a = compute_cost(arm_a)
    cost_b = compute_cost(arm_b)

    print(f"\n\n  {'▓'*60}")
    print(f"  FEATURE: {feature} — A/B COMPARISON")
    print(f"  {'▓'*60}")

    print(f"\n  ┌─ ARM A: {arm_a.label} ─────────────────────────┐")
    _print_round_table(arm_a)

    print(f"\n  ┌─ ARM B: {arm_b.label} ─────────────────────────┐")
    _print_round_table(arm_b)

    # Summary
    def _delta(old_v, new_v, lower_better=True):
        if old_v == 0:
            return "N/A"
        pct = (new_v - old_v) / old_v * 100
        better = pct < 0 if lower_better else pct > 0
        sym = "✅" if better else ("⚠️" if abs(pct) > 5 else "➖")
        return f"{pct:+.1f}% {sym}"

    print(f"\n  {'Metric':<35} │ {'ARM A':>12} │ {'ARM B':>12} │ {'Delta':>12}")
    print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")

    metrics = [
        ("Uncached prompt (tokens)", arm_a.total_prompt, arm_b.total_prompt, True),
        ("Cache reads (tokens)", arm_a.total_cache_read, arm_b.total_cache_read, False),
        ("Cache writes (tokens)", arm_a.total_cache_write, arm_b.total_cache_write, True),
        ("Output (tokens)", arm_a.total_output, arm_b.total_output, True),
    ]
    for name, a, b, lb in metrics:
        print(f"  {name:<35} │ {a:>12,} │ {b:>12,} │ {_delta(a, b, lb):>12}")

    print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")

    cost_metrics = [
        ("TOTAL COST", cost_a['total'], cost_b['total'], True),
        ("Cache savings (%)", cost_a['savings_pct'], cost_b['savings_pct'], False),
    ]
    for name, a, b, lb in cost_metrics:
        if '%' in name:
            print(f"  {name:<35} │ {a:>11.1f}% │ {b:>11.1f}% │ {_delta(a, b, lb):>12}")
        else:
            print(f"  {name:<35} │ ${a:>11.4f} │ ${b:>11.4f} │ {_delta(a, b, lb):>12}")

    # Avg TTFT
    valid_a = arm_a.valid_rounds
    valid_b = arm_b.valid_rounds
    if valid_a and valid_b:
        avg_ttft_a = sum(r.ttft for r in valid_a) / len(valid_a)
        avg_ttft_b = sum(r.ttft for r in valid_b) / len(valid_b)
        avg_time_a = sum(r.elapsed for r in valid_a) / len(valid_a)
        avg_time_b = sum(r.elapsed for r in valid_b) / len(valid_b)
        print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")
        print(f"  {'Avg TTFT (s)':<35} │ {avg_ttft_a:>12.1f} │ {avg_ttft_b:>12.1f} │ "
              f"{_delta(avg_ttft_a, avg_ttft_b, True):>12}")
        print(f"  {'Avg round time (s)':<35} │ {avg_time_a:>12.1f} │ {avg_time_b:>12.1f} │ "
              f"{_delta(avg_time_a, avg_time_b, True):>12}")

    # Verdict
    money_diff = cost_a['total'] - cost_b['total']
    if abs(money_diff) < 0.001:
        verdict = "NEUTRAL — no significant difference"
    elif money_diff > 0:
        verdict = f"ARM B saves ${money_diff:.4f} ({money_diff/cost_a['total']*100:.1f}%)"
    else:
        verdict = f"ARM A saves ${-money_diff:.4f} ({-money_diff/cost_b['total']*100:.1f}%)"
    print(f"\n  💰 {verdict}")

    # Feature-specific validation
    print(f"\n  📋 Feature Validation:")
    if feature == 'ttl_latch':
        # Check: did ARM A (no latch) suffer a cache drop at round 5?
        if len(arm_a.valid_rounds) >= 5:
            r4 = arm_a.valid_rounds[3]  # round 4 (before toggle)
            r5 = arm_a.valid_rounds[4]  # round 5 (after toggle)
            if r5.cache_read < r4.cache_read * 0.5:
                print(f"     ✅ No-latch arm shows cache drop at R5: "
                      f"{r4.cache_read:,} → {r5.cache_read:,} (TTL toggle eviction)")
            else:
                print(f"     ⚠️ No-latch arm did NOT drop at R5: "
                      f"{r4.cache_read:,} → {r5.cache_read:,} (toggle may not affect cache)")
        if len(arm_b.valid_rounds) >= 5:
            r4 = arm_b.valid_rounds[3]
            r5 = arm_b.valid_rounds[4]
            if r5.cache_read >= r4.cache_read * 0.8:
                print(f"     ✅ Latch arm maintains cache at R5: "
                      f"{r4.cache_read:,} → {r5.cache_read:,} (stable)")
            else:
                print(f"     ⚠️ Latch arm ALSO dropped at R5: "
                      f"{r4.cache_read:,} → {r5.cache_read:,} (investigate)")
    elif feature == 'tool_order':
        # Check: cache read consistency across rounds
        for arm in [arm_a, arm_b]:
            reads = [r.cache_read for r in arm.valid_rounds if r.round_num > 1]
            if len(reads) > 2:
                drops = sum(1 for i in range(1, len(reads)) if reads[i] < reads[i-1] * 0.9)
                tag = "SORTED" if arm == arm_b else "RANDOM"
                if drops == 0:
                    print(f"     ✅ {tag}: cache reads stable ({drops} drops)")
                else:
                    print(f"     ⚠️ {tag}: {drops} cache read drops")

    return cost_a, cost_b


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='A/B test: cache improvement features (TTL latch & tool ordering)')
    parser.add_argument('--model', default=DEFAULT_MODEL,
                        help=f'Model (default: {DEFAULT_MODEL})')
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS,
                        help=f'Rounds per arm (default: {DEFAULT_ROUNDS})')
    parser.add_argument('--feature', default='all',
                        choices=['all', 'ttl_latch', 'tool_order'],
                        help='Feature to test (default: all)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulate without API calls')
    parser.add_argument('--wait', type=int, default=15,
                        help='Seconds between arms (default: 15)')
    args = parser.parse_args()

    features = ['ttl_latch', 'tool_order'] if args.feature == 'all' else [args.feature]

    print(f"\n{'█'*70}")
    print(f"  CACHE IMPROVEMENTS A/B TEST")
    print(f"  Model: {args.model}")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"  Features: {', '.join(features)}")
    print(f"  Rounds per arm: {args.rounds}")
    print(f"  Dry run: {args.dry_run}")
    print(f"{'█'*70}")

    if not args.dry_run and not is_claude(args.model):
        print(f"\n⚠️ Model '{args.model}' is not Claude — cache features are "
              f"Claude-specific. Results may be less meaningful.")

    all_results = {}

    for feature in features:
        print(f"\n\n{'▓'*70}")
        print(f"  TESTING FEATURE: {feature}")
        print(f"{'▓'*70}")

        if feature == 'ttl_latch':
            # ARM A: NO latch (vulnerable to mid-session toggle)
            arm_a = _run_ttl_latch_test(
                args.model, args.rounds,
                'NO_LATCH', use_latch=False, dry_run=args.dry_run)

            if not args.dry_run:
                print(f"\n  ⏳ Waiting {args.wait}s between arms...")
                time.sleep(args.wait)

            # ARM B: WITH latch (stable through mid-session toggle)
            arm_b = _run_ttl_latch_test(
                args.model, args.rounds,
                'WITH_LATCH', use_latch=True, dry_run=args.dry_run)

        elif feature == 'tool_order':
            # ARM A: RANDOM order (simulates async tool completion)
            arm_a = _run_tool_order_test(
                args.model, args.rounds,
                'RANDOM_ORDER', use_sorting=False, dry_run=args.dry_run)

            if not args.dry_run:
                print(f"\n  ⏳ Waiting {args.wait}s between arms...")
                time.sleep(args.wait)

            # ARM B: SORTED order
            arm_b = _run_tool_order_test(
                args.model, args.rounds,
                'SORTED_ORDER', use_sorting=True, dry_run=args.dry_run)

        cost_a, cost_b = print_comparison(arm_a, arm_b, feature)
        all_results[feature] = {
            'arm_a': arm_a,
            'arm_b': arm_b,
            'cost_a': cost_a,
            'cost_b': cost_b,
        }

        if feature != features[-1] and not args.dry_run:
            print(f"\n  ⏳ Waiting {args.wait}s between features...")
            time.sleep(args.wait)

    # Final summary
    print(f"\n\n{'█'*70}")
    print(f"  FINAL SUMMARY")
    print(f"{'█'*70}")

    for feature, data in all_results.items():
        cost_a = data['cost_a']
        cost_b = data['cost_b']
        diff = cost_a['total'] - cost_b['total']
        pct = diff / cost_a['total'] * 100 if cost_a['total'] > 0 else 0

        if feature == 'ttl_latch':
            a_label = "NO_LATCH"
            b_label = "WITH_LATCH"
        else:
            a_label = "RANDOM"
            b_label = "SORTED"

        if diff > 0.001:
            print(f"  ✅ {feature}: {b_label} saves ${diff:.4f} ({pct:.1f}%) vs {a_label}")
        elif diff < -0.001:
            print(f"  ⚠️ {feature}: {a_label} is ${-diff:.4f} ({-pct:.1f}%) cheaper — {b_label} regresses!")
        else:
            print(f"  ➖ {feature}: No significant difference (${abs(diff):.4f}, {abs(pct):.1f}%)")

    # Save results
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    output_path = f"debug/cache_improvements_ab_{timestamp}.json"
    try:
        output = {}
        for feature, data in all_results.items():
            output[feature] = {
                'arm_a': {
                    'label': data['arm_a'].label,
                    'rounds': [{k: v for k, v in r.__dict__.items()} for r in data['arm_a'].rounds],
                    'cost': data['cost_a'],
                },
                'arm_b': {
                    'label': data['arm_b'].label,
                    'rounds': [{k: v for k, v in r.__dict__.items()} for r in data['arm_b'].rounds],
                    'cost': data['cost_b'],
                },
            }
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n📁 Results saved to: {output_path}")
    except Exception as e:
        logger.warning('[AB] Could not save results: %s', e)
        print(f"⚠️ Could not save: {e}")

    print(f"{'█'*70}\n")


if __name__ == '__main__':
    main()
