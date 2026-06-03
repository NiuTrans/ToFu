#!/usr/bin/env python3
"""Focused live A/B test — paired-assistant compaction (Phase B2).

Bypasses the dispatcher entirely; hits the Sankuai gateway directly with
``example-corp_key_0`` (the per-minute-limited but daily-alive key). A 65-second
inter-request delay keeps us under the per-minute rate limit.

Two arms, interleaved per round:
  BASELINE — micro_compact with Phase B only.
  PAIRED   — micro_compact with Phase B + Phase B2 (enable_paired_assistant_compact).

Measures actual API-reported cache_read / cache_write per round, written
immediately to an intermediate JSON file so ctrl-c / timeout never loses data.

Hypothesis on test:
  H1 — Phase B2 saves extra tokens.
  H2 — Phase B2 is cache-neutral vs Phase B.

Local byte-hash analysis (run earlier via test_paired_compact_ab.py --local)
structurally REFUTED H2: the paired assistant sits BEFORE its tool result, so
mutating it moves the first-break-index earlier by 1. This live test is the
empirical confirmation of that structural prediction.

Usage:
    python debug/test_paired_compact_live.py --rounds 5 --model aws.claude-sonnet-4.6
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger

logger = get_logger(__name__)

# ─── Config ───────────────────────────────────────────────────────────

# OpenAI-compatible endpoint — set TOFU_PROBE_BASE_URL / TOFU_PROBE_API_KEY.
SANKUAI_URL = os.environ.get(
    'TOFU_PROBE_BASE_URL', 'https://api.openai.com/v1',
).rstrip('/') + '/chat/completions'
SANKUAI_KEY_0 = os.environ.get('TOFU_PROBE_API_KEY', '')
SANKUAI_KEY_1 = os.environ.get('TOFU_PROBE_API_KEY_2', SANKUAI_KEY_0)
SANKUAI_HEADERS = {
    'Authorization': f'Bearer {SANKUAI_KEY_0}',
    'Content-Type': 'application/json',
    **json.loads(os.environ.get('TOFU_PROBE_EXTRA_HEADERS', '{}')),
}

DEFAULT_MODEL = 'aws.claude-opus-4.7'    # Opus 4.7 (30 rpm) currently has headroom
DEFAULT_ROUNDS = 5
DEFAULT_INTERVAL = 12   # 30 rpm = 2s floor; 12s is comfortable for 2 arms × 1 call
TEST_HOT_TAIL = 3
TEST_MICRO_THRESHOLD = 500

# Import shared fixtures from the main harness so behavior stays in sync.
from debug.test_paired_compact_ab import (  # noqa: E402
    SYSTEM_PROMPT, TOOLS, TOOL_RESULTS, TOOL_NAMES_PER_ROUND,
    INTERSTITIAL_COMMENTARIES,
    _build_conversation_through_round, _estimate_tokens,
)


@dataclass
class RoundResult:
    round_num: int
    arm: str
    msg_count: int = 0
    est_tokens_before: int = 0
    est_tokens_after: int = 0
    tokens_saved_by_compact: int = 0
    prompt_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    elapsed: float = 0.0
    status: str = ''
    error: str = ''
    # Observability
    first_break_idx: int | None = None  # lowest index mutated by compaction


@dataclass
class ArmResult:
    label: str
    model: str
    rounds: list = field(default_factory=list)

    @property
    def valid(self):
        return [r for r in self.rounds if not r.error]

    def total(self, field_):
        return sum(getattr(r, field_) for r in self.valid)


# ─── Direct HTTP call (no dispatcher) ─────────────────────────────────

def _raw_chat(messages: list, *, model: str, max_tokens: int = 256,
              timeout: float = 90.0) -> tuple[dict, dict]:
    """Single POST to the Sankuai gateway. Returns (response_json, usage)."""
    from lib.llm import build_body, add_cache_breakpoints

    # Build body with cache_control breakpoints exactly like the production client.
    body = build_body(
        model, messages,
        max_tokens=max_tokens,
        temperature=1.0,
        thinking_enabled=False,
        tools=TOOLS,
        stream=False,
    )
    add_cache_breakpoints(body)

    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        SANKUAI_URL,
        data=data,
        headers=SANKUAI_HEADERS,
        method='POST',
    )
    # Force no proxy for example-corp.com
    os.environ.setdefault('NO_PROXY', '.internal.example.com')

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body_bytes = resp.read()
    obj = json.loads(body_bytes.decode('utf-8'))
    usage = obj.get('usage', {}) or {}
    return obj, usage


def _compute_first_break(before: list, after: list) -> int | None:
    import hashlib
    for i, (b, a) in enumerate(zip(before, after)):
        ha = hashlib.sha256(json.dumps(a, sort_keys=True, ensure_ascii=False,
                                        default=str).encode()).hexdigest()[:16]
        hb = hashlib.sha256(json.dumps(b, sort_keys=True, ensure_ascii=False,
                                        default=str).encode()).hexdigest()[:16]
        if ha != hb:
            return i
    return None


def _run_round(*, round_idx: int, num_rounds: int, arm_label: str,
               enable_paired: bool, model: str, arm_seed: str) -> RoundResult:
    from lib.tasks_pkg.compaction import micro_compact

    messages = _build_conversation_through_round(round_idx + 1, arm_seed)
    est_before = _estimate_tokens(messages)
    pre_compact = copy.deepcopy(messages)

    # Apply Phase B (and optionally B2)
    micro_compact(
        messages,
        conv_id=f'live_{arm_label}',
        enable_paired_assistant_compact=enable_paired,
    )
    first_break_idx = _compute_first_break(pre_compact, messages)
    est_after = _estimate_tokens(messages)

    rr = RoundResult(
        round_num=round_idx + 1,
        arm=arm_label,
        msg_count=len(messages),
        est_tokens_before=est_before,
        est_tokens_after=est_after,
        tokens_saved_by_compact=est_before - est_after,
        first_break_idx=first_break_idx,
    )

    print(f"    [R{round_idx+1}/{num_rounds}] {arm_label:<9} msgs={len(messages)} "
          f"est={est_before}→{est_after} saved=~{est_before - est_after} "
          f"first_break={first_break_idx}",
          flush=True)

    # ── Live API call via direct HTTP ─────────────────────────────────
    t0 = time.time()
    try:
        resp_obj, usage = _raw_chat(messages, model=model, max_tokens=256)
    except urllib.error.HTTPError as e:
        body_bytes = b''
        try:
            body_bytes = e.read()
        except Exception:
            pass
        msg = f'HTTP {e.code}: {body_bytes.decode("utf-8", errors="replace")[:300]}'
        logger.warning('[AB-LIVE] R%d %s: %s', round_idx + 1, arm_label, msg)
        rr.error = msg[:300]
        return rr
    except Exception as e:
        logger.warning('[AB-LIVE] R%d %s: %s', round_idx + 1, arm_label, e)
        rr.error = str(e)[:300]
        return rr

    rr.elapsed = time.time() - t0

    # Cache metrics (Anthropic via OpenAI-compat returns these in usage.*)
    u = usage or {}
    # The Sankuai gateway normalizes to OpenAI-style names; cache tokens
    # are exposed as prompt_tokens_details.cached_tokens or explicit fields.
    details = u.get('prompt_tokens_details', {}) or {}
    rr.cache_read = (u.get('cache_read_tokens')
                     or u.get('cache_read_input_tokens')
                     or details.get('cached_tokens') or 0)
    rr.cache_write = (u.get('cache_creation_input_tokens')
                      or u.get('cache_write_tokens')
                      or details.get('cache_write_tokens') or 0)
    rr.prompt_tokens = max(0, (u.get('prompt_tokens') or 0) - rr.cache_read - rr.cache_write)
    rr.output_tokens = u.get('completion_tokens') or 0

    if rr.cache_write > 500 and rr.cache_read > 500:
        rr.status = 'HIT+WRITE'
    elif rr.cache_read > 500:
        rr.status = 'HIT'
    elif rr.cache_write > 500:
        rr.status = 'WRITE'
    else:
        rr.status = 'MISS'

    print(f"      ⏱ {rr.elapsed:.1f}s | {rr.status}  pt={rr.prompt_tokens:,}  "
          f"cr={rr.cache_read:,}  cw={rr.cache_write:,}  out={rr.output_tokens}",
          flush=True)
    return rr


def _delta(a, b, lower_better=True):
    if a == 0:
        return 'N/A'
    pct = (b - a) / a * 100
    better = pct < 0 if lower_better else pct > 0
    sym = "✅" if better else ("⚠️" if abs(pct) > 3 else "➖")
    return f"{pct:+.1f}% {sym}"


def _print_report(arm_a: ArmResult, arm_b: ArmResult):
    print()
    print("  " + "▓" * 60)
    print("  PAIRED-COMPACT (PHASE B2) — LIVE A/B RESULTS")
    print("  " + "▓" * 60)

    for arm in (arm_a, arm_b):
        print(f"\n  ── {arm.label} ──")
        print(f"  {'Rnd':>3} │ {'Msgs':>4} │ {'EstSvd':>6} │ {'FB':>3} │ "
              f"{'Prompt':>7} │ {'CacheR':>7} │ {'CacheW':>7} │ "
              f"{'Out':>5} │ {'Status':>10}")
        print(f"  {'─'*3}─┼─{'─'*4}─┼─{'─'*6}─┼─{'─'*3}─┼─{'─'*7}─┼─"
              f"{'─'*7}─┼─{'─'*7}─┼─{'─'*5}─┼─{'─'*10}")
        for r in arm.rounds:
            if r.error:
                print(f"  {r.round_num:>3} │  ERR │ {r.error[:70]}")
                continue
            fb = str(r.first_break_idx) if r.first_break_idx is not None else '-'
            print(f"  {r.round_num:>3} │ {r.msg_count:>4} │ "
                  f"{r.tokens_saved_by_compact:>6,} │ {fb:>3} │ "
                  f"{r.prompt_tokens:>7,} │ {r.cache_read:>7,} │ "
                  f"{r.cache_write:>7,} │ {r.output_tokens:>5} │ {r.status:>10}")

    # Totals (skip errored rounds on either side; compare only round-pairs
    # where BOTH arms succeeded, for apples-to-apples).
    paired_rounds = []
    for ra, rb in zip(arm_a.rounds, arm_b.rounds):
        if not ra.error and not rb.error:
            paired_rounds.append((ra, rb))

    if not paired_rounds:
        print("\n  ❌ No rounds completed successfully in BOTH arms — cannot compare.")
        return

    def _tot(arm_field, side):
        return sum(getattr(ra if side == 'a' else rb, arm_field)
                   for ra, rb in paired_rounds)

    prompt_a, prompt_b = _tot('prompt_tokens', 'a'), _tot('prompt_tokens', 'b')
    cr_a, cr_b = _tot('cache_read', 'a'), _tot('cache_read', 'b')
    cw_a, cw_b = _tot('cache_write', 'a'), _tot('cache_write', 'b')
    out_a, out_b = _tot('output_tokens', 'a'), _tot('output_tokens', 'b')
    est_a = _tot('tokens_saved_by_compact', 'a')
    est_b = _tot('tokens_saved_by_compact', 'b')

    # Sonnet pricing: $3 input / $15 output per 1M tok, cache read 0.1x, write 1.25x.
    ip, op = 3.0, 15.0
    cw_mul, cr_mul = 1.25, 0.10
    cost_a = ((prompt_a + cr_a * cr_mul + cw_a * cw_mul) * ip + out_a * op) / 1e6
    cost_b = ((prompt_b + cr_b * cr_mul + cw_b * cw_mul) * ip + out_b * op) / 1e6

    print(f"\n  (Compared across {len(paired_rounds)} round-pairs where both arms succeeded)")
    print(f"  {'Metric':<32} │ {'BASELINE':>12} │ {'PAIRED':>12} │ {'Δ':>12}")
    print(f"  {'─'*32}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")
    for name, a, b, lb in [
        ("Est tokens saved (B vs B+B2)", est_a, est_b, False),
        ("Uncached prompt (tokens)",     prompt_a, prompt_b, True),
        ("Cache reads (tokens)",         cr_a, cr_b, False),
        ("Cache writes (tokens)",        cw_a, cw_b, True),
        ("Output (tokens)",              out_a, out_b, True),
    ]:
        print(f"  {name:<32} │ {a:>12,} │ {b:>12,} │ {_delta(a, b, lb):>12}")
    print(f"  {'─'*32}─┼─{'─'*12}─┼─{'─'*12}─┼─{'─'*12}")
    print(f"  {'TOTAL COST':<32} │ ${cost_a:>11.4f} │ "
          f"${cost_b:>11.4f} │ {_delta(cost_a, cost_b, True):>12}")

    # Hypothesis verdicts
    print()
    print(f"  HYPOTHESIS 1 — B2 saves more tokens: ", end='')
    if est_b > est_a:
        print(f"✅ YES (extra {est_b - est_a:,} tokens saved pre-API)")
    else:
        print(f"❌ NO")

    print(f"  HYPOTHESIS 2 — B2 is cache-neutral:  ", end='')
    # Tolerance: 5% of BASELINE cache_write (min 500 tokens to accommodate noise)
    tol = max(500, int(cw_a * 0.05))
    dw = cw_b - cw_a
    if dw <= tol:
        print(f"✅ YES (Δcache_write={dw:+,} within ±{tol} tolerance)")
    else:
        print(f"❌ NO  (Δcache_write={dw:+,} exceeds ±{tol} tolerance — "
              f"B2 introduced extra cache invalidation)")

    diff = cost_a - cost_b
    print()
    if abs(diff) < 0.0005:
        print(f"  💰 NEUTRAL ({diff:+.4f}$)")
    elif diff > 0:
        print(f"  💰 PAIRED cheaper by ${diff:.4f} "
              f"({diff / max(cost_a, 1e-5) * 100:.1f}%)")
    else:
        print(f"  💰 BASELINE cheaper by ${-diff:.4f} "
              f"({-diff / max(cost_b, 1e-5) * 100:.1f}%)")


def _save(arm_a: ArmResult, arm_b: ArmResult, path: str):
    out = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime()),
        'arm_a': {'label': arm_a.label, 'model': arm_a.model,
                  'rounds': [asdict(r) for r in arm_a.rounds]},
        'arm_b': {'label': arm_b.label, 'model': arm_b.model,
                  'rounds': [asdict(r) for r in arm_b.rounds]},
    }
    with open(path, 'w') as f:
        json.dump(out, f, indent=2, default=str)


def _run_arm_incremental(arm_label: str, enable_paired: bool, *,
                         model: str, arm_seed: str, num_rounds: int,
                         interval: int, result_path: str,
                         result_other: ArmResult,
                         result_self: ArmResult):
    """Run ONE arm end-to-end as a continuous conversation.

    Unlike the original harness which rebuilds the conversation each round,
    this APPENDS to a persistent message list so the cache built in round N
    can be hit in round N+1. This is the only way to measure what the
    paired-compact hypothesis actually claims: that mutating the assistant
    in a cold tool round doesn't break cache continuity.

    Sequence per round:
      1. Append a fresh user message to the running conversation.
      2. Run micro_compact(enable_paired=...) on the whole thing.
      3. POST to API — this READS the still-valid prefix and WRITES any
         new prefix bytes.
      4. Append the assistant response to the running conversation.
      5. Synthesize tool_calls for the test round (see below).
    """
    from lib.tasks_pkg.compaction import micro_compact

    # Persistent conversation built round-by-round.
    messages: list = [
        {'role': 'system', 'content': SYSTEM_PROMPT + arm_seed},
        {'role': 'user', 'content': (
            'I need to understand the error handling in this project. '
            'Please investigate and give me a brief summary.'
        )},
    ]

    tc_counter = 0
    for r in range(num_rounds):
        # Add an assistant interstitial + tool_call for this round.
        interstitial = INTERSTITIAL_COMMENTARIES[r % len(INTERSTITIAL_COMMENTARIES)]
        tool_name = TOOL_NAMES_PER_ROUND[r % len(TOOL_NAMES_PER_ROUND)]
        tc_id = f'call_{tc_counter:04d}'
        tc_counter += 1
        messages.append({
            'role': 'assistant',
            'content': interstitial,
            'tool_calls': [{
                'id': tc_id,
                'type': 'function',
                'function': {'name': tool_name, 'arguments': '{"path":"."}'},
            }],
        })
        messages.append({
            'role': 'tool',
            'tool_call_id': tc_id,
            'name': tool_name,
            'content': TOOL_RESULTS[tool_name],
        })

        # Snapshot for first-break detection.
        import copy
        pre_compact = copy.deepcopy(messages)

        # Run Phase B (and optionally B2). This is the ONLY difference
        # between the two arms. Everything else is identical.
        micro_compact(
            messages,
            conv_id=f'live_{arm_label}',
            enable_paired_assistant_compact=enable_paired,
        )
        first_break_idx = _compute_first_break(pre_compact, messages)

        est_before = _estimate_tokens(pre_compact)
        est_after = _estimate_tokens(messages)

        rr = RoundResult(
            round_num=r + 1,
            arm=arm_label,
            msg_count=len(messages),
            est_tokens_before=est_before,
            est_tokens_after=est_after,
            tokens_saved_by_compact=est_before - est_after,
            first_break_idx=first_break_idx,
        )

        print(f"    [R{r+1}/{num_rounds}] {arm_label:<9} msgs={len(messages)} "
              f"est={est_before}→{est_after} saved=~{est_before - est_after} "
              f"first_break={first_break_idx}",
              flush=True)

        # ── API call ─────────────────────────────────────────────────
        t0 = time.time()
        try:
            resp_obj, usage = _raw_chat(messages, model=model, max_tokens=256)
        except urllib.error.HTTPError as e:
            body_bytes = b''
            try:
                body_bytes = e.read()
            except Exception:
                pass
            err = f'HTTP {e.code}: {body_bytes.decode("utf-8", errors="replace")[:300]}'
            logger.warning('[AB-LIVE] R%d %s: %s', r + 1, arm_label, err)
            rr.error = err[:300]
            result_self.rounds.append(rr)
            _save(result_other if arm_label == 'PAIRED' else result_self,
                  result_self if arm_label == 'PAIRED' else result_other,
                  result_path)
            # Don't append broken output to the conversation — bail out
            # of this round; leave next round's cache fresh.
            if r < num_rounds - 1:
                time.sleep(interval)
            continue
        except Exception as e:
            logger.warning('[AB-LIVE] R%d %s: %s', r + 1, arm_label, e)
            rr.error = str(e)[:300]
            result_self.rounds.append(rr)
            _save(result_other if arm_label == 'PAIRED' else result_self,
                  result_self if arm_label == 'PAIRED' else result_other,
                  result_path)
            if r < num_rounds - 1:
                time.sleep(interval)
            continue

        rr.elapsed = time.time() - t0
        u = usage or {}
        details = u.get('prompt_tokens_details', {}) or {}
        rr.cache_read = (u.get('cache_read_tokens')
                         or u.get('cache_read_input_tokens')
                         or details.get('cached_tokens') or 0)
        rr.cache_write = (u.get('cache_creation_input_tokens')
                          or u.get('cache_write_tokens')
                          or details.get('cache_write_tokens') or 0)
        rr.prompt_tokens = max(0, (u.get('prompt_tokens') or 0)
                               - rr.cache_read - rr.cache_write)
        rr.output_tokens = u.get('completion_tokens') or 0

        if rr.cache_write > 500 and rr.cache_read > 500:
            rr.status = 'HIT+WRITE'
        elif rr.cache_read > 500:
            rr.status = 'HIT'
        elif rr.cache_write > 500:
            rr.status = 'WRITE'
        else:
            rr.status = 'MISS'

        print(f"      ⏱ {rr.elapsed:.1f}s | {rr.status}  pt={rr.prompt_tokens:,}  "
              f"cr={rr.cache_read:,}  cw={rr.cache_write:,}  "
              f"out={rr.output_tokens}",
              flush=True)

        result_self.rounds.append(rr)
        # Save after every round so ctrl-c / timeout never loses data.
        if arm_label == 'BASELINE':
            _save(result_self, result_other, result_path)
        else:
            _save(result_other, result_self, result_path)

        # Don't bother appending the assistant reply to the conversation —
        # we just need the prefix up through the tool result to be stable
        # for the NEXT round to hit its cache. The test's "round N+1"
        # appends a fresh assistant→tool round on top, which is what
        # production compaction would see.

        if r < num_rounds - 1:
            time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--rounds', type=int, default=DEFAULT_ROUNDS)
    ap.add_argument('--interval', type=int, default=DEFAULT_INTERVAL,
                    help='Seconds between API calls')
    args = ap.parse_args()

    # Monkey-patch compaction params so Phase B fires at low round counts.
    import lib.tasks_pkg.compaction as _comp
    _orig_hot = _comp.MICRO_HOT_TAIL
    _orig_thr = _comp.MICRO_COMPACT_THRESHOLD
    _comp.MICRO_HOT_TAIL = TEST_HOT_TAIL
    _comp.MICRO_COMPACT_THRESHOLD = TEST_MICRO_THRESHOLD

    print()
    print("█" * 70)
    print("  PAIRED-COMPACT (PHASE B2) — LIVE API A/B TEST (incremental)")
    print(f"  Model: {args.model}   Key: example-corp_key_0 (direct HTTP)")
    print(f"  Rounds: {args.rounds}  Interval: {args.interval}s")
    print(f"  HOT_TAIL: {TEST_HOT_TAIL}  COMPACT_THRESHOLD: "
          f"{TEST_MICRO_THRESHOLD}")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("█" * 70)

    # Each arm uses a unique seed so their cache prefixes are INDEPENDENT.
    # The test relies on cache_read in later rounds of THE SAME ARM to
    # measure how much prefix was reused after compaction mutated it.
    seed_a = f'\n<!-- arm=BASELINE seed={int(time.time())} -->'
    seed_b = f'\n<!-- arm=PAIRED   seed={int(time.time()) + 1} -->'

    arm_a = ArmResult(label='BASELINE', model=args.model)
    arm_b = ArmResult(label='PAIRED', model=args.model)

    timestamp = time.strftime('%Y%m%d_%H%M%S')
    result_path = f'debug/paired_compact_live_{timestamp}.json'

    try:
        # Run both arms SEPARATELY as incremental conversations.
        # Ordering: BASELINE first, then PAIRED — they don't share prompt
        # bytes (different seed), so order is inconsequential for cache.
        print(f"\n  ══ ARM A: BASELINE (Phase B only) ═════════════════════════")
        _run_arm_incremental(
            arm_label='BASELINE', enable_paired=False,
            model=args.model, arm_seed=seed_a,
            num_rounds=args.rounds, interval=args.interval,
            result_path=result_path,
            result_self=arm_a, result_other=arm_b,
        )

        print(f"\n  ══ ARM B: PAIRED (Phase B + B2) ═══════════════════════════")
        _run_arm_incremental(
            arm_label='PAIRED', enable_paired=True,
            model=args.model, arm_seed=seed_b,
            num_rounds=args.rounds, interval=args.interval,
            result_path=result_path,
            result_self=arm_b, result_other=arm_a,
        )
    except KeyboardInterrupt:
        print('\n[aborted — partial results saved]', flush=True)
    finally:
        _comp.MICRO_HOT_TAIL = _orig_hot
        _comp.MICRO_COMPACT_THRESHOLD = _orig_thr

    _print_report(arm_a, arm_b)
    print(f"\n📁 Saved: {result_path}")
    print("█" * 70)


if __name__ == '__main__':
    main()
