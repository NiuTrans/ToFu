#!/usr/bin/env python3
"""Probe aws.claude-opus-4.7-nova04 via the example-corp Anthropic-format endpoint.

Endpoint: https://api.openai.com/v1/anthropic/v1/messages
"""
import json
import os
import time
import urllib.request
import urllib.error


KEYS = {
    'key0': 'YOUR_API_KEY_HERE',
    'key1': 'YOUR_API_KEY_HERE',
}
BASE_URL = 'https://api.openai.com/v1/anthropic'
MODEL = 'aws.claude-opus-4.7-nova04'

os.environ.setdefault('NO_PROXY', '.internal.example.com')


def hit(api_key, thinking_obj):
    url = BASE_URL.rstrip('/') + '/v1/messages'
    body = {
        'model': MODEL,
        'max_tokens': 64,
        'messages': [{'role': 'user', 'content': 'Reply with just OK.'}],
    }
    if thinking_obj is not None:
        body['thinking'] = thinking_obj
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'x-api-key': api_key,
            'authorization': 'Bearer ' + api_key,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
            'X-Custom-Header': 'your-header-value',
        },
        method='POST',
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            elapsed = time.time() - t0
            raw = resp.read().decode('utf-8', errors='replace')
            try:
                obj = json.loads(raw)
            except Exception:
                obj = raw
            preview = str(obj)[:200].replace('\n', ' ')
            return resp.getcode(), elapsed, preview
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        try:
            body_txt = e.read().decode('utf-8', errors='replace')
        except Exception:
            body_txt = ''
        return e.code, elapsed, ('✗ ' + body_txt[:300].replace('\n', ' '))
    except Exception as e:
        return -1, time.time() - t0, '✗ ' + repr(e)[:200]


def main():
    print('Probing %s via %s' % (MODEL, BASE_URL))
    variants = [
        ('plain (no thinking field)', None),
        ('thinking={"type":"adaptive","display":"summarized"}', {'type': 'adaptive', 'display': 'summarized'}),
        ('thinking={"type":"disabled"}', {'type': 'disabled'}),
    ]
    for label, thinking in variants:
        print()
        print('--- %s ---' % label)
        print('%-7s %6s %8s  %s' % ('key', 'status', 'elapsed', 'result'))
        print('-' * 100)
        for kname, kval in KEYS.items():
            code, el, prev = hit(kval, thinking)
            print('%-7s %6s %7.2fs  %s' % (kname, code, el, prev))
            time.sleep(0.4)


if __name__ == '__main__':
    main()
