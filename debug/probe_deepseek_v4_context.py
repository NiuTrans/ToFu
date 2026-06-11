#!/usr/bin/env python3
"""probe_deepseek_v4_context.py — empirically find the REAL context ceiling
of ``deepseek-v4-pro`` on the sankuai gateway.

Background
----------
``data/config/server_config.json`` carries a *learned* override
``"sankuai::deepseek-v4-pro": 200278`` that beats the 1M static preset.
That 200278 was learned from a single HTTP-400 PromptTooLongError. This
script tests whether the gateway really rejects >200K, or whether the
model genuinely accepts up to its advertised 1M context.

Strategy
--------
Send escalating single-turn requests (needle-in-a-haystack so we can also
confirm the model actually READ the long context) and print, for each
size, the HTTP code + the gateway-reported ``prompt_tokens``. We stop at
the first hard rejection (or after the largest size succeeds).

Config is read straight from ``data/config/server_config.json`` so the
probe uses the exact same base_url / api_key / extra headers as the app.
``deepseek-v4-pro`` is disabled on key index 1, so we use key index 0.

Usage:
    python debug/probe_deepseek_v4_context.py
    python debug/probe_deepseek_v4_context.py --model=deepseek-v4-flash
    python debug/probe_deepseek_v4_context.py --sizes=250000,500000,1000000
"""

import json
import os
import sys
import time
import uuid

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CFG_PATH = os.path.join(_ROOT, 'data', 'config', 'server_config.json')

NO_PROXY = {'http': '', 'https': '', 'no_proxy': '*'}


def load_provider(provider_id='sankuai'):
    with open(_CFG_PATH) as f:
        cfg = json.load(f)
    for p in cfg.get('providers', []):
        if p.get('id') == provider_id:
            return p
    raise RuntimeError(f'provider {provider_id!r} not found in {_CFG_PATH}')


def pick_key(provider, model):
    """Return the first api_key index NOT disabled for *model*."""
    keys = provider.get('api_keys', [])
    for idx, key in enumerate(keys):
        disabled = (provider.get('models') and any(
            (m.get('model_id') == model or model in (m.get('aliases') or []))
            and str(idx) in (m.get('key_access') or {})
            and model in (m['key_access'][str(idx)].get('disabled_ids') or [])
            for m in provider['models']
        ))
        if not disabled:
            return idx, key.strip()
    # Fallback: first key
    return 0, keys[0].strip()


def make_padding(target_tokens: int) -> str:
    """~target_tokens tokens of varied English text (≈4 chars/token)."""
    target_chars = target_tokens * 4
    paragraphs = [
        "Section {n}: The quick brown fox jumps over the lazy dog. "
        "Pack my box with five dozen liquor jugs. How vexingly quick daft "
        "zebras jump. The five boxing wizards jump quickly. [ref-{n}]",
        "Chapter {n}: Lorem ipsum dolor sit amet, consectetur adipiscing "
        "elit. Sed do eiusmod tempor incididunt ut labore et dolore magna "
        "aliqua. Ut enim ad minim veniam, quis nostrud. [data-{n}]",
        "Article {n}: In a hole in the ground there lived a hobbit. Not a "
        "nasty, dirty, wet hole, filled with the ends of worms and an oozy "
        "smell. It was a hobbit-hole, and that means comfort. [item-{n}]",
    ]
    chunks = []
    n = 0
    total = 0
    while total < target_chars:
        p = paragraphs[n % len(paragraphs)].replace('{n}', str(n))
        chunks.append(p)
        total += len(p) + 1
        n += 1
    return '\n'.join(chunks)


def probe(base_url, api_key, extra_headers, model, target_tokens):
    needle = 'magic_password'
    needle_value = f'unicorn-{uuid.uuid4().hex[:8]}'
    padding = make_padding(target_tokens)
    needle_text = f'\n\n[SECRET NEEDLE] The {needle} is: {needle_value}\n\n'
    user_content = (
        f"I'm giving you a long document. Somewhere in it is a secret needle "
        f"with a value for '{needle}'. Read carefully.\n\n"
        f"--- DOCUMENT START ---\n{needle_text}{padding}\n--- DOCUMENT END ---\n\n"
        f"Question: What is the value of '{needle}'? Reply with ONLY the value."
    )
    messages = [{'role': 'user', 'content': user_content}]
    est_input = len(user_content) // 4

    body = {
        'model': model,
        'messages': messages,
        'max_tokens': 64,
        'temperature': 0,
        'stream': False,
    }
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'M-TraceId': uuid.uuid4().hex,
    }
    headers.update(extra_headers or {})

    print(f"\n{'='*72}")
    print(f"  model={model}  target≈{target_tokens:,} tok  "
          f"(payload {len(user_content):,} chars ≈ {est_input:,} tok est)")
    print(f"{'='*72}")
    t0 = time.time()
    try:
        resp = requests.post(
            f'{base_url}/chat/completions', headers=headers, json=body,
            timeout=(60, 600), proxies=NO_PROXY,
        )
    except Exception as e:
        print(f"  💥 request exception after {time.time()-t0:.1f}s: {e}")
        return {'target': target_tokens, 'status': 'EXCEPTION', 'error': str(e)}
    elapsed = time.time() - t0
    print(f"  HTTP {resp.status_code}  ({elapsed:.1f}s)")

    if resp.status_code == 200:
        data = resp.json()
        usage = data.get('usage', {}) or {}
        ch = data.get('choices', [])
        content = ch[0]['message'].get('content', '') if ch else ''
        ptok = usage.get('prompt_tokens', 0)
        ctok = usage.get('completion_tokens', 0)
        found = needle_value.lower() in (content or '').lower()
        print(f"  ✅ SUCCESS  prompt_tokens={ptok:,}  completion_tokens={ctok:,}")
        print(f"     reply: {content[:120]!r}")
        print(f"     needle: {'✅ FOUND' if found else '❌ NOT found'} "
              f"(expected {needle_value})")
        return {'target': target_tokens, 'status': 'SUCCESS', 'http': 200,
                'prompt_tokens': ptok, 'needle_found': found}
    else:
        err = resp.text[:600]
        print(f"  ❌ REJECTED  body: {err}")
        return {'target': target_tokens, 'status': 'REJECTED',
                'http': resp.status_code, 'error': err}


def main():
    model = 'deepseek-v4-pro'
    sizes = [180_000, 250_000, 400_000, 600_000, 1_000_000]
    for arg in sys.argv[1:]:
        if arg.startswith('--model='):
            model = arg.split('=', 1)[1]
        elif arg.startswith('--sizes='):
            sizes = [int(x) for x in arg.split('=', 1)[1].split(',')]

    provider = load_provider('sankuai')
    base_url = provider['base_url']
    extra_headers = provider.get('extra_headers') or {}
    key_idx, api_key = pick_key(provider, model)

    print('=' * 72)
    print('  DeepSeek V4 context-ceiling probe')
    print(f'  gateway : {base_url}')
    print(f'  model   : {model}')
    print(f'  key idx : {key_idx} (…{api_key[-4:]})')
    print(f'  learned override in config: '
          f'{json.load(open(_CFG_PATH)).get("model_context_limits", {}).get(f"sankuai::{model}")}')
    print(f'  sizes   : {", ".join(f"{s:,}" for s in sizes)}')
    print('=' * 72)

    results = []
    for sz in sizes:
        r = probe(base_url, api_key, extra_headers, model, sz)
        results.append(r)
        if r['status'] == 'REJECTED':
            print('\n  → first rejection reached; stopping escalation.')
            break
        if r['status'] == 'EXCEPTION':
            print('\n  → request error; stopping.')
            break

    print('\n' + '=' * 72)
    print('  SUMMARY')
    print('=' * 72)
    print(f"  {'target':>12} {'status':<10} {'http':<6} {'prompt_tokens':<14} needle")
    for r in results:
        ptok = f"{r['prompt_tokens']:,}" if r.get('prompt_tokens') else '-'
        print(f"  {r['target']:>12,} {r['status']:<10} "
              f"{str(r.get('http', '-')):<6} {ptok:<14} "
              f"{r.get('needle_found', '-')}")
    last_ok = max((r['prompt_tokens'] for r in results
                   if r['status'] == 'SUCCESS' and r.get('prompt_tokens')),
                  default=None)
    print()
    if last_ok:
        print(f"  ✅ Largest accepted prompt: {last_ok:,} tokens (gateway-counted)")
    rej = next((r for r in results if r['status'] == 'REJECTED'), None)
    if rej:
        print(f"  ❌ Rejected at target {rej['target']:,} (HTTP {rej['http']})")
        print(f"     → real ceiling is between the last success and {rej['target']:,}")
    else:
        print("  🎉 No rejection across all tested sizes — the 200278 override "
              "looks STALE; the model accepts far more.")
    print()


if __name__ == '__main__':
    main()
