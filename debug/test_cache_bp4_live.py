#!/usr/bin/env python3
"""Live API test: verify cache breakpoint BP4 placement in multi-round tool conversations.

This test simulates 6 rounds of a tool conversation with aws.claude-opus-4.6,
measuring cache_read_tokens and cache_creation_input_tokens (cache_write) to confirm:

1. BP4 is placed correctly even when assistant messages have empty content (tool_calls only)
2. Cache hits are stable across rounds (no oscillation)
3. The system prompt + tools cache is reused (BP1-BP3)
4. The conversation tail cache advances correctly (BP4)

Usage:
    python debug/test_cache_bp4_live.py [--model MODEL] [--rounds N]

Expects:
    - Server config at data/config/server_config.json with valid provider keys
    - Or LLM_API_KEYS / LLM_BASE_URL env vars set
"""

import argparse
import copy
import json
import os
import sys
import time

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm import (
    add_cache_breakpoints,
    build_body,
    stream_chat,
)
from lib.model_info import is_claude

# ═══════════════════════════════════════════════════════════
#  Test configuration
# ═══════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 6

# Realistic system prompt (>= 2048 tokens to exceed Anthropic's 1024 minimum cache threshold)
# In real conversations this is 5-10x larger (with full tool guidance, FRC, etc.)
SYSTEM_PROMPT = """You are an AI coding assistant called Tofu (豆腐). You help users with programming tasks.

## Rules
1. Always write clean, well-documented code
2. Follow the project's coding conventions
3. Test your changes before suggesting them
4. Use the project tools to explore and modify code
5. Never modify files without reading them first
6. When making multiple edits, prefer batch apply_diff over separate calls
7. Read WIDE, not narrow — read 200+ lines in one shot
8. Prefer reading the WHOLE file for files under 500 lines
9. Use grep_search for finding code patterns
10. Use read_files for understanding code
11. Prefer run_command for shell operations

## Project Context
This is a Python Flask web application with a vanilla JS frontend.
The project uses PostgreSQL for persistence and SSE for streaming.
Key directories: lib/ (backend logic), routes/ (Flask blueprints), static/js/ (frontend).

Architecture:
- Flask Blueprint registration: routes/*.py as Blueprints, routes/__init__.py wires them
- Task lifecycle (SSE streaming): POST /api/chat/start → background thread → SSE events → persist
- LLM client flow: build_body() constructs model-specific payloads, stream_chat() handles SSE
- Tool execution: tools.py defines, executor.py executes
- Token-saving tools: emit_to_user and content_ref avoid re-generating content

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
    logger.warning('Invalid JSON: %s', e)
    data = {}
```

### Database operations
```python
try:
    db.execute(sql, params)
    db.commit()
except Exception as e:
    logger.error('DB write failed: %s', e, exc_info=True)
    db.rollback()
    raise
```

## Output Guidelines
- Be concise and direct
- Lead with the answer, not the reasoning
- Show exact code with file paths
- Use apply_diff for small edits, write_file for new files
- Keep text output brief and direct
- Focus on decisions that need user input
- High-level status updates at natural milestones
- Errors or blockers that change the plan

## Logging Discipline
Every code path that can fail MUST leave a trace in the log file.
Silent failures are the enemy. Every except block logs something.
Use %-style formatting for lazy evaluation.
Sanitize secrets: never log API keys, tokens, or passwords.
Truncate large data with %.500s or [:500].

## Code Style
- Imports: stdlib → third-party → lib.* → routes.*, blank line between groups
- Logger init: from lib.log import get_logger; logger = get_logger(__name__)
- Type hints: encouraged on public functions
- Docstrings: Google-style on modules and public functions
- Constants: UPPER_SNAKE_CASE at module level
- Private helpers: prefix with _ (e.g., _parse_sse_line())

## File Modification Checklist
- Logger present in every file
- No silent catches — every except block logs something
- Context in logs: relevant IDs (conv_id, task_id, url, model)
- Tracebacks on errors: exc_info=True on logger.error()
- No f-strings in log calls
- Secrets not logged
- Large data truncated
"""

# Realistic tool definitions (subset of actual project tools)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory in the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from project root."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_files",
            "description": "Read the contents of one or more files in the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "start_line": {"type": "integer"},
                                "end_line": {"type": "integer"}
                            },
                            "required": ["path"]
                        }
                    }
                },
                "required": ["reads"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search for a pattern across project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search pattern"},
                    "path": {"type": "string", "description": "Path to search in"},
                    "include": {"type": "string", "description": "File glob filter"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file in the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command and return output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and read the full content of a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"}
                },
                "required": ["url"]
            }
        }
    },
]

# Fake tool results (simulating real tool execution)
FAKE_TOOL_RESULTS = {
    'list_dir': lambda args: (
        "Directory: lib/\n\n"
        "Files:\n"
        "  📄 __init__.py (481L, 15.2KB)\n"
        "  📄 llm_client.py (1731L, 67.5KB)\n"
        "  📄 model_info.py (311L, 10.2KB)\n"
        "  📄 tools.py (890L, 32.1KB)\n"
        "\nSubdirectories:\n"
        "  📁 tasks_pkg/ (15 items)\n"
        "  📁 project_mod/ (8 items)\n"
        "  📁 llm_dispatch/ (7 items)\n"
    ),
    'read_files': lambda args: (
        "File: lib/llm_client.py (lines 765-870 of 1731)\n"
        "────────────────────────────────\n"
        "def add_cache_breakpoints(body, log_prefix=''):\n"
        '    """Add Anthropic-style ephemeral cache breakpoints.\n\n'
        "    Annotates up to 4 content blocks with cache_control.\n"
        '    """\n'
        "    model = body.get('model', '')\n"
        "    if not is_claude(model):\n"
        "        return\n"
        "    messages = body.get('messages', [])\n"
        "    # ... implementation ...\n"
        "    bp = 0\n"
        "    # Cache system messages\n"
        "    for i, msg in enumerate(messages):\n"
        "        if msg.get('role') != 'system' or bp >= 4:\n"
        "            continue\n"
        "        # ... place breakpoints ...\n"
    ),
    'grep_search': lambda args: (
        'grep "add_cache_breakpoints" — 10 matches:\n\n'
        'lib/llm_client.py:765:def add_cache_breakpoints(body, log_prefix=\'\'):\n'
        'lib/llm_client.py:1190:    add_cache_breakpoints(body, log_prefix)\n'
        'lib/tasks_pkg/cache_tracking.py:26:  to cover the growing prefix.\n'
        'tests/test_cc_alignment.py:668:    from lib.llm import add_cache_breakpoints\n'
    ),
    'web_search': lambda args: (
        "Search results for: " + args.get('query', '?') + "\n\n"
        "1. [Anthropic Docs] Prompt Caching - https://docs.anthropic.com/...\n"
        "   Cache breakpoints allow reusing computed KV-cache across requests.\n"
        "2. [Blog] Reducing LLM Costs with Caching - https://blog.example.com/...\n"
        "   Strategies for effective prompt caching with Claude models.\n"
    ),
    'fetch_url': lambda args: (
        "# Anthropic Prompt Caching Documentation\n\n"
        "Prompt caching is a feature that allows you to cache the prefix of your\n"
        "prompts across requests. This can significantly reduce costs and latency\n"
        "for conversations with repetitive context.\n\n"
        "## How it works\n"
        "Cache breakpoints are placed on content blocks using the cache_control\n"
        "parameter. The system generates cache keys using cryptographic hashes.\n"
        "Only requests with identical content up to a breakpoint achieve cache hits.\n\n"
        "## Pricing\n"
        "- Cache writes: 1.25x of base input price\n"
        "- Cache reads: 0.1x of base input price (90% savings)\n"
        "- TTL: 5 minutes (auto-extended on hit)\n"
    ),
}


def get_fake_result(tool_name, args):
    """Generate a fake tool result."""
    fn = FAKE_TOOL_RESULTS.get(tool_name, lambda a: f"Tool {tool_name} executed with args: {json.dumps(a)}")
    return fn(args)


# ═══════════════════════════════════════════════════════════
#  Multi-round conversation simulation
# ═══════════════════════════════════════════════════════════

def run_test(model: str, num_rounds: int):
    """Run a multi-round tool conversation test and report cache stats."""

    print(f"\n{'='*70}")
    print(f"  LIVE CACHE BREAKPOINT TEST — {model}")
    print(f"  Rounds: {num_rounds}")
    print(f"{'='*70}\n")

    if not is_claude(model):
        print(f"❌ Model '{model}' is not a Claude model — cache breakpoints are Claude-only.")
        return False

    # Build initial messages
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': (
            'I want to understand how the cache breakpoint system works in this project. '
            'Please find the relevant code in lib/llm_client.py and explain the '
            'add_cache_breakpoints function. Also check how the orchestrator calls it.'
        )},
    ]

    results = []
    all_passed = True
    tc_counter = 0  # unique tool_call_id counter

    for round_num in range(num_rounds):
        print(f"\n{'─'*50}")
        print(f"  Round {round_num + 1}/{num_rounds}")
        print(f"{'─'*50}")

        # Build body (this also calls add_cache_breakpoints internally)
        body = build_body(
            model, messages,
            max_tokens=4096,
            temperature=1.0,
            thinking_enabled=True,
            preset='medium',
            thinking_depth='medium',
            tools=TOOLS,
            stream=True,
        )

        # add_cache_breakpoints is called inside _stream_chat_once, NOT in build_body.
        # We need to call it here to verify BP4 placement, then strip the markers
        # before the real API call (which will re-add them).
        # Instead, let's simulate what _stream_chat_once does:
        import copy as _copy
        _test_body = _copy.deepcopy(body)
        add_cache_breakpoints(_test_body, log_prefix=f'[TestBP R{round_num+1}]')

        # Verify BP4 placement on the annotated body
        bp4_found = False
        bp4_idx = None
        bp4_role = None
        total_bps = 0
        bp_locations = []
        for idx, msg in enumerate(_test_body['messages']):
            content = msg.get('content')
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and 'cache_control' in blk:
                        total_bps += 1
                        bp_locations.append(f"msg[{idx}]/{msg.get('role')}")
                        if msg.get('role') != 'system':
                            bp4_found = True
                            bp4_idx = idx
                            bp4_role = msg.get('role')
        # Also check tools for BPs
        for ti, tool in enumerate(_test_body.get('tools', [])):
            fn = tool.get('function', {})
            if 'cache_control' in fn:
                total_bps += 1
                bp_locations.append(f"tool[{ti}]")

        bp4_status = f"✅ BP4 on msg[{bp4_idx}] role={bp4_role}" if bp4_found else "❌ BP4 NOT PLACED"
        print(f"  BP4 check: {bp4_status}")
        print(f"  Total BPs: {total_bps} at: {', '.join(bp_locations)}")
        print(f"  Messages: {len(messages)} (body: {len(body['messages'])})")

        if not bp4_found and round_num > 0:
            print(f"  ⚠️  FAIL: BP4 should be placed in multi-round conversations!")
            all_passed = False

        # Describe msg[-2] in messages for diagnostics
        if len(messages) >= 2:
            msg_m2 = messages[-2]
            m2_role = msg_m2.get('role', '?')
            m2_content = msg_m2.get('content', '')
            m2_has_tc = bool(msg_m2.get('tool_calls'))
            m2_content_len = len(m2_content) if isinstance(m2_content, str) else (
                sum(len(b.get('text', '')) for b in m2_content if isinstance(b, dict)) if isinstance(m2_content, list) else 0
            )
            print(f"  msg[-2]: role={m2_role}, content_len={m2_content_len}, has_tool_calls={m2_has_tc}")

        # Make real API call
        t0 = time.time()
        thinking_buf = []
        content_buf = []

        def on_thinking(td):
            thinking_buf.append(td)

        def on_content(cd):
            content_buf.append(cd)

        try:
            assistant_msg, finish_reason, usage = stream_chat(
                body,
                on_thinking=on_thinking,
                on_content=on_content,
                log_prefix=f'[Test R{round_num+1}]',
            )
        except Exception as e:
            print(f"  ❌ API call failed: {e}")
            all_passed = False
            break

        elapsed = time.time() - t0
        content = ''.join(content_buf)
        thinking = ''.join(thinking_buf)

        # Extract cache stats — dump raw usage for debugging
        u = usage or {}
        print(f"  📦 Raw usage keys: {sorted(k for k in u.keys() if not k.startswith('_'))}")
        cache_keys = {k: v for k, v in u.items() if 'cache' in k.lower() and not k.startswith('_')}
        if cache_keys:
            print(f"  📦 Cache fields: {cache_keys}")
        else:
            print(f"  📦 No cache fields in usage!")

        cache_read = u.get('cache_read_tokens') or u.get('cache_read_input_tokens') or 0
        cache_write = u.get('cache_creation_input_tokens') or u.get('cache_write_tokens') or 0
        prompt_tokens = u.get('prompt_tokens', 0)
        output_tokens = u.get('completion_tokens', 0)

        # Classify round
        if cache_write > 1000 and cache_read > 1000:
            cache_status = "HIT+WRITE"
        elif cache_read > 1000:
            cache_status = "HIT"
        elif cache_write > 1000:
            cache_status = "WRITE"
        else:
            cache_status = "MISS ❌"
            if round_num > 0:
                all_passed = False

        result = {
            'round': round_num + 1,
            'prompt_tokens': prompt_tokens,
            'cache_read': cache_read,
            'cache_write': cache_write,
            'output_tokens': output_tokens,
            'finish_reason': finish_reason,
            'content_len': len(content),
            'thinking_len': len(thinking),
            'bp4_placed': bp4_found,
            'bp4_idx': bp4_idx,
            'bp4_role': bp4_role,
            'cache_status': cache_status,
            'elapsed': elapsed,
            'tool_calls': len(assistant_msg.get('tool_calls', [])),
        }
        results.append(result)

        print(f"  ⏱  {elapsed:.1f}s | finish={finish_reason}")
        print(f"  📊 prompt={prompt_tokens:,} cache_read={cache_read:,} "
              f"cache_write={cache_write:,} output={output_tokens:,}")
        print(f"  📝 content={len(content)} chars, thinking={len(thinking)} chars")
        print(f"  🏷  Cache: {cache_status}")
        print(f"  🔧 Tool calls: {result['tool_calls']}")

        # ── Simulate tool execution for next round ──
        tool_calls = assistant_msg.get('tool_calls', [])
        if tool_calls and round_num < num_rounds - 1:
            # Append assistant message with tool_calls (content may be empty!)
            clean_msg = {'role': 'assistant'}
            clean_msg['tool_calls'] = tool_calls
            if assistant_msg.get('content'):
                clean_msg['content'] = assistant_msg['content']
            else:
                # This is the key case: assistant with tool_calls but empty content
                clean_msg['content'] = ''
            messages.append(clean_msg)

            # Append tool results
            for tc in tool_calls:
                tc_counter += 1
                fn_name = tc.get('function', {}).get('name', 'unknown')
                fn_args_str = tc.get('function', {}).get('arguments', '{}')
                try:
                    fn_args = json.loads(fn_args_str)
                except:
                    fn_args = {}

                tool_result = get_fake_result(fn_name, fn_args)
                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc.get('id', f'call_{tc_counter}'),
                    'content': tool_result,
                })
                print(f"     → Simulated {fn_name} → {len(tool_result)} chars")

        elif not tool_calls:
            # Model finished without tool calls — conversation complete
            print(f"  ✅ Model completed without tool calls at round {round_num + 1}")
            break

    # ═══════════════════════════════════════════════════════════
    #  Summary
    # ═══════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print(f"  RESULTS SUMMARY — {model}")
    print(f"{'='*70}\n")

    # Table header
    print(f"  {'Rnd':>3} │ {'Prompt':>8} │ {'CacheRead':>10} │ {'CacheWrite':>11} │ "
          f"{'Output':>7} │ {'Status':>10} │ {'BP4':>6} │ {'Time':>5}")
    print(f"  {'─'*3}─┼─{'─'*8}─┼─{'─'*10}─┼─{'─'*11}─┼─"
          f"{'─'*7}─┼─{'─'*10}─┼─{'─'*6}─┼─{'─'*5}")

    total_prompt = 0
    total_cache_read = 0
    total_cache_write = 0
    total_output = 0
    miss_count = 0
    hit_count = 0

    for r in results:
        total_prompt += r['prompt_tokens']
        total_cache_read += r['cache_read']
        total_cache_write += r['cache_write']
        total_output += r['output_tokens']
        if 'MISS' in r['cache_status']:
            miss_count += 1
        if 'HIT' in r['cache_status']:
            hit_count += 1

        bp4_str = f"m[{r['bp4_idx']}]" if r['bp4_placed'] else "NONE"
        print(f"  {r['round']:>3} │ {r['prompt_tokens']:>8,} │ {r['cache_read']:>10,} │ "
              f"{r['cache_write']:>11,} │ {r['output_tokens']:>7,} │ {r['cache_status']:>10} │ "
              f"{bp4_str:>6} │ {r['elapsed']:>5.1f}s")

    print()
    total_input = total_prompt + total_cache_read + total_cache_write
    print(f"  Total input tokens:  {total_input:>10,}")
    print(f"  ├─ Uncached prompt:  {total_prompt:>10,}  ({total_prompt/max(total_input,1)*100:.1f}%)")
    print(f"  ├─ Cache reads:      {total_cache_read:>10,}  ({total_cache_read/max(total_input,1)*100:.1f}%)")
    print(f"  ├─ Cache writes:     {total_cache_write:>10,}  ({total_cache_write/max(total_input,1)*100:.1f}%)")
    print(f"  └─ Output tokens:    {total_output:>10,}")

    # Cost estimation (Opus 4 pricing: $15/M input, $75/M output, 1.25x write, 0.1x read)
    cost_prompt = total_prompt * 15.0 / 1_000_000
    cost_read = total_cache_read * 15.0 * 0.10 / 1_000_000
    cost_write = total_cache_write * 15.0 * 1.25 / 1_000_000
    cost_output = total_output * 75.0 / 1_000_000
    total_cost = cost_prompt + cost_read + cost_write + cost_output

    # What it would cost WITHOUT caching
    cost_no_cache = (total_prompt + total_cache_read + total_cache_write) * 15.0 / 1_000_000 + cost_output

    savings = cost_no_cache - total_cost if cost_no_cache > 0 else 0
    savings_pct = savings / cost_no_cache * 100 if cost_no_cache > 0 else 0

    print(f"\n  💰 Cost breakdown:")
    print(f"     Uncached prompt:  ${cost_prompt:.4f}")
    print(f"     Cache reads:      ${cost_read:.4f}")
    print(f"     Cache writes:     ${cost_write:.4f}")
    print(f"     Output:           ${cost_output:.4f}")
    print(f"     ─────────────────────────")
    print(f"     Total:            ${total_cost:.4f}")
    print(f"     Without caching:  ${cost_no_cache:.4f}")
    print(f"     Savings:          ${savings:.4f} ({savings_pct:.1f}%)")

    # Cache hit rate
    total_rounds = len(results)
    cache_hit_rate = hit_count / max(total_rounds, 1) * 100
    print(f"\n  📈 Cache hit rate: {hit_count}/{total_rounds} = {cache_hit_rate:.0f}%")
    print(f"     Misses: {miss_count}")

    # Verdict
    print(f"\n  {'='*50}")
    if miss_count == 0 or (miss_count <= 1 and total_rounds > 3):
        print(f"  ✅ PASS — Cache breakpoints working correctly!")
        print(f"     Cache hit rate: {cache_hit_rate:.0f}%")
    elif cache_hit_rate >= 60:
        print(f"  ⚠️  WARN — Some cache misses detected ({miss_count}/{total_rounds})")
        print(f"     This may indicate server-side TTL expiry or routing changes.")
        all_passed = False
    else:
        print(f"  ❌ FAIL — Significant cache misses: {miss_count}/{total_rounds}")
        print(f"     The BP4 fix may not be working correctly.")
        all_passed = False
    print(f"  {'='*50}\n")

    return all_passed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Live cache breakpoint test')
    parser.add_argument('--model', default=DEFAULT_MODEL, help=f'Model to test (default: {DEFAULT_MODEL})')
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS, help=f'Number of rounds (default: {DEFAULT_ROUNDS})')
    args = parser.parse_args()

    success = run_test(args.model, args.rounds)
    sys.exit(0 if success else 1)
