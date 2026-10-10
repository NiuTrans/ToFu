#!/usr/bin/env python3
"""Second-path cache A/B probe — the control the gateway report is missing.

WHAT THIS ANSWERS
=================
The report in ``docs/LLM_COST_OPTIMIZATION.md`` says: a byte-identical
prefix, identical routing, gap far past the write-visibility window, and
``cache_read_input_tokens = 0``. That is a strong observation, but it is
one-sided — we only measured the `sankuai_anthropic` gateway. The claim the
gateway team cannot easily argue with is the COMPARISON:

    the identical prefix reuses on Anthropic direct, and does not reuse
    through your gateway

This script runs that comparison. For each configured path it sends the SAME
synthetic prefix twice, separated by ``--gap`` seconds, and reports the second
round's ``cache_read_input_tokens``. A path that caches correctly reads back
~the whole prefix on send #2.

WHY IT IS DRY-RUN BY DEFAULT
============================
Arming it costs real tokens, and on the OAuth path it consumes the owner's
Claude subscription quota (which can affect their rate limits, not just their
bill). Per charter #16 an autonomous agent must not spend that on its own
initiative, so ``--arm`` is required and the default run only prints the plan.

Cost is deliberately small: a ~4k-token synthetic prefix is comfortably over
Anthropic's 1024-token cacheable minimum while costing ~0.18 CNY per cold write
at Opus rates. We do NOT reproduce production's 100k–500k prefixes.

    ⚠ READ THIS BEFORE PUBLISHING A RESULT
    A 4k prefix does not exercise the capacity-eviction hypothesis, which is
    specifically about large prefixes. If both paths HIT, that does NOT clear
    the gateway — it narrows the mechanism to something size-dependent. Report
    a null result as "no difference at 4k", never as exoneration.

USAGE
    python3 scripts/cache_ab_probe.py                    # print the plan only
    python3 scripts/cache_ab_probe.py --arm              # actually send
    python3 scripts/cache_ab_probe.py --arm --gap 40     # match the observed p50
    python3 scripts/cache_ab_probe.py --arm --only oauth_claude

The synthetic prefix contains no conversation data — it is generated filler, so
arming this discloses nothing about our users or our code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Anthropic will not cache a prefix below ~1024 tokens; 4k gives comfortable
# headroom while keeping a cold write to roughly a fifth of a yuan.
_TARGET_PREFIX_TOKENS = 4000
_CHARS_PER_TOKEN = 4  # rough; only needs to clear the minimum, not be exact


def build_synthetic_prefix(tokens: int = _TARGET_PREFIX_TOKENS) -> str:
    """Deterministic filler, byte-identical between the two sends.

    Deterministic matters: the whole point is that send #2 carries the SAME
    bytes as send #1. Anything time- or random-seeded here would invalidate the
    probe by changing the cache key.
    """
    unit = ('The quick brown fox jumps over the lazy dog. '
            'Pack my box with five dozen liquor jugs. ')
    need = tokens * _CHARS_PER_TOKEN
    return (unit * (need // len(unit) + 1))[:need]


def _paths_from_config():
    """Return [(provider_id, base_url, protocol)] for Claude-capable paths."""
    cfg = os.path.join('data', 'config', 'server_config.json')
    try:
        with open(cfg) as fh:
            d = json.load(fh)
    except Exception as e:
        raise SystemExit(f'cannot read {cfg}: {e}')
    out = []
    for p in d.get('providers') or []:
        if not isinstance(p, dict) or not p.get('enabled'):
            continue
        out.append((p.get('id') or '?', p.get('base_url') or '',
                    p.get('protocol') or 'openai'))
    return out


def probe_one(provider_id: str, model: str, prefix: str, gap: float,
              arm: bool) -> dict:
    """Send the prefix twice through ``provider_id``; report send #2's read."""
    plan = {'provider': provider_id, 'model': model,
            'prefix_chars': len(prefix), 'gap_s': gap}
    if not arm:
        plan['status'] = 'DRY-RUN (not sent)'
        return plan

    from lib.llm_dispatch import dispatch_chat

    messages = [
        {'role': 'user', 'content': prefix + '\n\nReply with the single word OK.'},
    ]

    reads = []
    for send in (1, 2):
        if send == 2:
            time.sleep(gap)
        try:
            resp = dispatch_chat(
                messages=messages, model=model, provider_id=provider_id,
                max_tokens=16, cache=True)
        except Exception as e:
            plan['status'] = f'send #{send} failed: {type(e).__name__}: {e}'
            return plan
        usage = (resp or {}).get('usage') or {}
        from lib.cost import normalize_usage
        u = normalize_usage(usage)
        reads.append({'send': send, 'cache_read': u['cache_read'],
                      'cache_write': u['cache_write'], 'input': u['input']})

    plan['sends'] = reads
    second = reads[-1]
    plan['verdict'] = ('REUSED' if second['cache_read'] > 0
                       else 'NOT REUSED (cache_read=0)')
    plan['status'] = 'sent'
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--arm', action='store_true',
                    help='ACTUALLY send. Costs real tokens and, on the OAuth '
                         'path, subscription quota. Requires a human decision.')
    ap.add_argument('--gap', type=float, default=40.0,
                    help='seconds between the two sends (default 40, matching '
                         'the observed upstream_identical p50 of 38.7s)')
    ap.add_argument('--model', default='claude-opus-5')
    ap.add_argument('--only', default='',
                    help='restrict to one provider id')
    args = ap.parse_args(argv)

    prefix = build_synthetic_prefix()
    paths = _paths_from_config()
    if args.only:
        paths = [p for p in paths if p[0] == args.only]
    if not paths:
        print('no enabled providers matched', file=sys.stderr)
        return 1

    print(f'synthetic prefix: {len(prefix):,} chars '
          f'(~{len(prefix)//_CHARS_PER_TOKEN:,} tokens, deterministic)')
    print(f'gap between sends: {args.gap}s')
    print(f'armed: {args.arm}')
    if not args.arm:
        print('\n*** DRY RUN — nothing will be sent. Re-run with --arm once a '
              'human has approved spending the tokens. ***')
    print()

    for pid, base, proto in paths:
        print(f'--- {pid}  ({proto} @ {base})')
        res = probe_one(pid, args.model, prefix, args.gap, args.arm)
        for k, v in res.items():
            if k == 'sends':
                for s in v:
                    print(f'    send #{s["send"]}: cache_read={s["cache_read"]:,} '
                          f'cache_write={s["cache_write"]:,} input={s["input"]:,}')
            else:
                print(f'    {k}: {v}')
        print()

    if args.arm:
        print('Interpretation: a path that caches correctly reads back ~the '
              'whole prefix on send #2.\nIf BOTH paths reused, see the '
              'size-dependence caveat in this file\'s docstring — a 4k null '
              'result does NOT clear the gateway.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
