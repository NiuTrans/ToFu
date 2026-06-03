#!/usr/bin/env python3
"""Probe whether key1 429s are per-model quota or per-key quota.

For each model, fire ONE non-stream call and report HTTP status,
elapsed seconds, and (on 429) the gateway error message.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error


API_KEY = 'YOUR_API_KEY_HERE'
BASE_URL = 'https://api.openai.com/v1'

# Mix: the model we just exercised, project's main model, sister models.
MODELS = [
    'aws.claude-opus-4.6-b',
    'aws.claude-opus-4.6',
    'vertex.claude-opus-4.6',
    'aws.claude-opus-4.7',
    'aws.claude-opus-4.7-b',
    'vertex.claude-opus-4.7',
    'aws.claude-sonnet-4.6',
    'gemini-2.5-flash',
    'deepseek-v4-flash',
    'gpt-4.1-mini',
]

os.environ.setdefault('NO_PROXY', '.internal.example.com')


def hit(model: str) -> tuple[int, float, str]:
    url = BASE_URL.rstrip('/') + '/chat/completions'
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Reply with just OK.'}],
        'temperature': 0.0,
        'max_tokens': 16,
        'stream': False,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {API_KEY}',
            'Content-Type': 'application/json',
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
        body_txt = ''
        try:
            body_txt = e.read().decode('utf-8', errors='replace')
        except Exception:
            pass
        return e.code, elapsed, body_txt[:200].replace('\n', ' ')
    except Exception as e:
        elapsed = time.time() - t0
        return -1, elapsed, f'{type(e).__name__}: {e}'


def main() -> None:
    print(f'Testing key=...{API_KEY[-4:]} on {len(MODELS)} models\n')
    print(f'{"model":<32}  {"status":>6}  {"elapsed":>8}  result')
    print('-' * 100)
    ok, fail = 0, 0
    for m in MODELS:
        status, elapsed, msg = hit(m)
        marker = '✓' if status == 200 else '✗'
        print(f'{m:<32}  {status:>6}  {elapsed:>7.2f}s  {marker} {msg}')
        if status == 200:
            ok += 1
        else:
            fail += 1
        time.sleep(0.5)
    print('-' * 100)
    print(f'Summary: {ok} OK, {fail} failed (out of {len(MODELS)})')


if __name__ == '__main__':
    main()
