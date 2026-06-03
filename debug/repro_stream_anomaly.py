#!/usr/bin/env python3
"""Reproduce / measure SSE-stream anomalies against the Sankuai gateway.

Goal: empirically confirm the anomaly signature seen in
``logs/raw_sse_anomaly.log`` (chunks=0, no [DONE], elapsed 5–40 s) and
exercise the upstream gateway directly with a raw API key so we can
distinguish gateway flakes from anything our streaming code is doing.

Usage:
    python3 debug/repro_stream_anomaly.py [--n N] [--model MODEL]
                                          [--prompt-tokens TOKENS]
                                          [--tools]

Reads provider config from ``data/config/server_config.json`` (first
provider's first key + base_url).  No env-var setup needed.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_provider():
    cfg_path = os.path.join(ROOT, 'data/config/server_config.json')
    with open(cfg_path) as f:
        cfg = json.load(f)
    p = cfg['providers'][0]
    keys = p.get('api_keys') or p.get('keys') or []
    if not keys:
        sys.exit('ERROR: no api_keys in server_config.json')
    k = keys[0]
    api_key = k['key'] if isinstance(k, dict) else k
    return p['base_url'].rstrip('/'), api_key, p.get('name', '?')


def _build_messages(prompt_tokens: int, nonce: str = ''):
    """Make a request whose prompt is roughly N tokens.

    The ``nonce`` makes each request unique so the gateway does not
    serve a cached / dedup-shortened response.
    """
    nonce = nonce or os.urandom(4).hex()
    if prompt_tokens <= 0:
        return [{'role': 'user',
                 'content': f'(nonce={nonce}) Tell me one fun fact about '
                            f'octopuses in 1-2 sentences.'}]
    pad_chars = max(0, prompt_tokens - 50) * 4
    pad = ('# pad\n' * (pad_chars // 6))[:pad_chars]
    return [{
        'role': 'user',
        'content': (
            f'(nonce={nonce}) Below is some context.\n\n' + pad
            + '\n\nQuestion: tell me one short fact, in 1-2 sentences.'
        ),
    }]


_TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'get_weather',
        'description': 'Get the current weather in a given location.',
        'parameters': {
            'type': 'object',
            'properties': {'location': {'type': 'string'}},
            'required': ['location'],
        },
    },
}]


def one_call(base_url, api_key, model, messages, tools=False,
             timeout=(60, 300), capture_lines=False):
    """Make ONE streaming call. Return (verdict, info_dict)."""
    body = {
        'model': model,
        'messages': messages,
        'max_tokens': 1024,
        'stream': True,
        'temperature': 1.0,
    }
    if tools:
        body['tools'] = _TOOLS
    if 'claude-opus' in model.lower() and '4.7' in model:
        # Match the exact payload shape used by build_body() for Opus 4.7
        body['thinking'] = {'type': 'adaptive', 'display': 'summarized'}
        body['effort'] = 'medium'
        body.pop('temperature', None)  # 4.7 rejects temperature
    elif 'claude' in model.lower():
        body['thinking'] = {'type': 'adaptive'}
        body['effort'] = 'medium'

    url = base_url + '/chat/completions'
    headers = {
        'Authorization': 'Bearer ' + api_key,
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
    }
    t0 = time.time()
    chunks = 0
    saw_done = False
    saw_finish = False
    finish_reason = None
    content_chars = 0
    thinking_chars = 0
    parse_errors = 0
    http_status = None
    trace = ''
    err = ''
    raw_lines = []
    try:
        resp = requests.post(url, headers=headers, json=body,
                             stream=True, timeout=timeout)
        http_status = resp.status_code
        trace = resp.headers.get('M-TraceId', '')
        if resp.status_code != 200:
            return 'http_error', {
                'status': resp.status_code,
                'body': resp.text[:400],
                'trace': trace,
                'elapsed': time.time() - t0,
            }
        for line in resp.iter_lines(decode_unicode=True):
            if line is not None and capture_lines:
                raw_lines.append(line)
            if not line:
                continue
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                saw_done = True
                break
            if not data:
                continue
            chunks += 1
            try:
                ch = json.loads(data)
            except Exception:
                parse_errors += 1
                continue
            choices = ch.get('choices') or []
            if choices:
                delta = choices[0].get('delta', {})
                fr = choices[0].get('finish_reason')
                if fr:
                    saw_finish = True
                    finish_reason = fr
                content_chars += len(delta.get('content') or '')
                thinking_chars += len(
                    delta.get('reasoning_content')
                    or delta.get('thinking')
                    or ''
                )
        resp.close()
    except requests.Timeout as e:
        err = f'Timeout: {e}'
    except requests.RequestException as e:
        err = f'RequestException: {e}'
    except Exception as e:
        err = f'Exception: {type(e).__name__}: {e}'

    elapsed = time.time() - t0
    info = {
        'chunks': chunks,
        'saw_done': saw_done,
        'saw_finish': saw_finish,
        'finish_reason': finish_reason,
        'content_chars': content_chars,
        'thinking_chars': thinking_chars,
        'parse_errors': parse_errors,
        'http_status': http_status,
        'trace': trace,
        'elapsed': round(elapsed, 2),
        'err': err,
    }
    if capture_lines:
        info['raw_lines'] = raw_lines
    if err:
        return 'transport_error', info
    if not saw_done:
        return 'missing_done', info
    if not saw_finish and chunks > 0:
        return 'missing_finish_reason', info
    if (saw_finish and finish_reason == 'stop'
            and content_chars == 0 and chunks > 0):
        return 'empty_stop', info
    return 'ok', info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=20,
                    help='Number of calls to make')
    ap.add_argument('--model', default='aws.claude-opus-4.7')
    ap.add_argument('--prompt-tokens', type=int, default=0,
                    help='Pad prompt to roughly N input tokens')
    ap.add_argument('--tools', action='store_true',
                    help='Include a tool definition (mimics agent loop)')
    ap.add_argument('--concurrency', type=int, default=1,
                    help='Concurrent requests')
    ap.add_argument('--sleep', type=float, default=0.5,
                    help='Sleep between calls (seconds)')
    args = ap.parse_args()

    base_url, api_key, provider_name = _load_provider()
    print(f'Provider:  {provider_name}')
    print(f'Base URL:  {base_url}')
    print(f'Model:     {args.model}')
    print(f'Calls:     {args.n} (concurrency={args.concurrency} '
          f'prompt_tokens≈{args.prompt_tokens} tools={args.tools})')
    print('=' * 80)

    verdicts = Counter()
    samples = []

    def run_one(i):
        messages = _build_messages(args.prompt_tokens)
        v, info = one_call(base_url, api_key, args.model,
                           messages, tools=args.tools)
        verdicts[v] += 1
        sample = {'i': i, 'verdict': v, **info}
        samples.append(sample)
        flag = '✓' if v == 'ok' else '⚠'
        print(f'  [{i:02d}] {flag} {v:22s} elapsed={info["elapsed"]:6.2f}s '
              f'chunks={info["chunks"]:4d} content={info["content_chars"]:4d} '
              f'finish={info["finish_reason"]} trace={info["trace"][:8] or "-"} '
              f'{info.get("err","")}')

    if args.concurrency <= 1:
        for i in range(args.n):
            run_one(i)
            if i < args.n - 1 and args.sleep > 0:
                time.sleep(args.sleep)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            list(ex.map(run_one, range(args.n)))

    print('=' * 80)
    print('Verdict tallies:')
    for v, n in verdicts.most_common():
        print(f'  {v:22s} {n:4d} ({100 * n / args.n:5.1f}%)')

    anomalies = [s for s in samples if s['verdict'] != 'ok']
    if anomalies:
        print('\nAnomaly elapsed-time distribution:')
        elapsed = sorted(s['elapsed'] for s in anomalies)
        print(f'  count={len(elapsed)} min={elapsed[0]} max={elapsed[-1]} '
              f'median={elapsed[len(elapsed)//2]}')
        zeroes = [s for s in anomalies if s['chunks'] == 0]
        print(f'  with chunks=0: {len(zeroes)}')
        if zeroes:
            ze = sorted(s['elapsed'] for s in zeroes)
            print(f'    chunks=0 elapsed min={ze[0]} max={ze[-1]} '
                  f'median={ze[len(ze)//2]}')

    out = os.path.join(ROOT, 'logs/repro_stream_anomaly.json')
    with open(out, 'w') as f:
        json.dump({'args': vars(args), 'samples': samples,
                   'verdicts': dict(verdicts)}, f, indent=2)
    print(f'\nFull dump: {out}')


if __name__ == '__main__':
    main()
