#!/usr/bin/env python3
"""probe_gemini_thinking.py — Discover the correct thinking dialect for Gemini 3.x.

For each candidate request-body shape, stream a reasoning-heavy prompt and
report whether the gateway emitted any ``reasoning_content`` (i.e. surfaced
thinking) and how long it was. This tells us empirically which parameter the
sankuai OpenAI-compat gateway honors for gemini-3.5-flash / gemini-3-flash,
and crucially whether thoughts can be SURFACED (include_thoughts).

Usage:
    python debug/probe_gemini_thinking.py [model]
"""

import json, os, sys, time, logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from lib import LLM_API_KEYS, LLM_BASE_URL

CHAT_URL = f'{LLM_BASE_URL}/chat/completions'
KEYS = {f'key_{i}': k for i, k in enumerate(LLM_API_KEYS) if k}

PROMPT = 'What is 17 * 23? Show your reasoning step by step before the answer.'


def _headers(api_key):
    return {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}


def _no_proxy():
    return {'http': None, 'https': None}


def run_variant(api_key, model, name, extra, timeout=120):
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': PROMPT}],
        'max_tokens': 2048,
        'stream': True,
    }
    body.update(extra)
    t0 = time.time()
    thinking = ''
    content = ''
    err = None
    saw_keys = set()
    try:
        resp = requests.post(CHAT_URL, headers=_headers(api_key), json=body,
                             timeout=timeout, stream=True, proxies=_no_proxy())
        if resp.status_code != 200:
            return {'name': name, 'extra': extra, 'ok': False,
                    'http': resp.status_code, 'detail': resp.text[:300]}
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data: '):
                continue
            payload = line[6:].strip()
            if payload == '[DONE]':
                break
            try:
                chunk = json.loads(payload)
                delta = chunk.get('choices', [{}])[0].get('delta', {})
                for k in delta:
                    saw_keys.add(k)
                rc = (delta.get('reasoning_content') or delta.get('thinking')
                      or delta.get('reasoning') or '')
                if isinstance(rc, str) and rc:
                    thinking += rc
                cd = delta.get('content') or ''
                if isinstance(cd, str) and cd:
                    content += cd
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                logger.debug('skip chunk: %s', e)
        resp.close()
    except Exception as e:
        err = str(e)
    lat = (time.time() - t0) * 1000
    return {
        'name': name, 'extra': extra, 'ok': err is None,
        'http': 200 if err is None else None, 'error': err,
        'latency_ms': round(lat),
        'thinking_len': len(thinking), 'content_len': len(content),
        'delta_keys': sorted(saw_keys),
        'thinking_preview': thinking[:160], 'content_preview': content[:160],
    }


VARIANTS = [
    ('reasoning_effort=high',         {'reasoning_effort': 'high'}),
    ('thinking incl_thoughts high',
     {'thinking': {'thinking_level': 'high', 'include_thoughts': True}}),
    ('extra_body google thinking_config',
     {'extra_body': {'google': {'thinking_config': {'thinking_level': 'high', 'include_thoughts': True}}}}),
    ('thinking type=enabled',         {'thinking': {'type': 'enabled'}}),
    ('reasoning effort obj',          {'reasoning': {'effort': 'high'}}),
    ('include_reasoning=True',        {'include_reasoning': True}),
]


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else 'gemini-3.5-flash'
    if not KEYS:
        print('No API keys configured.')
        return
    key_items = list(KEYS.items())
    print(f'Probing model={model} via {CHAT_URL} keys={[k for k,_ in key_items]}\n')
    results = []
    for i, (name, extra) in enumerate(VARIANTS):
        # rotate keys to dodge per-key RPM caps
        key_name, api_key = key_items[i % len(key_items)]
        r = run_variant(api_key, model, name, extra)
        r['key'] = key_name
        results.append(r)
        if r['ok']:
            print(f'  [{key_name}] {name:<34} think={r["thinking_len"]:>5}c  '
                  f'content={r["content_len"]:>5}c  {r["latency_ms"]}ms  '
                  f'keys={r["delta_keys"]}')
        else:
            print(f'  [{key_name}] {name:<34} ERROR http={r.get("http")} '
                  f'{(r.get("error") or r.get("detail"))[:120]}')
        time.sleep(8.0)
    out = os.path.join(os.path.dirname(__file__), 'probe_gemini_thinking.json')
    with open(out, 'w') as f:
        json.dump({'model': model, 'results': results}, f, indent=2, ensure_ascii=False)
    print(f'\nSaved {out}')


if __name__ == '__main__':
    main()
