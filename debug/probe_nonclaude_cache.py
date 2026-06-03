#!/usr/bin/env python3
"""Probe: does the YourProvider/example-corp gateway honor cache_control on
non-Claude models (MiniMax / GLM / Doubao / Qwen / DeepSeek / etc.)?

Motivation
----------
Our SWE-bench results showed tofu-minimax had 0% cache hit rate while
cc-minimax (same model, same gateway) hit 57.9%. The cause is that
``lib.llm.add_cache_breakpoints`` bails out unless
``is_claude(model)`` is true — so for non-Claude models we send NO
``cache_control`` markers and thus never cache anything client-side.

The example-corp gateway has historically been documented as an Anthropic
prompt-caching bridge for Claude models. But since Claude Code proxies
non-Claude models through the SAME gateway and DOES get cache hits
(see the ``claude-code-proxy-cache-control-passthrough`` memory note),
the gateway must support ephemeral cache markers for at least some
non-Claude models.

This script tests that hypothesis directly, per-model.

Methodology
-----------
For each model under test we run TWO arms sequentially (same key, same
gateway), each arm sends the SAME large-ish system+user payload TWICE:

  Arm A (control)  — no ``cache_control`` anywhere in the body.
  Arm B (treatment) — ``cache_control: {type: "ephemeral"}`` attached to
                      the system block and the last user content block.

Between requests we wait a few seconds. For each response we harvest
the usage dict and look for:

  • ``cache_read_tokens`` (example-corp convention, top-level in usage)
  • ``cache_creation_input_tokens`` / ``cache_write_tokens``
  • ``prompt_tokens_details.cached_tokens`` (OpenAI convention)

Interpretation
--------------
  • Arm A req2 cache_read>0  → gateway does automatic prefix caching for
                               this model, no client markers needed.
  • Arm B req2 cache_read>0 but Arm A req2 cache_read=0
                             → markers are REQUIRED and the gateway
                               honors them → we can enable by extending
                               ``add_cache_breakpoints`` to this model.
  • Both arms req2 cache_read=0 → gateway doesn't support caching for
                                  this model, skip.

Run::
    python3 debug/probe_nonclaude_cache.py --model MiniMax-M2.7
    python3 debug/probe_nonclaude_cache.py --all
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
from dataclasses import dataclass, asdict, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger

logger = get_logger(__name__)

# ─── Config ────────────────────────────────────────────────────────────

SANKUAI_URL = os.environ.get(
    'TOFU_PROBE_BASE_URL', 'https://api.openai.com/v1',
).rstrip('/') + '/chat/completions'
SANKUAI_KEYS = [k for k in os.environ.get('TOFU_PROBE_API_KEYS', '').split(',') if k]
SANKUAI_HEADERS_BASE = {
    'Content-Type': 'application/json',
    **json.loads(os.environ.get('TOFU_PROBE_EXTRA_HEADERS', '{}')),
}

# Models under test. ``kind`` is used to derive any model-specific
# body tweaks (e.g. MiniMax reasoning_split).
MODELS_UNDER_TEST = [
    # (model_id, kind, notes)
    ('MiniMax-M2.7',              'minimax', 'SWE-bench underdog; 0% hit under Tofu'),
    ('MiniMax-M2.5',              'minimax', 'Same family, different version'),
    ('glm-5.1',                   'glm',     'Another example-corp-bridged model'),
    ('Doubao-Seed-2.0-pro',       'doubao',  'Volcengine; check if example-corp caches'),
    ('qwen3.5-plus',              'qwen',    'Qwen-Plus via example-corp'),
    ('deepseek-v4-flash',         'deepseek','DeepSeek via example-corp'),
    ('LongCat-Flash-Thinking-2601','longcat','LongCat — cost 0, but tokens count'),
]

# Baseline: Claude as the known-working positive control.
CLAUDE_MODEL = 'aws.claude-opus-4.6'


def _big_system_prompt() -> str:
    """A ~4-5K token block of stable text, well above Anthropic's
    minimum cacheable prefix (Opus/Haiku=4096, Sonnet=1024).
    Most non-Claude models likely have similar thresholds."""
    # Use the real CLAUDE.md content as the bulk of the prompt — it's
    # stable and large, which is what caching is designed for.
    try:
        with open('CLAUDE.md', 'r', encoding='utf-8') as f:
            claude_md = f.read()
    except OSError:
        claude_md = ('You are a careful senior engineer. ' * 200)
    # Prepend a fixed preamble so the system block is self-contained.
    return (
        'You are a code-review assistant. Below is the project\'s '
        'coding standard. Refer to it when answering.\n\n'
        '=========== PROJECT STANDARD ===========\n'
        + claude_md
        + '\n=========== END PROJECT STANDARD ===========\n'
    )


USER_Q1 = 'What is the #1 rule of the project logging discipline?'
USER_Q2 = USER_Q1  # exact same question so request 2 is byte-identical


# ─── Body builders ─────────────────────────────────────────────────────

def _build_body(model: str, *, kind: str, with_cache_control: bool,
                user_msg: str, system_prompt: str) -> dict:
    """Build a minimal chat-completions body.

    When ``with_cache_control`` is True, we attach ``cache_control: ephemeral``
    to:
      - the system block (BP1: stable prefix)
      - the last user block (BP2: tail)

    This mirrors what ``lib.llm.add_cache_breakpoints`` does for
    Claude — just without the ``is_claude`` gate.
    """
    if with_cache_control:
        system_content: list | str = [{
            'type': 'text',
            'text': system_prompt,
            'cache_control': {'type': 'ephemeral'},
        }]
        user_content: list | str = [{
            'type': 'text',
            'text': user_msg,
            'cache_control': {'type': 'ephemeral'},
        }]
    else:
        system_content = system_prompt
        user_content = user_msg

    body: dict = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_content},
            {'role': 'user',   'content': user_content},
        ],
        'temperature': 0.0,
        'max_tokens': 128,
        'stream': False,
    }

    # Kind-specific tweaks so the model doesn't refuse / think excessively.
    if kind == 'minimax':
        # MiniMax supports reasoning_split to separate <think> output
        body['reasoning_split'] = True
    # We intentionally do NOT send enable_thinking / thinking toggles —
    # we want the cheapest, most-cache-friendly request shape.
    return body


def _extract_usage(usage: dict) -> dict:
    """Harvest all cache-relevant fields from a usage object, defensively."""
    details = (usage.get('prompt_tokens_details') or {})
    cache_creation = (usage.get('cache_creation') or {})
    return {
        'prompt_tokens':       usage.get('prompt_tokens', 0),
        'completion_tokens':   usage.get('completion_tokens', 0),
        'total_tokens':        usage.get('total_tokens', 0),
        # example-corp convention (top-level)
        'cache_read_tokens':   usage.get('cache_read_tokens', 0),
        'cache_write_tokens':  usage.get('cache_write_tokens', 0),
        # Anthropic convention (underscored)
        'cache_read_input_tokens':      usage.get('cache_read_input_tokens', 0),
        'cache_creation_input_tokens':  usage.get('cache_creation_input_tokens', 0),
        # OpenAI convention (nested)
        'cached_tokens_openai': details.get('cached_tokens', 0),
        # Anthropic nested (creation breakdown)
        'ephemeral_5m_input_tokens': cache_creation.get('ephemeral_5m_input_tokens', 0),
        'ephemeral_1h_input_tokens': cache_creation.get('ephemeral_1h_input_tokens', 0),
    }


def _any_cache_read(u: dict) -> int:
    return max(u['cache_read_tokens'],
               u['cache_read_input_tokens'],
               u['cached_tokens_openai'])


def _any_cache_write(u: dict) -> int:
    return max(u['cache_write_tokens'],
               u['cache_creation_input_tokens'],
               u['ephemeral_5m_input_tokens'] + u['ephemeral_1h_input_tokens'])


# ─── HTTP ──────────────────────────────────────────────────────────────

@dataclass
class RespRec:
    req_idx: int
    arm: str
    elapsed: float = 0.0
    http_status: int = 0
    error: str = ''
    usage_raw: dict = field(default_factory=dict)
    usage_normalized: dict = field(default_factory=dict)
    content_preview: str = ''


def _post(body: dict, *, api_key: str, timeout: float = 90.0) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    headers = dict(SANKUAI_HEADERS_BASE)
    headers['Authorization'] = f'Bearer {api_key}'
    req = urllib.request.Request(
        SANKUAI_URL, data=data, headers=headers, method='POST',
    )
    # example-corp.com is in the proxy bypass list — don't send the corporate proxy
    os.environ.setdefault('NO_PROXY', '.internal.example.com')

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body_bytes = resp.read()
        obj = json.loads(body_bytes.decode('utf-8'))
        return status, obj
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8', errors='replace')
        except Exception:
            err_body = ''
        return e.code, {'error': err_body[:500]}


# ─── Arm runner ────────────────────────────────────────────────────────

def _run_arm(model: str, *, kind: str, with_cc: bool, api_key: str,
             system_prompt: str, pause_s: float = 3.0) -> list[RespRec]:
    """Run one arm: 2 sequential requests with the same body."""
    arm_label = 'WITH_CC' if with_cc else 'NO_CC'
    recs: list[RespRec] = []

    for req_idx in (1, 2):
        body = _build_body(
            model, kind=kind, with_cache_control=with_cc,
            user_msg=USER_Q1 if req_idx == 1 else USER_Q2,
            system_prompt=system_prompt,
        )
        t0 = time.time()
        status, obj = _post(body, api_key=api_key)
        elapsed = time.time() - t0
        rec = RespRec(req_idx=req_idx, arm=arm_label,
                      elapsed=elapsed, http_status=status)
        if 'error' in obj and status >= 400:
            rec.error = str(obj.get('error'))[:300]
        else:
            usage = obj.get('usage') or {}
            rec.usage_raw = usage
            rec.usage_normalized = _extract_usage(usage)
            try:
                choice = (obj.get('choices') or [{}])[0]
                msg = choice.get('message') or {}
                rec.content_preview = (msg.get('content') or '')[:120]
            except Exception as e:
                logger.debug('content preview extract failed: %s', e)

        u = rec.usage_normalized or {}
        cr = _any_cache_read(u) if u else 0
        cw = _any_cache_write(u) if u else 0
        print(f"    [{arm_label:<7}] req{req_idx} "
              f"status={status} elapsed={elapsed:.1f}s "
              f"pt={u.get('prompt_tokens',0):,} "
              f"cache_read={cr:,} cache_write={cw:,} "
              f"out={u.get('completion_tokens',0)}"
              f"{' ERR:'+rec.error if rec.error else ''}")
        recs.append(rec)
        if req_idx == 1:
            time.sleep(pause_s)

    return recs


# ─── Report ────────────────────────────────────────────────────────────

def _verdict(arm_a: list[RespRec], arm_b: list[RespRec]) -> str:
    """Classify the model based on the two arms' second-request cache_read."""
    if len(arm_a) < 2 or len(arm_b) < 2:
        return 'INCONCLUSIVE (missing requests)'
    a2 = _any_cache_read(arm_a[1].usage_normalized) if arm_a[1].usage_normalized else 0
    b2 = _any_cache_read(arm_b[1].usage_normalized) if arm_b[1].usage_normalized else 0
    a2_err, b2_err = arm_a[1].error, arm_b[1].error

    if a2_err and b2_err:
        return f'BOTH FAILED ({a2_err[:40]} / {b2_err[:40]})'
    if a2 > 500 and b2 > 500:
        return 'AUTO-CACHING (gateway caches without markers)'
    if a2 <= 500 and b2 > 500:
        return '✅ MARKERS REQUIRED & HONORED (safe to enable in add_cache_breakpoints)'
    if a2 > 500 and b2 <= 500:
        return '⚠️ AUTO-CACHES BUT MARKERS BREAK IT (do not add markers!)'
    return '❌ NO CACHING (gateway does not cache this model)'


def _print_report(results: dict):
    print()
    print('━' * 72)
    print('  NON-CLAUDE CACHE PROBE — RESULTS')
    print('━' * 72)
    for model, data in results.items():
        print(f"\n▸ {model}  ({data.get('kind','?')})")
        for arm_name in ('NO_CC', 'WITH_CC'):
            recs = data.get(arm_name, [])
            for rec in recs:
                u = rec.usage_normalized or {}
                cr = _any_cache_read(u) if u else 0
                cw = _any_cache_write(u) if u else 0
                pt = u.get('prompt_tokens', 0)
                out = u.get('completion_tokens', 0)
                print(f"    {arm_name:<7} req{rec.req_idx}  "
                      f"pt={pt:>6,} cr={cr:>6,} cw={cw:>6,} out={out:>3}"
                      f"  {('✗ ' + rec.error[:60]) if rec.error else ''}")
        print(f"  ▸ VERDICT: {data.get('verdict', 'N/A')}")
    print()
    print('━' * 72)


def _save(results: dict, path: str):
    serializable = {}
    for model, data in results.items():
        serializable[model] = {
            'kind': data.get('kind'),
            'verdict': data.get('verdict'),
            'NO_CC':   [asdict(r) for r in data.get('NO_CC', [])],
            'WITH_CC': [asdict(r) for r in data.get('WITH_CC', [])],
        }
    with open(path, 'w') as f:
        json.dump(serializable, f, indent=2, default=str)
    print(f"📁 Saved detailed results to {path}")


# ─── Main ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--model', help='Single model to test (shortcut for --all over 1)')
    ap.add_argument('--all', action='store_true', help='Test all models')
    ap.add_argument('--include-claude', action='store_true',
                    help='Also run Claude as a known-good positive control')
    ap.add_argument('--key', type=int, default=0, choices=[0, 1],
                    help='Which example-corp key to use (default 0)')
    ap.add_argument('--pause', type=float, default=3.0,
                    help='Seconds between req1 and req2 within an arm')
    ap.add_argument('--between-arms', type=float, default=5.0,
                    help='Seconds between arms (same model)')
    args = ap.parse_args()

    if not args.model and not args.all:
        ap.error('Pass --model MODEL_ID or --all')

    api_key = SANKUAI_KEYS[args.key]
    system_prompt = _big_system_prompt()
    sys_tok_est = len(system_prompt) // 4
    print(f"\n  sys_prompt: {len(system_prompt):,} chars (~{sys_tok_est:,} tokens)")
    print(f"  using example-corp key_{args.key}")
    print(f"  pause_within_arm={args.pause}s  pause_between_arms={args.between_arms}s")

    to_run: list[tuple[str, str]] = []
    if args.all:
        to_run = [(m, k) for (m, k, _) in MODELS_UNDER_TEST]
    if args.model:
        # Try to find kind from the table; fall back to 'other'.
        kind = next((k for (m, k, _) in MODELS_UNDER_TEST if m == args.model), 'other')
        to_run.append((args.model, kind))
    if args.include_claude:
        to_run.insert(0, (CLAUDE_MODEL, 'claude'))

    results: dict = {}

    for model, kind in to_run:
        print(f"\n━━━━ {model} ({kind}) ━━━━")
        try:
            print("  ▸ Arm A: NO cache_control markers")
            arm_a = _run_arm(model, kind=kind, with_cc=False,
                             api_key=api_key, system_prompt=system_prompt,
                             pause_s=args.pause)
            time.sleep(args.between_arms)
            print("  ▸ Arm B: WITH cache_control markers")
            arm_b = _run_arm(model, kind=kind, with_cc=True,
                             api_key=api_key, system_prompt=system_prompt,
                             pause_s=args.pause)
        except Exception as e:
            logger.error('Model %s probe crashed: %s', model, e, exc_info=True)
            results[model] = {
                'kind': kind, 'NO_CC': [], 'WITH_CC': [],
                'verdict': f'CRASHED: {e}',
            }
            continue

        verdict = _verdict(arm_a, arm_b)
        results[model] = {
            'kind': kind, 'NO_CC': arm_a, 'WITH_CC': arm_b,
            'verdict': verdict,
        }
        print(f"  ▸ VERDICT: {verdict}")
        # Pause between models to avoid cross-model RPM pressure.
        time.sleep(args.between_arms)

    _print_report(results)

    ts = time.strftime('%Y%m%d_%H%M%S')
    _save(results, f'debug/nonclaude_cache_probe_{ts}.json')


if __name__ == '__main__':
    main()
