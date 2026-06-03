#!/usr/bin/env python3
"""Cache Behavior Validation & A/B Test Suite.

Validates that the current cache invalidation pattern is normal by:
  1. Running multi-round, multi-tool conversations with OLD vs NEW BP4
  2. Verifying per-round cache_read/cache_write token progression
  3. Detecting abnormal patterns: excessive cache writes, oscillation, 
     unexplained drops, cost regression
  4. Multi-user/assistant alternation with interleaved tool calls

Scenarios tested:
  - Scenario A: Single user query → 12+ rounds of diverse tool calls
  - Scenario B: Multi-turn user conversation (3 user messages) with tool calls
  - Scenario C: Parallel tool calls (multiple tool_calls per assistant)
  - Scenario D: Mixed content assistants (some with text, some empty)

For each scenario, runs both OLD (msg[-2]) and NEW (msg[-1]) BP4 placement,
compares cache efficiency, cost, and detects anomalies.

Usage:
    python debug/test_cache_validation.py [--model MODEL] [--rounds N] [--scenario all|A|B|C|D]
    python debug/test_cache_validation.py --dry-run   # Validate logic without API calls

Expects:
    - Valid provider config in data/config/server_config.json or env vars
"""

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm import add_cache_breakpoints, build_body, stream_chat
from lib.model_info import is_claude

# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 12

# Realistic system prompt (> 4096 tokens for Opus cache eligibility)
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
"""

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List contents of a directory. Shows files with line counts and sizes, and subdirectories with item counts.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path from project root."}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read the contents of one or more files. Can read specific line ranges. Each entry in 'reads' array has 'path' (required), 'start_line' and 'end_line' (optional). Max 20 files per batch.", "parameters": {"type": "object", "properties": {"reads": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}}, "required": ["reads"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Search for a pattern across project files. Returns matching lines with file paths and line numbers. Case-insensitive.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "context_lines": {"type": "integer"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file. Creates if doesn't exist. Overwrites entirely.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "description": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "apply_diff", "description": "Apply targeted search-and-replace edit(s). Search must match EXACTLY.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}, "description": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "search", "replace"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute a shell command and return stdout + stderr.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web for information.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch and read full content of a URL (HTML, PDF, plain text).", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "find_files", "description": "Find files by name pattern (glob).", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "emit_to_user", "description": "End response by pointing the user to an existing tool result. TERMINAL tool.", "parameters": {"type": "object", "properties": {"tool_round": {"type": "integer"}, "comment": {"type": "string"}}, "required": ["tool_round", "comment"]}}},
]

# ── Simulated tool results with realistic sizes ──
# Each produces a response of varying length to simulate real tool output diversity
TOOL_RESULTS = {
    'list_dir': (
        "Directory: .\n\nFiles:\n"
        "  📄 server.py (245L, 8.2KB)\n  📄 bootstrap.py (189L, 6.1KB)\n"
        "  📄 export.py (1120L, 42.3KB)\n  📄 CLAUDE.md (380L, 14.8KB)\n"
        "  📄 requirements.txt (28L, 0.6KB)\n\nSubdirectories:\n"
        "  📁 lib/ (42 items)\n  📁 routes/ (18 items)\n  📁 static/ (31 items)\n"
        "  📁 debug/ (15 items)\n  📁 tests/ (22 items)\n  📁 benchmarks/ (4 items)\n"
    ),
    'read_files': (
        "File: lib/llm_client.py (lines 764-920 of 1736)\n"
        "────────────────────────────────────────\n"
        "764 │ def add_cache_breakpoints(body, log_prefix=''):\n"
        '765 │     """Add Anthropic-style ephemeral cache breakpoints.\n'
        "766 │ \n"
        "767 │     Annotates up to 4 content blocks with cache_control for:\n"
        "768 │       1. System messages (1-2 breakpoints for static/dynamic blocks)\n"
        "769 │       2. Last tool definition\n"
        "770 │       3. Conversation tail message\n"
        '771 │     """\n'
        "772 │     model = body.get('model', '')\n"
        "773 │     if not is_claude(model):\n"
        "774 │         return\n"
        "775 │     messages = body.get('messages', [])\n"
        "776 │     # Phase 0: Strip ALL existing cache_control\n"
        "777 │     for i, msg in enumerate(messages):\n"
        "778 │         content = msg.get('content')\n"
        "779 │         if isinstance(content, list):\n"
        "780 │             for j, block in enumerate(content):\n"
        "781 │                 if isinstance(block, dict) and 'cache_control' in block:\n"
        "782 │                     content[j] = {k: v for k, v in block.items() if k != 'cache_control'}\n"
    ),
    'grep_search': (
        'grep "add_cache_breakpoints" — 12 matches:\n\n'
        'lib/llm_client.py:764:def add_cache_breakpoints(body, log_prefix=\'\'):\n'
        'lib/llm_client.py:1190:    add_cache_breakpoints(body, log_prefix)\n'
        'lib/tasks_pkg/cache_tracking.py:26:  add_cache_breakpoints() places breakpoints to cover the growing prefix.\n'
        'lib/tasks_pkg/cache_tracking.py:45:  from lib.llm import add_cache_breakpoints\n'
        'tests/test_cache_breakpoints.py:1:"""Comprehensive regression tests for prompt-cache breakpoint placement.\n'
        'tests/test_cache_breakpoints.py:35:from lib.llm import add_cache_breakpoints\n'
        'debug/test_cache_ab.py:259:\'lib/llm_client.py:765:def add_cache_breakpoints...\n'
        'debug/test_cache_bp4_live.py:278:"def add_cache_breakpoints(body, log_prefix=\'\')...\n'
    ),
    'web_search': (
        "Search results:\n\n"
        "1. [Anthropic Docs] Prompt Caching — https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching\n"
        "   Prompt caching reduces costs by up to 90% and latency by up to 85% for long prompts.\n\n"
        "2. [Blog] Reducing LLM API Costs — https://blog.langchain.dev/reducing-llm-costs/\n"
        "   Caching strategies for production LLM applications.\n"
    ),
    'fetch_url': (
        "# Anthropic Prompt Caching Documentation\n\n"
        "## Overview\n"
        "Prompt caching optimizes API usage by caching frequently used context.\n\n"
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
        "Found 12 files matching '*.py' in lib/tasks_pkg/:\n"
        "  lib/tasks_pkg/__init__.py\n  lib/tasks_pkg/orchestrator.py\n"
        "  lib/tasks_pkg/executor.py\n  lib/tasks_pkg/compaction.py\n"
        "  lib/tasks_pkg/cache_tracking.py\n  lib/tasks_pkg/streaming_tool_executor.py\n"
        "  lib/tasks_pkg/context_assembly.py\n  lib/tasks_pkg/result_persistence.py\n"
        "  lib/tasks_pkg/tool_dispatch.py\n  lib/tasks_pkg/checkpoint.py\n"
    ),
    'run_command': (
        "$ find . -name '*.py' | wc -l\n87\n\n"
        "$ wc -l lib/llm_client.py\n1736 lib/llm_client.py\n"
    ),
    'apply_diff': "✅ Applied 1 edit to lib/example.py",
    'write_file': "✅ Wrote 42 lines to debug/test_output.py",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  OLD BP4 implementation (for A/B comparison)
# ═══════════════════════════════════════════════════════════════════════════════

def _add_cache_breakpoints_SINGLE(body, log_prefix='', use_1h=False):
    """Claude Code style: single breakpoint on msg[-1].

    Claude Code's philosophy (from their source):
      - Only 1 cache_control marker per request
      - Placed on msg[-1] (or msg[-2] if skipCacheWrite)
      - Reason: Mycro KV cache engine retains local-attention pages at each
        cache_control position. Multiple markers waste KV pages.
    """
    model = body.get('model', '')
    if not is_claude(model):
        return

    messages = body.get('messages', [])
    _cc = {'type': 'ephemeral'}
    if use_1h:
        _cc['ttl'] = '1h'

    # Strip all existing cache_control
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

    # Single breakpoint: scan from msg[-1] backwards for content
    if len(messages) >= 2:
        for offset in range(1, min(6, len(messages))):
            idx = len(messages) - offset
            if idx <= 0:
                break
            msg = messages[idx]
            if msg.get('role') == 'system':
                break
            content = msg.get('content', '')
            if isinstance(content, str) and content:
                messages[idx] = {**msg, 'content': [
                    {'type': 'text', 'text': content, 'cache_control': dict(_cc)}
                ]}
                break
            elif isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    content[-1] = {**last, 'cache_control': dict(_cc)}
                    break


def _add_cache_breakpoints_OLD(body, log_prefix=''):
    """OLD version: BP4 scans from msg[-2] — the buggy behavior."""
    model = body.get('model', '')
    if not is_claude(model):
        return

    messages = body.get('messages', [])

    # Phase 0: strip
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

    tools = body.get('tools')
    if tools and bp < 4:
        fn = tools[-1].get('function')
        if fn:
            tools[-1] = {**tools[-1], 'function': {**fn, 'cache_control': {'type': 'ephemeral'}}}
            bp += 1

    # OLD: scan from msg[-2]
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


# Arm configurations for the test
# Each arm defines a label, description, BP function override, and TTL setting
ARM_CONFIGS = {
    'OLD':       {'desc': '4 BPs, BP4=msg[-2], 5m TTL',      'bp_fn': 'old',    'ttl': False},
    'NEW':       {'desc': '4 BPs, BP4=msg[-1], 5m TTL',      'bp_fn': 'new',    'ttl': False},
    'NEW_1h':    {'desc': '4 BPs, BP4=msg[-1], mixed TTL',   'bp_fn': 'new',    'ttl': True},
    'SINGLE':    {'desc': '1 BP on msg[-1], 5m TTL (CC)',     'bp_fn': 'single', 'ttl': False},
    'SINGLE_1h': {'desc': '1 BP on msg[-1], 1h TTL (CC+1h)', 'bp_fn': 'single_1h', 'ttl': True},
}


def _monkeypatch_arm(arm_key: str):
    """Configure add_cache_breakpoints for the given arm."""
    import lib.llm as _mod
    import lib as _lib

    if not hasattr(_mod, '_original_add_cache_breakpoints'):
        _mod._original_add_cache_breakpoints = _mod.add_cache_breakpoints

    cfg = ARM_CONFIGS[arm_key]
    _lib.CACHE_EXTENDED_TTL = cfg['ttl']

    if cfg['bp_fn'] == 'old':
        _mod.add_cache_breakpoints = _add_cache_breakpoints_OLD
    elif cfg['bp_fn'] == 'new':
        _mod.add_cache_breakpoints = _mod._original_add_cache_breakpoints
    elif cfg['bp_fn'] == 'single':
        _mod.add_cache_breakpoints = lambda body, log_prefix='': _add_cache_breakpoints_SINGLE(body, log_prefix, use_1h=False)
    elif cfg['bp_fn'] == 'single_1h':
        _mod.add_cache_breakpoints = lambda body, log_prefix='': _add_cache_breakpoints_SINGLE(body, log_prefix, use_1h=True)


def _restore_arm():
    """Restore original add_cache_breakpoints."""
    import lib.llm as _mod
    import lib as _lib
    if hasattr(_mod, '_original_add_cache_breakpoints'):
        _mod.add_cache_breakpoints = _mod._original_add_cache_breakpoints
        del _mod._original_add_cache_breakpoints
    _lib.CACHE_EXTENDED_TTL = False


def _monkeypatch_bp4(use_old: bool):
    """Legacy: swap add_cache_breakpoints with OLD or restore NEW."""
    if use_old:
        _monkeypatch_arm('OLD')
    else:
        _restore_arm()


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
    scenario: str
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
#  Scenario definitions
# ═══════════════════════════════════════════════════════════════════════════════

def _build_scenario_A_messages():
    """Scenario A: Single user query → multi-round tool exploration.
    User asks a complex question requiring many tool calls."""
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': (
            'I want to understand the full cache breakpoint system in this project. '
            'Please: (1) find the add_cache_breakpoints function, (2) read the cache_tracking.py module, '
            '(3) check how the orchestrator uses them, (4) look at the test coverage, '
            'and (5) explain the full flow from breakpoint placement to cache detection.'
        )},
    ]


def _build_scenario_B_messages():
    """Scenario B: Multi-turn user conversation with tool calls.
    Simulates user asking follow-up questions across 3 user messages."""
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': 'What is the project structure? Give me an overview.'},
    ]


def _build_scenario_C_messages():
    """Scenario C: Conversation likely to produce parallel tool calls.
    User asks for multiple things at once, encouraging batched calls."""
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': (
            'I need you to do several things simultaneously:\n'
            '1. List the root directory structure\n'
            '2. Find all Python files that import from lib.log\n'
            '3. Search for all TODO comments in the project\n'
            '4. Check the test directory structure\n'
            'Do as many of these in parallel as you can.'
        )},
    ]


def _build_scenario_D_messages():
    """Scenario D: Mixed content assistants.
    Some assistant messages will have text before tool calls, others won't.
    Simulates realistic conversation where model sometimes explains before acting."""
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': (
            'Help me debug the cache breakpoint issue. I think there is a bug '
            'where cache_write tokens are too high. Read the relevant code and '
            'give me your analysis step by step.'
        )},
    ]


# Follow-up user messages for Scenario B (injected after certain rounds)
SCENARIO_B_FOLLOWUPS = {
    4: "Great, now show me the main LLM client code — how does build_body work?",
    8: "One more thing — what tests exist for cache breakpoints? Show me the test file.",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Conversation runner
# ═══════════════════════════════════════════════════════════════════════════════

def _get_tool_result(fn_name):
    """Get a simulated tool result."""
    return TOOL_RESULTS.get(fn_name, f"Tool {fn_name} executed successfully.")


def run_conversation(
    model: str,
    num_rounds: int,
    label: str,
    scenario: str,
    arm_key: str = 'NEW',
    *,
    use_old_bp4: bool = False,  # legacy compat
    dry_run: bool = False,
) -> ConvResult:
    """Run one multi-round conversation.

    Args:
        model: Model ID (e.g. 'aws.claude-opus-4.6')
        num_rounds: Max rounds to run
        label: Display label
        scenario: Scenario ID ('A', 'B', 'C', 'D')
        arm_key: Arm configuration key from ARM_CONFIGS
        use_old_bp4: Legacy flag (if True, overrides arm_key to 'OLD')
        dry_run: If True, skip API calls and use mock data
    """
    # Legacy compat
    if use_old_bp4:
        arm_key = 'OLD'

    cfg = ARM_CONFIGS.get(arm_key, ARM_CONFIGS['NEW'])
    print(f"\n  {'═'*60}")
    print(f"  {label} — Scenario {scenario}")
    print(f"  Strategy: {cfg['desc']}")
    print(f"  {'═'*60}")

    # Select scenario messages
    builders = {'A': _build_scenario_A_messages, 'B': _build_scenario_B_messages,
                'C': _build_scenario_C_messages, 'D': _build_scenario_D_messages}
    messages = builders[scenario]()

    _monkeypatch_arm(arm_key)

    result = ConvResult(label=label, scenario=scenario, model=model)
    tc_counter = 0

    try:
        for round_num in range(num_rounds):
            # Scenario B: inject follow-up user messages at specific rounds
            if scenario == 'B' and round_num in SCENARIO_B_FOLLOWUPS:
                messages.append({'role': 'user', 'content': SCENARIO_B_FOLLOWUPS[round_num]})
                print(f"\n  👤 User follow-up injected at round {round_num + 1}")

            # Log message structure
            roles = [m.get('role', '?')[:1].upper() for m in messages]
            n_empty = sum(1 for m in messages
                         if m.get('role') == 'assistant' and not m.get('content'))
            print(f"\n  ── R{round_num+1}/{num_rounds} ── "
                  f"msgs={len(messages)} ({''.join(roles[-8:])}) "
                  f"empty_asst={n_empty}")

            if dry_run:
                rr = _dry_run_round(round_num, messages)
                result.rounds.append(rr)
                # Simulate tool call for next round
                messages.append({
                    'role': 'assistant', 'content': '',
                    'tool_calls': [{'id': f'tc_{tc_counter}', 'type': 'function',
                                    'function': {'name': 'read_files', 'arguments': '{}'}}],
                })
                messages.append({'role': 'tool', 'tool_call_id': f'tc_{tc_counter}',
                                 'content': _get_tool_result('read_files')})
                tc_counter += 1
                continue

            # Build API body
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

            # Make API call
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
                    log_prefix=f'[{label}-{scenario} R{round_num+1}]',
                )
            except Exception as e:
                print(f"    ❌ API error: {e}")
                result.rounds.append(RoundResult(round_num=round_num+1, error=str(e)))
                break

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
            result.rounds.append(rr)

            print(f"    ⏱ {elapsed:.1f}s (TTFT: {rr.ttft:.1f}s) | {status}")
            print(f"    📊 pt={prompt_tokens:,}  cr={cache_read:,}  "
                  f"cw={cache_write:,}  out={output_tokens:,}")
            print(f"    🔧 tools={rr.tool_calls}  finish={finish_reason}")

            # Append assistant + tool results for next round
            tool_calls = assistant_msg.get('tool_calls', [])
            if tool_calls and round_num < num_rounds - 1:
                clean_msg = {'role': 'assistant', 'tool_calls': tool_calls}
                clean_msg['content'] = assistant_msg.get('content') or ''
                messages.append(clean_msg)

                for tc in tool_calls:
                    tc_counter += 1
                    fn_name = tc.get('function', {}).get('name', 'unknown')
                    tool_result = _get_tool_result(fn_name)
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc.get('id', f'call_{tc_counter}'),
                        'content': tool_result,
                    })
                    print(f"       → {fn_name}: {len(tool_result)} chars")
            elif not tool_calls:
                print(f"    ✅ Complete (no tool calls)")
                break

    finally:
        _restore_arm()

    return result


def _dry_run_round(round_num, messages):
    """Simulate a round without API calls (for --dry-run mode)."""
    # Simulate increasing cache reads, decreasing uncached
    base_input = max(1, 20 - round_num)
    cache_read = min(50000, 5000 + round_num * 4000)
    cache_write = max(500, 3000 - round_num * 200)
    return RoundResult(
        round_num=round_num+1,
        prompt_tokens=base_input,
        cache_read=cache_read,
        cache_write=cache_write,
        output_tokens=200 + round_num * 50,
        total_input=base_input + cache_read + cache_write,
        elapsed=3.0 + round_num * 0.2,
        ttft=2.0,
        tool_calls=2,
        finish_reason='tool_calls',
        status='HIT+WRITE' if round_num > 0 else 'WRITE',
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Cost computation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_cost(cr: ConvResult, *, input_price=15.0, output_price=75.0,
                 cw_mul=1.25, cr_mul=0.10):
    """Compute cost breakdown for a conversation result.

    Returns dict with cost components and anomaly flags.
    """
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

    # No-cache baseline
    cost_no_cache = ti * input_price / 1_000_000 + cost_output
    savings = cost_no_cache - total
    savings_pct = savings / cost_no_cache * 100 if cost_no_cache > 0 else 0

    # ── Anomaly detection ──
    anomalies = []
    n = len(cr.valid_rounds)

    # 1. Cache write ratio: if cache_write > 50% of total_input, something's wrong
    cw_ratio = tw / ti * 100 if ti > 0 else 0
    if cw_ratio > 50 and n > 3:
        anomalies.append(f"⚠️ High cache write ratio: {cw_ratio:.0f}% (expect <40%)")

    # 2. Cache read trend: should generally increase over rounds
    reads = [r.cache_read for r in cr.valid_rounds if r.round_num > 1]
    if len(reads) > 3:
        drops = sum(1 for i in range(1, len(reads)) if reads[i] < reads[i-1] * 0.5)
        drop_rate = drops / len(reads) * 100
        if drop_rate > 30:
            anomalies.append(f"⚠️ Cache read instability: {drops}/{len(reads)} drops ({drop_rate:.0f}%)")

    # 3. Prompt tokens: after cache is established, should be near 0-10 (Anthropic)
    # or near total_input (OpenAI convention)
    first_hit_round = next((r.round_num for r in cr.valid_rounds if r.cache_read > 0), None)
    if first_hit_round:
        late_prompts = [r.prompt_tokens for r in cr.valid_rounds
                        if r.round_num >= first_hit_round]
    else:
        late_prompts = []
    if late_prompts:
        avg_late_prompt = sum(late_prompts) / len(late_prompts)
        # Anthropic convention: prompt_tokens = uncached only ≈ 1-10
        # OpenAI convention: prompt_tokens = total ≈ 10000+
        if 10 < avg_late_prompt < 1000:
            anomalies.append(f"⚠️ Unusual prompt_tokens range: avg={avg_late_prompt:.0f} "
                             f"(expected ~1-5 for Anthropic or ~10k+ for OpenAI)")

    # 4. Cache write spikes: rounds where cw > 80% of total_input indicate full re-cache
    full_recache_rounds = [r.round_num for r in cr.valid_rounds
                           if r.round_num > 1 and r.total_input > 0
                           and r.cache_write / r.total_input > 0.8]
    if full_recache_rounds:
        anomalies.append(f"⚠️ Full re-cache on rounds: {full_recache_rounds} "
                         f"(server-side TTL eviction)")

    # 5. Cost regression check: negative savings means caching costs MORE
    if savings < 0:
        anomalies.append(f"🔴 COST REGRESSION: caching costs ${-savings:.4f} MORE than no caching!")

    return {
        'cost_prompt': cost_prompt,
        'cost_read': cost_read,
        'cost_write': cost_write,
        'cost_output': cost_output,
        'total': total,
        'cost_no_cache': cost_no_cache,
        'savings': savings,
        'savings_pct': savings_pct,
        'cw_ratio': cw_ratio,
        'anomalies': anomalies,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Report generation
# ═══════════════════════════════════════════════════════════════════════════════

def print_per_round_table(cr: ConvResult):
    """Print per-round details."""
    print(f"\n  {'Rnd':>3} │ {'Prompt':>8} │ {'CacheRead':>10} │ {'CacheWrite':>11} │ "
          f"{'Output':>7} │ {'Status':>10} │ {'Tools':>5} │ {'TTFT':>5} │ {'Total':>5}")
    print(f"  {'─'*3}─┼─{'─'*8}─┼─{'─'*10}─┼─{'─'*11}─┼─"
          f"{'─'*7}─┼─{'─'*10}─┼─{'─'*5}─┼─{'─'*5}─┼─{'─'*5}")
    for r in cr.rounds:
        if r.error:
            print(f"  {r.round_num:>3} │ {'ERROR':>8} │ {r.error[:30]:>30}")
            continue
        print(f"  {r.round_num:>3} │ {r.prompt_tokens:>8,} │ {r.cache_read:>10,} │ "
              f"{r.cache_write:>11,} │ {r.output_tokens:>7,} │ {r.status:>10} │ "
              f"{r.tool_calls:>5} │ {r.ttft:>5.1f}s │ {r.elapsed:>5.1f}s")


def print_comparison(old: ConvResult, new: ConvResult, scenario: str):
    """Print A/B comparison for one scenario."""
    cost_old = compute_cost(old)
    cost_new = compute_cost(new)

    print(f"\n\n  {'▓'*60}")
    print(f"  SCENARIO {scenario} — A/B COMPARISON")
    print(f"  {'▓'*60}")

    print(f"\n  ┌─ OLD (msg[-2]) ─────────────────────────────────┐")
    print_per_round_table(old)

    print(f"\n  ┌─ NEW (msg[-1]) ─────────────────────────────────┐")
    print_per_round_table(new)

    # Summary comparison
    def _delta(old_v, new_v, lower_better=True):
        if old_v == 0:
            return "N/A"
        pct = (new_v - old_v) / old_v * 100
        better = pct < 0 if lower_better else pct > 0
        sym = "✅" if better else ("⚠️" if abs(pct) > 5 else "➖")
        return f"{pct:+.1f}% {sym}"

    print(f"\n  {'Metric':<35} │ {'OLD':>12} │ {'NEW':>12} │ {'Delta':>12}")
    print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")

    metrics = [
        ("Rounds completed", len(old.valid_rounds), len(new.valid_rounds), False),
        ("Uncached prompt (tokens)", old.total_prompt, new.total_prompt, True),
        ("Cache reads (tokens)", old.total_cache_read, new.total_cache_read, False),
        ("Cache writes (tokens)", old.total_cache_write, new.total_cache_write, True),
        ("Output (tokens)", old.total_output, new.total_output, True),
        ("Total input (tokens)", old.total_input, new.total_input, True),
    ]
    for name, o, n, lb in metrics:
        print(f"  {name:<35} │ {o:>12,} │ {n:>12,} │ {_delta(o, n, lb):>12}")

    print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")

    cost_metrics = [
        ("Uncached input cost", cost_old['cost_prompt'], cost_new['cost_prompt'], True),
        ("Cache read cost", cost_old['cost_read'], cost_new['cost_read'], True),
        ("Cache write cost", cost_old['cost_write'], cost_new['cost_write'], True),
        ("Output cost", cost_old['cost_output'], cost_new['cost_output'], True),
        ("TOTAL COST", cost_old['total'], cost_new['total'], True),
        ("Cost without caching", cost_old['cost_no_cache'], cost_new['cost_no_cache'], True),
        ("Cache savings ($)", cost_old['savings'], cost_new['savings'], False),
        ("Cache savings (%)", cost_old['savings_pct'], cost_new['savings_pct'], False),
    ]
    for name, o, n, lb in cost_metrics:
        if 'savings (%)' in name:
            print(f"  {name:<35} │ {o:>11.1f}% │ {n:>11.1f}% │ {_delta(o, n, lb):>12}")
        else:
            print(f"  {name:<35} │ ${o:>11.4f} │ ${n:>11.4f} │ {_delta(o, n, lb):>12}")

    # Avg TTFT
    old_ttft = [r.ttft for r in old.valid_rounds]
    new_ttft = [r.ttft for r in new.valid_rounds]
    if old_ttft and new_ttft:
        avg_old = sum(old_ttft) / len(old_ttft)
        avg_new = sum(new_ttft) / len(new_ttft)
        print(f"  {'─'*35}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")
        print(f"  {'Avg TTFT (s)':<35} │ {avg_old:>12.1f} │ {avg_new:>12.1f} │ {_delta(avg_old, avg_new, True):>12}")
        avg_old_t = sum(r.elapsed for r in old.valid_rounds) / len(old.valid_rounds)
        avg_new_t = sum(r.elapsed for r in new.valid_rounds) / len(new.valid_rounds)
        print(f"  {'Avg round time (s)':<35} │ {avg_old_t:>12.1f} │ {avg_new_t:>12.1f} │ {_delta(avg_old_t, avg_new_t, True):>12}")

    # Anomalies
    for label_name, cost_info in [("OLD", cost_old), ("NEW", cost_new)]:
        if cost_info['anomalies']:
            print(f"\n  🔍 {label_name} Anomalies:")
            for a in cost_info['anomalies']:
                print(f"     {a}")

    # Verdict
    money_saved = cost_old['total'] - cost_new['total']
    pct_saved = money_saved / cost_old['total'] * 100 if cost_old['total'] > 0 else 0
    print(f"\n  💰 NEW saves ${money_saved:.4f} ({pct_saved:+.1f}%) vs OLD")

    # Cache behavior validation
    print(f"\n  📋 Cache Behavior Validation:")

    # Check 1: prompt_tokens should be ~1 per round for NEW (Anthropic convention)
    # Only check rounds AFTER cache is established (first round with cache_read > 0)
    first_hit = next((r.round_num for r in new.valid_rounds if r.cache_read > 0), None)
    if first_hit:
        new_late_prompts = [r.prompt_tokens for r in new.valid_rounds
                           if r.round_num >= first_hit]
    else:
        new_late_prompts = []
    if new_late_prompts:
        avg_p = sum(new_late_prompts) / len(new_late_prompts)
        if avg_p <= 5:
            print(f"     ✅ Uncached tokens ~{avg_p:.1f}/round (expected for Anthropic)")
        elif avg_p <= 50:
            print(f"     ⚠️ Uncached tokens ~{avg_p:.1f}/round (slightly high, may have edge cases)")
        else:
            print(f"     🔴 Uncached tokens ~{avg_p:.1f}/round (BP4 not caching properly!)")

    # Check 2: cache_read should grow monotonically (with allowed drops for TTL)
    new_reads = [r.cache_read for r in new.valid_rounds if r.round_num > 1]
    if len(new_reads) > 2:
        monotonic_violations = sum(1 for i in range(1, len(new_reads))
                                   if new_reads[i] < new_reads[i-1] * 0.95)
        if monotonic_violations == 0:
            print(f"     ✅ Cache reads monotonically increasing (stable caching)")
        elif monotonic_violations <= len(new_reads) * 0.3:
            print(f"     ⚠️ {monotonic_violations} cache read drops (server-side TTL, normal)")
        else:
            print(f"     🔴 {monotonic_violations} cache read drops (possible BP4 issue!)")

    # Check 3: NEW should have fewer total uncached tokens than OLD
    if old.total_prompt > 0 and new.total_prompt < old.total_prompt * 0.5:
        print(f"     ✅ NEW has {(1-new.total_prompt/old.total_prompt)*100:.0f}% fewer uncached tokens")
    elif old.total_prompt > 0:
        ratio = new.total_prompt / old.total_prompt
        if ratio < 1:
            print(f"     ✅ NEW has {(1-ratio)*100:.0f}% fewer uncached tokens (modest)")
        else:
            print(f"     ⚠️ NEW has {(ratio-1)*100:.0f}% MORE uncached tokens (investigate)")


def print_multi_arm_comparison(arms: dict, scenario: str):
    """Print comparison matrix for multiple arms.

    Args:
        arms: {arm_key: ConvResult}
        scenario: Scenario ID
    """
    arm_keys = list(arms.keys())
    costs = {k: compute_cost(v) for k, v in arms.items()}

    print(f"\n\n  {'▓'*70}")
    print(f"  SCENARIO {scenario} — {len(arm_keys)}-ARM COMPARISON")
    print(f"  {'▓'*70}")

    # Per-arm tables
    for ak in arm_keys:
        cfg = ARM_CONFIGS.get(ak, {})
        print(f"\n  ┌─ {ak}: {cfg.get('desc', '')} ─────────────┐")
        print_per_round_table(arms[ak])

    # Cross-arm metrics table
    col_width = max(10, max(len(k) for k in arm_keys) + 2)
    header = f"  {'Metric':<35}"
    for ak in arm_keys:
        header += f" │ {ak:>{col_width}}"
    print(f"\n{header}")
    print(f"  {'─'*35}" + "".join(f"─┼─{'─'*col_width}" for _ in arm_keys))

    metrics = [
        ("Rounds completed",   lambda a: len(a.valid_rounds)),
        ("Uncached (tokens)",   lambda a: a.total_prompt),
        ("Cache reads (tokens)", lambda a: a.total_cache_read),
        ("Cache writes (tokens)", lambda a: a.total_cache_write),
        ("Output (tokens)",     lambda a: a.total_output),
        ("Total input (tokens)", lambda a: a.total_input),
    ]
    for name, fn in metrics:
        row = f"  {name:<35}"
        for ak in arm_keys:
            row += f" │ {fn(arms[ak]):>{col_width},}"
        print(row)

    print(f"  {'─'*35}" + "".join(f"─┼─{'─'*col_width}" for _ in arm_keys))

    cost_metrics = [
        ("Uncached input cost",  lambda c: c['cost_prompt']),
        ("Cache read cost",      lambda c: c['cost_read']),
        ("Cache write cost",     lambda c: c['cost_write']),
        ("Output cost",          lambda c: c['cost_output']),
        ("TOTAL COST",           lambda c: c['total']),
        ("No-cache baseline",    lambda c: c['cost_no_cache']),
        ("Cache savings ($)",    lambda c: c['savings']),
        ("Cache savings (%)",    lambda c: c['savings_pct']),
    ]
    for name, fn in cost_metrics:
        row = f"  {name:<35}"
        for ak in arm_keys:
            val = fn(costs[ak])
            if '%' in name:
                row += f" │ {val:>{col_width - 1}.1f}%"
            else:
                row += f" │ ${val:>{col_width - 1}.4f}"
        print(row)

    # Avg TTFT and round time
    print(f"  {'─'*35}" + "".join(f"─┼─{'─'*col_width}" for _ in arm_keys))
    for name, fn in [
        ("Avg TTFT (s)", lambda a: sum(r.ttft for r in a.valid_rounds) / max(1, len(a.valid_rounds))),
        ("Avg round time (s)", lambda a: sum(r.elapsed for r in a.valid_rounds) / max(1, len(a.valid_rounds))),
    ]:
        row = f"  {name:<35}"
        for ak in arm_keys:
            row += f" │ {fn(arms[ak]):>{col_width}.1f}"
        print(row)

    # Pairwise cost comparison matrix
    if len(arm_keys) > 2:
        print(f"\n  Cost comparison matrix (row saves vs column):")
        header = f"  {'':>12}"
        for ak in arm_keys:
            header += f" │ {ak:>10}"
        print(header)
        print(f"  {'─'*12}" + "".join(f"─┼─{'─'*10}" for _ in arm_keys))
        for ak1 in arm_keys:
            row = f"  {ak1:>12}"
            for ak2 in arm_keys:
                if ak1 == ak2:
                    row += f" │ {'—':>10}"
                else:
                    diff = costs[ak2]['total'] - costs[ak1]['total']
                    pct = diff / costs[ak2]['total'] * 100 if costs[ak2]['total'] > 0 else 0
                    sym = '✅' if pct > 5 else ('🔴' if pct < -5 else '➖')
                    row += f" │ {pct:>+7.1f}%{sym}"
            print(row)
        print(f"  (positive = row is cheaper than column)")

    # Anomalies
    for ak in arm_keys:
        if costs[ak]['anomalies']:
            print(f"\n  🔍 {ak} Anomalies:")
            for a in costs[ak]['anomalies']:
                print(f"     {a}")

    # Best arm determination
    best_key = min(arm_keys, key=lambda k: costs[k]['total'])
    worst_key = max(arm_keys, key=lambda k: costs[k]['total'])
    savings = costs[worst_key]['total'] - costs[best_key]['total']
    pct_gap = savings / costs[worst_key]['total'] * 100 if costs[worst_key]['total'] > 0 else 0
    print(f"\n  🏆 Best: {best_key} (${costs[best_key]['total']:.4f})")
    print(f"  💸 Worst: {worst_key} (${costs[worst_key]['total']:.4f})")
    print(f"  📊 Gap: ${savings:.4f} ({pct_gap:.1f}%)")

    return costs


def print_final_summary_multi(all_results: dict):
    """Print final summary across all scenarios for multi-arm test.

    Args:
        all_results: {scenario_id: {arm_key: ConvResult}}
    """
    print(f"\n\n{'█'*70}")
    print(f"  FINAL SUMMARY — ALL SCENARIOS × ALL ARMS")
    print(f"{'█'*70}")

    # Collect all arm keys across scenarios
    all_arm_keys = []
    for scenario_arms in all_results.values():
        for k in scenario_arms:
            if k not in all_arm_keys:
                all_arm_keys.append(k)

    # Per-arm totals
    arm_totals = {k: 0.0 for k in all_arm_keys}
    arm_wins = {k: 0 for k in all_arm_keys}

    for scenario_id, arms in sorted(all_results.items()):
        costs = {k: compute_cost(v) for k, v in arms.items()}
        best_key = min(arms.keys(), key=lambda k: costs[k]['total'])
        arm_wins[best_key] += 1
        for ak in arms:
            arm_totals[ak] += costs[ak]['total']

        print(f"\n  Scenario {scenario_id}:")
        for ak in all_arm_keys:
            if ak in costs:
                c = costs[ak]
                marker = " 🏆" if ak == best_key else ""
                print(f"    {ak:>12}: ${c['total']:.4f}  "
                      f"(uncached={arms[ak].total_prompt:>6,}  "
                      f"cw_ratio={c['cw_ratio']:.0f}%  "
                      f"savings={c['savings_pct']:.0f}%){marker}")

    # Overall ranking
    print(f"\n  {'─'*60}")
    print(f"  OVERALL RANKING (lower cost = better):")
    ranking = sorted(all_arm_keys, key=lambda k: arm_totals[k])
    for i, ak in enumerate(ranking, 1):
        wins = arm_wins[ak]
        total = arm_totals[ak]
        vs_worst = arm_totals[ranking[-1]] - total
        pct = vs_worst / arm_totals[ranking[-1]] * 100 if arm_totals[ranking[-1]] > 0 else 0
        medal = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣'][min(i-1, 4)]
        cfg = ARM_CONFIGS.get(ak, {})
        print(f"  {medal} {ak:>12}: ${total:.4f}  "
              f"({wins}/{len(all_results)} wins, "
              f"saves {pct:.1f}% vs worst) — {cfg.get('desc', '')}")

    # Recommendation
    best = ranking[0]
    second = ranking[1] if len(ranking) > 1 else None
    gap = (arm_totals[second] - arm_totals[best]) / arm_totals[best] * 100 if second and arm_totals[best] > 0 else 0

    print(f"\n  💡 RECOMMENDATION:")
    if gap > 10:
        print(f"     Use {best} — clearly the best ({gap:.1f}% cheaper than 2nd place)")
    elif gap > 3:
        print(f"     Use {best} — modestly better ({gap:.1f}% cheaper than {second})")
    else:
        print(f"     {best} and {second} are within {gap:.1f}% — pick either")
        # If close, prefer simpler strategy
        if 'SINGLE' in best and 'NEW' in second:
            print(f"     (SINGLE has fewer breakpoints = simpler, less KV waste)")
        elif 'NEW' in best and 'SINGLE' in second:
            print(f"     (NEW has more breakpoints = finer cache granularity)")

    print(f"{'█'*70}\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Cache behavior validation — multi-arm A/B test suite')
    parser.add_argument('--model', default=DEFAULT_MODEL,
                        help=f'Model (default: {DEFAULT_MODEL})')
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS,
                        help=f'Max rounds per scenario arm (default: {DEFAULT_ROUNDS})')
    parser.add_argument('--scenario', default='all',
                        choices=['all', 'A', 'B', 'C', 'D'],
                        help='Scenario to run (default: all)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulate without API calls (validate test logic)')
    parser.add_argument('--wait', type=int, default=15,
                        help='Seconds to wait between arms (default: 15)')
    parser.add_argument('--arms', default='all',
                        help='Comma-separated arm keys to test '
                             '(default: all = OLD,NEW,NEW_1h,SINGLE,SINGLE_1h)')
    args = parser.parse_args()

    scenarios = ['A', 'B', 'C', 'D'] if args.scenario == 'all' else [args.scenario]

    # Parse arm selection
    if args.arms == 'all':
        arm_keys = ['OLD', 'NEW', 'NEW_1h', 'SINGLE', 'SINGLE_1h']
    else:
        arm_keys = [a.strip() for a in args.arms.split(',')]
        for ak in arm_keys:
            if ak not in ARM_CONFIGS:
                print(f"❌ Unknown arm '{ak}'. Available: {', '.join(ARM_CONFIGS.keys())}")
                return

    print(f"\n{'█'*70}")
    print(f"  CACHE BEHAVIOR VALIDATION — {len(arm_keys)}-ARM TEST")
    print(f"  Model: {args.model}")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"  Scenarios: {', '.join(scenarios)}")
    print(f"  Arms: {', '.join(arm_keys)}")
    print(f"  Rounds per arm: {args.rounds}")
    print(f"  Dry run: {args.dry_run}")
    for ak in arm_keys:
        cfg = ARM_CONFIGS[ak]
        print(f"    {ak:>12}: {cfg['desc']}")
    print(f"{'█'*70}")

    if not args.dry_run and not is_claude(args.model):
        print(f"\n❌ Model '{args.model}' is not Claude — cache breakpoints are Claude-only.")
        return

    # Run all arms × all scenarios
    # all_results[scenario] = {arm_key: ConvResult}
    all_results = {}

    for scenario in scenarios:
        scenario_desc = {
            'A': 'Single query → multi-round tool exploration',
            'B': 'Multi-turn user conversation with tool calls',
            'C': 'Parallel tool calls (batched)',
            'D': 'Mixed content assistants (text + empty)',
        }
        print(f"\n\n{'▓'*70}")
        print(f"  SCENARIO {scenario}: {scenario_desc[scenario]}")
        print(f"  Arms: {' → '.join(arm_keys)}")
        print(f"{'▓'*70}")

        scenario_arms = {}

        for i, ak in enumerate(arm_keys):
            if i > 0 and not args.dry_run:
                print(f"\n  ⏳ Waiting {args.wait}s between arms...")
                time.sleep(args.wait)

            result = run_conversation(
                args.model, args.rounds, ak, scenario,
                arm_key=ak, dry_run=args.dry_run)
            scenario_arms[ak] = result

        all_results[scenario] = scenario_arms

        # Print multi-arm comparison for this scenario
        print_multi_arm_comparison(scenario_arms, scenario)

        if not args.dry_run and scenario != scenarios[-1]:
            print(f"\n  ⏳ Waiting {args.wait}s between scenarios...")
            time.sleep(args.wait)

    # Final cross-scenario summary
    print_final_summary_multi(all_results)

    # Save results to JSON
    output_path = f"debug/cache_validation_{time.strftime('%Y%m%d_%H%M%S')}.json"
    output_data = {}
    for scenario_id, arms in all_results.items():
        entry = {}
        for ak, conv in arms.items():
            entry[ak] = {
                'label': conv.label,
                'arm_config': ARM_CONFIGS.get(ak, {}),
                'rounds': [{k: v for k, v in r.__dict__.items()} for r in conv.rounds],
                'cost': compute_cost(conv),
            }
        output_data[scenario_id] = entry
    try:
        with open(output_path, 'w') as f:
            json.dump(output_data, f, indent=2, default=str)
        print(f"📁 Results saved to: {output_path}")
    except Exception as e:
        print(f"⚠️ Could not save results: {e}")


if __name__ == '__main__':
    main()
