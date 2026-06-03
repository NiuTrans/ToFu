#!/usr/bin/env python3
"""Real A/B: L1 cache-prefix-skip vs L1 aggressive (compact INSIDE prefix).

The cache-prefix-skip policy in `compaction.py:1547-1550` says: if a cold
tool result's index is below `_cache_prefix_count` (the cached prefix
boundary), skip it. The hypothesis is that mutating cache-prefix bytes
forces a full prefix rewrite (cache_write at 1.25× input rate) which is
12.5× more expensive than reading the un-compacted bytes from cache
(cache_read at 0.10× input rate).

This test:
  1. Clones the real `mp0sggcln5pruo` conversation into TWO test convs.
  2. Truncates each to a slice with substantial bulk + many tool calls.
  3. For each round:
     - ARM A (STATUS_QUO): compaction.py honors `_cache_prefix_count`.
     - ARM B (AGGRESSIVE): we monkey-patch `get_cache_prefix_count` to
       return 0 for arm B's conv_id, forcing L1 to compact every cold
       round regardless of cache-prefix protection.
  4. Sends each round via `dispatch_stream` so:
     - `add_cache_breakpoints()` actually attaches BP1-BP4
     - `detect_cache_break()` updates `cache_tracking._cache_states`
       with real `last_cache_read_tokens` from the API response
  5. Records the API-reported `cache_read_tokens` / `cache_write_tokens`
     and computes USD cost using Opus 4.7 prices.
  6. Reports cumulative cost difference.

Expected outcomes:
  - If status quo wins: AGGRESSIVE pays bigger cache_writes (prefix
    rewrites every time L1 mutates a prefix-cached round), which
    overwhelms the smaller-prompt savings.
  - If aggressive wins: prompt-token reduction outpaces cache-write
    cost, suggesting we should drop the skip.

Usage:
    python debug/test_l1_prefix_skip_vs_aggressive.py --rounds 8

The first run is the cold-cache "warmup" round which both arms pay.
Steady-state behavior emerges from R3 onward.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger
from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
from lib.tasks_pkg.compaction import micro_compact
from lib.llm_dispatch import dispatch_stream
import lib.tasks_pkg.cache_tracking as cache_tracking

logger = get_logger(__name__)

DEFAULT_MODEL = 'aws.claude-opus-4.7'
DEFAULT_ROUNDS = 8
DEFAULT_INTERVAL = 5
DEFAULT_SRC_CONV = 'mp0sggcln5pruo'
DEFAULT_INITIAL_ROUNDS = 25  # bulk to compact

# Opus 4.7 ($/MTok)
INPUT_PRICE_USD_PER_MTOK = 5.0
OUTPUT_PRICE = 25.0
CACHE_READ_MUL = 0.10   # cache_read price = base × 0.10
CACHE_WRITE_MUL = 1.25  # cache_write price = base × 1.25 (5m TTL)


@dataclass
class RoundResult:
    arm: str
    round_num: int
    msg_count: int = 0
    tool_msg_count: int = 0
    big_tool_count: int = 0
    api_input_chars: int = 0
    prompt_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    elapsed: float = 0.0
    status: str = ''
    error: str = ''
    cost_usd: float = 0.0
    skipped_already: int = 0
    compacted_l1: int = 0
    cache_prefix_count: int = 0


@dataclass
class ArmResult:
    label: str
    rounds: list = field(default_factory=list)


# ───────────── conv setup helpers ─────────────

def _clone_conv(src_id: str, dst_id: str, n_rounds: int) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT user_id, title, messages, settings, created_at, updated_at, '
        'msg_count, search_text FROM conversations WHERE id=?',
        (src_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f'Source conv {src_id!r} not found in DB')
    user_id, title, messages, settings, created_at, updated_at, msg_count, search_text = row
    db.execute('DELETE FROM conversations WHERE id=?', (dst_id,))

    msgs = json.loads(messages) if isinstance(messages, str) else messages
    asst = 0
    cut = len(msgs)
    for i, m in enumerate(msgs):
        if m.get('role') == 'assistant':
            asst += 1
            if asst >= n_rounds:
                cut = i + 1
                break
    msgs = msgs[:cut]

    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, settings, '
        'created_at, updated_at, msg_count, search_text) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (dst_id, user_id, f'L1-A/B {dst_id}', json_dumps_pg(msgs), settings,
         created_at, int(time.time() * 1000), len(msgs), search_text),
    )
    db.commit()


def _drop_test_conv(conv_id: str) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    db.execute('DELETE FROM conversations WHERE id=?', (conv_id,))
    db.commit()


def _append_user(conv_id: str, prompt: str) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=?',
                     (conv_id,)).fetchone()
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    msgs.append({'role': 'user', 'content': prompt})
    db.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?',
               (json_dumps_pg(msgs), int(time.time() * 1000), conv_id))
    db.commit()


def _append_assistant(conv_id: str, content: str) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=?',
                     (conv_id,)).fetchone()
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    msgs.append({'role': 'assistant', 'content': content, 'toolRounds': []})
    db.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?',
               (json_dumps_pg(msgs), int(time.time() * 1000), conv_id))
    db.commit()


# ───────────── API call helper ─────────────

def _send(messages: list, model: str, max_tokens: int = 200,
          arm_seed: str = ''):
    """Streaming dispatch — fires add_cache_breakpoints; returns
    (content_text, usage). Injects an arm-specific salt into the system
    message so the two arms have DIFFERENT cache keys and don't share
    server-side cache entries."""
    if arm_seed:
        salted = list(messages)
        # Find or create the system message
        if salted and salted[0].get('role') == 'system':
            sys_content = salted[0].get('content', '')
            if isinstance(sys_content, str):
                salted[0] = {'role': 'system',
                             'content': f'<!-- arm-salt:{arm_seed} -->\n{sys_content}'}
        else:
            salted = [{'role': 'system',
                       'content': f'<!-- arm-salt:{arm_seed} -->'}] + salted
        messages = salted
    msg, _finish, usage = dispatch_stream(
        messages,
        max_tokens=max_tokens,
        prefer_model=model, strict_model=True,
        max_retries=1,
    )
    if isinstance(msg, dict):
        content = msg.get('content', '') or ''
    else:
        content = msg or ''
    return content, usage


def _stats(api_msgs: list) -> tuple[int, int, int]:
    tools = [m for m in api_msgs if m.get('role') == 'tool']
    big = [m for m in tools if isinstance(m.get('content'), str)
           and len(m['content']) > 2000]
    total = sum(len(m.get('content', '') or '')
                for m in api_msgs if isinstance(m.get('content'), str))
    return len(tools), len(big), total


def _cost(prompt_tokens: int, cache_read: int, cache_write: int,
          output_tokens: int) -> float:
    return (
        (prompt_tokens
         + cache_read * CACHE_READ_MUL
         + cache_write * CACHE_WRITE_MUL) * INPUT_PRICE_USD_PER_MTOK / 1e6
        + output_tokens * OUTPUT_PRICE / 1e6
    )


# ───────────── round runner ─────────────

# Monkey-patch container so we can flip the policy on a per-conv basis
_AGGRESSIVE_CONVS: set[str] = set()
_orig_get_cache_prefix_count = cache_tracking.get_cache_prefix_count


def _patched_get_cache_prefix_count(conv_id: str) -> int:
    """For convs in _AGGRESSIVE_CONVS, return 0 so L1 ignores the cache
    prefix and compacts every cold round regardless of cache state."""
    if conv_id in _AGGRESSIVE_CONVS:
        return 0
    return _orig_get_cache_prefix_count(conv_id)


def _setup_arm_patch():
    cache_tracking.get_cache_prefix_count = _patched_get_cache_prefix_count
    # Also patch the in-module reference used by compaction.py via from-import
    import lib.tasks_pkg.compaction as compaction_mod
    # compaction.py does `from lib.tasks_pkg.cache_tracking import
    # get_cache_prefix_count` LAZILY inside micro_compact (line 1484), so
    # the monkey-patch on cache_tracking is what counts.


def _teardown_arm_patch():
    cache_tracking.get_cache_prefix_count = _orig_get_cache_prefix_count


def _run_round(arm: str, conv_id: str, round_idx: int, prompt: str,
               model: str, arm_seed: str = '') -> RoundResult:
    # 1. Read pre-round cache_prefix_count for diagnostic
    pre_pfx = cache_tracking.get_cache_prefix_count(conv_id)

    # 2. Append user followup
    _append_user(conv_id, prompt)

    # 3. Build api messages
    api_msgs = build_api_messages_from_db(conv_id, {'systemPrompt': ''})
    if not api_msgs:
        return RoundResult(arm=arm, round_num=round_idx + 1,
                           error='no api messages')

    # 4. Run L1 (with arm-specific cache_prefix policy)
    fake_task = {
        'toolRounds': [], 'model': model,
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'events': [], 'id': f'ab-{arm}',
    }
    saved = micro_compact(api_msgs, conv_id=conv_id, task=fake_task)

    tools, big, total_chars = _stats(api_msgs)
    rr = RoundResult(arm=arm, round_num=round_idx + 1,
                     msg_count=len(api_msgs), tool_msg_count=tools,
                     big_tool_count=big, api_input_chars=total_chars,
                     cache_prefix_count=pre_pfx)

    print(f'    [R{round_idx+1}] {arm:<11} pfx_count={pre_pfx:>3}  '
          f'msgs={len(api_msgs)} tools={tools} big={big} '
          f'chars={total_chars:>9,}', flush=True)

    # 5. Send via streaming dispatcher
    t0 = time.time()
    try:
        content, usage = _send(api_msgs, model, arm_seed=arm_seed)
    except Exception as e:
        rr.error = f'{type(e).__name__}: {str(e)[:200]}'
        print(f'      ERR: {rr.error}', flush=True)
        return rr

    rr.elapsed = time.time() - t0
    u = usage or {}
    rr.cache_read = (u.get('cache_read_tokens')
                     or u.get('cache_read_input_tokens')
                     or u.get('cached_tokens') or 0)
    rr.cache_write = (u.get('cache_write_tokens')
                      or u.get('cache_creation_input_tokens') or 0)
    pt = u.get('prompt_tokens') or u.get('input_tokens') or 0
    # OpenAI convention: prompt_tokens INCLUDES cache. Make uncached visible.
    rr.prompt_tokens = max(0, pt - rr.cache_read - rr.cache_write)
    rr.output_tokens = (u.get('completion_tokens') or u.get('output_tokens') or 0)

    rr.cost_usd = _cost(rr.prompt_tokens, rr.cache_read, rr.cache_write,
                        rr.output_tokens)

    if rr.cache_write > 500 and rr.cache_read > 500:
        rr.status = 'HIT+W'
    elif rr.cache_read > 500:
        rr.status = 'HIT'
    elif rr.cache_write > 500:
        rr.status = 'WRITE'
    else:
        rr.status = 'MISS'

    print(f'      {rr.elapsed:>5.1f}s | {rr.status:>5}  '
          f'pt={rr.prompt_tokens:>6,} cr={rr.cache_read:>7,} '
          f'cw={rr.cache_write:>6,} out={rr.output_tokens:>4} '
          f'${rr.cost_usd:.4f}', flush=True)

    # 6. Manually run the cache_tracking detector so _cache_states updates
    #    (production calls this from stream_handler / orchestrator)
    cache_tracking.detect_cache_break(conv_id, api_msgs, [], model, usage=u)

    # 7. Append assistant reply (real content, truncated to keep test fast)
    short = (content[:200] or '[empty]')
    _append_assistant(conv_id, short)

    return rr


# ───────────── reporting ─────────────

def _print_table(arm: ArmResult):
    print(f'\n  ── {arm.label} ──')
    print(f"  {'Rnd':>3} │ {'Pfx':>4} │ {'Tools':>5} │ {'Big':>3} │ "
          f"{'Chars':>8} │ {'Prompt':>6} │ {'CacheR':>7} │ {'CacheW':>6} │ "
          f"{'Out':>4} │ {'Stat':>5} │ {'$':>7}")
    sep = '  ' + '─' * 99
    print(sep)
    for r in arm.rounds:
        if r.error:
            print(f'  {r.round_num:>3} │ ERR: {r.error[:80]}')
            continue
        print(f"  {r.round_num:>3} │ {r.cache_prefix_count:>4} │ "
              f"{r.tool_msg_count:>5} │ {r.big_tool_count:>3} │ "
              f"{r.api_input_chars:>8,} │ {r.prompt_tokens:>6,} │ "
              f"{r.cache_read:>7,} │ {r.cache_write:>6,} │ "
              f"{r.output_tokens:>4} │ {r.status:>5} │ ${r.cost_usd:>6.4f}")


def _print_report(arm_a: ArmResult, arm_b: ArmResult):
    print()
    print('  ' + '▓' * 70)
    print('  L1 CACHE-PREFIX SKIP — LIVE A/B (Opus 4.7)')
    print('  ' + '▓' * 70)
    _print_table(arm_a)
    _print_table(arm_b)

    paired = [(a, b) for a, b in zip(arm_a.rounds, arm_b.rounds)
              if not a.error and not b.error]
    if not paired:
        print('\n  no completed pairs')
        return

    def _T(field, side):
        return sum(getattr(a if side == 'a' else b, field) for a, b in paired)

    p_a, p_b = _T('prompt_tokens', 'a'), _T('prompt_tokens', 'b')
    cr_a, cr_b = _T('cache_read', 'a'), _T('cache_read', 'b')
    cw_a, cw_b = _T('cache_write', 'a'), _T('cache_write', 'b')
    out_a, out_b = _T('output_tokens', 'a'), _T('output_tokens', 'b')
    cost_a, cost_b = _T('cost_usd', 'a'), _T('cost_usd', 'b')
    chars_a, chars_b = _T('api_input_chars', 'a'), _T('api_input_chars', 'b')
    skipped_a, skipped_b = _T('skipped_already', 'a'), _T('skipped_already', 'b')

    print()
    print(f"\n  Across {len(paired)} round-pairs (both arms succeeded):")
    print(f"  {'Metric':<26} │ {'STATUS_QUO':>12} │ {'AGGRESSIVE':>12} │ "
          f"{'Δ':>10}")
    print('  ' + '─' * 70)
    for name, a, b, lower_better in [
        ('Total api chars', chars_a, chars_b, True),
        ('Prompt tokens (uncached)', p_a, p_b, True),
        ('Cache reads', cr_a, cr_b, False),
        ('Cache writes', cw_a, cw_b, True),
        ('Output tokens', out_a, out_b, True),
        ('Cost USD', cost_a, cost_b, True),
    ]:
        if a == 0 and b == 0:
            d = '-'
        else:
            pct = (b - a) / max(a, 1e-9) * 100
            d = f'{pct:+.1f}%'
        if isinstance(a, float):
            print(f"  {name:<26} │ {a:>12.4f} │ {b:>12.4f} │ {d:>10}")
        else:
            print(f"  {name:<26} │ {a:>12,} │ {b:>12,} │ {d:>10}")

    diff = cost_a - cost_b
    print('  ' + '─' * 70)
    if abs(diff) < 0.0005:
        v = 'NEUTRAL'
    elif diff > 0:
        v = f'AGGRESSIVE cheaper by ${diff:.4f} ({diff/cost_a*100:.1f}%)'
    else:
        v = f'STATUS_QUO  cheaper by ${-diff:.4f} ({-diff/cost_b*100:.1f}%)'
    print(f"\n  VERDICT: {v}")


def _save(path, arm_a: ArmResult, arm_b: ArmResult):
    with open(path, 'w') as f:
        json.dump({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'arm_a': {'label': arm_a.label, 'rounds': [asdict(r) for r in arm_a.rounds]},
            'arm_b': {'label': arm_b.label, 'rounds': [asdict(r) for r in arm_b.rounds]},
        }, f, indent=2)


# ───────────── main ─────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src-conv', default=DEFAULT_SRC_CONV)
    ap.add_argument('--initial-rounds', type=int, default=DEFAULT_INITIAL_ROUNDS)
    ap.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    test_a = f'l1ab-A-{timestamp}'
    test_b = f'l1ab-B-{timestamp}'

    print('█' * 70)
    print(f'  L1 CACHE-PREFIX A/B  src={args.src_conv}')
    print(f'  initial_rounds={args.initial_rounds}  test_rounds={args.rounds}')
    print(f'  model={args.model}  interval={args.interval}s')
    print('█' * 70)

    print('\n  [Setup] Cloning + truncating into two test convs...')
    _clone_conv(args.src_conv, test_a, args.initial_rounds)
    _clone_conv(args.src_conv, test_b, args.initial_rounds)

    # Mark arm B as aggressive
    _AGGRESSIVE_CONVS.add(test_b)
    _setup_arm_patch()

    arm_a = ArmResult(label='STATUS_QUO (cache-prefix skip honored)')
    arm_b = ArmResult(label='AGGRESSIVE (cache-prefix skip ignored)')
    out_path = f'debug/l1_prefix_skip_{timestamp}.json'

    prompts = [
        'In one sentence: what does this codebase do?',
        'What test framework is used?',
        'Name three Python files in lib/.',
        'What HTTP framework backs the routes?',
        'Where are the templates served from?',
        'List two database backends supported.',
        'What is the default LLM model?',
        'Describe the compaction architecture briefly.',
        'What is logged to logs/error.log?',
        'Done.',
    ]

    seed_a = f'A-{timestamp}'
    seed_b = f'B-{timestamp}'
    try:
        for r in range(args.rounds):
            prompt = prompts[r % len(prompts)]
            ra = _run_round('STATUS_QUO', test_a, r, prompt, args.model,
                            arm_seed=seed_a)
            arm_a.rounds.append(ra)
            _save(out_path, arm_a, arm_b)
            time.sleep(args.interval)

            rb = _run_round('AGGRESSIVE', test_b, r, prompt, args.model,
                            arm_seed=seed_b)
            arm_b.rounds.append(rb)
            _save(out_path, arm_a, arm_b)
            if r < args.rounds - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n[aborted — partial results saved]')
    finally:
        _AGGRESSIVE_CONVS.discard(test_b)
        _teardown_arm_patch()
        cache_tracking.cleanup_cache_state(test_a)
        cache_tracking.cleanup_cache_state(test_b)
        _drop_test_conv(test_a)
        _drop_test_conv(test_b)
        print(f'\n  [Cleanup] Dropped {test_a}, {test_b}')

    _print_report(arm_a, arm_b)
    print(f'\n  Saved: {out_path}')


if __name__ == '__main__':
    main()
