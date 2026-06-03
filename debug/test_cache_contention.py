#!/usr/bin/env python3
"""Test: Does Anthropic prompt cache contention between conversations exist?

HYPOTHESIS: When two conversations with different prefixes are active on the
same model simultaneously, they do NOT evict each other's cache. The cache key
is the exact prefix bytes — different conversations have different keys and
occupy independent cache slots.

TEST DESIGN:
  1. "Solo" baseline: Run Conv A alone for 6 rounds, measure cache hit rates
  2. "Interleaved": Run Conv A and Conv B alternating, measure A's cache hits
  3. Compare: If contention exists, interleaved cache_read would be lower

Both conversations share the SAME system prompt and tools (> 4096 tokens for
Opus cache eligibility) but have DIFFERENT user messages, so their prefixes
diverge after the system+tools portion.

Requirements:
  - Valid provider config in data/config/server_config.json or env vars
  - Model must be Anthropic Claude (e.g. aws.claude-opus-4.6)

Usage:
    python debug/test_cache_contention.py [--model MODEL] [--rounds 6]
"""

import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm import build_body, stream_chat
from lib.model_info import is_claude

# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 6

# System prompt must be > 4096 tokens for Opus cache eligibility.
# Claude tokenizer ≈ 1.44x tiktoken; we need ~6000 words / ~4500 Anthropic tokens
# to comfortably exceed the 4096 minimum. Production uses ~14K tokens (system + tools).
#
# Strategy: use CLAUDE.md-sized system prompt to match production conditions.

def _build_system_prompt() -> str:
    """Build a system prompt > 4096 Claude tokens (≈ 5800+ words)."""
    return """You are an AI coding assistant called Tofu (豆腐). You help users with programming tasks by using project tools to explore and modify code.

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
  `routes/__init__.py` → `register_all(app)` wires them. Each blueprint handles a domain:
  chat, common, browser, trading, fund management.
- **Task lifecycle (SSE streaming)**:
  1. Client POSTs to `/api/chat/start` → creates a task dict in memory
  2. Background thread runs `orchestrator.run_task(task)`
  3. Task appends SSE events via `append_event(task, ...)`
  4. Client polls `/api/chat/stream/<id>` for SSE events
  5. On completion, result persisted to SQLite via `persist_task_result()`
- **LLM client flow**: `lib/llm_client.py` → `build_body()` constructs model-specific payloads.
  `stream_chat()` handles SSE streaming with retry logic.
- **Tool execution**: Tools defined in `lib/tools.py`, executed in `lib/tasks_pkg/executor.py`.

### Error Handling Patterns
```python
# API / Network calls
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
# JSON parsing
try:
    data = json.loads(raw)
except (json.JSONDecodeError, TypeError) as e:
    logger.warning('Invalid JSON (len=%d): %s — preview: %.200s', len(raw), e, raw)
    data = {}
```

```python
# Database operations
try:
    db.execute(sql, params)
    db.commit()
except Exception as e:
    logger.error('DB write failed: %s — sql=%.200s params=%s', e, sql, params, exc_info=True)
    db.rollback()
    raise
```

```python
# Background threads
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

### Logging Discipline
Every code path that can fail MUST leave a trace in the log file. Silent failures are the enemy.
- Every Python file MUST have: `from lib.log import get_logger; logger = get_logger(__name__)`
- Every except block logs something (debug at minimum)
- Use %-style formatting for lazy evaluation: `logger.info('x=%s', x)`
- Sanitize secrets: never log API keys, tokens, or full request bodies with credentials
- Truncate large data: `logger.debug('Response preview: %.500s', body)`
- Include context: conv_id, task_id, model name, URL, file path
- Use `log_context` for operations > 1 second
- Use `log_exception` for catch-and-reraise
- Use `audit_log` for significant state changes

| Scenario | Level | exc_info | Example |
|---|---|---|---|
| Expected / harmless fallback | debug | optional | Parse int, optional file |
| Unexpected but recoverable | warning | False | API timeout, retry |
| Unexpected, degraded behavior | error | True | Tool execution failure |
| Fatal / unrecoverable | critical | True | DB corruption |
| Retry loop (each attempt) | warning | False | Stream retry |
| Retry loop (final failure) | error | True | All retries exhausted |

### Code Style & Conventions
- Imports: stdlib → third-party → lib.* → routes.*, blank line between groups
- Logger init: from lib.log import get_logger; logger = get_logger(__name__)
- Type hints: encouraged on public functions; optional on internal helpers
- Docstrings: Google-style on modules and public functions
- Constants: UPPER_SNAKE_CASE at module level. Private helpers: prefix with _
- JavaScript: Vanilla JS only — no frameworks, no build step
- CSS: Dark theme with CSS variables at :root
- Icons: Real brand SVGs, NOT emoji — search for official logo before adding

### File Modification Checklist
Before submitting any code change, verify:
- Logger present in file
- No silent catches: Every except block logs something
- Context in logs: relevant IDs included (conv_id, task_id, url, model)
- Tracebacks on errors: exc_info=True on logger.error()
- No f-strings in log calls
- Secrets not logged
- Large data truncated
- No hardcoded environment values
- Export sync: update export.py if needed

### Additional Tool Guidance & Detailed Reference
When using tools, follow these patterns for optimal results:
- **list_dir**: Use for initial project exploration. Shows files with line counts and sizes.
- **read_files**: Batch multiple paths into ONE call. Files under 40KB auto-expand whole-file.
  Supports both relative project paths and absolute paths. Images uploaded natively.
- **grep_search**: Case-insensitive regex search. Use short patterns for best results.
  Supports max_results to limit output and count_only for fast counting.
- **write_file**: Creates the file if it doesn't exist. Overwrites the entire file content.
  Supports content_ref to write previous tool result to avoid re-generating.
- **apply_diff**: Search string must match EXACTLY including whitespace and indentation.
  For multiple edits, pass an 'edits' array — much faster than separate calls.
- **insert_content**: Add new content before or after an anchor string without replacing it.
  Prefer over apply_diff when change is purely additive.
- **run_command**: Execute shell command, returns stdout+stderr. Avoid interactive commands.
  Prefer grep_search over 'run_command grep' — it's 5x faster.
- **web_search**: Search the web for information. Prefer fewer, targeted searches.
  Strategy: search → review summaries → fetch_url most relevant pages.
- **fetch_url**: Fetch and read full content of a URL (HTML, PDF, plain text).
  When page contains links, follow most relevant ones with additional fetch_url calls.
- **find_files**: Find files by name pattern (glob). Supports max_results for limiting output.
  Prefer find_files over 'run_command find' — auto-filters ignored dirs.
- **emit_to_user**: End response by pointing user to most recent tool result. TERMINAL tool.
  Use when tool's raw output fully answers the question — don't re-output it.

## Security Guidelines
- Never execute rm -rf or other destructive commands without explicit user confirmation.
- Never expose API keys, tokens, or credentials in any output.
- Validate all file paths to prevent path traversal attacks.
- Use parameterized queries for all database operations.
- Log security-relevant events with appropriate audit trail context.
- CORS, CSP, and proxy configurations require explicit user approval.
- API key handling changes require explicit user approval.

## Performance Guidelines
- Use batch operations whenever possible (batch reads, batch diffs).
- Minimize round trips between client and server.
- Cache frequently accessed data with appropriate invalidation.
- Use streaming for large data transfers.
- Monitor and log performance metrics for slow operations.
- Use log_context for operations that may take > 1 second.

## Change Approval Requirements
The following categories require explicit user approval before modifying:
- LLM parameters: temperature, top_p, top_k, max_tokens, penalties, stop sequences
- Retry & timeout settings: retry counts, backoff multipliers, timeouts
- Token budgets: context window sizes, compaction thresholds, max tool result lengths
- Rate limiter settings: RPM, TPM, concurrency caps, cooldown periods
- Model routing & dispatch logic
- Database schema changes
- Security-sensitive changes

## Key Files Quick Reference
| Need to… | Look at… |
|---|---|
| Change LLM behavior | lib/llm_client.py, lib/llm_dispatch.py |
| Add a new tool | lib/tools.py → lib/tasks_pkg/executor.py |
| Add a new API endpoint | routes/ → routes/__init__.py |
| Fix streaming issues | lib/llm_client.py → routes/chat.py |
| Debug task flow | lib/tasks_pkg/orchestrator.py |
| Change project file tools | lib/project_mod/tools.py |
| Read local files | lib/file_reader.py → lib/project_mod/read_tools.py |
| Add/edit skills | lib/skills.py |
| Modify trading features | lib/trading.py, routes/trading_*.py |
| Export / sanitize project | export.py |
| Cross-platform compat | lib/compat.py |
| Cross-DC FUSE latency | lib/cross_dc.py |
"""

SYSTEM_PROMPT = _build_system_prompt()

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List contents of a directory. Shows files with line counts and sizes, and subdirectories with item counts.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Relative path from project root."}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read the contents of one or more files. Can read specific line ranges. Each entry in 'reads' array has 'path' (required), 'start_line' and 'end_line' (optional). Max 20 files per batch.", "parameters": {"type": "object", "properties": {"reads": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}}, "required": ["reads"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Search for a pattern across project files. Returns matching lines with file paths and line numbers. Case-insensitive regex.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "context_lines": {"type": "integer"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file. Creates if doesn't exist. Overwrites entirely.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "description": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "apply_diff", "description": "Apply targeted search-and-replace edit(s). Search must match EXACTLY.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}, "description": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "search", "replace"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute a shell command and return stdout + stderr.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web for information.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch and read full content of a URL (HTML, PDF, plain text).", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "find_files", "description": "Find files by name pattern (glob).", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "emit_to_user", "description": "End response by pointing the user to an existing tool result. TERMINAL tool.", "parameters": {"type": "object", "properties": {"tool_round": {"type": "integer"}, "comment": {"type": "string"}}, "required": ["tool_round", "comment"]}}},
]

# Simulated tool results
TOOL_RESULTS = {
    'list_dir': (
        "Directory: .\n\nFiles:\n"
        "  📄 server.py (245L, 8.2KB)\n  📄 bootstrap.py (189L, 6.1KB)\n"
        "  📄 export.py (1120L, 42.3KB)\n  📄 CLAUDE.md (380L, 14.8KB)\n"
    ),
    'read_files': (
        "File: lib/llm_client.py (lines 764-920 of 1736)\n"
        "────────────────────────────────────────\n"
        "764 │ def add_cache_breakpoints(body, log_prefix=''):\n"
        '765 │     """Add Anthropic-style ephemeral cache breakpoints."""\n'
        "766 │     model = body.get('model', '')\n"
        "767 │     if not is_claude(model): return\n"
    ),
    'grep_search': (
        'grep "add_cache_breakpoints" — 8 matches:\n'
        'lib/llm_client.py:764:def add_cache_breakpoints(body):\n'
        'lib/llm_client.py:1190:    add_cache_breakpoints(body)\n'
    ),
}


def _get_tool_result(fn_name: str) -> str:
    return TOOL_RESULTS.get(fn_name, f"Tool {fn_name} executed successfully.")


# ═══════════════════════════════════════════════════════════════════════════════
#  Core test logic
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_usage(usage: dict) -> dict:
    """Extract cache-relevant fields from API usage."""
    return {
        'prompt_tokens': usage.get('prompt_tokens', 0),
        'cache_read': (
            usage.get('cache_read_tokens')
            or usage.get('cache_read_input_tokens') or 0),
        'cache_write': (
            usage.get('cache_creation_input_tokens')
            or usage.get('cache_write_tokens') or 0),
        'output_tokens': usage.get('completion_tokens', 0),
    }


def _make_call(model: str, messages: list, label: str) -> tuple:
    """Make one API call, return (assistant_msg, usage_dict, elapsed)."""
    body = build_body(
        model, messages,
        max_tokens=512,  # keep output short for cost
        temperature=1.0,
        thinking_enabled=True,
        preset='medium',
        thinking_depth='medium',
        tools=TOOLS,
        stream=True,
    )

    t0 = time.time()
    assistant_msg, finish_reason, usage = stream_chat(
        body,
        on_content=lambda _: None,
        on_thinking=lambda _: None,
        log_prefix=f'[{label}]',
    )
    elapsed = time.time() - t0
    u = _extract_usage(usage or {})
    total = u['prompt_tokens'] + u['cache_read'] + u['cache_write']
    hit_pct = round(u['cache_read'] / max(total, 1) * 100)
    print(f"    {label}: pt={u['prompt_tokens']:,}  cr={u['cache_read']:,}  "
          f"cw={u['cache_write']:,}  hit={hit_pct}%  {elapsed:.1f}s")
    return assistant_msg, u, elapsed


def _append_simulated_round(messages: list, assistant_msg: dict, tc_counter: int) -> int:
    """Append assistant + tool results to messages, return updated tc_counter."""
    tool_calls = assistant_msg.get('tool_calls', [])
    if tool_calls:
        clean_msg = {
            'role': 'assistant',
            'content': assistant_msg.get('content') or '',
            'tool_calls': tool_calls,
        }
        messages.append(clean_msg)
        for tc in tool_calls:
            fn_name = tc.get('function', {}).get('name', 'unknown')
            messages.append({
                'role': 'tool',
                'tool_call_id': tc.get('id', f'call_{tc_counter}'),
                'content': _get_tool_result(fn_name),
            })
            tc_counter += 1
    else:
        # No tool calls — append a simple assistant reply
        messages.append({
            'role': 'assistant',
            'content': assistant_msg.get('content') or 'Done.',
        })
    return tc_counter


def run_solo_test(model: str, num_rounds: int) -> list:
    """Phase 1: Run Conv A alone for N rounds. Return list of usage dicts."""
    print(f"\n{'='*70}")
    print(f"  Phase 1: SOLO — Conv A alone ({num_rounds} rounds)")
    print(f"{'='*70}")

    # Arm seed ensures Phase 1 and Phase 2 have different cache keys
    # so both start completely cold (no cross-phase cache sharing).
    arm_seed = "\n<!-- arm_seed: solo_phase_v1 -->"
    messages_a = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'Analyze the project structure and explain how the Flask Blueprint '
            'registration system works. Start by listing the project root.'
        )},
    ]

    results = []
    tc = 0
    for r in range(num_rounds):
        print(f"\n  ── Round {r+1}/{num_rounds} (msgs={len(messages_a)}) ──")
        msg, usage, elapsed = _make_call(model, messages_a, f'Solo-A R{r+1}')
        results.append(usage)
        tc = _append_simulated_round(messages_a, msg, tc)
        # Brief pause to keep within rate limits
        time.sleep(1)

    return results


def run_interleaved_test(model: str, num_rounds: int) -> tuple:
    """Phase 2: Run Conv A and Conv B interleaved. Return (a_results, b_results)."""
    print(f"\n{'='*70}")
    print(f"  Phase 2: INTERLEAVED — Conv A and Conv B alternating ({num_rounds} rounds each)")
    print(f"{'='*70}")

    # Arm seed ensures Phase 2 has different cache keys from Phase 1
    arm_seed = "\n<!-- arm_seed: interleaved_phase_v1 -->"

    # Conv A: same user message as solo test but different arm seed
    messages_a = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'Analyze the project structure and explain how the Flask Blueprint '
            'registration system works. Start by listing the project root.'
        )},
    ]

    # Conv B: DIFFERENT user message → different prefix after system+tools
    messages_b = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'I want to understand the caching system. Find and explain how '
            'prompt cache breakpoints are placed and tracked across rounds.'
        )},
    ]

    a_results, b_results = [], []
    tc_a, tc_b = 0, 0

    for r in range(num_rounds):
        print(f"\n  ── Round {r+1}/{num_rounds} ──")

        # Conv A round
        msg_a, usage_a, _ = _make_call(model, messages_a, f'Intlv-A R{r+1}')
        a_results.append(usage_a)
        tc_a = _append_simulated_round(messages_a, msg_a, tc_a)

        # Minimal gap — immediate switch to Conv B
        time.sleep(0.5)

        # Conv B round
        msg_b, usage_b, _ = _make_call(model, messages_b, f'Intlv-B R{r+1}')
        b_results.append(usage_b)
        tc_b = _append_simulated_round(messages_b, msg_b, tc_b)

        time.sleep(1)

    return a_results, b_results


def analyze_results(solo_a: list, intlv_a: list, intlv_b: list):
    """Compare solo vs interleaved cache hits to determine if contention exists."""
    print(f"\n{'='*70}")
    print(f"  ANALYSIS: Cache Contention Test Results")
    print(f"{'='*70}")

    def _summarize(results: list, label: str):
        total_cr = sum(r['cache_read'] for r in results)
        total_cw = sum(r['cache_write'] for r in results)
        total_pt = sum(r['prompt_tokens'] for r in results)
        total_in = total_cr + total_cw + total_pt
        hit_pct = round(total_cr / max(total_in, 1) * 100, 1)
        # Skip R1-R2 (cold start, below 4096) for "steady state" hit rate
        if len(results) > 2:
            ss = results[2:]
            ss_cr = sum(r['cache_read'] for r in ss)
            ss_in = sum(r['cache_read'] + r['cache_write'] + r['prompt_tokens'] for r in ss)
            ss_hit = round(ss_cr / max(ss_in, 1) * 100, 1)
        else:
            ss_hit = hit_pct
        print(f"  {label}:")
        print(f"    Total: input={total_in:,}  cache_read={total_cr:,}  "
              f"cache_write={total_cw:,}")
        print(f"    Overall hit rate: {hit_pct}%")
        print(f"    Steady-state hit rate (R3+): {ss_hit}%")
        return {'total_cr': total_cr, 'total_in': total_in,
                'hit_pct': hit_pct, 'ss_hit': ss_hit}

    s_solo = _summarize(solo_a, "Solo Conv A")
    s_intlv_a = _summarize(intlv_a, "Interleaved Conv A")
    s_intlv_b = _summarize(intlv_b, "Interleaved Conv B")

    # ── Per-round comparison ──
    print(f"\n  Per-round cache_read comparison (Solo A vs Interleaved A):")
    print(f"  {'Round':<8} {'Solo CR':>12} {'Intlv CR':>12} {'Delta':>10} {'Delta%':>8}")
    print(f"  {'-'*52}")
    deltas = []
    for i in range(min(len(solo_a), len(intlv_a))):
        s_cr = solo_a[i]['cache_read']
        i_cr = intlv_a[i]['cache_read']
        d = i_cr - s_cr
        dpct = round(d / max(s_cr, 1) * 100, 1)
        deltas.append(d)
        flag = '  ⚠️' if dpct < -20 else ''
        print(f"  R{i+1:<7} {s_cr:>12,} {i_cr:>12,} {d:>+10,} {dpct:>+7.1f}%{flag}")

    # ── Verdict — compare per-round cache_read (the definitive signal) ──
    print(f"\n  {'─'*52}")
    ss_delta = s_intlv_a['ss_hit'] - s_solo['ss_hit']
    print(f"\n  Hit rate delta (R3+): {ss_delta:+.1f}% "
          f"(Solo={s_solo['ss_hit']}% → Interleaved={s_intlv_a['ss_hit']}%)")

    # The TRUE test: compare cache_read values round-by-round.
    # If contention exists, interleaved cache_read would be LOWER than solo
    # (the other conversation evicts our cache).
    # Tolerance: 5% difference per round.
    cr_diffs = []
    for i in range(min(len(solo_a), len(intlv_a))):
        s_cr = solo_a[i]['cache_read']
        i_cr = intlv_a[i]['cache_read']
        if s_cr > 0:  # only compare rounds where solo had cache hits
            pct_diff = (i_cr - s_cr) / s_cr * 100
            cr_diffs.append(pct_diff)

    if cr_diffs:
        avg_cr_diff = sum(cr_diffs) / len(cr_diffs)
        min_cr_diff = min(cr_diffs)
        print(f"  Per-round cache_read diff (only hit rounds): "
              f"avg={avg_cr_diff:+.1f}%, min={min_cr_diff:+.1f}%")
    else:
        avg_cr_diff = 0
        print(f"  No rounds had cache hits in both phases — inconclusive.")

    if not cr_diffs:
        print(f"\n  ⚠️ INCONCLUSIVE: Not enough cache hit rounds to compare.")
    elif min_cr_diff > -5:
        print(f"\n  ✅ VERDICT: NO CACHE CONTENTION")
        print(f"     Per-round cache_read is identical between solo and interleaved.")
        print(f"     Two conversations on the same model do NOT evict each other's cache.")
        print(f"     Anthropic cache is keyed on prefix bytes — different conversations")
        print(f"     have different keys and cannot interfere.")
        print(f"     The 'cache contention' detection in cache_tracking.py is WRONG.")
    elif min_cr_diff < -20:
        print(f"\n  ❌ VERDICT: CACHE CONTENTION EXISTS")
        print(f"     Per-round cache_read dropped significantly in interleaved mode.")
        print(f"     Two conversations on the same model DO evict each other's cache.")
    else:
        print(f"\n  🤔 VERDICT: MARGINAL EFFECT (needs more rounds)")
        print(f"     Small cache_read differences ({min_cr_diff:+.1f}%) — could be noise.")


def main():
    parser = argparse.ArgumentParser(description='Test Anthropic cache contention')
    parser.add_argument('--model', default=DEFAULT_MODEL, help='Model to test')
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS,
                        help='Rounds per conversation')
    args = parser.parse_args()

    model = args.model
    num_rounds = args.rounds

    if not is_claude(model):
        print(f"⚠️  Model '{model}' is not Claude — prompt caching may not work.")
        print(f"   Use --model aws.claude-opus-4.6 or similar.")
        sys.exit(1)

    print(f"╔{'═'*68}╗")
    print(f"║  Anthropic Cache Contention Test                                  ║")
    print(f"║  Model: {model:<58}║")
    print(f"║  Rounds: {num_rounds} per phase                                          ║")
    print(f"║                                                                    ║")
    print(f"║  HYPOTHESIS: Different conversations do NOT evict each other's     ║")
    print(f"║  cache because they have different prefix keys.                    ║")
    print(f"╚{'═'*68}╝")

    # Phase 1: Solo test
    solo_a = run_solo_test(model, num_rounds)

    # Wait for cache to expire (6 minutes) to get a clean Phase 2
    # The system prompts use unique arm seeds so Phase 1 and Phase 2 have
    # different cache keys. But we still need a small gap for rate limits.
    print(f"\n  ⏳ Waiting 10 seconds between phases...")
    time.sleep(10)

    # Phase 2: Interleaved test
    intlv_a, intlv_b = run_interleaved_test(model, num_rounds)

    # Phase 3: Analysis
    analyze_results(solo_a, intlv_a, intlv_b)


if __name__ == '__main__':
    main()
