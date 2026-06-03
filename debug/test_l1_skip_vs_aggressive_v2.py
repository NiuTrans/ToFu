#!/usr/bin/env python3
"""L1 cache-prefix-skip vs aggressive — V2 with pre-seeded warm cache.

The V1 test (`test_l1_prefix_skip_vs_aggressive.py`) couldn't surface a
difference because both arms hit `pfx_count=0` on R1 (fresh test conv
has no cache_tracking state). STATUS_QUO compacted everything on R1
just like AGGRESSIVE, then both arms were byte-identical thereafter.

V2 fix: pre-seed `_cache_states[conv_id]` for STATUS_QUO arm with a
synthetic CacheState that has `last_cache_read_tokens > 1000` and
`message_count` set to a reasonable warm-cache value. Then on the first
L1 call, `get_cache_prefix_count` returns a large protective value and
STATUS_QUO will actually skip cold rounds. AGGRESSIVE arm leaves
cache_states untouched (pfx=0 always).

Both arms then run M rounds and we measure cumulative API cost.

The decision criterion: does the long-tail cache_read savings from a
smaller prefix in AGGRESSIVE outweigh its one-time cache rebuild cost
in R1? If yes → drop the skip. If no → keep it.

Usage:
    python debug/test_l1_skip_vs_aggressive_v2.py --rounds 10
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
DEFAULT_ROUNDS = 10
DEFAULT_INTERVAL = 4
DEFAULT_SRC_CONV = 'mp0sggcln5pruo'
DEFAULT_INITIAL_ROUNDS = 25

INPUT_PRICE_USD_PER_MTOK = 5.0
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
    cache_prefix_count: int = 0
    pre_compact_chars: int = 0


@dataclass
class ArmResult:
    label: str
    rounds: list = field(default_factory=list)


# ───── conv setup ─────

def _clone_and_truncate(src_id: str, dst_id: str, n_rounds: int) -> None:
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT user_id, title, messages, settings, created_at, updated_at, '
        'msg_count, search_text FROM conversations WHERE id=?', (src_id,)
    ).fetchone()
    if not row:
        raise SystemExit(f'src conv {src_id!r} not found')
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

    # CRITICAL: clear any pre-existing L1 stamps on the cloned conv so
    # both arms start with the same un-compacted baseline. Without this,
    # the source's existing compaction bleeds into the test.
    for m in msgs:
        for r in (m.get('toolRounds') or []):
            r.pop('compactionLayer', None)
            r.pop('compactedFromChars', None)
            r.pop('compactedToChars', None)

    db.execute(
        'INSERT INTO conversations (id, user_id, title, messages, settings, '
        'created_at, updated_at, msg_count, search_text) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (dst_id, user_id, f'L1V2 {dst_id}', json_dumps_pg(msgs), settings,
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


# ───── pre-seed cache_tracking state for STATUS_QUO arm ─────

def _preseed_cache_state(conv_id: str, message_count: int,
                         fake_cache_read_tokens: int = 50000) -> None:
    """Inject a synthetic CacheState so get_cache_prefix_count returns
    message_count - 2 immediately. This simulates the production warm-
    cache scenario where pfx_count > 0 from R1 onward, forcing
    STATUS_QUO arm to actually skip cold rounds."""
    with cache_tracking._cache_lock:
        st = cache_tracking.CacheState()
        st.message_count = message_count
        st.last_cache_read_tokens = fake_cache_read_tokens
        st.last_update_time = time.time()
        st.call_count = 1
        st.first_call_time = time.time()
        cache_tracking._cache_states[conv_id] = st


# ───── send / measure ─────

def _send(messages: list, model: str, max_tokens: int = 200,
          arm_seed: str = ''):
    if arm_seed and messages and messages[0].get('role') == 'system':
        sys_content = messages[0].get('content', '')
        if isinstance(sys_content, str):
            messages = [{'role': 'system',
                         'content': f'<!-- arm:{arm_seed} -->\n{sys_content}'}] + messages[1:]
    msg, _finish, usage = dispatch_stream(
        messages, max_tokens=max_tokens,
        prefer_model=model, strict_model=True, max_retries=1,
    )
    if isinstance(msg, dict):
        return msg.get('content', '') or '', usage
    return msg or '', usage


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


# Per-conv aggressive override
_AGGRESSIVE_CONVS: set[str] = set()
_orig_get_pfx = cache_tracking.get_cache_prefix_count


def _patched_get_pfx(conv_id: str) -> int:
    if conv_id in _AGGRESSIVE_CONVS:
        return 0
    return _orig_get_pfx(conv_id)


def _run_round(arm: str, conv_id: str, round_idx: int, prompt: str,
               model: str, arm_seed: str) -> RoundResult:
    pre_pfx = cache_tracking.get_cache_prefix_count(conv_id)
    _append_user(conv_id, prompt)

    api_msgs = build_api_messages_from_db(conv_id, {'systemPrompt': ''})
    if not api_msgs:
        return RoundResult(arm=arm, round_num=round_idx + 1,
                           error='no api messages')

    # Pre-compact stats
    _, _, pre_chars = _stats(api_msgs)

    # Run L1
    fake_task = {
        'toolRounds': [], 'model': model,
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'events': [], 'id': f'ab-{arm}',
    }
    micro_compact(api_msgs, conv_id=conv_id, task=fake_task)

    tools, big, post_chars = _stats(api_msgs)
    rr = RoundResult(arm=arm, round_num=round_idx + 1,
                     msg_count=len(api_msgs), tool_msg_count=tools,
                     big_tool_count=big, api_input_chars=post_chars,
                     pre_compact_chars=pre_chars,
                     cache_prefix_count=pre_pfx)

    print(f'    [R{round_idx+1}] {arm:<11} pfx={pre_pfx:>4}  '
          f'pre={pre_chars:>9,} → post={post_chars:>9,}  '
          f'big={big:>3}', flush=True)

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

    cache_tracking.detect_cache_break(conv_id, api_msgs, [], model, usage=u)
    _append_assistant(conv_id, (content[:200] or '[empty]'))
    return rr


# ───── reporting ─────

def _print_table(arm: ArmResult):
    print(f'\n  ── {arm.label} ──')
    print(f"  {'Rnd':>3} │ {'Pfx':>5} │ {'PreChars':>9} │ {'PostChars':>9} │ "
          f"{'Big':>3} │ {'CacheR':>7} │ {'CacheW':>6} │ {'$':>7}")
    print('  ' + '─' * 78)
    for r in arm.rounds:
        if r.error:
            print(f'  {r.round_num:>3} │ ERR: {r.error[:60]}'); continue
        print(f"  {r.round_num:>3} │ {r.cache_prefix_count:>5} │ "
              f"{r.pre_compact_chars:>9,} │ {r.api_input_chars:>9,} │ "
              f"{r.big_tool_count:>3} │ {r.cache_read:>7,} │ "
              f"{r.cache_write:>6,} │ ${r.cost_usd:>6.4f}")


def _print_report(arm_a: ArmResult, arm_b: ArmResult):
    print('\n  ' + '▓' * 70)
    print('  L1 SKIP vs AGGRESSIVE — V2 with pre-seeded warm cache')
    print('  ' + '▓' * 70)
    _print_table(arm_a)
    _print_table(arm_b)

    paired = [(a, b) for a, b in zip(arm_a.rounds, arm_b.rounds)
              if not a.error and not b.error]
    if not paired:
        print('\n  no completed pairs'); return

    def _T(field, side):
        return sum(getattr(a if side == 'a' else b, field) for a, b in paired)

    metrics = [
        ('Total post-compact chars', _T('api_input_chars', 'a'),
         _T('api_input_chars', 'b'), True),
        ('Prompt tokens (uncached)', _T('prompt_tokens', 'a'),
         _T('prompt_tokens', 'b'), True),
        ('Cache reads', _T('cache_read', 'a'), _T('cache_read', 'b'), False),
        ('Cache writes', _T('cache_write', 'a'), _T('cache_write', 'b'), True),
        ('Output tokens', _T('output_tokens', 'a'), _T('output_tokens', 'b'), True),
        ('Cost USD', _T('cost_usd', 'a'), _T('cost_usd', 'b'), True),
    ]

    print(f'\n  {len(paired)} round-pairs:')
    print(f"  {'Metric':<26} │ {'STATUS_QUO':>12} │ {'AGGRESSIVE':>12} │ {'Δ':>10}")
    print('  ' + '─' * 70)
    for name, a, b, _ in metrics:
        if a == 0 and b == 0:
            d = '-'
        else:
            pct = (b - a) / max(a, 1e-9) * 100
            d = f'{pct:+.1f}%'
        if isinstance(a, float):
            print(f"  {name:<26} │ {a:>12.4f} │ {b:>12.4f} │ {d:>10}")
        else:
            print(f"  {name:<26} │ {a:>12,} │ {b:>12,} │ {d:>10}")

    cost_a, cost_b = _T('cost_usd', 'a'), _T('cost_usd', 'b')
    diff = cost_a - cost_b
    print('  ' + '─' * 70)
    if abs(diff) < 0.005:
        v = f'NEUTRAL (within ${abs(diff):.4f})'
    elif diff > 0:
        v = f'AGGRESSIVE cheaper by ${diff:.4f} ({diff/cost_a*100:.1f}%) → DROP THE SKIP'
    else:
        v = f'STATUS_QUO  cheaper by ${-diff:.4f} ({-diff/cost_b*100:.1f}%) → KEEP THE SKIP'
    print(f"\n  VERDICT: {v}")


def _save(path, arm_a, arm_b):
    with open(path, 'w') as f:
        json.dump({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
            'arm_a': {'label': arm_a.label, 'rounds': [asdict(r) for r in arm_a.rounds]},
            'arm_b': {'label': arm_b.label, 'rounds': [asdict(r) for r in arm_b.rounds]},
        }, f, indent=2)


# ───── main ─────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src-conv', default=DEFAULT_SRC_CONV)
    ap.add_argument('--initial-rounds', type=int, default=DEFAULT_INITIAL_ROUNDS)
    ap.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    args = ap.parse_args()

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    test_a = f'l1v2-A-{timestamp}'
    test_b = f'l1v2-B-{timestamp}'

    print('█' * 70)
    print(f'  L1 SKIP vs AGGRESSIVE — V2  src={args.src_conv}')
    print(f'  initial_rounds={args.initial_rounds}  test_rounds={args.rounds}')
    print(f'  model={args.model}')
    print('█' * 70)

    print('\n  [Setup] Cloning + truncating + clearing prior L1 stamps...')
    _clone_and_truncate(args.src_conv, test_a, args.initial_rounds)
    _clone_and_truncate(args.src_conv, test_b, args.initial_rounds)

    # Pre-seed cache_tracking state for STATUS_QUO arm so pfx_count > 0
    # from R1 onward (simulates production warm-cache scenario).
    initial_msgs_a = build_api_messages_from_db(test_a, {'systemPrompt': ''})
    seed_msg_count = len(initial_msgs_a)
    print(f'  [Setup] Pre-seeding cache state for STATUS_QUO arm '
          f'(message_count={seed_msg_count}, last_cache_read=50,000)')
    _preseed_cache_state(test_a, seed_msg_count, fake_cache_read_tokens=50000)

    # Force AGGRESSIVE for arm B
    _AGGRESSIVE_CONVS.add(test_b)
    cache_tracking.get_cache_prefix_count = _patched_get_pfx

    arm_a = ArmResult(label='STATUS_QUO (skip honored, pre-seeded warm)')
    arm_b = ArmResult(label='AGGRESSIVE (skip ignored)')
    out_path = f'debug/l1_skip_v2_{timestamp}.json'

    prompts = [
        'In one sentence: what is this codebase?',
        'Where is the main entry point?',
        'List two database backends supported.',
        'What HTTP framework backs the routes?',
        'Where are templates served from?',
        'What is logged to logs/error.log?',
        'Name a key compaction parameter.',
        'What is MICRO_HOT_TAIL set to?',
        'Describe the cache-prefix skip briefly.',
        'Done.',
    ]

    seed_a = f'A-{timestamp}'
    seed_b = f'B-{timestamp}'

    try:
        for r in range(args.rounds):
            prompt = prompts[r % len(prompts)]
            ra = _run_round('STATUS_QUO', test_a, r, prompt, args.model, seed_a)
            arm_a.rounds.append(ra); _save(out_path, arm_a, arm_b)
            time.sleep(args.interval)

            rb = _run_round('AGGRESSIVE', test_b, r, prompt, args.model, seed_b)
            arm_b.rounds.append(rb); _save(out_path, arm_a, arm_b)
            if r < args.rounds - 1:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\n[aborted — partial results saved]')
    finally:
        _AGGRESSIVE_CONVS.discard(test_b)
        cache_tracking.get_cache_prefix_count = _orig_get_pfx
        cache_tracking.cleanup_cache_state(test_a)
        cache_tracking.cleanup_cache_state(test_b)
        _drop_test_conv(test_a)
        _drop_test_conv(test_b)
        print(f'\n  [Cleanup] Dropped {test_a}, {test_b}')

    _print_report(arm_a, arm_b)
    print(f'\n  Saved: {out_path}')


if __name__ == '__main__':
    main()
