#!/usr/bin/env python3
"""Hit aws.claude-opus-4.7 and vertex.claude-opus-4.7 with BOTH keys.

Aim:
  - Confirm vertex.claude-opus-4.7 is a real gateway model (not just an alias).
  - Confirm whether the 429s on key1:4.7 are transient RPM contention with
    the running app, or persistent on a fresh manual request.
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
MODELS = ['aws.claude-opus-4.7', 'vertex.claude-opus-4.7']

os.environ.setdefault('NO_PROXY', '.internal.example.com')


def hit(api_key: str, model: str) -> tuple[int, float, str]:
    url = BASE_URL.rstrip('/') + '/chat/completions'
    # NOTE: opus-4.7 rejects sampling params (temperature/top_p) with HTTP 400
    # — see memory `claude-opus-4.7-breaking-changes`. Send only required fields.
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Reply with just OK.'}],
        'max_tokens': 16,
        'stream': False,
    }
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
            return resp.getcode(), elapsed, (content or '<empty>').replace('\n', ' ')[:80]
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        try:
            body_txt = e.read().decode('utf-8', errors='replace')
        except Exception:
            body_txt = ''
        return e.code, elapsed, body_txt[:200].replace('\n', ' ')


def main() -> None:
    print(f'{"key":<5}  {"model":<26}  {"status":>6}  {"elapsed":>8}  result')
    print('-' * 100)
    for key_name, api_key in KEYS.items():
        for model in MODELS:
            status, elapsed, msg = hit(api_key, model)
            marker = '✓' if status == 200 else '✗'
            print(f'{key_name:<5}  {model:<26}  {status:>6}  {elapsed:>7.2f}s  {marker} {msg}')
            time.sleep(0.5)


if __name__ == '__main__':
    main()
