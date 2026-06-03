#!/usr/bin/env python3
"""A/B test: compare OLD vs NEW cache breakpoint placement on live Opus 4.6 API.

Runs two identical multi-round tool conversations:
  A (OLD): BP4 starts scanning from msg[-2] — the previous behavior
  B (NEW): BP4 starts scanning from msg[-1] — the current fix

Both conversations use identical messages, tools, and system prompts.
The ONLY difference is where BP4 is placed.

Measures: cache_read_tokens, cache_write_tokens, prompt_tokens, latency.
Calculates: cost savings, cache hit rate, TTFT improvements.

Usage:
    python debug/test_cache_ab.py [--model MODEL] [--rounds N]
"""

import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm import build_body, stream_chat, is_claude
from lib.model_info import is_claude

# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 8

# Realistic system prompt — must be > 4096 tokens for Opus cache eligibility.
# This mirrors the actual FRC + tool guidance + project context sent in production.
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
  skills.py            — Memory accumulation system
routes/                — Flask Blueprints (chat, common, browser, fund_*)
static/js/             — Frontend (core.js, main.js, ui.js, trading/*.js)
static/                — CSS
debug/                 — Standalone test/benchmark scripts
logs/
  app.log              — Business logic only (lib.*, routes.*, server) INFO+, daily rotation, 30 days
  access.log           — HTTP request log (werkzeug), daily rotation, 14 days
  error.log            — WARNING/ERROR/CRITICAL from ALL sources (5 MB × 10)
  vendor.log           — Third-party libraries WARNING+ (5 MB × 3)
  audit.log            — Structured JSON audit trail
```

## Error Handling Patterns

### API / Network calls
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

### JSON parsing
```python
try:
    data = json.loads(raw)
except (json.JSONDecodeError, TypeError) as e:
    logger.warning('Invalid JSON (len=%d): %s — preview: %.200s', len(raw), e, raw)
    data = {}
```

### Database operations
```python
try:
    db.execute(sql, params)
    db.commit()
except Exception as e:
    logger.error('DB write failed: %s — sql=%.200s params=%s', e, sql, params, exc_info=True)
    db.rollback()
    raise
```

### Background threads
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

## Logging Discipline
Every code path that can fail MUST leave a trace in the log file. Silent failures are the enemy.
- Every except block logs something (debug at minimum).
- Use %-style formatting for lazy evaluation: `logger.info('x=%s', x)`.
- Sanitize secrets: never log API keys, tokens, or full request bodies with credentials.
- Truncate large data: `logger.debug('Response preview: %.500s', body)`.
- Structured prefix: Use `[Module]` or `[op:name]` prefix for easy grepping.

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
- Use apply_diff for small targeted edits, write_file for new files or major rewrites.
- Keep text output brief and direct. Skip filler words, preamble, and unnecessary transitions.
- Focus text output on: decisions that need user input, high-level status updates, errors/blockers.
- If you can say it in one sentence, don't use three.

## File Modification Checklist
Before submitting any code change, verify:
- Logger present: File has `from lib.log import get_logger; logger = get_logger(__name__)`.
- No silent catches: Every except block logs something (debug at minimum).
- Context in logs: Log messages include relevant IDs (conv_id, task_id, url, model, etc.).
- Tracebacks on errors: exc_info=True on logger.error() for unexpected exceptions.
- No f-strings in log calls: Use `logger.info('x=%s', x)` not `logger.info(f'x={x}')`.
- Secrets not logged: API keys, tokens, passwords never appear in log output.
- Large data truncated: Use `%.500s` or `[:500]` to cap logged payloads.

## Additional Context for Tool Guidance
When using tools, follow these patterns:
- **list_dir**: Use for initial project exploration. Shows files with line counts and sizes.
- **read_files**: Batch multiple paths/ranges into ONE call. Each entry: {path, start_line?, end_line?}.
  Files under 40KB auto-expand to whole-file.
- **grep_search**: Case-insensitive regex. Use short patterns like 'handleRequest' not 'def handle.*request'.
- **write_file**: Creates the file if it doesn't exist. Overwrites the entire file.
- **apply_diff**: Search string must match EXACTLY including whitespace/indentation.
  For MULTIPLE edits, pass an 'edits' array — applied sequentially.
- **run_command**: Execute shell command, returns stdout+stderr. Avoid interactive commands.
- **web_search**: Search the web. Prefer fewer, targeted searches over many broad ones.
- **fetch_url**: Fetch full page content. Use after web_search to read promising URLs.

## Security Considerations
- Never execute rm -rf, format, or other destructive commands without explicit user approval.
- File system access is sandboxed to the project directory.
- Commands that modify system state require user confirmation.
- API keys and credentials must never appear in tool results or log output.
"""

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List contents of a directory in the project. Shows files with line counts and sizes, and subdirectories with item counts.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path from project root. Use '.' for root."}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read the contents of one or more files in the project. Can read specific line ranges for large files. Returns file content with line numbers. Each entry in the 'reads' array has 'path' (required), 'start_line' and 'end_line' (optional). When you need to read multiple files, put them all in one call — maximum 20 files per batch.", "parameters": {"type": "object", "properties": {"reads": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}}, "required": ["reads"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Search for a pattern across project files. Returns matching lines with file paths and line numbers. Very useful for finding function definitions, imports, usages, etc. Search is case-insensitive.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "Search pattern — prefer short literal substrings."}, "path": {"type": "string", "description": "Relative path to search in (optional)"}, "include": {"type": "string", "description": "File glob filter, e.g. '*.py' (optional)"}, "context_lines": {"type": "integer", "description": "Number of context lines (optional, default 0, max 10)"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file in the project. Creates the file if it doesn't exist. Overwrites the entire file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "description": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "apply_diff", "description": "Apply targeted search-and-replace edit(s) to file(s). The 'search' string must match EXACTLY (including whitespace/indentation).", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}, "description": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "search", "replace"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute a shell command in the project directory and return its output (stdout + stderr).", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command to execute"}, "timeout": {"type": "integer", "description": "Timeout in seconds (optional)"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web. You may call this multiple times with different queries.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query — be specific and targeted"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch and read the full content of a specific URL (HTML, PDF, plain text).", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Complete URL starting with https://"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "find_files", "description": "Find files by name pattern (glob) in the project.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "File name glob pattern"}, "path": {"type": "string", "description": "Relative path to search in (optional)"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "emit_to_user", "description": "End your response by pointing the user to an existing tool result. TERMINAL tool.", "parameters": {"type": "object", "properties": {"tool_round": {"type": "integer"}, "comment": {"type": "string"}}, "required": ["tool_round", "comment"]}}},
]

# Simulated tool results with realistic content sizes
TOOL_RESULTS = {
    'list_dir': (
        "Directory: .\n\nFiles:\n"
        "  📄 server.py (245L, 8.2KB)\n  📄 bootstrap.py (189L, 6.1KB)\n"
        "  📄 export.py (1120L, 42.3KB)\n  📄 CLAUDE.md (380L, 14.8KB)\n"
        "  📄 requirements.txt (28L, 0.6KB)\n\nSubdirectories:\n"
        "  📁 lib/ (42 items)\n  📁 routes/ (18 items)\n  📁 static/ (31 items)\n"
        "  📁 debug/ (15 items)\n  📁 data/ (6 items)\n  📁 logs/ (5 items)\n"
    ),
    'read_files': (
        "File: lib/llm_client.py (lines 765-920 of 1739)\n"
        "────────────────────────────────────────\n"
        "765 │ def add_cache_breakpoints(body, log_prefix=''):\n"
        '766 │     """Add Anthropic-style ephemeral cache breakpoints.\n'
        "767 │ \n"
        "768 │     Annotates up to 4 content blocks with cache_control for:\n"
        "769 │       1. System messages (1-2 breakpoints for static/dynamic blocks)\n"
        "770 │       2. Last tool definition\n"
        "771 │       3. Conversation tail message\n"
        '772 │     """\n'
        "773 │     model = body.get('model', '')\n"
        "774 │     if not is_claude(model):\n"
        "775 │         return\n"
        "776 │ \n"
        "777 │     messages = body.get('messages', [])\n"
        "778 │ \n"
        "779 │     # Phase 0: Strip ALL existing cache_control from messages & tools\n"
        "780 │     for i, msg in enumerate(messages):\n"
        "781 │         content = msg.get('content')\n"
        "782 │         if isinstance(content, list):\n"
        "783 │             for j, block in enumerate(content):\n"
        "784 │                 if isinstance(block, dict) and 'cache_control' in block:\n"
        "785 │                     content[j] = {k: v for k, v in block.items() if k != 'cache_control'}\n"
        "786 │     tools = body.get('tools')\n"
        "787 │     if tools:\n"
        "788 │         for t_idx, tool in enumerate(tools):\n"
        "789 │             fn = tool.get('function')\n"
        "790 │             if fn and 'cache_control' in fn:\n"
        "791 │                 tools[t_idx] = {**tool, 'function': {k: v for k, v in fn.items() if k != 'cache_control'}}\n"
        "792 │ \n"
        "793 │     bp = 0\n"
        "794 │ \n"
        "795 │     # Cache system messages\n"
        "796 │     for i, msg in enumerate(messages):\n"
        "797 │         if msg.get('role') != 'system' or bp >= 4:\n"
        "798 │             continue\n"
        "799 │         content = msg.get('content', '')\n"
        "800 │         if isinstance(content, str) and content.strip():\n"
        "801 │             messages[i] = {**msg, 'content': [\n"
        "802 │                 {'type': 'text', 'text': content,\n"
        "803 │                  'cache_control': {'type': 'ephemeral'}}\n"
        "804 │             ]}\n"
        "805 │             bp += 1\n"
    ),
    'grep_search': (
        'grep "add_cache_breakpoints" — 12 matches:\n\n'
        'lib/llm_client.py:765:def add_cache_breakpoints(body, log_prefix=\'\'):\n'
        'lib/llm_client.py:1190:    add_cache_breakpoints(body, log_prefix)\n'
        'lib/tasks_pkg/cache_tracking.py:26:  add_cache_breakpoints() places breakpoints to cover the growing prefix.\n'
        'lib/tasks_pkg/cache_tracking.py:45:  from lib.llm import add_cache_breakpoints\n'
        'tests/test_cc_alignment.py:668:    from lib.llm import add_cache_breakpoints\n'
        'debug/test_cache_bp4_live.py:31:    add_cache_breakpoints,\n'
    ),
    'web_search': (
        "Search results:\n\n"
        "1. [Anthropic Docs] Prompt Caching - https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching\n"
        "   Prompt caching reduces costs by up to 90% and latency by up to 85% for long prompts.\n\n"
        "2. [Blog] Reducing LLM API Costs - https://blog.langchain.dev/reducing-llm-costs/\n"
        "   Caching strategies for production LLM applications.\n\n"
        "3. [GitHub] anthropics/anthropic-cookbook - https://github.com/anthropics/anthropic-cookbook\n"
        "   Examples of prompt caching implementation.\n"
    ),
    'fetch_url': (
        "# Anthropic Prompt Caching Documentation\n\n"
        "## Overview\n"
        "Prompt caching is a feature that optimizes API usage by allowing you to cache frequently used context.\n\n"
        "## How it works\n"
        "Cache breakpoints are placed on content blocks. The system creates cache entries for content up to\n"
        "and including each breakpoint. On subsequent requests, if the prefix matches, cached content is read\n"
        "at 0.1x the base input price.\n\n"
        "## Minimum cacheable length\n"
        "- Claude Opus 4, Haiku 4.5: 4,096 tokens\n"
        "- Claude Sonnet 4: 1,024 tokens\n\n"
        "## Cache lifetime\n"
        "- TTL: 5 minutes from last access\n"
        "- Auto-extended on cache hit\n\n"
        "## Pricing\n"
        "- Cache writes: 1.25x base input price\n"
        "- Cache reads: 0.1x base input price (90% savings)\n"
        "- Standard input: 1.0x base input price\n"
    ),
    'find_files': (
        "Found 8 files matching '*.py' in lib/tasks_pkg/:\n"
        "  lib/tasks_pkg/__init__.py\n"
        "  lib/tasks_pkg/orchestrator.py\n"
        "  lib/tasks_pkg/executor.py\n"
        "  lib/tasks_pkg/compaction.py\n"
        "  lib/tasks_pkg/cache_tracking.py\n"
        "  lib/tasks_pkg/streaming_tool_executor.py\n"
        "  lib/tasks_pkg/context_assembly.py\n"
        "  lib/tasks_pkg/result_persistence.py\n"
    ),
}


def _add_cache_breakpoints_OLD(body, log_prefix=''):
    """OLD version: scan from msg[-2] — the buggy behavior."""
    from lib.llm import is_claude as _is_claude
    model = body.get('model', '')
    if not _is_claude(model):
        return

    messages = body.get('messages', [])

    # Strip existing
    for i, msg in enumerate(messages):
        content = msg.get('content')
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and 'cache_control' in block:
                    content[j] = {k: v for k, v in block.items() if k != 'cache_control'}
    tools = body.get('tools')
    if tools:
        for t_idx, tool in enumerate(tools):
            fn = tool.get('function')
            if fn and 'cache_control' in fn:
                tools[t_idx] = {**tool, 'function': {k: v for k, v in fn.items() if k != 'cache_control'}}

    bp = 0

    # Cache system messages
    for i, msg in enumerate(messages):
        if msg.get('role') != 'system' or bp >= 4:
            continue
        content = msg.get('content', '')
        if isinstance(content, str) and content.strip():
            messages[i] = {**msg, 'content': [
                {'type': 'text', 'text': content, 'cache_control': {'type': 'ephemeral'}}
            ]}
            bp += 1
        elif isinstance(content, list) and content:
            for blk_idx, blk in enumerate(content):
                if bp >= 4:
                    break
                if isinstance(blk, dict) and blk.get('type') == 'text':
                    content[blk_idx] = {**blk, 'cache_control': {'type': 'ephemeral'}}
                    bp += 1

    # Cache last tool definition
    tools = body.get('tools')
    if tools and bp < 4:
        fn = tools[-1].get('function')
        if fn:
            tools[-1] = {**tools[-1], 'function': {**fn, 'cache_control': {'type': 'ephemeral'}}}
            bp += 1

    # OLD: scan from msg[-2] only
    if len(messages) >= 3 and bp < 4:
        for _bp4_offset in range(2, min(6, len(messages))):
            idx = len(messages) - _bp4_offset
            if idx <= 0:
                break
            msg = messages[idx]
            if msg.get('role') == 'system':
                break
            content = msg.get('content', '')
            if isinstance(content, str) and content:
                messages[idx] = {**msg, 'content': [
                    {'type': 'text', 'text': content, 'cache_control': {'type': 'ephemeral'}}
                ]}
                bp += 1
                break
            elif isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    content[-1] = {**last, 'cache_control': {'type': 'ephemeral'}}
                    bp += 1
                    break


def _monkeypatch_old_bp4(enable: bool):
    """Temporarily replace add_cache_breakpoints with the OLD version."""
    import lib.llm as _mod
    if enable:
        _mod._original_add_cache_breakpoints = _mod.add_cache_breakpoints
        _mod.add_cache_breakpoints = _add_cache_breakpoints_OLD
    else:
        if hasattr(_mod, '_original_add_cache_breakpoints'):
            _mod.add_cache_breakpoints = _mod._original_add_cache_breakpoints
            del _mod._original_add_cache_breakpoints


def _run_conversation(model: str, num_rounds: int, label: str, use_old_bp4: bool):
    """Run one multi-round tool conversation, collecting per-round stats."""
    print(f"\n{'═'*70}")
    print(f"  {label}")
    print(f"  Model: {model}  |  Rounds: {num_rounds}  |  BP4: {'OLD (msg[-2])' if use_old_bp4 else 'NEW (msg[-1])'}")
    print(f"{'═'*70}")

    if use_old_bp4:
        _monkeypatch_old_bp4(True)

    try:
        return _run_conversation_inner(model, num_rounds, label)
    finally:
        if use_old_bp4:
            _monkeypatch_old_bp4(False)


def _run_conversation_inner(model, num_rounds, label):
    """Inner loop: run rounds, collect stats."""
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': (
            'I want to understand how the cache breakpoint system works in this project. '
            'Please find the relevant code in lib/llm_client.py and explain the '
            'add_cache_breakpoints function. Also check how the orchestrator calls it '
            'and how cache_tracking.py monitors cache performance.'
        )},
    ]

    results = []
    tc_counter = 0
    tool_sequence = [
        # Pre-determined tool calls to make conversations structurally identical
        'list_dir', 'read_files', 'grep_search', 'read_files',
        'web_search', 'fetch_url', 'grep_search', 'find_files',
    ]

    for round_num in range(num_rounds):
        print(f"\n  ── Round {round_num + 1}/{num_rounds} ──")

        # Describe message structure for diagnostics
        roles = [m.get('role', '?') for m in messages]
        contents_empty = []
        for m in messages:
            c = m.get('content', '')
            is_empty = (not c) if isinstance(c, str) else (not c)
            has_tc = bool(m.get('tool_calls'))
            if is_empty and has_tc:
                contents_empty.append('AST_TC_EMPTY')
            elif is_empty:
                contents_empty.append('EMPTY')
            else:
                contents_empty.append('ok')
        print(f"    msgs={len(messages)}: {' → '.join(roles[-6:])}")
        if len(messages) >= 3:
            m2 = messages[-2]
            m1 = messages[-1]
            print(f"    msg[-2]: role={m2.get('role')}, content_empty={not m2.get('content')}, has_tc={bool(m2.get('tool_calls'))}")
            print(f"    msg[-1]: role={m1.get('role')}, content_empty={not m1.get('content')}")

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

        # Measure TTFT and total time
        t0 = time.time()
        ttft = None
        thinking_buf = []
        content_buf = []

        def on_thinking(td):
            thinking_buf.append(td)

        def on_content(cd):
            nonlocal ttft
            if ttft is None:
                ttft = time.time() - t0
            content_buf.append(cd)

        try:
            assistant_msg, finish_reason, usage = stream_chat(
                body,
                on_thinking=on_thinking,
                on_content=on_content,
                log_prefix=f'[{label} R{round_num+1}]',
            )
        except Exception as e:
            print(f"    ❌ API error: {e}")
            results.append({'round': round_num+1, 'error': str(e)})
            break

        elapsed = time.time() - t0
        content = ''.join(content_buf)
        thinking = ''.join(thinking_buf)

        u = usage or {}
        cache_read = u.get('cache_read_tokens') or u.get('cache_read_input_tokens') or u.get('cached_tokens') or 0
        cache_write = u.get('cache_creation_input_tokens') or u.get('cache_write_tokens') or 0
        prompt_tokens = u.get('prompt_tokens', 0)
        output_tokens = u.get('completion_tokens', 0)

        # Classify
        if cache_write > 500 and cache_read > 500:
            status = "HIT+WRITE"
        elif cache_read > 500:
            status = "HIT"
        elif cache_write > 500:
            status = "WRITE"
        else:
            status = "MISS"

        result = {
            'round': round_num + 1,
            'prompt_tokens': prompt_tokens,
            'cache_read': cache_read,
            'cache_write': cache_write,
            'output_tokens': output_tokens,
            'status': status,
            'elapsed': elapsed,
            'ttft': ttft or elapsed,
            'finish_reason': finish_reason,
            'tool_calls': len(assistant_msg.get('tool_calls', [])),
            'content_len': len(content),
            'thinking_len': len(thinking),
            'total_input': prompt_tokens + cache_read + cache_write,
        }
        results.append(result)

        print(f"    ⏱  {elapsed:.1f}s (TTFT: {result['ttft']:.1f}s) | {status}")
        print(f"    📊 prompt={prompt_tokens:,}  cache_read={cache_read:,}  cache_write={cache_write:,}  output={output_tokens:,}")
        print(f"    🔧 tool_calls={result['tool_calls']}  finish={finish_reason}  content={len(content)}  thinking={len(thinking)}")

        # Simulate tool execution for next round
        tool_calls = assistant_msg.get('tool_calls', [])
        if tool_calls and round_num < num_rounds - 1:
            # Append assistant message with tool_calls
            clean_msg = {'role': 'assistant', 'tool_calls': tool_calls}
            if assistant_msg.get('content'):
                clean_msg['content'] = assistant_msg['content']
            else:
                clean_msg['content'] = ''  # Empty content — the bug trigger
            messages.append(clean_msg)

            for tc in tool_calls:
                tc_counter += 1
                fn_name = tc.get('function', {}).get('name', 'unknown')
                # Use pre-determined tool or match by name
                tool_result = TOOL_RESULTS.get(fn_name, f"Tool {fn_name} executed successfully. Result: (mock data)")
                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id', f'call_{tc_counter}'),
                    'content': tool_result,
                })
                print(f"       → {fn_name} result: {len(tool_result)} chars")
        elif not tool_calls:
            print(f"    ✅ Conversation complete (no tool calls)")
            break

    return results


def _compute_cost(results, model='aws.claude-opus-4.6'):
    """Compute total cost from results. Opus 4.6: $15/M in, $75/M out, 1.25x write, 0.1x read."""
    INPUT_PRICE = 15.0   # $/M tokens
    OUTPUT_PRICE = 75.0  # $/M tokens
    CACHE_WRITE_MUL = 1.25
    CACHE_READ_MUL = 0.10

    total_prompt = sum(r.get('prompt_tokens', 0) for r in results if 'error' not in r)
    total_read = sum(r.get('cache_read', 0) for r in results if 'error' not in r)
    total_write = sum(r.get('cache_write', 0) for r in results if 'error' not in r)
    total_output = sum(r.get('output_tokens', 0) for r in results if 'error' not in r)

    cost_prompt = total_prompt * INPUT_PRICE / 1_000_000
    cost_read = total_read * INPUT_PRICE * CACHE_READ_MUL / 1_000_000
    cost_write = total_write * INPUT_PRICE * CACHE_WRITE_MUL / 1_000_000
    cost_output = total_output * OUTPUT_PRICE / 1_000_000
    total_cost = cost_prompt + cost_read + cost_write + cost_output

    # Cost without any caching
    total_all_input = total_prompt + total_read + total_write
    cost_no_cache = total_all_input * INPUT_PRICE / 1_000_000 + cost_output

    return {
        'total_prompt': total_prompt,
        'total_read': total_read,
        'total_write': total_write,
        'total_output': total_output,
        'cost_prompt': cost_prompt,
        'cost_read': cost_read,
        'cost_write': cost_write,
        'cost_output': cost_output,
        'total_cost': total_cost,
        'cost_no_cache': cost_no_cache,
        'savings_vs_no_cache': cost_no_cache - total_cost,
        'savings_pct': (cost_no_cache - total_cost) / cost_no_cache * 100 if cost_no_cache > 0 else 0,
    }


def _print_results_table(results, label):
    """Print per-round results table."""
    print(f"\n  {'Rnd':>3} │ {'Prompt':>8} │ {'CacheRead':>10} │ {'CacheWrite':>11} │ "
          f"{'Output':>7} │ {'Status':>10} │ {'TTFT':>5} │ {'Total':>5}")
    print(f"  {'─'*3}─┼─{'─'*8}─┼─{'─'*10}─┼─{'─'*11}─┼─"
          f"{'─'*7}─┼─{'─'*10}─┼─{'─'*5}─┼─{'─'*5}")

    for r in results:
        if 'error' in r:
            print(f"  {r['round']:>3} │ {'ERROR':>8} │ {'':>10} │ {'':>11} │ {'':>7} │ {'':>10} │ {'':>5} │ {'':>5}")
            continue
        print(f"  {r['round']:>3} │ {r['prompt_tokens']:>8,} │ {r['cache_read']:>10,} │ "
              f"{r['cache_write']:>11,} │ {r['output_tokens']:>7,} │ {r['status']:>10} │ "
              f"{r['ttft']:>5.1f}s │ {r['elapsed']:>5.1f}s")


def run_ab_test(model: str, num_rounds: int):
    """Run the full A/B test."""
    print(f"\n{'█'*70}")
    print(f"  CACHE BREAKPOINT A/B TEST — {model}")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"  Rounds per arm: {num_rounds}")
    print(f"{'█'*70}")

    # ── Arm A: OLD BP4 (scan from msg[-2]) ──
    print(f"\n\n{'▓'*70}")
    print(f"  ARM A: OLD BP4 METHOD (scan from msg[-2])")
    print(f"{'▓'*70}")
    results_old = _run_conversation(model, num_rounds, 'OLD', use_old_bp4=True)

    # Wait 10s between arms to let cache entries expire / avoid cross-contamination
    print(f"\n  ⏳ Waiting 10s between arms to avoid cache cross-contamination...")
    time.sleep(10)

    # ── Arm B: NEW BP4 (scan from msg[-1]) ──
    print(f"\n\n{'▓'*70}")
    print(f"  ARM B: NEW BP4 METHOD (scan from msg[-1])")
    print(f"{'▓'*70}")
    results_new = _run_conversation(model, num_rounds, 'NEW', use_old_bp4=False)

    # ══════════════════════════════════════════════════════
    #  COMPARISON REPORT
    # ══════════════════════════════════════════════════════
    print(f"\n\n{'█'*70}")
    print(f"  COMPARISON REPORT")
    print(f"{'█'*70}")

    # Per-round tables
    print(f"\n  ┌─ ARM A: OLD (msg[-2]) ──────────────────────────────┐")
    _print_results_table(results_old, 'OLD')

    print(f"\n  ┌─ ARM B: NEW (msg[-1]) ──────────────────────────────┐")
    _print_results_table(results_new, 'NEW')

    # Cost comparison
    cost_old = _compute_cost(results_old, model)
    cost_new = _compute_cost(results_new, model)

    valid_old = [r for r in results_old if 'error' not in r]
    valid_new = [r for r in results_new if 'error' not in r]

    hit_old = sum(1 for r in valid_old if 'HIT' in r.get('status', ''))
    hit_new = sum(1 for r in valid_new if 'HIT' in r.get('status', ''))
    miss_old = sum(1 for r in valid_old if r.get('status') == 'MISS')
    miss_new = sum(1 for r in valid_new if r.get('status') == 'MISS')

    avg_ttft_old = sum(r.get('ttft', 0) for r in valid_old) / max(len(valid_old), 1)
    avg_ttft_new = sum(r.get('ttft', 0) for r in valid_new) / max(len(valid_new), 1)
    avg_time_old = sum(r.get('elapsed', 0) for r in valid_old) / max(len(valid_old), 1)
    avg_time_new = sum(r.get('elapsed', 0) for r in valid_new) / max(len(valid_new), 1)

    print(f"\n\n  {'='*60}")
    print(f"  METRICS COMPARISON")
    print(f"  {'='*60}")
    print(f"")
    print(f"  {'Metric':<30} │ {'OLD (A)':>12} │ {'NEW (B)':>12} │ {'Delta':>10}")
    print(f"  {'─'*30}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*10}")

    def _row(label, old_val, new_val, fmt=',.0f', is_cost=False, lower_better=True):
        if isinstance(old_val, float) and abs(old_val) < 100:
            old_s = f"${old_val:.4f}" if is_cost else f"{old_val:{fmt}}"
            new_s = f"${new_val:.4f}" if is_cost else f"{new_val:{fmt}}"
        else:
            old_s = f"${old_val:.4f}" if is_cost else f"{old_val:{fmt}}"
            new_s = f"${new_val:.4f}" if is_cost else f"{new_val:{fmt}}"

        if old_val > 0:
            delta_pct = (new_val - old_val) / old_val * 100
            better = delta_pct < 0 if lower_better else delta_pct > 0
            symbol = "✅" if better else ("⚠️" if abs(delta_pct) > 5 else "➖")
            delta_s = f"{delta_pct:+.1f}% {symbol}"
        else:
            delta_s = "N/A"
        print(f"  {label:<30} │ {old_s:>12} │ {new_s:>12} │ {delta_s:>10}")

    _row("Uncached prompt (tokens)", cost_old['total_prompt'], cost_new['total_prompt'], ',.0f')
    _row("Cache reads (tokens)", cost_old['total_read'], cost_new['total_read'], ',.0f', lower_better=False)
    _row("Cache writes (tokens)", cost_old['total_write'], cost_new['total_write'], ',.0f')
    _row("Output (tokens)", cost_old['total_output'], cost_new['total_output'], ',.0f')
    print(f"  {'─'*30}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*10}")
    _row("Total input cost", cost_old['cost_prompt']+cost_old['cost_read']+cost_old['cost_write'],
         cost_new['cost_prompt']+cost_new['cost_read']+cost_new['cost_write'], '.4f', is_cost=True)
    _row("Output cost", cost_old['cost_output'], cost_new['cost_output'], '.4f', is_cost=True)
    _row("TOTAL COST", cost_old['total_cost'], cost_new['total_cost'], '.4f', is_cost=True)
    _row("Cost w/o any caching", cost_old['cost_no_cache'], cost_new['cost_no_cache'], '.4f', is_cost=True)
    print(f"  {'─'*30}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*10}")
    _row("Cache hit rate", hit_old/max(len(valid_old),1)*100, hit_new/max(len(valid_new),1)*100, '.0f', lower_better=False)
    _row("Cache miss count", miss_old, miss_new, '.0f')
    _row("Avg TTFT (s)", avg_ttft_old, avg_ttft_new, '.1f')
    _row("Avg total time (s)", avg_time_old, avg_time_new, '.1f')

    # Money saved
    money_saved = cost_old['total_cost'] - cost_new['total_cost']
    pct_saved = money_saved / cost_old['total_cost'] * 100 if cost_old['total_cost'] > 0 else 0

    print(f"\n\n  {'='*60}")
    print(f"  BOTTOM LINE")
    print(f"  {'='*60}")
    print(f"")
    print(f"  💰 Money saved (NEW vs OLD):   ${money_saved:.4f}  ({pct_saved:+.1f}%)")
    print(f"  📈 Cache hit rate improvement:  {hit_old}/{len(valid_old)} → {hit_new}/{len(valid_new)}")
    print(f"  ⏱  TTFT change:                {avg_ttft_old:.1f}s → {avg_ttft_new:.1f}s ({(avg_ttft_new-avg_ttft_old)/max(avg_ttft_old,0.01)*100:+.1f}%)")

    # Projection to conversation mnk84kthdr2x08 scale
    if len(valid_old) > 1 and len(valid_new) > 1:
        # Per-round savings
        per_round_prompt_old = cost_old['total_prompt'] / len(valid_old)
        per_round_prompt_new = cost_new['total_prompt'] / len(valid_new)
        per_round_read_old = cost_old['total_read'] / len(valid_old)
        per_round_read_new = cost_new['total_read'] / len(valid_new)

        # Project to 54 rounds (mnk84kthdr2x08 scale)
        proj_cost_old = cost_old['total_cost'] / len(valid_old) * 54
        proj_cost_new = cost_new['total_cost'] / len(valid_new) * 54
        proj_saved = proj_cost_old - proj_cost_new

        print(f"\n  📐 Projection to 54-round conversation (mnk84kthdr2x08 scale):")
        print(f"     OLD method: ~${proj_cost_old:.2f}")
        print(f"     NEW method: ~${proj_cost_new:.2f}")
        print(f"     Projected savings: ~${proj_saved:.2f} ({proj_saved/max(proj_cost_old,0.01)*100:.0f}%)")

    print(f"\n  {'='*60}")
    if pct_saved > 10:
        print(f"  ✅ SIGNIFICANT IMPROVEMENT — NEW BP4 method saves {pct_saved:.0f}% on input costs")
    elif pct_saved > 0:
        print(f"  ✅ IMPROVEMENT — NEW BP4 method saves {pct_saved:.1f}% (modest)")
    elif pct_saved > -5:
        print(f"  ➖ NEUTRAL — No significant cost difference ({pct_saved:.1f}%)")
    else:
        print(f"  ⚠️  REGRESSION — NEW method costs MORE ({pct_saved:.1f}%) — investigate!")
    print(f"  {'='*60}\n")

    return results_old, results_new


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='A/B test: OLD vs NEW cache breakpoint placement')
    parser.add_argument('--model', default=DEFAULT_MODEL, help=f'Model (default: {DEFAULT_MODEL})')
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS, help=f'Rounds per arm (default: {DEFAULT_ROUNDS})')
    args = parser.parse_args()

    run_ab_test(args.model, args.rounds)
