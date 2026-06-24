#!/usr/bin/env python3
"""Live cache experiment — settle the 'body never reads back' question on the
ACTUAL production model with REAL signed-thinking replay.

Why this harness (vs debug/test_cache_validation.py)
====================================================
The old harness fabricates tool results and never replays a real signed
Claude thinking block, so it cannot reproduce the production wire shape
(every stored assistant turn carries reasoning_content + thinking_signature).
This one drives a real multi-round tool loop against the live gateway, feeds
each round's REAL assistant message (incl. thinking_signature + tool_calls)
back into history exactly like lib/tasks_pkg/orchestrator.py does, and reads
the API-reported cache_read/cache_write every round.

Three questions, three arms
===========================
Q1  Does the conversation BODY read back, or is cache_read pinned at the
    system+tools floor while cache_write climbs?  → watch the cache_read curve.
Q2  Is the pin caused by BREAKPOINT STRUCTURE (only a moving tail BP covers
    the body) or by SERVER-SIDE behavior?
      arm CURRENT  = production add_cache_breakpoints (system≤2 + tool + tail)
      arm BODY_BP  = sacrifice one system BP for a STABLE intermediate body
                     breakpoint anchored at a fixed early message index, so the
                     body has a non-moving cache_control marker.
    If BODY_BP makes cache_read grow while CURRENT stays pinned → structure.
    If BOTH stay pinned → server-side (gateway/Bedrock) is not doing
    longest-prefix readback past ~20 blocks regardless of our markers.
Q3  Stochastic sub-5min miss: arm REPLAY sends the SAME large prompt 8× at
    ~3s gaps; count how many of calls 2..8 miss despite byte-identical input.

Usage:
    python debug/cache_live_experiment.py --model aws.claude-opus-4.8 \
        --arms CURRENT,BODY_BP --rounds 16
    python debug/cache_live_experiment.py --arms REPLAY --replay-n 8
"""

import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.llm as _llm  # noqa: E402
from lib.llm import build_body, stream_chat  # noqa: E402
from lib.model_info import is_claude  # noqa: E402

DEFAULT_MODEL = 'aws.claude-opus-4.8'

# Large-ish system prompt so the prefix clears the 4096-token Opus floor and
# the system+tools "floor" is clearly distinguishable from the body.
SYSTEM_PROMPT = ('You are Tofu, an AI coding assistant. Follow project '
                 'conventions strictly and use tools to explore code.\n'
                 + ('Project rule: never guess file contents; read first. ' * 600))

TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'read_files',
        'description': 'Read one or more files from the project.',
        'parameters': {
            'type': 'object',
            'properties': {'path': {'type': 'string',
                                    'description': 'Relative file path'}},
            'required': ['path'],
        },
    },
}]

# A fat fake tool result so each round adds real body bytes (~1.5k tokens).
_FAKE_FILE = '\n'.join(f'line {i}: some representative source code here;'
                       for i in range(120))


# ── BODY_BP arm: stable intermediate body breakpoint ────────────────────────
# Anchor a cache_control marker at a FIXED early message index that never
# moves between rounds (so the body prefix up to it is byte-stable), plus the
# usual system + tail markers. This directly tests whether a non-moving body
# breakpoint lets the gateway read the body back.
def _add_breakpoints_BODY_BP(body, log_prefix=''):
    model = body.get('model', '')
    if not is_claude(model):
        return
    messages = body.get('messages', [])
    # strip existing
    for msg in messages:
        c = msg.get('content')
        if isinstance(c, list):
            for j, blk in enumerate(c):
                if isinstance(blk, dict) and 'cache_control' in blk:
                    c[j] = {k: v for k, v in blk.items() if k != 'cache_control'}
    tools = body.get('tools') or []
    for t in tools:
        fn = t.get('function')
        if fn and 'cache_control' in fn:
            t['function'] = {k: v for k, v in fn.items() if k != 'cache_control'}

    def _mark(msg, ttl=None):
        cc = {'type': 'ephemeral'}
        if ttl:
            cc['ttl'] = ttl
        c = msg.get('content', '')
        if isinstance(c, str) and c:
            msg['content'] = [{'type': 'text', 'text': c, 'cache_control': cc}]
            return True
        if isinstance(c, list) and c and isinstance(c[-1], dict):
            c[-1] = {**c[-1], 'cache_control': cc}
            return True
        return False

    bp = 0
    # BP1: system (1h)
    for msg in messages:
        if msg.get('role') == 'system' and _mark(msg, '1h'):
            bp += 1
            break
    # BP2: last tool definition (1h)
    if tools and tools[-1].get('function'):
        tools[-1]['function']['cache_control'] = {'type': 'ephemeral', 'ttl': '1h'}
        bp += 1
    # BP3: STABLE intermediate body anchor — a fixed early message index.
    #   Pick a CONTENT-BEARING message (tool results always have string
    #   content; empty-content assistant tool_call turns are skipped or the
    #   marker silently fails) at ~1/3 into the body. The index is stable
    #   because history only ever grows at the end, so this marker does NOT
    #   move between rounds — that is the whole point of the arm.
    _anchor = None
    body_msgs = [i for i, m in enumerate(messages)
                 if i > 0 and (
                     (m.get('role') == 'tool')
                     or (m.get('role') in ('assistant', 'user')
                         and m.get('content')))]
    if len(body_msgs) >= 6:
        _anchor = body_msgs[len(body_msgs) // 3]
        if _mark(messages[_anchor]):
            bp += 1
        else:
            _anchor = None
    # BP4: tail (5m)
    if len(messages) >= 2:
        for off in range(1, min(6, len(messages))):
            idx = len(messages) - off
            if idx <= 0 or messages[idx].get('role') == 'system':
                break
            if _mark(messages[idx]):
                bp += 1
                break
    if log_prefix:
        print(f'      {log_prefix} BODY_BP placed {bp} markers '
              f'(body anchor at idx {_anchor})')


def _add_breakpoints_LEGACY(body, log_prefix=''):
    """Pre-2026-06-23 behavior: system(≤2) + tool + MOVING tail, NO stable
    body anchor. Baseline arm to measure the body-breakpoint improvement."""
    model = body.get('model', '')
    if not is_claude(model):
        return
    messages = body.get('messages', [])
    for msg in messages:
        c = msg.get('content')
        if isinstance(c, list):
            for j, blk in enumerate(c):
                if isinstance(blk, dict) and 'cache_control' in blk:
                    c[j] = {k: v for k, v in blk.items() if k != 'cache_control'}
    tools = body.get('tools') or []
    for t in tools:
        fn = t.get('function')
        if fn and 'cache_control' in fn:
            t['function'] = {k: v for k, v in fn.items() if k != 'cache_control'}

    def _mark(msg, ttl=None):
        cc = {'type': 'ephemeral'}
        if ttl:
            cc['ttl'] = ttl
        c = msg.get('content', '')
        if isinstance(c, str) and c:
            msg['content'] = [{'type': 'text', 'text': c, 'cache_control': cc}]
            return True
        if isinstance(c, list) and c and isinstance(c[-1], dict):
            c[-1] = {**c[-1], 'cache_control': cc}
            return True
        return False

    for msg in messages:
        if msg.get('role') == 'system':
            _mark(msg, '1h')
            break
    if tools and tools[-1].get('function'):
        tools[-1]['function']['cache_control'] = {'type': 'ephemeral', 'ttl': '1h'}
    if len(messages) >= 2:
        for off in range(1, min(6, len(messages))):
            idx = len(messages) - off
            if idx <= 0 or messages[idx].get('role') == 'system':
                break
            if _mark(messages[idx]):
                break


def _set_arm(arm):
    if not hasattr(_llm, '_orig_acb'):
        _llm._orig_acb = _llm.add_cache_breakpoints
    import lib as _lib
    _lib.CACHE_EXTENDED_TTL = True
    if arm == 'CURRENT':
        # Production as shipped (now includes the stable body breakpoint).
        _llm.add_cache_breakpoints = _llm._orig_acb
    elif arm == 'BODY_BP':
        _llm.add_cache_breakpoints = _add_breakpoints_BODY_BP
    elif arm == 'LEGACY':
        _llm.add_cache_breakpoints = _add_breakpoints_LEGACY


def _restore_arm():
    if hasattr(_llm, '_orig_acb'):
        _llm.add_cache_breakpoints = _llm._orig_acb


def _usage_tokens(u):
    u = u or {}
    cr = u.get('cache_read_tokens') or u.get('cache_read_input_tokens') or 0
    cw = (u.get('cache_creation_input_tokens') or u.get('cache_write_tokens') or 0)
    pt = u.get('prompt_tokens', 0)
    ot = u.get('completion_tokens', 0)
    return pt, cr, cw, ot


def run_tool_loop(model, arm, rounds):
    print(f'\n{"="*70}\n  ARM {arm} — {rounds} rounds — model={model}\n{"="*70}')
    _set_arm(arm)
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content':
            'Explore the project: read several files one at a time, calling '
            'read_files each turn. Keep going until I say stop.'},
    ]
    rows = []
    tcc = 0
    try:
        for r in range(rounds):
            body = build_body(model, messages, max_tokens=1024,
                              thinking_enabled=True, thinking_depth='medium',
                              tools=TOOLS, stream=True)
            body['_task_id'] = ''  # non-latched
            t0 = time.time()
            try:
                amsg, finish, usage = stream_chat(
                    body, log_prefix=f'[{arm} R{r+1}]')
            except Exception as e:
                print(f'  R{r+1} API error: {e}')
                break
            dt = time.time() - t0
            pt, cr, cw, ot = _usage_tokens(usage)
            grew = '↑GROW' if rows and cr > rows[-1][2] + 500 else (
                   '=flat' if rows and abs(cr - rows[-1][2]) <= 500 else '')
            print(f'  R{r+1:2d}  dt={dt:4.1f}s  prompt={pt:6d}  '
                  f'cache_read={cr:7d}  cache_write={cw:7d}  out={ot:4d}  {grew}')
            rows.append((r + 1, pt, cr, cw, ot))

            # Feed REAL assistant message back (orchestrator-faithful)
            tcs = amsg.get('tool_calls')
            if not tcs:
                print('  (model stopped calling tools — injecting forced call)')
                tcs = [{'id': f'call_{tcc}', 'type': 'function',
                        'function': {'name': 'read_files',
                                     'arguments': json.dumps({'path': f'f{tcc}.py'})}}]
            clean = {'role': 'assistant', 'tool_calls': tcs}
            if amsg.get('content'):
                clean['content'] = amsg['content']
            if amsg.get('reasoning_content'):
                clean['reasoning_content'] = amsg['reasoning_content']
            if amsg.get('thinking_signature'):
                clean['thinking_signature'] = amsg['thinking_signature']
            messages.append(clean)
            for tc in tcs:
                tcc += 1
                messages.append({'role': 'tool',
                                 'tool_call_id': tc.get('id', f'call_{tcc}'),
                                 'content': f'{_FAKE_FILE}\n# call {tcc}'})
            time.sleep(2)
    finally:
        _restore_arm()
    return rows


def run_replay(model, n):
    """Q3: same large prompt N times at ~3s gaps; count sub-5min misses."""
    print(f'\n{"="*70}\n  ARM REPLAY — {n} identical calls — model={model}\n{"="*70}')
    _set_arm('CURRENT')
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': 'Summarize the project rules in one line.'},
    ]
    misses = 0
    try:
        for i in range(n):
            body = build_body(model, messages, max_tokens=64,
                              thinking_enabled=False, tools=TOOLS, stream=True)
            body['_task_id'] = ''
            try:
                _, _, usage = stream_chat(body, log_prefix=f'[REPLAY {i+1}]')
            except Exception as e:
                print(f'  call {i+1} error: {e}')
                break
            pt, cr, cw, ot = _usage_tokens(usage)
            miss = (i > 0 and cr < 1000)
            misses += 1 if miss else 0
            print(f'  call {i+1:2d}  prompt={pt:6d}  cache_read={cr:7d}  '
                  f'cache_write={cw:7d}  {"MISS" if miss else "hit" if i else "write"}')
            time.sleep(3)
    finally:
        _restore_arm()
    if n > 1:
        print(f'\n  sub-5min identical-prompt MISS rate: {misses}/{n-1}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--arms', default='CURRENT,BODY_BP')
    ap.add_argument('--rounds', type=int, default=16)
    ap.add_argument('--replay-n', type=int, default=8)
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(',') if a.strip()]
    summary = {}
    for arm in arms:
        if arm == 'REPLAY':
            run_replay(args.model, args.replay_n)
            continue
        rows = run_tool_loop(args.model, arm, args.rounds)
        summary[arm] = rows

    # Verdict table
    for arm, rows in summary.items():
        if len(rows) < 3:
            continue
        reads = [r[2] for r in rows]
        writes = [r[3] for r in rows]
        # body reads back if final read >> floor (first non-zero read)
        floor = next((x for x in reads if x > 0), 0)
        peak = max(reads)
        verdict = ('BODY READS BACK ✓ (read grew %d→%d)' % (floor, peak)
                   if peak > floor * 1.5
                   else 'PINNED AT FLOOR ✗ (read ~%d, write peak %d)'
                        % (floor, max(writes)))
        total_w = sum(writes)
        total_r = sum(reads)
        n_zero = sum(1 for x in reads[1:] if x < 1000)
        print(f'\n[VERDICT {arm}] {verdict}')
        print(f'           total cache_write={total_w:,}  total cache_read='
              f'{total_r:,}  drops-to-floor(read<1k after R1)={n_zero}')


if __name__ == '__main__':
    main()
