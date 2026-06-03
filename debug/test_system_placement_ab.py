#!/usr/bin/env python3
"""A/B Test: CLAUDE.md placement — system message vs first user message.

Compares ChatUI's current approach (Arm A) with Claude Code's approach (Arm B):

  Arm A (SYSTEM_MSG, ChatUI current):
      ALL context (CLAUDE.md + static guidance + memory instructions + date)
      goes into messages[0] with role='system' as two text blocks.
      add_cache_breakpoints() places a breakpoint on each block.

  Arm B (USER_MSG, Claude Code style):
      ONLY the static base prompt + guidance stays in messages[0] role='system'.
      CLAUDE.md is placed in a PREPENDED user message (messages[1]) wrapped
      in <system-reminder> tags — exactly how Claude Code does it.

Cache breakpoint placement (4 total, placed by add_cache_breakpoints):

  For Arm A:
    messages[0] role=system content=[
        {text: CLAUDE.md...,       cache_control: {ephemeral, ttl:1h}}  ← breakpoint 1
        {text: static+guidance..., cache_control: {ephemeral, ttl:1h}}  ← breakpoint 2
    ]
    tools = [..., last_tool: {cache_control: {ephemeral, ttl:1h}}]      ← breakpoint 3
    messages[-1] = {content: [..., cache_control: {ephemeral}]}         ← breakpoint 4

  For Arm B:
    messages[0] role=system content="static+guidance..."
        → Single string, converted to one text block by add_cache_breakpoints
        → breakpoint 1 placed here (~900 tokens — BELOW 4096 Opus threshold!)
    messages[1] role=user = "<system-reminder>CLAUDE.md...</system-reminder>"
        → No breakpoint here (it's a user message, not system)
    tools = [..., last_tool: {cache_control}]                           ← breakpoint 2
    messages[-1] = {content: [..., cache_control]}                      ← breakpoint 3

  How breakpoints cache (Anthropic prefix caching):
    Each breakpoint creates a cache entry for the byte prefix from position 0
    up to and including that block. On the next request, if the prefix bytes
    match, those tokens are served from cache (10% of base price).

    Minimum cacheable prefix size (Opus 4.5/4.6): 4,096 tokens.
    If the prefix up to a breakpoint is < 4096 tokens, the breakpoint is
    silently ignored — NO cache entry is created.

  Key structural difference:
    Arm A: breakpoint 1 covers ~6700 tokens (CLAUDE.md) → ✅ above 4096
    Arm B: breakpoint 1 covers ~900 tokens (static only) → ❌ below 4096!
    This means Arm B's system message breakpoint is WASTED in our setup.
    Arm B effectively has only 2 useful breakpoints (tools + tail).

Usage:
    python debug/test_system_placement_ab.py
    python debug/test_system_placement_ab.py --model aws.claude-opus-4.6 --rounds 8
    python debug/test_system_placement_ab.py --dry-run
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger
from lib.llm import build_body, stream_chat, add_cache_breakpoints
from lib.model_info import is_claude

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 8

# ── Load the REAL CLAUDE.md from the project (not a truncated version) ──
# The real CLAUDE.md is ~6,654 tokens (~26,618 chars). This ensures the
# system message exceeds the 4,096-token Opus cache threshold from R1.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_claude_md_path = os.path.join(_project_root, 'CLAUDE.md')
try:
    with open(_claude_md_path) as f:
        CLAUDE_MD_CONTENT = f.read()
except FileNotFoundError:
    raise RuntimeError(
        f'CLAUDE.md not found at {_claude_md_path}. '
        'This test requires the real CLAUDE.md for realistic token sizes.'
    )

# ── Static guidance sections (from lib/tasks_pkg/system_context.py) ──
# In production, these are injected as a SEPARATE text block in the system
# message so they get their own cache breakpoint.
try:
    from lib.tasks_pkg.system_context import (
        _FUNCTION_RESULT_CLEARING_SECTION,
        _SUMMARIZE_TOOL_RESULTS_SECTION,
        _TOOL_USAGE_GUIDANCE,
        _OUTPUT_EFFICIENCY_GUIDANCE,
    )
    from lib.memory import MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT

    STATIC_GUIDANCE = '\n\n'.join([
        _FUNCTION_RESULT_CLEARING_SECTION,
        _SUMMARIZE_TOOL_RESULTS_SECTION,
        _TOOL_USAGE_GUIDANCE,
        _OUTPUT_EFFICIENCY_GUIDANCE,
    ])
    MEMORY_INSTRUCTIONS = MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT
except ImportError:
    # Fallback if imports fail
    STATIC_GUIDANCE = """\
# Function Result Clearing

Old tool results will be automatically cleared from context to free up space. \
The 30 most recent results are always kept.

When working with tool results, write down any important information you \
might need later in your response, as the original tool result may be \
cleared later.

# Using your tools
 - You can call multiple tools in a single response. If there are no dependencies, make all calls in parallel.
 - Prefer grep_search for finding code patterns. Prefer read_files for understanding code.
 - Use apply_diff for small targeted edits, write_file for new files or major rewrites.
 - Use insert_content to add new code next to existing code without replacing it.

# Output efficiency

Go straight to the point. Try the simplest approach first. Be extra concise."""

    MEMORY_INSTRUCTIONS = """\
<memory_accumulation>
You have memory CRUD tools: create_memory, update_memory, delete_memory, merge_memories.
Proactively save memories when you discover: bug patterns, project conventions,
user preferences, complex workflows, or tool/API quirks.
</memory_accumulation>"""


# ── Static base prompt ──
STATIC_BASE_PROMPT = """\
You are an AI coding assistant called Tofu (豆腐). You help users with programming tasks \
by using project tools to explore and modify code.

Tools for code exploration:
- list_dir(path) — List directory contents
- read_files(reads) — Read one or more files/ranges in a single call (up to 20)
- grep_search(pattern, path?, include?) — Search patterns across files
- find_files(pattern, path?) — Find files by name glob

Tools for code modification:
- write_file(path, content, description?) — Write/create a file (overwrites entirely)
- apply_diff(path, search, replace, description?) — Apply targeted search-and-replace edit
- insert_content(path, anchor, content, position?) — Insert new content before or after anchor
- run_command(command, timeout?, working_dir?) — Execute shell command

Strategy:
1. Start with list_dir('.') to understand project structure
2. Use grep_search to locate relevant code
3. Use read_files to examine files — batch multiple paths into ONE call
4. Use apply_diff for small targeted edits, write_file for new files or major rewrites"""

# ── Current date ──
CURRENT_DATE = f"Current date: {time.strftime('%Y-%m-%d')}"


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

# Simulated tool results for multi-round conversation.
TOOL_RESULTS = {
    'list_dir': (
        "Directory: .\n\nFiles:\n"
        "  📄 server.py (245L, 8.2KB)\n  📄 bootstrap.py (189L, 6.1KB)\n"
        "  📄 export.py (1120L, 42.3KB)\n  📄 CLAUDE.md (380L, 14.8KB)\n"
        "  📄 requirements.txt (28L, 0.6KB)\n  📄 README.md (120L, 4.8KB)\n\n"
        "Subdirectories:\n"
        "  📁 lib/ (42 items)\n  📁 routes/ (18 items)\n  📁 static/ (31 items)\n"
        "  📁 debug/ (15 items)\n  📁 tests/ (22 items)\n  📁 data/ (5 items)\n"
    ),
    'read_files': (
        "File: lib/llm_client.py (lines 900-1000 of 1965)\n"
        "────────────────────────────────────────\n"
        "900 │ def add_cache_breakpoints(body, log_prefix=''):\n"
        '901 │     """Add Anthropic-style ephemeral cache breakpoints with mixed TTL.\n'
        "902 │     Annotates up to 4 content blocks with cache_control.\n"
        '903 │     """\n'
        "904 │     model = body.get('model', '')\n"
        "905 │     if not is_claude(model): return\n"
        "906 │     messages = body.get('messages', [])\n"
        "907 │     # Phase 0: Strip ALL existing cache_control\n"
        "908 │     for i, msg in enumerate(messages):\n"
        "909 │         content = msg.get('content')\n"
        "910 │         if isinstance(content, list):\n"
        "911 │             for j, block in enumerate(content):\n"
        "912 │                 if isinstance(block, dict) and 'cache_control' in block:\n"
        "913 │                     content[j] = {k: v for k, v in block.items()\n"
        "914 │                                    if k != 'cache_control'}\n"
    ),
    'grep_search': (
        'grep "add_cache_breakpoints" — 12 matches:\n\n'
        'lib/llm_client.py:904:def add_cache_breakpoints(body, log_prefix=\'\'):\n'
        'lib/llm_client.py:1386:    add_cache_breakpoints(body, log_prefix)\n'
        'tests/test_cache_breakpoints.py:112:  from lib.llm import add_cache_breakpoints\n'
    ),
    'run_command': (
        "$ wc -l lib/llm_client.py\n1965 lib/llm_client.py\n"
        "[exit code: 0]"
    ),
    'web_search': (
        "Search results:\n\n"
        "1. [Anthropic Docs] Prompt Caching\n"
        "   90% cost reduction for cached prompts.\n"
    ),
    'fetch_url': (
        "# Anthropic Prompt Caching\n\n"
        "## How it works\n"
        "Mark content blocks with cache_control: {type: 'ephemeral'}\n"
    ),
    'find_files': (
        "Found 5 files matching '*.py' in lib/tasks_pkg/:\n"
        "  lib/tasks_pkg/__init__.py\n"
        "  lib/tasks_pkg/cache_tracking.py\n"
        "  lib/tasks_pkg/orchestrator.py\n"
    ),
    'apply_diff': "✅ Applied 1 edit to lib/example.py",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RoundResult:
    """Stats for a single API call round."""
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
class ArmResult:
    """Aggregate stats for an A/B test arm."""
    label: str
    desc: str
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
#  API call helper
# ═══════════════════════════════════════════════════════════════════════════════

_last_assistant_msg = None


def _run_api_round(body, round_num, label, messages):
    """Make one API call and return a RoundResult."""
    global _last_assistant_msg

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
        logger.warning('[AB:SysPlacement] API error in %s R%d: %s',
                       label, round_num + 1, e)
        return RoundResult(round_num=round_num + 1, error=str(e))

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
        round_num=round_num + 1,
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
        tool_result = TOOL_RESULTS.get(fn_name,
                                        f"Tool {fn_name} executed successfully.")
        messages.append({
            'role': 'tool',
            'tool_call_id': tc.get('id', f'call_{tc_counter}'),
            'content': tool_result,
        })
        tc_counter += 1
        print(f"       → {fn_name}: {len(tool_result)} chars")

    _last_assistant_msg = None
    return True


def _dry_run_round(round_num, arm='A'):
    """Simulate a round for dry-run mode."""
    base_cw = 6000 if round_num == 0 else max(500, 2000 - round_num * 200)
    base_cr = 0 if round_num == 0 else min(50000, 4000 + round_num * 4500)
    if arm == 'B' and round_num > 0:
        base_cr = int(base_cr * 1.05)
        base_cw = int(base_cw * 0.95)
    prompt_tokens = max(1, 50 - round_num * 5)
    return RoundResult(
        round_num=round_num + 1,
        prompt_tokens=prompt_tokens,
        cache_read=base_cr,
        cache_write=base_cw,
        output_tokens=200 + round_num * 30,
        total_input=prompt_tokens + base_cr + base_cw,
        elapsed=3.5 + round_num * 0.2,
        ttft=2.0 + round_num * 0.1,
        tool_calls=2,
        finish_reason='tool_calls' if round_num < 6 else 'stop',
        status=('WRITE' if round_num == 0
                else ('HIT+WRITE' if base_cw > 500 else 'HIT')),
    )


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


# ═══════════════════════════════════════════════════════════════════════════════
#  Arm A: ChatUI current — ALL context in messages[0] (system)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_system_msg_arm_a(arm_seed):
    """Build the full system message for Arm A (ChatUI current approach).

    Mirrors _inject_system_contexts() in system_context.py:
      messages[0] = {role: 'system', content: [
          {text: CLAUDE.md (~6700 tokens)},          ← breakpoint 1 (1h TTL)
          {text: static_base + guidance + memory + date} ← breakpoint 2 (1h TTL)
      ]}

    breakpoint 1 caches prefix [0 .. CLAUDE.md block] = ~6700 tokens ✅ above 4096
    breakpoint 2 caches prefix [0 .. static block]     = ~7600 tokens ✅ above 4096
    """
    # Block 1: Project context (CLAUDE.md) — prepended, wrapped in system-reminder
    block1 = f"<system-reminder>\n{CLAUDE_MD_CONTENT}\n</system-reminder>"

    # Block 2: Static base prompt + guidance + memory + date
    block2 = '\n\n'.join([
        STATIC_BASE_PROMPT,
        STATIC_GUIDANCE,
        f"<system-reminder>\n{MEMORY_INSTRUCTIONS}\n</system-reminder>",
        CURRENT_DATE,
        arm_seed,
    ])

    return {
        'role': 'system',
        'content': [
            {'type': 'text', 'text': block1},
            {'type': 'text', 'text': block2},
        ]
    }


def run_arm_system_msg(model, num_rounds, arm_seed, dry_run=False):
    """ARM A: ALL context in messages[0] with role='system' (ChatUI current)."""
    label = 'SYSTEM_MSG'
    print(f"\n  ╔═══ Arm A: {label}")
    print(f"  ║  ALL context in messages[0] (role=system) — ChatUI current")
    print(f"  ║  Model: {model}, Rounds: {num_rounds}")

    result = ArmResult(
        label=label,
        desc='All context in messages[0] system msg (ChatUI current)',
        model=model,
    )

    system_msg = _build_system_msg_arm_a(arm_seed)

    messages = [
        system_msg,
        {'role': 'user', 'content': (
            'Explain the prompt caching system in this project. '
            'Start by listing the project structure, then read the cache '
            'breakpoint code, search for related patterns, and summarize.'
        )},
    ]
    tc_counter = 0

    for round_num in range(num_rounds):
        print(f"\n  ║  ── R{round_num+1}/{num_rounds} ── msgs={len(messages)}")

        if dry_run:
            rr = _dry_run_round(round_num, arm='A')
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

        rr = _run_api_round(body, round_num, label, messages)
        result.rounds.append(rr)
        if rr.error:
            break

        if not _append_real_tool_round(messages, rr, tc_counter):
            break
        tc_counter += rr.tool_calls

    # ── Task 2: inter-task cache test ──
    _run_task2(messages, model, result, label, arm_seed, dry_run, arm='A')

    _print_arm_summary(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Arm B: Claude Code style — CLAUDE.md in first user message
# ═══════════════════════════════════════════════════════════════════════════════

def _build_messages_arm_b(arm_seed, user_query):
    """Build messages for Arm B (Claude Code approach).

    Layout:
      messages[0] = {role: 'system', content: "static_base + guidance + memory"}
          → ~900 tokens (a single text string)
          → add_cache_breakpoints() will place breakpoint 1 here
          → BUT ~900 tokens < 4096 minimum → breakpoint SILENTLY IGNORED

      messages[1] = {role: 'user', content: "<system-reminder>CLAUDE.md...</system-reminder>"}
          → No breakpoint placed here by add_cache_breakpoints()
            (it only places breakpoints on system messages, tools, and tail)
          → This content IS part of the cached prefix when breakpoints on
            later messages (tools, tail) create cache entries

      messages[2] = {role: 'user', content: actual user query}

    Effective breakpoint usage:
      breakpoint 1: system msg (~900 tokens) — WASTED (below 4096 threshold)
      breakpoint 2: last tool definition — caches [system + user:CLAUDE.md + ... + tools]
      breakpoint 3: tail message — caches growing conversation
      breakpoint 4: unused
    """
    # System message: ONLY static content (small, never changes)
    system_block = '\n\n'.join([
        STATIC_BASE_PROMPT,
        STATIC_GUIDANCE,
        f"<system-reminder>\n{MEMORY_INSTRUCTIONS}\n</system-reminder>",
        arm_seed,
    ])

    system_msg = {
        'role': 'system',
        'content': system_block,
    }

    # Prepended user message with CLAUDE.md in <system-reminder> tags
    # (mirrors Claude Code's prependUserContext)
    prepended_user = {
        'role': 'user',
        'content': (
            '<system-reminder>\n'
            'As you answer the user\'s questions, you can use the following context:\n'
            f'# claudeMd\n{CLAUDE_MD_CONTENT}\n'
            f'# currentDate\n{CURRENT_DATE}\n'
            '\nIMPORTANT: this context may or may not be relevant to your tasks. '
            'You should not respond to this context unless it is highly relevant '
            'to your task.\n'
            '</system-reminder>\n'
        ),
    }

    # Actual user query
    user_msg = {'role': 'user', 'content': user_query}

    return [system_msg, prepended_user, user_msg]


def run_arm_user_msg(model, num_rounds, arm_seed, dry_run=False):
    """ARM B: CLAUDE.md in first user message (Claude Code style)."""
    label = 'USER_MSG'
    print(f"\n  ╔═══ Arm B: {label}")
    print(f"  ║  CLAUDE.md in first user message (<system-reminder>)")
    print(f"  ║  System msg = static instructions only (Claude Code style)")
    print(f"  ║  Model: {model}, Rounds: {num_rounds}")

    result = ArmResult(
        label=label,
        desc='CLAUDE.md in user msg + static-only system msg (Claude Code style)',
        model=model,
    )

    user_query = (
        'Explain the prompt caching system in this project. '
        'Start by listing the project structure, then read the cache '
        'breakpoint code, search for related patterns, and summarize.'
    )
    messages = _build_messages_arm_b(arm_seed, user_query)
    tc_counter = 0

    for round_num in range(num_rounds):
        print(f"\n  ║  ── R{round_num+1}/{num_rounds} ── msgs={len(messages)}")

        if dry_run:
            rr = _dry_run_round(round_num, arm='B')
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

        rr = _run_api_round(body, round_num, label, messages)
        result.rounds.append(rr)
        if rr.error:
            break

        if not _append_real_tool_round(messages, rr, tc_counter):
            break
        tc_counter += rr.tool_calls

    # ── Task 2: inter-task cache test ──
    _run_task2(messages, model, result, label, arm_seed, dry_run, arm='B')

    _print_arm_summary(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Task 2 (inter-task cache test)
# ═══════════════════════════════════════════════════════════════════════════════

def _run_task2(messages, model, result, label, arm_seed, dry_run, arm='A'):
    """Simulate a second task to test inter-task cache retention."""
    print(f"  ║")
    print(f"  ║  ── Task 2 (inter-task cache test) ──")

    messages_t2 = list(messages)
    messages_t2.append({
        'role': 'user',
        'content': 'Now explain the mixed TTL strategy and how session-stable '
                   'TTL latching works. What did the A/B test show?',
    })

    if dry_run:
        rr = _dry_run_round(99, arm=arm)
        rr.round_num = 0
        result.rounds.append(rr)
        print(f"  ║  T2: cr={rr.cache_read:,}  cw={rr.cache_write:,}  "
              f"{rr.status} (dry-run)")
        return

    body = build_body(
        model, messages_t2,
        max_tokens=1024,
        temperature=1.0,
        thinking_enabled=True,
        preset='medium',
        thinking_depth='medium',
        tools=TOOLS,
        stream=True,
    )

    rr = _run_api_round(body, -1, f'{label}:T2', messages_t2)
    rr.round_num = 0
    result.rounds.append(rr)


def _print_arm_summary(result):
    """Print summary line for an arm."""
    print(f"  ╚═══ {result.label}: "
          f"total_cr={result.total_cache_read:,}  "
          f"total_cw={result.total_cache_write:,}  "
          f"total_pt={result.total_prompt:,}")


# ═══════════════════════════════════════════════════════════════════════════════
#  Cost computation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_cost(arm: ArmResult, *, input_price=15.0, output_price=75.0,
                 cw_mul=1.25, cr_mul=0.10):
    """Compute cost breakdown. Opus 4.6: $15/M in, $75/M out."""
    tp = arm.total_prompt
    tr = arm.total_cache_read
    tw = arm.total_cache_write
    to = arm.total_output
    ti = arm.total_input

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

def _print_round_table(arm: ArmResult):
    """Print per-round results table."""
    print(f"\n  {'Rnd':>3} │ {'Prompt':>8} │ {'CacheRead':>10} │ {'CacheWrite':>11} │ "
          f"{'Output':>7} │ {'Status':>10} │ {'TTFT':>5} │ {'Total':>5}")
    print(f"  {'─'*3}─┼─{'─'*8}─┼─{'─'*10}─┼─{'─'*11}─┼─"
          f"{'─'*7}─┼─{'─'*10}─┼─{'─'*5}─┼─{'─'*5}")

    for i, r in enumerate(arm.rounds):
        is_t2 = (i == len(arm.rounds) - 1 and len(arm.rounds) > 1)
        label = 'T2' if is_t2 else f'R{r.round_num}'
        if r.error:
            print(f"  {label:>3} │ {'ERROR':>8} │ {r.error[:40]:>40}")
            continue
        print(f"  {label:>3} │ {r.prompt_tokens:>8,} │ {r.cache_read:>10,} │ "
              f"{r.cache_write:>11,} │ {r.output_tokens:>7,} │ {r.status:>10} │ "
              f"{r.ttft:>5.1f}s │ {r.elapsed:>5.1f}s")


def print_comparison(arm_a: ArmResult, arm_b: ArmResult):
    """Print full A/B comparison report."""
    cost_a = compute_cost(arm_a)
    cost_b = compute_cost(arm_b)

    print(f"\n\n  {'█'*65}")
    print(f"  SYSTEM PLACEMENT A/B COMPARISON")
    print(f"  {'█'*65}")

    print(f"\n  ┌─ ARM A: {arm_a.label} — {arm_a.desc}")
    _print_round_table(arm_a)

    print(f"\n  ┌─ ARM B: {arm_b.label} — {arm_b.desc}")
    _print_round_table(arm_b)

    # ── Delta helper ──
    def _delta(old_v, new_v, lower_better=True):
        if old_v == 0:
            return "N/A"
        pct = (new_v - old_v) / old_v * 100
        better = pct < 0 if lower_better else pct > 0
        sym = "✅" if better else ("⚠️" if abs(pct) > 5 else "➖")
        return f"{pct:+.1f}% {sym}"

    # ── Metrics comparison table ──
    print(f"\n  {'='*65}")
    print(f"  METRICS COMPARISON")
    print(f"  {'='*65}")

    print(f"\n  {'Metric':<35} │ {'SYSTEM_MSG':>12} │ {'USER_MSG':>12} │ {'Delta':>12}")
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
        ("Input cost (prompt+cw+cr)", cost_a['cost_prompt']+cost_a['cost_read']+cost_a['cost_write'],
         cost_b['cost_prompt']+cost_b['cost_read']+cost_b['cost_write'], True),
        ("Output cost", cost_a['cost_output'], cost_b['cost_output'], True),
        ("TOTAL COST", cost_a['total'], cost_b['total'], True),
        ("Cost w/o caching", cost_a['cost_no_cache'], cost_b['cost_no_cache'], True),
        ("Cache savings (%)", cost_a['savings_pct'], cost_b['savings_pct'], False),
    ]
    for name, a, b, lb in cost_metrics:
        if '%' in name:
            print(f"  {name:<35} │ {a:>11.1f}% │ {b:>11.1f}% │ {_delta(a, b, lb):>12}")
        else:
            print(f"  {name:<35} │ ${a:>11.4f} │ ${b:>11.4f} │ {_delta(a, b, lb):>12}")

    # ── TTFT comparison ──
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

    # ── Per-round cache hit comparison ──
    print(f"\n  {'='*65}")
    print(f"  PER-ROUND CACHE HIT RATE")
    print(f"  {'='*65}")

    max_rounds = max(len(arm_a.rounds), len(arm_b.rounds))
    print(f"\n  {'Round':<6} │ {'SYSTEM_MSG':>18} │ {'USER_MSG':>18} │ {'Winner':>8}")
    print(f"  {'─'*6}─┼─{'─'*18}─┼─{'─'*18}─┼─{'─'*8}")

    for i in range(max_rounds):
        is_t2_a = (i == len(arm_a.rounds) - 1 and i > 0)
        is_t2_b = (i == len(arm_b.rounds) - 1 and i > 0)
        label_row = 'T2' if (is_t2_a or is_t2_b) else f'R{i+1}'

        a_str = '—'
        b_str = '—'
        a_pct = 0
        b_pct = 0

        if i < len(arm_a.rounds) and not arm_a.rounds[i].error:
            r = arm_a.rounds[i]
            total = r.total_input or 1
            a_pct = round(r.cache_read / total * 100)
            a_str = f"cr={r.cache_read:>6,} {a_pct:>2}%"

        if i < len(arm_b.rounds) and not arm_b.rounds[i].error:
            r = arm_b.rounds[i]
            total = r.total_input or 1
            b_pct = round(r.cache_read / total * 100)
            b_str = f"cr={r.cache_read:>6,} {b_pct:>2}%"

        winner = '—'
        if a_pct > b_pct + 2:
            winner = 'A ✅'
        elif b_pct > a_pct + 2:
            winner = 'B ✅'
        else:
            winner = 'tie ➖'

        print(f"  {label_row:<6} │ {a_str:>18} │ {b_str:>18} │ {winner:>8}")

    # ── Task 2 inter-task cache comparison ──
    if len(arm_a.rounds) > 1 and len(arm_b.rounds) > 1:
        t2_a = arm_a.rounds[-1]
        t2_b = arm_b.rounds[-1]
        print(f"\n  📋 Inter-task cache (Task 2):")
        if not t2_a.error:
            t2_a_pct = round(t2_a.cache_read / max(t2_a.total_input, 1) * 100)
            print(f"     A (SYSTEM_MSG): cr={t2_a.cache_read:,}  hit={t2_a_pct}%  "
                  f"{'✅ good' if t2_a_pct > 50 else '❌ poor'}")
        if not t2_b.error:
            t2_b_pct = round(t2_b.cache_read / max(t2_b.total_input, 1) * 100)
            print(f"     B (USER_MSG):   cr={t2_b.cache_read:,}  hit={t2_b_pct}%  "
                  f"{'✅ good' if t2_b_pct > 50 else '❌ poor'}")

    # ── Architecture analysis ──
    print(f"\n  {'='*65}")
    print(f"  ARCHITECTURE ANALYSIS")
    print(f"  {'='*65}")
    print()
    print(f"  Arm A (ChatUI current) — breakpoint layout:")
    print(f"    breakpoint 1: system block 1 (CLAUDE.md, ~6700 tokens) → ✅ above 4096")
    print(f"    breakpoint 2: system block 2 (static+guidance, ~7600 cumulative)")
    print(f"    breakpoint 3: last tool definition")
    print(f"    breakpoint 4: conversation tail (last message with content)")
    print()
    print(f"  Arm B (Claude Code style) — breakpoint layout:")
    print(f"    breakpoint 1: system msg (static only, ~900 tokens) → ❌ BELOW 4096, WASTED")
    print(f"    breakpoint 2: last tool definition (system + user:CLAUDE.md + tools)")
    print(f"    breakpoint 3: conversation tail")
    print(f"    breakpoint 4: unused")
    print()
    print(f"  Key difference:")
    print(f"    Arm A has 2 effective system breakpoints → CLAUDE.md cached independently")
    print(f"    Arm B has 0 effective system breakpoints → system prefix not cached")
    print(f"    Arm B's CLAUDE.md is only cached as part of the tools breakpoint prefix")

    # ── Verdict ──
    money_diff = cost_a['total'] - cost_b['total']
    pct_diff = money_diff / cost_a['total'] * 100 if cost_a['total'] > 0 else 0

    print(f"\n  {'='*65}")
    print(f"  VERDICT")
    print(f"  {'='*65}")
    print()

    if abs(pct_diff) < 3:
        print(f"  ➖ NEUTRAL — no significant cost difference ({pct_diff:+.1f}%)")
        print(f"     ${abs(money_diff):.4f} difference is within noise margin")
    elif money_diff > 0:
        print(f"  ✅ USER_MSG / Claude Code (B) WINS — saves ${money_diff:.4f} ({pct_diff:.1f}%)")
        print(f"     Moving CLAUDE.md to user message improves cache performance")
    else:
        print(f"  ✅ SYSTEM_MSG / ChatUI current (A) WINS — saves ${-money_diff:.4f} ({-pct_diff:.1f}%)")
        print(f"     Current approach with all context in system message is optimal")

    # ── Projection ──
    if valid_a and valid_b:
        per_round_a = cost_a['total'] / len(valid_a)
        per_round_b = cost_b['total'] / len(valid_b)
        proj_54_a = per_round_a * 54
        proj_54_b = per_round_b * 54
        proj_diff = proj_54_a - proj_54_b
        print(f"\n  📐 Projection to 54-round conversation:")
        print(f"     SYSTEM_MSG (A):  ~${proj_54_a:.2f}")
        print(f"     USER_MSG (B):    ~${proj_54_b:.2f}")
        print(f"     Difference:      ~${abs(proj_diff):.2f} "
              f"({'B saves' if proj_diff > 0 else 'A saves'})")

    print(f"\n  {'='*65}\n")

    return cost_a, cost_b


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='A/B test: CLAUDE.md in system msg vs first user msg')
    parser.add_argument('--model', default=DEFAULT_MODEL,
                        help=f'Model (default: {DEFAULT_MODEL})')
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS,
                        help=f'Rounds per arm (default: {DEFAULT_ROUNDS})')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulate without API calls')
    parser.add_argument('--wait', type=int, default=15,
                        help='Seconds between arms (default: 15)')
    args = parser.parse_args()

    # ── Print token size analysis ──
    _est = lambda t: len(t) // 4
    claude_md_wrapped = f"<system-reminder>\n{CLAUDE_MD_CONTENT}\n</system-reminder>"
    static_block = '\n\n'.join([STATIC_BASE_PROMPT, STATIC_GUIDANCE,
                                f"<system-reminder>\n{MEMORY_INSTRUCTIONS}\n</system-reminder>",
                                CURRENT_DATE])
    static_only = '\n\n'.join([STATIC_BASE_PROMPT, STATIC_GUIDANCE,
                               f"<system-reminder>\n{MEMORY_INSTRUCTIONS}\n</system-reminder>"])

    print(f"\n{'█'*70}")
    print(f"  CLAUDE.MD PLACEMENT A/B TEST")
    print(f"  {'─'*60}")
    print(f"  Question: Is it better to place CLAUDE.md in messages[0]")
    print(f"            (system msg, ChatUI) or in a user message")
    print(f"            (<system-reminder>, Claude Code style)?")
    print(f"  {'─'*60}")
    print(f"  Model: {args.model}")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"  Rounds per arm: {args.rounds}")
    print(f"  Dry run: {args.dry_run}")
    print(f"  {'─'*60}")
    print(f"  TOKEN SIZE ANALYSIS (chars/4 estimate):")
    print(f"    CLAUDE.md (wrapped):     ~{_est(claude_md_wrapped):>5} tokens")
    print(f"    Static block:            ~{_est(static_block):>5} tokens")
    print(f"    Static only (no CLAUDE): ~{_est(static_only):>5} tokens")
    print(f"  {'─'*60}")
    print(f"  BREAKPOINT EFFECTIVENESS (min 4096 for Opus):")
    print(f"    Arm A bp1 (CLAUDE.md):   ~{_est(claude_md_wrapped):>5} tokens  "
          f"{'✅ ABOVE' if _est(claude_md_wrapped) >= 4096 else '❌ BELOW'} 4096")
    print(f"    Arm B bp1 (static only): ~{_est(static_only):>5} tokens  "
          f"{'✅ ABOVE' if _est(static_only) >= 4096 else '❌ BELOW'} 4096")
    print(f"{'█'*70}")

    if not args.dry_run and not is_claude(args.model):
        print(f"\n⚠️ Model '{args.model}' is not Claude — prompt caching is "
              f"Claude-specific.")

    # ★ Unique arm seeds to prevent cross-arm cache sharing.
    arm_seed_a = f'\n\n<!-- ab_test arm=SYSTEM_MSG seed={time.time():.0f} -->'
    arm_seed_b = f'\n\n<!-- ab_test arm=USER_MSG seed={time.time():.0f}_b -->'

    # ── ARM A: all context in system message (ChatUI current) ──
    print(f"\n\n{'▓'*70}")
    print(f"  ARM A: SYSTEM_MSG — All context in system message (ChatUI)")
    print(f"{'▓'*70}")
    arm_a = run_arm_system_msg(args.model, args.rounds, arm_seed_a,
                               dry_run=args.dry_run)

    if not args.dry_run:
        print(f"\n  ⏳ Waiting {args.wait}s between arms...")
        time.sleep(args.wait)

    # ── ARM B: CLAUDE.md in user message (Claude Code style) ──
    print(f"\n\n{'▓'*70}")
    print(f"  ARM B: USER_MSG — CLAUDE.md in user message (Claude Code)")
    print(f"{'▓'*70}")
    arm_b = run_arm_user_msg(args.model, args.rounds, arm_seed_b,
                             dry_run=args.dry_run)

    # ── Comparison ──
    cost_a, cost_b = print_comparison(arm_a, arm_b)

    # ── Save results ──
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    output_path = f"debug/system_placement_ab_{timestamp}.json"
    try:
        output = {
            'test': 'claudemd_placement',
            'model': args.model,
            'date': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'rounds_per_arm': args.rounds,
            'token_analysis': {
                'claude_md_wrapped_tokens': _est(claude_md_wrapped),
                'static_block_tokens': _est(static_block),
                'static_only_tokens': _est(static_only),
                'opus_min_cache_threshold': 4096,
                'arm_a_bp1_above_threshold': _est(claude_md_wrapped) >= 4096,
                'arm_b_bp1_above_threshold': _est(static_only) >= 4096,
            },
            'description': {
                'arm_a': 'All context in messages[0] system msg (ChatUI current)',
                'arm_b': 'CLAUDE.md in user msg + static-only system (Claude Code style)',
            },
            'arm_a': {
                'label': arm_a.label,
                'desc': arm_a.desc,
                'rounds': [{k: v for k, v in r.__dict__.items()}
                           for r in arm_a.rounds],
                'cost': cost_a,
            },
            'arm_b': {
                'label': arm_b.label,
                'desc': arm_b.desc,
                'rounds': [{k: v for k, v in r.__dict__.items()}
                           for r in arm_b.rounds],
                'cost': cost_b,
            },
        }
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n📁 Results saved to: {output_path}")
    except Exception as e:
        logger.warning('[AB:ClaudeMdPlacement] Could not save results: %s', e)
        print(f"⚠️ Could not save: {e}")

    print(f"{'█'*70}\n")


if __name__ == '__main__':
    main()
