#!/usr/bin/env python3
"""A/B Test: Timestamp Injection Placement & Prompt Cache Impact.

Investigates WHY non-project conversations get 0% cache hit, and tests
different timestamp placement strategies for cost optimization.

Root cause found:
  `inject_search_addendum_to_user()` injects "Current date and time: ..."
  into the last user message EVERY round. This causes:
    1. INTRA-TASK: minute rolls over → user msg bytes change → cache miss
    2. INTER-TASK: cached prefix has user_msg+timestamp, but next task
       sends user_msg_clean (no timestamp) → prefix mismatch → cache miss
    3. NON-PROJECT: system prompt is too small (<4096 tokens for Opus),
       so only BP4 on the tail works — and the tail changes every round

Arms tested:
  A. BASELINE — timestamp injected every round into last user msg
  B. ROUND0_ONLY — timestamp injected only on round 0, skipped on R1+
  C. NO_TIMESTAMP — no timestamp injection at all (maximum cache)
  D. SYSTEM_TAIL — timestamp appended to system message (last block)

Each arm runs a 6-round tool conversation, then a fresh "Task 2" that
reuses the same conversation history (simulating inter-task cache).

Usage:
    python debug/test_timestamp_cache_ab.py
    python debug/test_timestamp_cache_ab.py --model aws.claude-opus-4.6 --rounds 8
    python debug/test_timestamp_cache_ab.py --dry-run
    python debug/test_timestamp_cache_ab.py --arms BASELINE,ROUND0_ONLY

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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm_client import add_cache_breakpoints, build_body, stream_chat
from lib.model_info import is_claude


# ═══════════════════════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL = 'aws.claude-opus-4.6'
DEFAULT_ROUNDS = 6

# Minimal system prompt (< 4096 tokens) — simulates non-project conversations
# This is deliberately SHORT to reproduce the no-project cache miss scenario.
SYSTEM_PROMPT_SMALL = """\
You are a helpful AI assistant. Answer questions accurately and concisely.
When given tools, use them to research before answering.
Be direct and provide well-sourced information."""

# Large system prompt (> 4096 tokens) — simulates project conversations
# Used for comparison to show cache works fine with large prompts.
SYSTEM_PROMPT_LARGE = """\
You are an AI coding assistant called Tofu. You help users with programming tasks.

## Core Rules
1. Always write clean, well-documented code.
2. Follow the project's coding conventions strictly.
3. Test your changes before suggesting them to the user.
4. Use project tools to explore and modify code — never guess.
5. Never modify files without reading them first.
6. Prefer batch apply_diff over separate calls.
7. Read WIDE, not narrow — read 200+ lines for context.

## Project Context
This is a Python Flask web application with vanilla JS frontend.

### Architecture
- Flask Blueprints for routing (routes/*.py)
- Task lifecycle: POST → background thread → SSE → persist
- LLM client: build_body() → stream_chat() with retry
- Tool execution: lib/tools.py → lib/tasks_pkg/executor.py

### Error Handling
```python
try:
    resp = requests.get(url, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
except requests.Timeout:
    logger.warning('[Fetch] Timeout: %s', url)
    return ''
except requests.RequestException as e:
    logger.warning('[Fetch] Failed: %s: %s', url, e)
    return ''
```

```python
try:
    data = json.loads(raw)
except (json.JSONDecodeError, TypeError) as e:
    logger.warning('Invalid JSON: %s', e)
    data = {}
```

### Logging Discipline
Every code path that can fail MUST leave a trace in the log file.
Silent failures are the enemy. Use %-style formatting (lazy eval).
Sanitize secrets: never log API keys. Truncate large data: %.500s.

### Code Style
- Imports: stdlib → third-party → lib.* → routes.*
- Logger: from lib.log import get_logger; logger = get_logger(__name__)
- Type hints on public functions. Google-style docstrings.
- Constants: UPPER_SNAKE_CASE. Private helpers: _ prefix.

### File Modification Checklist
- Logger present in every file
- No silent catches — every except logs something
- Context in logs (conv_id, task_id, url, model)
- exc_info=True on logger.error()
- No f-strings in log calls
- Secrets not logged
- Large data truncated

### Tool Guidance
- list_dir for exploration
- read_files for understanding (batch reads, auto-expand <40KB)
- grep_search for patterns (case-insensitive, context lines)
- write_file for new/major rewrites
- apply_diff for targeted edits (exact match required)
- run_command for shell ops
- web_search then fetch_url for web info

### Database Patterns
```python
try:
    db.execute(sql, params)
    db.commit()
except Exception as e:
    logger.error('DB write failed: %s', e, exc_info=True)
    db.rollback()
    raise
```

### Background Threads
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

### Additional Context
The project uses PostgreSQL for persistence and SSE for streaming.
Multi-file logging architecture: app.log, access.log, error.log, vendor.log.
Per-project config isolation in data/config/.
Trading module disabled by default (env-var TRADING_ENABLED=1).
Cross-platform support: Linux, macOS, Windows via lib/compat.py.
"""

TOOLS = [
    {"type": "function", "function": {"name": "list_dir", "description": "List directory contents.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "read_files", "description": "Read file contents.", "parameters": {"type": "object", "properties": {"reads": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}, "required": ["reads"]}}},
    {"type": "function", "function": {"name": "grep_search", "description": "Search for patterns in files.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "web_search", "description": "Search the web.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "Fetch URL content.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "run_command", "description": "Execute shell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]

TOOL_RESULTS = {
    'list_dir': "Directory: .\n  📄 server.py (245L)\n  📄 bootstrap.py (189L)\n  📁 lib/ (42 items)\n  📁 routes/ (18 items)\n  📁 static/ (31 items)",
    'read_files': "File: lib/llm_client.py (lines 1-50)\n────────────────\nimport json\nimport os\nimport time\n\nfrom lib.log import get_logger\nlogger = get_logger(__name__)\n\nMAX_RETRIES = 4\nRETRY_BACKOFF = 3\n",
    'grep_search': 'grep "cache" — 8 matches:\n\nlib/llm_client.py:904:def add_cache_breakpoints(body):\nlib/tasks_pkg/cache_tracking.py:1:"""Cache tracking.\n',
    'web_search': "1. [Anthropic] Prompt Caching — https://docs.anthropic.com/caching\n   90% cost reduction for cached prompts.\n2. [Blog] LLM Cost Optimization\n",
    'fetch_url': "# Prompt Caching\n\nCache breakpoints mark content for caching.\nMin: 4096 tokens (Opus), 1024 (Sonnet).\nTTL: 5 min default, 1h with extended beta.\nCost: write=1.25x, read=0.1x\n",
    'run_command': "$ wc -l lib/llm_client.py\n1959 lib/llm_client.py\n[exit code: 0]",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Arm implementations
# ═══════════════════════════════════════════════════════════════════════════════

def _inject_timestamp_BASELINE(messages, round_num):
    """BASELINE: Inject timestamp into last user message EVERY round."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    ts_line = f'Current date and time: {now}'
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            # Strip old timestamp
            lines = content.split('\n')
            cleaned = [ln for ln in lines if not ln.strip().startswith('Current date and time:')]
            content = '\n'.join(cleaned).rstrip()
            messages[i]['content'] = content + '\n\n' + ts_line
            return


def _inject_timestamp_ROUND0_ONLY(messages, round_num):
    """ROUND0_ONLY: Inject timestamp only on round 0, skip subsequent rounds."""
    if round_num > 0:
        return  # Skip — preserve cached prefix bytes
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    ts_line = f'Current date and time: {now}'
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            messages[i]['content'] = content + '\n\n' + ts_line
            return


def _inject_timestamp_NONE(messages, round_num):
    """NO_TIMESTAMP: No injection at all."""
    pass


def _inject_timestamp_SYSTEM_TAIL(messages, round_num):
    """SYSTEM_TAIL: Inject timestamp at end of system message."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    ts_line = f'Current date and time: {now}'
    if messages and messages[0].get('role') == 'system':
        sc = messages[0].get('content', '')
        if isinstance(sc, str):
            # Strip old timestamp
            lines = sc.split('\n')
            cleaned = [ln for ln in lines if not ln.strip().startswith('Current date and time:')]
            sc = '\n'.join(cleaned).rstrip()
            messages[0]['content'] = sc + '\n\n' + ts_line
        elif isinstance(sc, list):
            # Remove old timestamp blocks, add new one
            messages[0]['content'] = [
                b for b in sc
                if not (isinstance(b, dict) and b.get('type') == 'text'
                        and b.get('text', '').strip().startswith('Current date and time:'))
            ]
            messages[0]['content'].append({
                'type': 'text', 'text': '\n\n' + ts_line,
            })


ARM_CONFIGS = {
    'BASELINE': {
        'desc': 'Timestamp in last user msg every round (current behavior)',
        'inject_fn': _inject_timestamp_BASELINE,
    },
    'ROUND0_ONLY': {
        'desc': 'Timestamp in last user msg only on round 0',
        'inject_fn': _inject_timestamp_ROUND0_ONLY,
    },
    'NO_TIMESTAMP': {
        'desc': 'No timestamp injection (max cache)',
        'inject_fn': _inject_timestamp_NONE,
    },
    'SYSTEM_TAIL': {
        'desc': 'Timestamp at end of system message',
        'inject_fn': _inject_timestamp_SYSTEM_TAIL,
    },
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
    tool_calls: int = 0
    finish_reason: str = ''
    error: str = ''


@dataclass
class ArmResult:
    label: str
    desc: str
    system_type: str  # 'small' or 'large'
    rounds: list = field(default_factory=list)

    @property
    def valid_rounds(self):
        return [r for r in self.rounds if not r.error]

    @property
    def total_cache_read(self):
        return sum(r.cache_read for r in self.valid_rounds)

    @property
    def total_cache_write(self):
        return sum(r.cache_write for r in self.valid_rounds)

    @property
    def total_prompt(self):
        return sum(r.prompt_tokens for r in self.valid_rounds)

    @property
    def total_output(self):
        return sum(r.output_tokens for r in self.valid_rounds)

    def cost(self, cw_rate=1.25, cr_rate=0.1, base_rate=1.0, out_rate=1.0):
        """Relative cost (in arbitrary units based on rates)."""
        c = 0
        for r in self.valid_rounds:
            c += r.cache_write * cw_rate
            c += r.cache_read * cr_rate
            c += r.prompt_tokens * base_rate
            c += r.output_tokens * out_rate
        return c


# ═══════════════════════════════════════════════════════════════════════════════
#  Tool simulation
# ═══════════════════════════════════════════════════════════════════════════════

# Predefined tool call sequences — model doesn't actually choose
TOOL_SEQUENCE = [
    [{'name': 'list_dir', 'args': '{"path": "."}'}],
    [{'name': 'read_files', 'args': '{"reads": [{"path": "lib/llm_client.py"}]}'}],
    [{'name': 'grep_search', 'args': '{"pattern": "cache"}'},
     {'name': 'web_search', 'args': '{"query": "anthropic prompt caching best practices"}'}],
    [{'name': 'fetch_url', 'args': '{"url": "https://docs.anthropic.com/caching"}'}],
    [{'name': 'run_command', 'args': '{"command": "wc -l lib/llm_client.py"}'}],
]


def _make_tool_calls(round_num):
    """Get tool calls for a given round (cycles through TOOL_SEQUENCE)."""
    if round_num >= len(TOOL_SEQUENCE):
        return None  # No more tool calls — model should respond
    calls = TOOL_SEQUENCE[round_num]
    result = []
    for i, tc in enumerate(calls):
        result.append({
            'id': f'call_{round_num}_{i}',
            'type': 'function',
            'function': {'name': tc['name'], 'arguments': tc['args']},
        })
    return result


def _make_tool_results(tool_calls):
    """Generate tool results for given tool calls."""
    results = []
    for tc in tool_calls:
        fn_name = tc['function']['name']
        content = TOOL_RESULTS.get(fn_name, f'Result for {fn_name}')
        results.append({
            'role': 'tool',
            'tool_call_id': tc['id'],
            'content': content,
        })
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  Run one arm
# ═══════════════════════════════════════════════════════════════════════════════

def run_arm(label, model, max_rounds, system_prompt, arm_seed, dry_run=False):
    """Run one arm of the A/B test.

    Simulates a multi-round tool conversation:
    - Rounds 0..N-2: model calls tools, we inject results
    - Round N-1: model responds with text (no tools)

    Then simulates "Task 2": a new user message is appended to the
    conversation history, and we make one more API call to test
    inter-task cache hits.

    Args:
        label: Arm label (e.g. 'BASELINE', 'ROUND0_ONLY')
        model: Model to use
        max_rounds: Number of tool rounds + 1 final response
        system_prompt: System prompt text
        arm_seed: Unique seed appended to system prompt for cache isolation
        dry_run: If True, skip API calls

    Returns:
        ArmResult with per-round stats
    """
    cfg = ARM_CONFIGS[label]
    inject_fn = cfg['inject_fn']
    sys_type = 'large' if len(system_prompt) > 3000 else 'small'

    result = ArmResult(label=label, desc=cfg['desc'], system_type=sys_type)

    # Build initial messages
    messages = [
        {'role': 'system', 'content': system_prompt + arm_seed},
        {'role': 'user', 'content':
         'Investigate the prompt cache implementation in this project. '
         'Start by exploring the project structure, then read the relevant '
         'cache code, search for patterns, and look up documentation.'},
    ]

    print(f'\n  ╔═══ Arm {label}: {cfg["desc"]}')
    print(f'  ║  System prompt: ~{len(system_prompt)} chars ({sys_type})')
    print(f'  ║  Model: {model}')
    print(f'  ║  Max rounds: {max_rounds}')

    for round_num in range(max_rounds):
        t0 = time.time()

        # Apply timestamp injection for this arm
        inject_fn(messages, round_num)

        # Determine if this is a tool round or final response round
        tool_calls = _make_tool_calls(round_num)
        is_final = (tool_calls is None) or (round_num == max_rounds - 1)

        # Build body
        body = build_body(
            model, list(messages),
            max_tokens=1024 if is_final else 256,
            temperature=0,
            thinking_enabled=False,
            preset='low',
            tools=TOOLS if not is_final else None,
            stream=True,
        )

        if dry_run:
            rr = RoundResult(round_num=round_num)
            rr.prompt_tokens = 100
            rr.cache_write = 5000 + round_num * 500
            rr.cache_read = 0 if label == 'BASELINE' else 4000
            rr.output_tokens = 50
            rr.tool_calls = len(tool_calls) if tool_calls else 0
            rr.finish_reason = 'tool_calls' if not is_final else 'stop'
            rr.total_input = rr.prompt_tokens + rr.cache_write + rr.cache_read
            rr.elapsed = 0.1
            result.rounds.append(rr)
            _print_round(rr, is_final)

            # Simulate assistant response with tool calls
            if not is_final and tool_calls:
                messages.append({
                    'role': 'assistant',
                    'content': f'Let me use some tools (round {round_num}).',
                    'tool_calls': tool_calls,
                })
                for tr in _make_tool_results(tool_calls):
                    messages.append(tr)
            else:
                messages.append({
                    'role': 'assistant',
                    'content': 'Based on my investigation, the cache implementation works as follows...',
                })
            continue

        # Real API call
        try:
            msg_text, finish_reason, usage = stream_chat(
                body,
                on_thinking=lambda t: None,
                on_content=lambda c: None,
                log_prefix=f'[AB:{label}:R{round_num}]',
            )
        except Exception as e:
            rr = RoundResult(round_num=round_num, error=str(e))
            result.rounds.append(rr)
            print(f'  ║  R{round_num}: ERROR — {e}')
            break

        elapsed = time.time() - t0
        rr = RoundResult(round_num=round_num)
        rr.finish_reason = finish_reason or ''
        rr.elapsed = elapsed

        if usage:
            rr.prompt_tokens = (usage.get('prompt_tokens')
                                or usage.get('input_tokens') or 0)
            rr.cache_write = (usage.get('cache_write_tokens')
                              or usage.get('cache_creation_input_tokens') or 0)
            rr.cache_read = (usage.get('cache_read_tokens')
                             or usage.get('cache_read_input_tokens') or 0)
            rr.output_tokens = (usage.get('completion_tokens')
                                or usage.get('output_tokens') or 0)
            rr.total_input = rr.prompt_tokens + rr.cache_write + rr.cache_read

        result.rounds.append(rr)
        _print_round(rr, is_final)

        # Simulate tool round: add assistant + tool results
        if not is_final and tool_calls:
            messages.append({
                'role': 'assistant',
                'content': msg_text or '',
                'tool_calls': tool_calls,
            })
            for tr in _make_tool_results(tool_calls):
                messages.append(tr)
        else:
            messages.append({
                'role': 'assistant',
                'content': msg_text or 'Analysis complete.',
            })
            break

    # ── Task 2: simulate inter-task cache test ──
    # Append a new user message (as if the frontend sent a new query)
    # and make one more API call to test if the prefix gets cache hits.
    print(f'  ║')
    print(f'  ║  ── Task 2 (inter-task cache test) ──')

    # Clean historical messages: remove timestamps from old user messages
    # (simulating what the frontend would send — clean, no injected content)
    messages_task2 = _strip_injected_timestamps(messages)
    messages_task2.append({
        'role': 'user',
        'content': 'Now explain the cache breakpoint placement strategy '
                   'and how the mixed TTL strategy works.',
    })

    inject_fn(messages_task2, 0)  # Inject into new user message

    body = build_body(
        model, messages_task2,
        max_tokens=512,
        temperature=0,
        thinking_enabled=False,
        preset='low',
        tools=TOOLS,
        stream=True,
    )

    if dry_run:
        rr = RoundResult(round_num=max_rounds)
        rr.prompt_tokens = 100
        rr.cache_write = 2000
        rr.cache_read = 0 if label == 'BASELINE' else 8000
        rr.output_tokens = 100
        rr.finish_reason = 'stop'
        rr.total_input = rr.prompt_tokens + rr.cache_write + rr.cache_read
        rr.elapsed = 0.1
        result.rounds.append(rr)
        _print_round(rr, True, tag='T2')
    else:
        try:
            t0 = time.time()
            msg_text, finish_reason, usage = stream_chat(
                body,
                on_thinking=lambda t: None,
                on_content=lambda c: None,
                log_prefix=f'[AB:{label}:T2]',
            )
            elapsed = time.time() - t0
            rr = RoundResult(round_num=max_rounds)
            rr.finish_reason = finish_reason or ''
            rr.elapsed = elapsed
            if usage:
                rr.prompt_tokens = (usage.get('prompt_tokens')
                                    or usage.get('input_tokens') or 0)
                rr.cache_write = (usage.get('cache_write_tokens')
                                  or usage.get('cache_creation_input_tokens') or 0)
                rr.cache_read = (usage.get('cache_read_tokens')
                                 or usage.get('cache_read_input_tokens') or 0)
                rr.output_tokens = (usage.get('completion_tokens')
                                    or usage.get('output_tokens') or 0)
                rr.total_input = rr.prompt_tokens + rr.cache_write + rr.cache_read
            result.rounds.append(rr)
            _print_round(rr, True, tag='T2')
        except Exception as e:
            rr = RoundResult(round_num=max_rounds, error=str(e))
            result.rounds.append(rr)
            print(f'  ║  T2: ERROR — {e}')

    print(f'  ╚═══ Arm {label} complete: '
          f'total_cw={result.total_cache_write:,} '
          f'total_cr={result.total_cache_read:,} '
          f'cost={result.cost():.0f}')

    return result


def _strip_injected_timestamps(messages):
    """Strip injected timestamps from all user messages (simulates frontend clean messages)."""
    result = []
    for msg in messages:
        msg = dict(msg)
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, str):
                lines = content.split('\n')
                cleaned = [ln for ln in lines if not ln.strip().startswith('Current date and time:')]
                msg['content'] = '\n'.join(cleaned).rstrip()
        result.append(msg)
    return result


def _print_round(rr, is_final, tag=None):
    """Print a single round's results."""
    label = tag or f'R{rr.round_num}'
    hit_pct = round(rr.cache_read / max(rr.total_input, 1) * 100)
    marker = '✅' if hit_pct > 50 else ('🟡' if hit_pct > 0 else '❌')
    print(f'  ║  {label}: inp={rr.prompt_tokens:>6} cw={rr.cache_write:>6} '
          f'cr={rr.cache_read:>6} hit={hit_pct:>2}% {marker} '
          f'out={rr.output_tokens:>4} {rr.finish_reason} '
          f'({rr.elapsed:.1f}s)')


# ═══════════════════════════════════════════════════════════════════════════════
#  Results comparison
# ═══════════════════════════════════════════════════════════════════════════════

def compare_results(results: list[ArmResult]):
    """Print a comparative summary table."""
    print('\n' + '=' * 90)
    print('COMPARATIVE RESULTS')
    print('=' * 90)

    # Header
    print(f'\n{"Arm":<16} {"SysPrompt":<8} {"CacheWrite":>10} {"CacheRead":>10} '
          f'{"Uncached":>8} {"Output":>8} {"Cost":>10} {"Hit%":>6} {"Savings":>8}')
    print('-' * 90)

    baseline_cost = None
    for r in results:
        cost = r.cost()
        if baseline_cost is None:
            baseline_cost = cost
        total_inp = r.total_cache_write + r.total_cache_read + r.total_prompt
        hit_pct = round(r.total_cache_read / max(total_inp, 1) * 100)
        savings = f'{(1 - cost / baseline_cost) * 100:.1f}%' if baseline_cost else '-'
        if r is results[0]:
            savings = 'baseline'
        print(f'{r.label:<16} {r.system_type:<8} {r.total_cache_write:>10,} '
              f'{r.total_cache_read:>10,} {r.total_prompt:>8,} '
              f'{r.total_output:>8,} {cost:>10,.0f} {hit_pct:>5}% {savings:>8}')

    print()

    # Per-round comparison
    max_rounds = max(len(r.rounds) for r in results)
    print(f'\n{"Round":<6}', end='')
    for r in results:
        print(f'  {r.label + " hit%":>20}', end='')
    print()
    print('-' * (6 + 22 * len(results)))

    for i in range(max_rounds):
        is_t2 = i == max_rounds - 1 and i > 0
        label = f'T2' if is_t2 else f'R{i}'
        print(f'{label:<6}', end='')
        for r in results:
            if i < len(r.rounds):
                rr = r.rounds[i]
                total = rr.total_input or 1
                hit = round(rr.cache_read / total * 100)
                print(f'  {f"cw={rr.cache_write} cr={rr.cache_read} {hit}%":>20}', end='')
            else:
                print(f'  {"—":>20}', end='')
        print()

    print()

    # Winner
    costs = [(r.label, r.cost()) for r in results]
    costs.sort(key=lambda x: x[1])
    winner = costs[0]
    worst = costs[-1]
    if worst[1] > 0:
        savings_pct = (1 - winner[1] / worst[1]) * 100
        print(f'🏆 WINNER: {winner[0]} — {savings_pct:.1f}% cheaper than {worst[0]}')
    else:
        print(f'🏆 WINNER: {winner[0]}')

    # Recommendations
    print('\n📋 RECOMMENDATIONS:')
    for r in results:
        # Check if Task 2 had cache hits
        if len(r.rounds) > 0:
            t2 = r.rounds[-1]
            t2_hit = round(t2.cache_read / max(t2.total_input, 1) * 100)
            if t2_hit > 50:
                print(f'  ✅ {r.label}: Task 2 got {t2_hit}% cache hit (good inter-task cache)')
            elif t2_hit > 0:
                print(f'  🟡 {r.label}: Task 2 got {t2_hit}% cache hit (partial)')
            else:
                print(f'  ❌ {r.label}: Task 2 got 0% cache hit (inter-task cache miss)')


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='A/B test timestamp injection placement for prompt cache')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS,
                        help='Number of tool rounds per arm')
    parser.add_argument('--arms', default='BASELINE,ROUND0_ONLY,NO_TIMESTAMP,SYSTEM_TAIL',
                        help='Comma-separated arm labels')
    parser.add_argument('--system', default='both',
                        choices=['small', 'large', 'both'],
                        help='System prompt size (small=non-project, large=project)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Skip API calls, use simulated data')
    args = parser.parse_args()

    arms = [a.strip() for a in args.arms.split(',')]
    for a in arms:
        if a not in ARM_CONFIGS:
            print(f'Unknown arm: {a}. Available: {list(ARM_CONFIGS.keys())}')
            sys.exit(1)

    system_prompts = []
    if args.system in ('small', 'both'):
        system_prompts.append(('small', SYSTEM_PROMPT_SMALL))
    if args.system in ('large', 'both'):
        system_prompts.append(('large', SYSTEM_PROMPT_LARGE))

    print('═' * 70)
    print(f'TIMESTAMP PLACEMENT A/B TEST')
    print(f'Model: {args.model}')
    print(f'Arms: {", ".join(arms)}')
    print(f'Rounds per arm: {args.rounds}')
    print(f'System prompts: {", ".join(s[0] for s in system_prompts)}')
    print(f'Dry run: {args.dry_run}')
    print('═' * 70)

    all_results = []

    for sys_name, sys_prompt in system_prompts:
        print(f'\n{"━" * 70}')
        print(f'  SCENARIO: system_prompt={sys_name} ({len(sys_prompt)} chars)')
        print(f'{"━" * 70}')

        scenario_results = []
        for arm_label in arms:
            # ★ CRITICAL: unique arm seed to prevent cross-arm cache sharing
            arm_seed = f'\n\n<!-- ab_test arm={arm_label} sys={sys_name} seed={time.time():.0f} -->'

            result = run_arm(
                label=arm_label,
                model=args.model,
                max_rounds=args.rounds,
                system_prompt=sys_prompt,
                arm_seed=arm_seed,
                dry_run=args.dry_run,
            )
            scenario_results.append(result)

            # Brief pause between arms to let timestamp minute possibly roll over
            # (which is the exact scenario we're testing)
            if not args.dry_run:
                time.sleep(2)

        compare_results(scenario_results)
        all_results.extend(scenario_results)

    if len(system_prompts) > 1:
        print('\n' + '═' * 90)
        print('CROSS-SCENARIO SUMMARY')
        print('═' * 90)
        for sys_name, _ in system_prompts:
            subset = [r for r in all_results if r.system_type == sys_name]
            if subset:
                winner = min(subset, key=lambda r: r.cost())
                print(f'\n  {sys_name} system prompt:')
                for r in subset:
                    cost = r.cost()
                    savings = (1 - cost / subset[0].cost()) * 100 if subset[0].cost() else 0
                    marker = '🏆' if r is winner else '  '
                    print(f'    {marker} {r.label:<16} cost={cost:>10,.0f} '
                          f'({savings:+.1f}% vs baseline)')


if __name__ == '__main__':
    main()
