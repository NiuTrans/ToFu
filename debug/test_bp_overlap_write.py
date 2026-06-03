#!/usr/bin/env python3
"""Test: Do overlapping cache breakpoints cause redundant cache writes?

The question:
  bp=0 caches prefix [0..A]     (e.g. 6,000 tokens)
  bp=1 caches prefix [0..A+B]   (e.g. 7,000 tokens)
  
  The first 6,000 tokens are identical in both cache entries.
  On R1 (cold cache), does the API report:
    (a) cache_write = 7,000  (just the longest prefix, no redundancy), or
    (b) cache_write = 13,000 (6,000 + 7,000, redundant write)?
  
  And on R2 (warm cache), does the API report:
    (a) cache_read = 7,000  (longest matching prefix), or
    (b) cache_read = 6,000 + 7,000 = 13,000?

Method:
  ARM A — "1 BP":  Place ONE breakpoint at the end of the full system content.
                    Prefix [0..A+B] gets one cache entry.
  
  ARM B — "2 BP":  Place TWO breakpoints: one mid-system, one end-of-system.
                    Prefix [0..A] gets one entry, prefix [0..A+B] gets another.
  
  Both arms have IDENTICAL prompt content — same system message, same user query.
  The ONLY difference is the number of cache_control annotations.
  
  If there's no redundancy:
    R1: Both arms write ~7,000 tokens (same total content)
    R2: Both arms read  ~7,000 tokens 
  
  If there IS redundancy:
    R1: Arm B writes ~13,000 (6K + 7K) vs Arm A writes ~7,000
    R2: Arm B reads  ~13,000 vs Arm A reads ~7,000

  This directly answers whether overlapping prefixes double-count.

Requirements:
  - System content must exceed 4,096 tokens for Opus cache activation.
  - We use the REAL project CLAUDE.md (~6,700 tokens) to guarantee this.
  - Arms use unique seeds to prevent cross-arm cache sharing.

Usage:
    python debug/test_bp_overlap_write.py
    python debug/test_bp_overlap_write.py --rounds 4
    python debug/test_bp_overlap_write.py --dry-run
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

# Load REAL CLAUDE.md from project root
_claude_md_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'CLAUDE.md')
with open(_claude_md_path, 'r') as f:
    REAL_CLAUDE_MD = f.read()

# Static guidance (matches production system_context.py)
STATIC_GUIDANCE = """Tools for code exploration:
- list_dir(path) — List directory contents
- read_files(reads) — Read one or more files/ranges in a single call
- grep_search(pattern) — Search patterns across files
- find_files(pattern) — Find files by name glob

Strategy:
1. Start with list_dir('.') to understand project structure
2. Use grep_search to locate relevant code
3. Use read_files to examine files
4. Provide answers with specific file paths and line numbers

Current date: 2026-04-08"""

# Minimal tools (for bp on tools — same for both arms)
TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_dir', 'description': 'List directory contents',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
    }},
    {'type': 'function', 'function': {
        'name': 'read_files', 'description': 'Read file contents',
        'parameters': {'type': 'object', 'properties': {
            'reads': {'type': 'array', 'items': {'type': 'object', 'properties': {
                'path': {'type': 'string'}, 'start_line': {'type': 'integer'}, 'end_line': {'type': 'integer'}
            }, 'required': ['path']}}
        }, 'required': ['reads']},
    }},
    {'type': 'function', 'function': {
        'name': 'grep_search', 'description': 'Search for patterns in files',
        'parameters': {'type': 'object', 'properties': {
            'pattern': {'type': 'string'}, 'path': {'type': 'string'}, 'include': {'type': 'string'}
        }, 'required': ['pattern']},
    }},
]

USER_QUERY = "List the files in the project root and then read server.py to understand the entry point."


# ═══════════════════════════════════════════════════════════════════════════════
#  Arm builders
# ═══════════════════════════════════════════════════════════════════════════════

def build_arm_1bp(model, arm_seed, round_messages):
    """Arm A: ONE breakpoint on system message.

    System message = single text block containing [CLAUDE.md + static guidance].
    add_cache_breakpoints will place bp=0 on this single block.
    bp=1 is skipped (no second text block in system).
    bp=2 on last tool, bp=3 on tail message.
    
    Total: 3 breakpoints (bp=0 on system, bp=2 on tools, bp=3 on tail).
    """
    # arm_seed at FRONT of content to fully isolate cache prefix from other arms
    system_content = arm_seed + '\n\n' + REAL_CLAUDE_MD + '\n\n' + STATIC_GUIDANCE
    messages = [
        {'role': 'system', 'content': system_content},
        *round_messages,
    ]
    return messages


def build_arm_2bp(model, arm_seed, round_messages):
    """Arm B: TWO breakpoints on system message.

    System message = two text blocks:
      block[0] = CLAUDE.md  (~6,700 tokens)
      block[1] = static guidance  (~200 tokens)
    
    add_cache_breakpoints will place:
      bp=0 on block[0] (prefix = CLAUDE.md)
      bp=1 on block[1] (prefix = CLAUDE.md + static guidance)
      bp=2 on last tool
      bp=3 on tail message
    
    Total: 4 breakpoints.
    
    The question: does bp=0 (prefix 6,700 tokens) cause a redundant write
    that overlaps with bp=1 (prefix 6,900 tokens)?
    """
    # arm_seed at FRONT of first block to fully isolate cache prefix
    messages = [
        {'role': 'system', 'content': [
            {'type': 'text', 'text': arm_seed + '\n\n' + REAL_CLAUDE_MD},
            {'type': 'text', 'text': STATIC_GUIDANCE},
        ]},
        *round_messages,
    ]
    return messages


# ═══════════════════════════════════════════════════════════════════════════════
#  API helper
# ═══════════════════════════════════════════════════════════════════════════════

def _run_round(model, messages, tools, round_num, label, dry_run=False):
    """Send one API round and collect cache metrics."""
    body = build_body(
        model, messages,
        tools=tools,
        max_tokens=300,
        temperature=1.0,
        thinking_enabled=True,
        preset='medium',
        stream=True,
        thinking_format='',
        provider_id='',
    )

    if dry_run:
        # Show the breakpoint placement without calling API
        from lib.llm import add_cache_breakpoints
        body_copy = copy.deepcopy(body)
        add_cache_breakpoints(body_copy, f'[{label}:R{round_num}]')

        # Count breakpoints placed
        bp_count = 0
        bp_locations = []
        for i, msg in enumerate(body_copy.get('messages', [])):
            content = msg.get('content', '')
            if isinstance(content, list):
                for j, blk in enumerate(content):
                    if isinstance(blk, dict) and 'cache_control' in blk:
                        bp_count += 1
                        role = msg.get('role', '?')
                        text_preview = blk.get('text', '')[:60]
                        bp_locations.append(f"  msg[{i}].content[{j}] role={role} text={text_preview!r}...")
        for t_idx, tool in enumerate(body_copy.get('tools', [])):
            fn = tool.get('function', {})
            if 'cache_control' in fn:
                bp_count += 1
                bp_locations.append(f"  tools[{t_idx}] name={fn.get('name', '?')}")

        print(f'  [{label}] R{round_num}: {bp_count} breakpoints')
        for loc in bp_locations:
            print(loc)
        return {
            'round': round_num, 'label': label,
            'cache_read': 0, 'cache_write': 0, 'prompt_tokens': 0,
            'bp_count': bp_count,
        }

    # Real API call — stream_chat returns (assistant_msg, finish_reason, usage)
    t0 = time.time()
    assistant_msg, finish_reason, usage = stream_chat(
        body, log_prefix=f'[{label}:R{round_num}]')
    elapsed = time.time() - t0

    usage = usage or {}
    cr = (usage.get('cache_read_tokens')
          or usage.get('cache_read_input_tokens') or 0)
    cw = (usage.get('cache_write_tokens')
          or usage.get('cache_creation_input_tokens') or 0)
    pt = usage.get('prompt_tokens', 0)

    # Detect Anthropic convention (pt = uncached only)
    total_input = pt + cr + cw if (pt <= cr + cw) else pt

    content_text = assistant_msg.get('content', '') or ''
    tool_calls_raw = assistant_msg.get('tool_calls', []) or []

    result = {
        'round': round_num, 'label': label,
        'cache_read': cr, 'cache_write': cw, 'prompt_tokens': pt,
        'total_input': total_input,
        'elapsed': elapsed,
        'content_preview': content_text[:100] if isinstance(content_text, str) else '',
        'tool_calls': [tc.get('function', {}).get('name', '') for tc in tool_calls_raw],
        'assistant_msg': assistant_msg,
    }

    status = 'MISS' if cr == 0 and cw == 0 else ('HIT' if cr > cw else 'WRITE')
    print(f'  [{label}] R{round_num}: cr={cr:,} cw={cw:,} pt={pt:,} total_in={total_input:,} '
          f'{status} ({elapsed:.1f}s)')

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Main test
# ═══════════════════════════════════════════════════════════════════════════════

def run_test(model, num_rounds, dry_run=False):
    """Run the overlap test: 1 BP vs 2 BP on system message."""

    print('=' * 72)
    print('CACHE BREAKPOINT OVERLAP TEST')
    print(f'Model: {model}')
    print(f'Rounds: {num_rounds}')
    print(f'Dry run: {dry_run}')
    print()
    print('ARM A (1_BP): Single text block in system → 1 breakpoint on system')
    print('ARM B (2_BP): Two text blocks in system  → 2 breakpoints on system')
    print('Both arms have IDENTICAL total content. Only BP count differs.')
    print('=' * 72)

    arms = [
        ('1_BP', build_arm_1bp),
        ('2_BP', build_arm_2bp),
    ]

    all_results = {}

    for label, builder in arms:
        print(f'\n{"─" * 50}')
        print(f'ARM: {label}')
        print(f'{"─" * 50}')

        arm_seed = f'\n\n<!-- arm={label} seed={time.time():.0f} -->'
        time.sleep(1)  # Ensure different seeds

        results = []
        round_messages = [{'role': 'user', 'content': USER_QUERY}]

        for r in range(1, num_rounds + 1):
            messages = builder(model, arm_seed, round_messages)
            result = _run_round(model, messages, TOOLS, r, label, dry_run=dry_run)
            results.append(result)

            if dry_run:
                # Simulate conversation growth
                round_messages.append({
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{'id': f'call_{r}', 'type': 'function',
                                   'function': {'name': 'list_dir', 'arguments': '{"path":"."}'}}]
                })
                round_messages.append({
                    'role': 'tool',
                    'tool_call_id': f'call_{r}',
                    'content': f'File listing for round {r}: server.py, lib/, routes/, static/'
                })
            else:
                # Use actual model response to grow conversation
                asst_msg = result['assistant_msg']
                round_messages.append(asst_msg)

                if result['tool_calls']:
                    # Add simulated tool results for each tool call
                    for tc in asst_msg.get('tool_calls', []):
                        tc_id = tc.get('id', f'call_{r}')
                        round_messages.append({
                            'role': 'tool',
                            'tool_call_id': tc_id,
                            'content': json.dumps({
                                'files': ['server.py', 'bootstrap.py', 'export.py'],
                                'dirs': ['lib/', 'routes/', 'static/', 'debug/'],
                                'note': f'Simulated tool result for round {r}'
                            })
                        })
                else:
                    round_messages.append({'role': 'user', 'content': f'Continue with step {r+1}.'})

        all_results[label] = results

    # ── Analysis ──
    print('\n' + '=' * 72)
    print('ANALYSIS: Do overlapping BPs cause redundant writes?')
    print('=' * 72)

    if dry_run:
        print('\n(Dry run — no cache data to analyze. BP placement shown above.)')
        return

    a_results = all_results['1_BP']
    b_results = all_results['2_BP']

    print(f'\n{"Round":<6} {"1_BP cw":>10} {"2_BP cw":>10} {"Δcw":>10} '
          f'{"1_BP cr":>10} {"2_BP cr":>10} {"Δcr":>10}')
    print('─' * 68)

    for i in range(num_rounds):
        a = a_results[i]
        b = b_results[i]
        d_cw = b['cache_write'] - a['cache_write']
        d_cr = b['cache_read'] - a['cache_read']
        print(f'R{i+1:<5} {a["cache_write"]:>10,} {b["cache_write"]:>10,} {d_cw:>+10,} '
              f'{a["cache_read"]:>10,} {b["cache_read"]:>10,} {d_cr:>+10,}')

    # Totals
    a_total_cw = sum(r['cache_write'] for r in a_results)
    b_total_cw = sum(r['cache_write'] for r in b_results)
    a_total_cr = sum(r['cache_read'] for r in a_results)
    b_total_cr = sum(r['cache_read'] for r in b_results)

    print('─' * 68)
    d_cw = b_total_cw - a_total_cw
    d_cr = b_total_cr - a_total_cr
    print(f'{"TOTAL":<6} {a_total_cw:>10,} {b_total_cw:>10,} {d_cw:>+10,} '
          f'{a_total_cr:>10,} {b_total_cr:>10,} {d_cr:>+10,}')

    # Interpretation
    print('\n── INTERPRETATION ──')
    pct_cw = (b_total_cw - a_total_cw) / max(a_total_cw, 1) * 100
    pct_cr = (b_total_cr - a_total_cr) / max(a_total_cr, 1) * 100

    print(f'\ncache_write difference: {pct_cw:+.1f}%')
    print(f'cache_read  difference: {pct_cr:+.1f}%')

    if abs(pct_cw) < 10:
        print('\n✅ CONCLUSION: Overlapping breakpoints do NOT cause redundant writes.')
        print('   The API charges for the longest prefix only. Adding a second BP on')
        print('   a sub-prefix does not double-count the overlapping tokens.')
    elif pct_cw > 10:
        print(f'\n⚠️  CONCLUSION: 2-BP arm writes {pct_cw:.1f}% MORE tokens than 1-BP arm.')
        print('   This suggests the API DOES charge redundantly for overlapping prefixes.')
        print('   Each BP creates an independent cache write, including the overlap.')
    else:
        print(f'\n🤔 CONCLUSION: 2-BP arm writes {pct_cw:.1f}% FEWER tokens — unexpected.')
        print('   Possible noise or interaction with cache warmup timing.')

    # R1 specific analysis (most important — cold cache)
    if a_results[0]['cache_write'] > 0 or b_results[0]['cache_write'] > 0:
        a_r1_cw = a_results[0]['cache_write']
        b_r1_cw = b_results[0]['cache_write']
        print(f'\n── R1 (Cold Cache) Detail ──')
        print(f'  1_BP R1 cache_write: {a_r1_cw:,} tokens')
        print(f'  2_BP R1 cache_write: {b_r1_cw:,} tokens')
        if b_r1_cw > a_r1_cw * 1.1:
            print(f'  → 2_BP writes {b_r1_cw - a_r1_cw:,} MORE tokens on R1.')
            print(f'    This is the overlap: bp=0 writes [0..A], bp=1 writes [0..A+B].')
            print(f'    The extra {b_r1_cw - a_r1_cw:,} could be:')
            print(f'      (a) Just the delta B tokens (no redundancy), or')
            print(f'      (b) A full re-write of prefix A (redundancy)')
            # Compare to expected content size
            print(f'    If the total system content ≈ {a_r1_cw:,} tokens (1-BP baseline),')
            print(f'    then 2-BP writing {b_r1_cw:,} means the API counts')
            if b_r1_cw > a_r1_cw * 1.5:
                print(f'    ~{b_r1_cw / a_r1_cw:.1f}x the baseline → likely REDUNDANT writes.')
            else:
                print(f'    ~{b_r1_cw / a_r1_cw:.2f}x the baseline → minimal/no redundancy.')
        elif abs(b_r1_cw - a_r1_cw) < a_r1_cw * 0.05:
            print(f'  → Both write approximately the same. No redundancy detected.')
        else:
            print(f'  → 2_BP writes {a_r1_cw - b_r1_cw:,} FEWER tokens — unexpected.')


def main():
    parser = argparse.ArgumentParser(description='Test BP overlap cache writes')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    run_test(args.model, args.rounds, args.dry_run)


if __name__ == '__main__':
    main()
