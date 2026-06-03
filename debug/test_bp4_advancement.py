#!/usr/bin/env python3
"""Test: Does BP4 advancement (breakpoint moving forward) cause cache misses?

HYPOTHESIS: When BP4 moves from message[N] to message[N+2] on the next round,
the cached content at position [N] can no longer be read because there's no
breakpoint at that position anymore. Only the stable BP1-BP3 (system+tools)
cache survives, causing cache_read to drop to the system+tools baseline.

TEST DESIGN:
  Round 1: Send [system, user_msg]                   → BP4 on user_msg
  Round 2: Send [system, user_msg, asst, tool_result] → BP4 on tool_result
           Expected: cache_read ≈ Round 1's total (prefix match up to user_msg)
  Round 3: Grow by 2 more messages                    → BP4 advances again
           If BP4 advancement causes miss: cache_read drops to system+tools baseline
           If caching works incrementally: cache_read ≈ Round 2's total

CONTROL: Also test with FIXED BP4 position (always on the same message) to
         confirm that keeping BP4 stable preserves the full cache.

Requirements:
  - Valid provider config
  - Anthropic Claude model with 4096 token minimum cache block

Usage:
    python debug/test_bp4_advancement.py [--model aws.claude-opus-4.6]
"""

import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm import build_body, stream_chat, add_cache_breakpoints, is_claude
from lib.model_info import is_claude

# ═══════════════════════════════════════════════════════════════════════════════
#  Large system prompt (> 4096 Claude tokens)
# ═══════════════════════════════════════════════════════════════════════════════

# Reuse the system prompt from the contention test
from debug.test_cache_contention import SYSTEM_PROMPT, TOOLS


def _extract_usage(usage: dict) -> dict:
    return {
        'prompt_tokens': usage.get('prompt_tokens', 0),
        'cache_read': (
            usage.get('cache_read_tokens')
            or usage.get('cache_read_input_tokens') or 0),
        'cache_write': (
            usage.get('cache_creation_input_tokens')
            or usage.get('cache_write_tokens') or 0),
    }


def _make_call(model: str, messages: list, label: str,
               custom_bp_func=None) -> tuple:
    """Make one API call, return (assistant_msg, usage_dict).

    If custom_bp_func is provided, it overrides the default add_cache_breakpoints
    to place BP4 at a fixed position.
    """
    body = build_body(
        model, messages,
        max_tokens=256,
        temperature=1.0,
        thinking_enabled=True,
        preset='medium',
        thinking_depth='medium',
        tools=TOOLS,
        stream=True,
    )

    # If custom BP function provided, strip default breakpoints and re-apply
    if custom_bp_func:
        # Strip all cache_control first
        for msg in body.get('messages', []):
            content = msg.get('content')
            if isinstance(content, list):
                for j, blk in enumerate(content):
                    if isinstance(blk, dict) and 'cache_control' in blk:
                        content[j] = {k: v for k, v in blk.items()
                                      if k != 'cache_control'}
        for tool in body.get('tools', []):
            fn = tool.get('function')
            if fn and 'cache_control' in fn:
                tool['function'] = {k: v for k, v in fn.items()
                                    if k != 'cache_control'}
        custom_bp_func(body)

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
    print(f"  {label}: msgs={len(messages):2d}  pt={u['prompt_tokens']:>6,}  "
          f"cr={u['cache_read']:>6,}  cw={u['cache_write']:>6,}  "
          f"hit={hit_pct:>3d}%  {elapsed:.1f}s")
    return assistant_msg, u


def _build_messages(num_turns: int, arm_seed: str = '') -> list:
    """Build a message list with a specific number of simulated turns.

    Each turn = 1 user message + 1 assistant message with tool call + 1 tool result.
    Returns messages list ready for API call.
    """
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content':
            'Analyze the project structure. List the root directory first, '
            'then read the main server file.'},
    ]

    tc_counter = 0
    for t in range(num_turns):
        # Simulated assistant with tool call
        messages.append({
            'role': 'assistant',
            'content': f'Let me explore the project structure (turn {t+1}).',
            'tool_calls': [{
                'id': f'call_{tc_counter}',
                'type': 'function',
                'function': {
                    'name': 'list_dir',
                    'arguments': json.dumps({'path': '.'}),
                }
            }],
        })
        # Simulated tool result
        messages.append({
            'role': 'tool',
            'tool_call_id': f'call_{tc_counter}',
            'content': (
                f"Directory: . (turn {t+1})\n\nFiles:\n"
                f"  📄 server.py (245L, 8.2KB)\n"
                f"  📄 bootstrap.py (189L, 6.1KB)\n"
                f"  📄 export.py (1120L, 42.3KB)\n"
                f"  📄 CLAUDE.md (380L, 14.8KB)\n"
                "  📁 lib/ (42 items)\n  📁 routes/ (15 items)\n"
                "  📁 static/ (28 items)\n  📁 debug/ (12 items)\n"
            ),
        })
        tc_counter += 1

    return messages


# ═══════════════════════════════════════════════════════════════════════════════
#  Test A: Default BP4 (advances with conversation growth)
# ═══════════════════════════════════════════════════════════════════════════════

def test_default_bp4(model: str):
    """Test with default BP4 placement (breakpoint on last message with content).

    Each round, BP4 moves forward as new messages are appended.
    """
    print(f"\n{'='*70}")
    print(f"  Test A: DEFAULT BP4 (advances each round)")
    print(f"  BP4 moves to the last message each round.")
    print(f"{'='*70}")

    arm = "\n<!-- bp4_test_default_v1 -->"
    results = []

    for num_turns in range(7):  # 0 to 6 turns of history
        messages = _build_messages(num_turns, arm_seed=arm)
        label = f'Default R{num_turns}'
        _, usage = _make_call(model, messages, label)
        results.append(usage)
        time.sleep(1.5)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  Test B: Fixed BP4 (always on the same position)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_fixed_bp4_func(fixed_idx: int):
    """Create a custom BP function that always places BP4 at a fixed message index."""
    def _custom_bp(body):
        messages = body.get('messages', [])
        tools = body.get('tools', [])

        _cc_stable = {'type': 'ephemeral', 'ttl': '1h'}
        _cc_tail = {'type': 'ephemeral'}
        bp = 0

        # BP1-BP2: system message blocks
        for i, msg in enumerate(messages):
            if msg.get('role') != 'system' or bp >= 4:
                continue
            content = msg.get('content', '')
            if isinstance(content, str) and content.strip():
                messages[i] = {
                    **msg,
                    'content': [{'type': 'text', 'text': content,
                                 'cache_control': dict(_cc_stable)}],
                }
                bp += 1

        # BP3: last tool definition
        if tools and bp < 4:
            fn = tools[-1].get('function')
            if fn:
                tools[-1] = {**tools[-1],
                             'function': {**fn, 'cache_control': dict(_cc_stable)}}
                bp += 1

        # BP4: FIXED position (not advancing)
        if bp < 4 and fixed_idx < len(messages) and fixed_idx > 0:
            msg = messages[fixed_idx]
            content = msg.get('content', '')
            if isinstance(content, str) and content:
                messages[fixed_idx] = {
                    **msg,
                    'content': [{'type': 'text', 'text': content,
                                 'cache_control': dict(_cc_tail)}],
                }
                bp += 1
            elif isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    content[-1] = {**last, 'cache_control': dict(_cc_tail)}
                    bp += 1

    return _custom_bp


def test_fixed_bp4(model: str):
    """Test with BP4 fixed at message index 3 (the first tool result).

    Even as new messages are appended, BP4 stays at the same position.
    This should give consistently high cache hits if our hypothesis is correct.
    """
    print(f"\n{'='*70}")
    print(f"  Test B: FIXED BP4 (always at message index 3)")
    print(f"  BP4 stays fixed — new content is appended AFTER the breakpoint.")
    print(f"{'='*70}")

    arm = "\n<!-- bp4_test_fixed_v1 -->"
    results = []

    # Start from 1 turn (so msg[3] = first tool result exists)
    for num_turns in range(1, 7):  # 1 to 6 turns of history
        messages = _build_messages(num_turns, arm_seed=arm)
        label = f'Fixed  R{num_turns}'
        custom_bp = _make_fixed_bp4_func(fixed_idx=3)
        _, usage = _make_call(model, messages, label, custom_bp_func=custom_bp)
        results.append(usage)
        time.sleep(1.5)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  Test C: Advancing BP4 — does the OLD breakpoint position still cache-read?
# ═══════════════════════════════════════════════════════════════════════════════

def test_bp4_old_position_readback(model: str):
    """Test whether removing a breakpoint at position N causes cache loss.

    Round 1: Place BP4 at message[3] → write cache at position 3
    Round 2: Move BP4 to message[5] → does cache at position 3 still read?
    Round 3: Move BP4 to message[7] → does cache at position 5 still read?

    If Anthropic reads cache at ANY prefix point (not just at breakpoint positions),
    then moving BP4 forward should give incremental cache hits.
    If cache reads REQUIRE a breakpoint at the exact position, moving BP4
    means the old entry is orphaned and only system+tools cache remains.
    """
    print(f"\n{'='*70}")
    print(f"  Test C: BP4 ADVANCEMENT with explicit position tracking")
    print(f"  Round 1: BP4 at msg[3], Round 2: BP4 at msg[5], etc.")
    print(f"  Question: Does old cached content still get read?")
    print(f"{'='*70}")

    arm = "\n<!-- bp4_test_advance_v1 -->"
    results = []

    for num_turns in range(1, 7):  # 1 to 6 turns
        messages = _build_messages(num_turns, arm_seed=arm)
        # BP4 always on the LAST tool result (which advances)
        bp4_idx = len(messages) - 1
        label = f'Advanc R{num_turns} (BP4@msg[{bp4_idx}])'
        custom_bp = _make_fixed_bp4_func(fixed_idx=bp4_idx)
        _, usage = _make_call(model, messages, label, custom_bp_func=custom_bp)
        results.append(usage)
        time.sleep(1.5)

    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  Analysis
# ═══════════════════════════════════════════════════════════════════════════════

def analyze(default_results: list, fixed_results: list, advance_results: list):
    print(f"\n{'='*70}")
    print(f"  ANALYSIS: BP4 Advancement Impact")
    print(f"{'='*70}")

    print(f"\n  Default BP4 (auto-advances):")
    print(f"  {'Turn':<8} {'CR':>8} {'CW':>8} {'Hit%':>6}")
    for i, r in enumerate(default_results):
        total = r['cache_read'] + r['cache_write'] + r['prompt_tokens']
        hit = round(r['cache_read'] / max(total, 1) * 100)
        print(f"  T{i:<7} {r['cache_read']:>8,} {r['cache_write']:>8,} {hit:>5}%")

    # The system+tools baseline is the minimum cache_read when everything else misses
    baseline = min(r['cache_read'] for r in default_results if r['cache_read'] > 0) if any(r['cache_read'] > 0 for r in default_results) else 0
    print(f"\n  System+tools cache baseline: {baseline:,} tokens")

    print(f"\n  Fixed BP4 (stays at msg[3]):")
    print(f"  {'Turn':<8} {'CR':>8} {'CW':>8} {'Hit%':>6}")
    for i, r in enumerate(fixed_results):
        total = r['cache_read'] + r['cache_write'] + r['prompt_tokens']
        hit = round(r['cache_read'] / max(total, 1) * 100)
        print(f"  T{i+1:<7} {r['cache_read']:>8,} {r['cache_write']:>8,} {hit:>5}%")

    print(f"\n  Advancing BP4 (BP4 moves forward each round):")
    print(f"  {'Turn':<8} {'CR':>8} {'CW':>8} {'Hit%':>6}")
    for i, r in enumerate(advance_results):
        total = r['cache_read'] + r['cache_write'] + r['prompt_tokens']
        hit = round(r['cache_read'] / max(total, 1) * 100)
        print(f"  T{i+1:<7} {r['cache_read']:>8,} {r['cache_write']:>8,} {hit:>5}%")

    # Compare: if Fixed BP4 has consistently higher CR than Advancing BP4,
    # it proves that BP4 advancement causes cache loss
    print(f"\n  ── Fixed vs Advancing cache_read comparison ──")
    print(f"  {'Turn':<6} {'Fixed CR':>10} {'Adv CR':>10} {'Diff':>10} {'Verdict':>20}")
    for i in range(min(len(fixed_results), len(advance_results))):
        f_cr = fixed_results[i]['cache_read']
        a_cr = advance_results[i]['cache_read']
        diff = a_cr - f_cr
        if i == 0:
            verdict = "(cold start)"
        elif a_cr >= f_cr * 0.9:
            verdict = "✅ same"
        elif a_cr > baseline * 1.5:
            verdict = "🔶 partial loss"
        else:
            verdict = "❌ dropped to baseline"
        print(f"  T{i+1:<5} {f_cr:>10,} {a_cr:>10,} {diff:>+10,} {verdict:>20}")

    # Final verdict
    # Check: in advancing test, does cache_read stay above baseline after R2?
    adv_hits_above_baseline = sum(
        1 for r in advance_results[1:]  # skip R1 (cold)
        if r['cache_read'] > baseline * 1.5
    )
    total_after_cold = len(advance_results) - 1

    print(f"\n  ── VERDICT ──")
    if adv_hits_above_baseline == total_after_cold:
        print(f"  ✅ Advancing BP4 retains full cache across rounds.")
        print(f"     Anthropic reads cached prefix regardless of breakpoint position.")
        print(f"     BP4 advancement is NOT the cause of production cache misses.")
    elif adv_hits_above_baseline == 0:
        print(f"  ❌ Advancing BP4 drops to baseline ({baseline:,}) every round!")
        print(f"     Cache reads REQUIRE a breakpoint at the exact cached position.")
        print(f"     Moving BP4 forward orphans old cache entries.")
        print(f"     This IS the root cause of production cache misses.")
        print(f"")
        print(f"  💡 FIX: Keep BP4 at a stable position (or add a 2nd tail breakpoint)")
    else:
        print(f"  🔶 Mixed results: {adv_hits_above_baseline}/{total_after_cold} "
              f"rounds above baseline.")
        print(f"     Needs further investigation.")


def main():
    parser = argparse.ArgumentParser(
        description='Test BP4 breakpoint advancement cache impact')
    parser.add_argument('--model', default='aws.claude-opus-4.6')
    args = parser.parse_args()

    model = args.model
    if not is_claude(model):
        print(f"⚠️  Model '{model}' is not Claude.")
        sys.exit(1)

    print(f"╔{'═'*68}╗")
    print(f"║  BP4 Advancement Cache Impact Test                                ║")
    print(f"║  Model: {model:<58}║")
    print(f"║                                                                    ║")
    print(f"║  Tests whether moving the tail breakpoint forward each round       ║")
    print(f"║  causes the older cached prefix to become unreadable.              ║")
    print(f"╚{'═'*68}╝")

    # Test A: Default behavior (BP4 auto-advances)
    default_results = test_default_bp4(model)

    time.sleep(5)

    # Test B: Fixed BP4 (control — should always hit)
    fixed_results = test_fixed_bp4(model)

    time.sleep(5)

    # Test C: Explicit advancing BP4
    advance_results = test_bp4_old_position_readback(model)

    # Analyze
    analyze(default_results, fixed_results, advance_results)


if __name__ == '__main__':
    main()
