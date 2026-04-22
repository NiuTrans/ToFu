#!/usr/bin/env python3
"""Probe variants of thinking params on Opus 4.7 via Sankuai→Bedrock.

Sends a handcrafted body (NOT via build_body) to isolate what the gateway
actually honours. Each variant goes through the same raw-SSE dumper.
"""
import os
import sys
import time
import json
import requests

os.environ.setdefault('LLM_DEBUG_RAW_SSE', 'opus')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib import LLM_API_KEYS, LLM_BASE_URL  # noqa: E402
from lib.llm_client import _RawSSEDumper  # noqa: E402

MESSAGES = [{
    'role': 'user',
    'content': (
        'Solve step by step: A farmer has chickens and cows. Together they '
        'have 30 heads and 74 legs. How many of each? Think carefully first.'
    ),
}]

VARIANTS = [
    ('adaptive+display+effort-max', {
        'thinking': {'type': 'adaptive', 'display': 'summarized'},
        'effort': 'max',
    }),
    ('enabled+budget_tokens=8000', {
        'thinking': {'type': 'enabled', 'budget_tokens': 8000},
    }),
    ('adaptive-only (no display)', {
        'thinking': {'type': 'adaptive'},
        'effort': 'max',
    }),
    ('adaptive+display+budget_tokens', {
        'thinking': {'type': 'adaptive', 'display': 'summarized', 'budget_tokens': 8000},
    }),
    ('display-only+effort-max', {
        'thinking': {'display': 'summarized'},
        'effort': 'max',
    }),
]

MODEL = sys.argv[1] if len(sys.argv) > 1 else 'aws.claude-opus-4.7'
api_key = LLM_API_KEYS[0]
URL = f'{LLM_BASE_URL}/chat/completions'
NP = {'http': None, 'https': None}


def run_variant(label, extra):
    body = {
        'model': MODEL,
        'messages': MESSAGES,
        'max_tokens': 4096,
        'stream': True,
        **extra,
    }
    print(f'\n=== {label} ===')
    print(f'body={json.dumps({k: v for k, v in body.items() if k != "messages"})}')
    trace_id = f'probe-{int(time.time()*1000)}'
    dumper = _RawSSEDumper(MODEL, trace_id, body)
    dumper.enabled = True  # force-enable
    dumper.start()
    dumper.t0 = time.time()

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    t0 = time.time()
    thinking_chars = 0
    content_chars = 0
    saw_reasoning_field = False
    saw_thinking_type = False
    lines_total = 0
    try:
        r = requests.post(URL, headers=headers, json=body, stream=True,
                          timeout=120, proxies=NP)
        if r.status_code != 200:
            err_body = r.text[:500]
            dumper.line(f'HTTP {r.status_code}: {err_body}')
            dumper.finish(error=f'HTTP {r.status_code}')
            print(f'  HTTP {r.status_code}: {err_body}')
            return
        for line in r.iter_lines(decode_unicode=True):
            dumper.line(line or '')
            if not line or not line.startswith('data: '):
                continue
            lines_total += 1
            payload = line[6:].strip()
            if payload == '[DONE]':
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            # OpenAI-compat shape
            for choice in chunk.get('choices', []):
                delta = choice.get('delta') or {}
                rc = delta.get('reasoning_content') or delta.get('thinking')
                if rc:
                    thinking_chars += len(rc)
                    saw_reasoning_field = True
                if delta.get('content'):
                    content_chars += len(delta['content'])
            # Anthropic native shape
            if chunk.get('type') in ('content_block_start', 'content_block_delta'):
                blk = chunk.get('content_block') or chunk.get('delta') or {}
                if blk.get('type') in ('thinking', 'thinking_delta'):
                    saw_thinking_type = True
                    thinking_chars += len(blk.get('thinking', '') or blk.get('text', ''))
        r.close()
    except Exception as e:
        print(f'  ERROR: {e}')
        dumper.finish(error=str(e))
        return
    elapsed = time.time() - t0
    summary = {
        'elapsed': round(elapsed, 2),
        'lines': lines_total,
        'content_chars': content_chars,
        'thinking_chars': thinking_chars,
        'saw_reasoning_field': saw_reasoning_field,
        'saw_thinking_type_block': saw_thinking_type,
    }
    dumper.finish(**summary)
    print(f'  → {summary}')


for lbl, extra in VARIANTS:
    run_variant(lbl, extra)
    time.sleep(1)

print(f'\nFull transcripts appended to: logs/raw_sse.log')
