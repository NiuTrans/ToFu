#!/usr/bin/env python3
"""Probe aws.claude-opus-4.7-nova04 with both example-corp keys.

Per memory `claude-opus-4.7-breaking-changes`: do NOT send sampling params
(temperature/top_p/top_k) on 4.7+ — they may return HTTP 400.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error


KEYS = {
    'key0': 'YOUR_API_KEY_HERE',
    'key1': 'YOUR_API_KEY_HERE',
}
BASE_URL = 'https://api.openai.com/v1'
MODEL = 'aws.claude-opus-4.7-nova04'

os.environ.setdefault('NO_PROXY', '.internal.example.com')


def hit(api_key: str, *, with_thinking: bool) -> tuple[int, float, str, dict]:
    url = BASE_URL.rstrip('/') + '/chat/completions'
    body: dict = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': 'Reply with just OK.'}],
        'max_tokens': 32,
        'stream': False,
    }
    if with_thinking:
        body['thinking'] = {'type': 'adaptive', 'display': 'summarized'}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'X-Custom-Header': 'your-header-value',
        },
        method='POST',
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            elapsed = time.time() - t0
            data = json.loads(resp.read().decode('utf-8'))
            content = ((data.get('choices') or [{}])[0].get('message') or {}).get('content', '')
            preview = (content or '<empty>').replace('\n', ' ')[:80]
            return resp.getcode(), elapsed, preview, data.get('usage') or {}
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        try:
            body_txt = e.read().decode('utf-8', errors='replace')
        except Exception:
            body_txt = ''
        return e.code, elapsed, body_txt[:240].replace('\n', ' '), {}


def main() -> None:
    print(f'Probing {MODEL}\n')
    for thinking in (False, True):
        label = 'thinking=adaptive+summarized' if thinking else 'plain (no thinking)'
        print(f'--- {label} ---')
        print(f'{"key":<5}  {"status":>6}  {"elapsed":>8}  result')
        print('-' * 100)
        for key_name, api_key in KEYS.items():
            status, elapsed, msg, usage = hit(api_key, with_thinking=thinking)
            marker = '✓' if status == 200 else '✗'
            print(f'{key_name:<5}  {status:>6}  {elapsed:>7.2f}s  {marker} {msg}')
            if usage:
                print(f'       usage: prompt={usage.get("prompt_tokens")} '
                      f'completion={usage.get("completion_tokens")}')
            time.sleep(0.5)
        print()


if __name__ == '__main__':
    main()
