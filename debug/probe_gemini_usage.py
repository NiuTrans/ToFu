#!/usr/bin/env python3
"""probe_gemini_usage.py — Detect which param controls Gemini 3.x thinking depth.

Gemini bills internal reasoning as thinking/reasoning tokens that appear in the
``usage`` object even when the thought text is not streamed back. We send a
reasoning-heavy prompt non-streaming for several candidate param shapes and
dump the full ``usage`` so we can see which knob changes the reasoning-token
count (and thus actually reaches Vertex's thinkingLevel).

Usage:
    python debug/probe_gemini_usage.py [model]
"""

import json, os, sys, time, logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib import LLM_API_KEYS, LLM_BASE_URL

CHAT_URL = f'{LLM_BASE_URL}/chat/completions'
KEYS = {f'key_{i}': k for i, k in enumerate(LLM_API_KEYS) if k}

PROMPT = ('A snail climbs a 10 metre well. Each day it climbs 3 metres and each '
          'night slides back 2 metres. On which day does it reach the top? Think '
          'carefully step by step, then give the final answer.')


def _headers(api_key):
    return {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}


def _no_proxy():
    return {'http': None, 'https': None}


def run_variant(api_key, model, name, extra, timeout=120):
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': PROMPT}],
        'max_tokens': 4096,
        'stream': False,
    }
    body.update(extra)
    t0 = time.time()
    try:
        resp = requests.post(CHAT_URL, headers=_headers(api_key), json=body,
                             timeout=timeout, proxies=_no_proxy())
        lat = round((time.time() - t0) * 1000)
        if resp.status_code != 200:
            return {'name': name, 'ok': False, 'http': resp.status_code,
                    'detail': resp.text[:300], 'latency_ms': lat}
        data = resp.json()
        usage = data.get('usage', {})
        msg = data.get('choices', [{}])[0].get('message', {})
        content = msg.get('content') or ''
        reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
        return {'name': name, 'ok': True, 'latency_ms': lat, 'usage': usage,
                'content_len': len(content) if isinstance(content, str) else -1,
                'msg_reasoning_len': len(reasoning) if isinstance(reasoning, str) else -1,
                'msg_keys': sorted(msg.keys())}
    except Exception as e:
        return {'name': name, 'ok': False, 'error': str(e),
                'latency_ms': round((time.time() - t0) * 1000)}


# Compare minimal vs high for each candidate param to see which one
# moves the reasoning-token count in usage.
VARIANTS = [
    ('baseline',                  {}),
    ('reasoning_effort=minimal',  {'reasoning_effort': 'minimal'}),
    ('reasoning_effort=high',     {'reasoning_effort': 'high'}),
    ('thinking_level=minimal',    {'thinking': {'thinking_level': 'minimal'}}),
    ('thinking_level=high',       {'thinking': {'thinking_level': 'high'}}),
    ('top thinking_level=minimal',{'thinking_level': 'minimal'}),
    ('top thinking_level=high',   {'thinking_level': 'high'}),
]


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else 'gemini-3.5-flash'
    if not KEYS:
        print('No API keys configured.')
        return
    key_items = list(KEYS.items())
    print(f'Probing usage model={model} via {CHAT_URL}\n')
    results = []
    for i, (name, extra) in enumerate(VARIANTS):
        key_name, api_key = key_items[i % len(key_items)]
        r = run_variant(api_key, model, name, extra)
        r['key'] = key_name
        results.append(r)
        if r['ok']:
            print(f'  [{key_name}] {name:<28} usage={json.dumps(r["usage"])}  '
                  f'content={r["content_len"]}c  msg_reasoning={r["msg_reasoning_len"]}c')
        else:
            print(f'  [{key_name}] {name:<28} ERR http={r.get("http")} '
                  f'{(r.get("error") or r.get("detail") or "")[:120]}')
        time.sleep(7.0)
    out = os.path.join(os.path.dirname(__file__), 'probe_gemini_usage.json')
    with open(out, 'w') as f:
        json.dump({'model': model, 'results': results}, f, indent=2, ensure_ascii=False)
    print(f'\nSaved {out}')


if __name__ == '__main__':
    main()
