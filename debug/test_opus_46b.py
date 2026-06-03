#!/usr/bin/env python3
"""Quick smoke test: call aws.claude-opus-4.6-b via the example-corp gateway.

Usage::
    python3 debug/test_opus_46b.py
    python3 debug/test_opus_46b.py --stream
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error


API_KEY = 'YOUR_API_KEY_HERE'
BASE_URL = 'https://api.openai.com/v1'
MODEL = 'aws.claude-opus-4.6-b'


def _request(stream: bool) -> None:
    url = BASE_URL.rstrip('/') + '/chat/completions'
    body = {
        'model': MODEL,
        'messages': [
            {'role': 'user', 'content': '用一句话介绍你自己，并说明你是哪个版本。'},
        ],
        'temperature': 0.0,
        'max_tokens': 256,
        'stream': stream,
    }
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/json',
    }
    if stream:
        headers['Accept'] = 'text/event-stream'

    # example-corp is on the proxy bypass list
    os.environ.setdefault('NO_PROXY', '.internal.example.com')

    print(f'→ POST {url}')
    print(f'  model={MODEL}  stream={stream}  key=...{API_KEY[-4:]}')
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status = resp.getcode()
            print(f'← HTTP {status}  ({time.time() - t0:.1f}s)')
            if not stream:
                obj = json.loads(resp.read().decode('utf-8'))
                _print_nonstream(obj)
            else:
                _print_stream(resp)
    except urllib.error.HTTPError as e:
        body_txt = ''
        try:
            body_txt = e.read().decode('utf-8', errors='replace')
        except Exception:
            pass
        print(f'✗ HTTP {e.code}: {e.reason}')
        print(f'  body: {body_txt[:1000]}')
    except urllib.error.URLError as e:
        print(f'✗ URLError: {e}')


def _print_nonstream(obj: dict) -> None:
    choice = (obj.get('choices') or [{}])[0]
    msg = choice.get('message') or {}
    content = msg.get('content') or ''
    usage = obj.get('usage') or {}
    print('--- content ---')
    print(content)
    print('--- usage ---')
    print(json.dumps(usage, ensure_ascii=False, indent=2))


def _print_stream(resp) -> None:
    print('--- streaming chunks ---')
    full = []
    for raw_line in resp:
        line = raw_line.decode('utf-8', errors='replace').rstrip('\n').rstrip('\r')
        if not line:
            continue
        # Sankuai sends 'data:{json}' (no space); accept both.
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            print(f'  (unparseable chunk: {payload[:120]})')
            continue
        delta = ((chunk.get('choices') or [{}])[0].get('delta') or {})
        piece = delta.get('content') or ''
        if piece:
            full.append(piece)
            sys.stdout.write(piece)
            sys.stdout.flush()
        usage = chunk.get('usage')
        if usage:
            print('\n--- usage ---')
            print(json.dumps(usage, ensure_ascii=False, indent=2))
    print(f'\n--- total chars: {sum(len(s) for s in full)} ---')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--stream', action='store_true', help='Use SSE streaming')
    args = ap.parse_args()
    _request(stream=args.stream)


if __name__ == '__main__':
    main()
