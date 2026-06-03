#!/usr/bin/env python3
"""Live A/B: should L1 compact INSIDE the cache prefix, or wait for cold cache?

Two arms, applied to the same real conversation snapshot replayed turn-by-turn:

  ARM A — STATUS QUO  : L1 skips messages where idx < _cache_prefix_count.
                        Cold rounds inside the cache prefix stay full-fat;
                        L1 only compacts after natural cache invalidation
                        (TTL expiry, BP4 advancement, etc.).

  ARM B — AGGRESSIVE  : L1 ignores _cache_prefix_count entirely. Every cold
                        round outside the hot tail gets compacted, even if
                        that means invalidating a warm cache prefix.

Hypothesis: ARM A is cheaper per round in steady state but more expensive
when cache invalidation eventually fires (the prefix is bigger). ARM B
pays a one-time bigger invalidation up front but every subsequent round
the prefix is smaller. Which wins depends on (a) how often the cache
goes cold, (b) the size of the un-compacted bulk.

Replay strategy:
  - Load the real `mp0sggcln5pruo` messages from DB.
  - Truncate to a slice of N rounds.
  - For each round, simulate the production loop: build api messages,
    run L1 with the arm-specific cache_prefix policy, send via the
    dispatcher, record usage. Append the assistant reply (real, from the
    response) to the local convo for the next round.
  - Each arm uses an arm-specific seed in the user prefix so the two
    arms don't share cache (so we measure each arm's cache hit rate on
    its OWN history, not by piggybacking on the other arm's prefix).

Usage:
  python debug/test_l1_compact_cache_tradeoff.py --rounds 6 --src-conv mp0sggcln5pruo
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
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
DEFAULT_ROUNDS = 6
DEFAULT_INTERVAL = 5
DEFAULT_SRC_CONV = 'mp0sggcln5pruo'

# Opus 4.7 prices ($/MTok)
INPUT_PRICE = 5.0
OUTPUT_PRICE = 25.0
CACHE_READ_MUL = 0.10
CACHE_WRITE_MUL = 1.25


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


@dataclass
class ArmResult:
    label: str
    rounds: list = field(default_factory=list)


def _clone_conv(src_id: str, dst_id: str) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT user_id, title, messages, settings, created_at, updated_at, '
        'msg_count, search_text FROM conversations WHERE id=?',
        (src_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f'Source conv {src_id!r} not found')
    user_id, title, messages, settings, created_at, updated_at, msg_count, search_text = row
    db.execute('DELETE FROM conversations WHERE id=?', (dst_id,))
    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, settings, '
        'created_at, updated_at, msg_count, search_text) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (dst_id, user_id, f'L1-A/B {dst_id}', messages, settings,
         created_at, int(time.time() * 1000), msg_count, search_text),
    )
    db.commit()


def _truncate_to_rounds(conv_id: str, n_rounds: int) -> None:
    """Keep only the first n_rounds user-assistant turns of the conv.

    A "round" is one user->assistant exchange.
    """
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=?',
                     (conv_id,)).fetchone()
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    # Find the n_rounds-th assistant message index, keep through it
    asst_count = 0
    cut = len(msgs)
    for i, m in enumerate(msgs):
        if m.get('role') == 'assistant':
            asst_count += 1
            if asst_count >= n_rounds:
                cut = i + 1
                break
    msgs = msgs[:cut]
    db.execute(
        'UPDATE conversations SET messages=?, updated_at=? WHERE id=?',
        (json_dumps_pg(msgs), int(time.time() * 1000), conv_id),
    )
    db.commit()


def _drop_test_conv(conv_id: str) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    db.execute('DELETE FROM conversations WHERE id=?', (conv_id,))
    db.commit()


def _stats(api_msgs: list) -> tuple[int, int, int]:
    tools = [m for m in api_msgs if m.get('role') == 'tool']
    big = [m for m in tools if isinstance(m.get('content'), str)
           and len(m['content']) > 2000]
    total = sum(len(m.get('content', '') or '')
                for m in api_msgs if isinstance(m.get('content'), str))
    return len(tools), len(big), total


def _send(messages: list, model: str, max_tokens: int = 256):
    """Send via dispatcher (streaming, so add_cache_breakpoints fires);
    drain the stream and return (content_text, usage)."""
    msg, finish, usage = dispatch_stream(
        messages,
        max_tokens=max_tokens,
        prefer_model=model, strict_model=True,
        max_retries=1,
    )
    if isinstance(msg, dict):
        content = msg.get('content', '')
    else:
        content = msg or ''
    return content, usage


def _append_user_followup(conv_id: str, prompt: str) -> None:
    """Append a fresh user message to the conv's saved messages list."""
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages, updated_at FROM conversations WHERE id=?',
                     (conv_id,)).fetchone()
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    msgs.append({'role': 'user', 'content': prompt})
    db.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?',
               (json_dumps_pg(msgs), int(time.time() * 1000), conv_id))
    db.commit()


def _append_assistant_reply(conv_id: str, content: str) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=?',
                     (conv_id,)).fetchone()
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    msgs.append({'role': 'assistant', 'content': content, 'toolRounds': []})
    db.execute('UPDATE conversations SET messages=?, updated_at=? WHERE id=?',
               (json_dumps_pg(msgs), int(time.time() * 1000), conv_id))
    db.commit()


def _run_round(arm: str, conv_id: str, round_idx: int, prompt: str,
               aggressive: bool, model: str, arm_seed: str) -> RoundResult:
    """One round per arm.

    aggressive=True forces _cache_prefix_count → 0 for this conv's L1 pass.
    """
    # 1. Append a fresh user message (with arm seed for cache isolation)
    _append_user_followup(conv_id, f'<!-- arm={arm_seed} -->\n{prompt}')

    # 2. Build api messages from DB (this is exactly what the real
    #    orchestrator does each round)
    api_msgs = build_api_messages_from_db(conv_id, {'systemPrompt': ''})
    if not api_msgs:
        return RoundResult(arm=arm, round_num=round_idx + 1,
                           error='build_api_messages returned None')

    # 3. Optionally clear cache_tracking state to force aggressive policy
    if aggressive:
        # Force _cache_prefix_count = 0 so L1 compacts EVERYTHING cold
        try:
            cache_tracking.cleanup_cache_state(conv_id)
        except Exception:
            pass

    # 4. Run L1 (this will mutate api_msgs AND persist placeholders to DB
    #    via my fix). The cache_prefix policy is automatic from
    #    cache_tracking state.
    fake_task = {'toolRounds': [], 'model': model,
                 'events_lock': __import__('threading').Lock(),
                 'events': [], 'id': 'ab-test'}
    micro_compact(api_msgs, conv_id=conv_id, task=fake_task)

    # 5. Stats
    tools, big, total_chars = _stats(api_msgs)

    rr = RoundResult(arm=arm, round_num=round_idx + 1,
                     msg_count=len(api_msgs), tool_msg_count=tools,
                     big_tool_count=big, api_input_chars=total_chars)

    print(f'    [R{round_idx+1}] {arm:<10} msgs={len(api_msgs)} '
          f'tools={tools} big={big} chars={total_chars:,}', flush=True)

    # 6. API call
    t0 = time.time()
    try:
        content, usage = _send(api_msgs, model)
    except Exception as e:
        rr.error = f'{type(e).__name__}: {str(e)[:300]}'
        print(f'      ❌ FAILED: {rr.error}', flush=True)
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

    # Cost
    rr.cost_usd = (
        (rr.prompt_tokens
         + rr.cache_read * CACHE_READ_MUL
         + rr.cache_write * CACHE_WRITE_MUL) * INPUT_PRICE / 1e6
        + rr.output_tokens * OUTPUT_PRICE / 1e6
    )

    if rr.cache_write > 500 and rr.cache_read > 500:
        rr.status = 'HIT+WRITE'
    elif rr.cache_read > 500:
        rr.status = 'HIT'
    elif rr.cache_write > 500:
        rr.status = 'WRITE'
    else:
        rr.status = 'MISS'

    print(f'      ⏱ {rr.elapsed:.1f}s | {rr.status}  '
          f'pt={rr.prompt_tokens:,} cr={rr.cache_read:,} '
          f'cw={rr.cache_write:,} out={rr.output_tokens} '
          f'${rr.cost_usd:.4f}', flush=True)

    # 7. Append a SHORT placeholder assistant reply so the next round's
    #    prefix grows (we don't care about the full response for this test)
    short_reply = (content or '')[:200] or '[empty]'
    _append_assistant_reply(conv_id, short_reply)

    return rr


def _print_report(arm_a: ArmResult, arm_b: ArmResult):
    print()
    print('  ' + '▓' * 70)
    print('  L1 CACHE-PREFIX SKIP — LIVE A/B RESULTS  (Opus 4.7)')
    print('  ' + '▓' * 70)
    for arm in (arm_a, arm_b):
        print(f'\n  ── {arm.label} ──')
        print(f"  {'Rnd':>3} │ {'Tools':>5} │ {'Big':>3} │ {'Chars':>9} │ "
              f"{'Prompt':>7} │ {'CacheR':>8} │ {'CacheW':>8} │ "
              f"{'Out':>4} │ {'Status':>10} │ {'$':>7}")
        sep = '  ' + '─' * 99
        print(sep)
        for r in arm.rounds:
            if r.error:
                print(f'  {r.round_num:>3} │  ERR: {r.error[:80]}')
                continue
            print(f"  {r.round_num:>3} │ {r.tool_msg_count:>5} │ "
                  f"{r.big_tool_count:>3} │ {r.api_input_chars:>9,} │ "
                  f"{r.prompt_tokens:>7,} │ {r.cache_read:>8,} │ "
                  f"{r.cache_write:>8,} │ {r.output_tokens:>4} │ "
                  f"{r.status:>10} │ ${r.cost_usd:>6.4f}")

    paired = [(a, b) for a, b in zip(arm_a.rounds, arm_b.rounds)
              if not a.error and not b.error]
    if not paired:
        print('\n  ❌ No rounds completed in BOTH arms')
        return

    def _t(field, side):
        return sum(getattr(a if side == 'a' else b, field) for a, b in paired)

    p_a, p_b = _t('prompt_tokens', 'a'), _t('prompt_tokens', 'b')
    cr_a, cr_b = _t('cache_read', 'a'), _t('cache_read', 'b')
    cw_a, cw_b = _t('cache_write', 'a'), _t('cache_write', 'b')
    out_a, out_b = _t('output_tokens', 'a'), _t('output_tokens', 'b')
    cost_a, cost_b = _t('cost_usd', 'a'), _t('cost_usd', 'b')
    chars_a, chars_b = _t('api_input_chars', 'a'), _t('api_input_chars', 'b')

    print()
    print(f"  Across {len(paired)} round-pairs (both arms succeeded):")
    print(f"  {'Metric':<28} │ {'STATUS_QUO':>12} │ {'AGGRESSIVE':>12} │ "
          f"{'Δ':>10}")
    print('  ' + '─' * 70)
    for name, a, b, lower_better in [
        ('Total api_input chars', chars_a, chars_b, True),
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
            sym = ('✅' if (pct < 0) == lower_better else
                   '❌' if abs(pct) > 3 else '➖')
            d = f'{pct:+.1f}% {sym}'
        if isinstance(a, float):
            print(f"  {name:<28} │ {a:>12.4f} │ {b:>12.4f} │ {d:>10}")
        else:
            print(f"  {name:<28} │ {a:>12,} │ {b:>12,} │ {d:>10}")
    print('  ' + '─' * 70)
    diff = cost_a - cost_b
    if abs(diff) < 0.0005:
        verdict = '💰 NEUTRAL'
    elif diff > 0:
        verdict = f'💰 AGGRESSIVE cheaper by ${diff:.4f} ({diff/cost_a*100:.1f}%)'
    else:
        verdict = f'💰 STATUS_QUO cheaper by ${-diff:.4f} ({-diff/cost_b*100:.1f}%)'
    print(f"\n  {verdict}")


def _save(arm_a, arm_b, path):
    with open(path, 'w') as f:
        json.dump({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'arm_a': {'label': arm_a.label, 'rounds': [asdict(r) for r in arm_a.rounds]},
            'arm_b': {'label': arm_b.label, 'rounds': [asdict(r) for r in arm_b.rounds]},
        }, f, indent=2, default=str)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src-conv', default=DEFAULT_SRC_CONV)
    ap.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)
    ap.add_argument('--initial-rounds', type=int, default=10,
                    help='How many rounds of the source conv to keep before '
                         'starting the A/B (provides bulk to compact)')
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    test_a_id = f'l1ab-A-{timestamp}'
    test_b_id = f'l1ab-B-{timestamp}'

    print('█' * 70)
    print(f'  L1 CACHE-PREFIX A/B  src={args.src_conv}  '
          f'rounds={args.rounds}  initial={args.initial_rounds}')
    print(f'  Model: {args.model}  Interval: {args.interval}s')
    print('█' * 70)

    print('\n  [Setup] Cloning + truncating source conv into two test convs...')
    _clone_conv(args.src_conv, test_a_id)
    _clone_conv(args.src_conv, test_b_id)
    _truncate_to_rounds(test_a_id, args.initial_rounds)
    _truncate_to_rounds(test_b_id, args.initial_rounds)

    arm_a = ArmResult(label='STATUS_QUO (cache-prefix skip)')
    arm_b = ArmResult(label='AGGRESSIVE (no cache-prefix skip)')
    result_path = f'debug/l1_cache_tradeoff_{timestamp}.json'

    prompts = [
        'Briefly: what is the file under exploration about?',
        'What error patterns dominate?',
        'Summarize the test coverage observed.',
        'Which module has the most tool calls?',
        'What would you investigate next?',
        'Any obvious refactor suggestions?',
        'Pick one bug to file an issue for.',
        'Done.',
    ]

    try:
        for r in range(args.rounds):
            prompt = prompts[r % len(prompts)]
            # Arm A: status quo (cache-prefix skip honored, default)
            ra = _run_round('STATUS_QUO', test_a_id, r, prompt,
                            aggressive=False, model=args.model,
                            arm_seed=f'A-{timestamp}')
            arm_a.rounds.append(ra)
            _save(arm_a, arm_b, result_path)
            time.sleep(args.interval)

            # Arm B: aggressive (force _cache_prefix_count=0 each round)
            rb = _run_round('AGGRESSIVE', test_b_id, r, prompt,
                            aggressive=True, model=args.model,
                            arm_seed=f'B-{timestamp}')
            arm_b.rounds.append(rb)
            _save(arm_a, arm_b, result_path)
            if r < args.rounds - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n[aborted — partial results saved]', flush=True)

    finally:
        _drop_test_conv(test_a_id)
        _drop_test_conv(test_b_id)
        print(f'\n  [Cleanup] Dropped {test_a_id} and {test_b_id}')

    _print_report(arm_a, arm_b)
    print(f'\n  📁 Saved: {result_path}')


if __name__ == '__main__':
    main()
