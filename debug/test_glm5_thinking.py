#!/usr/bin/env python3
"""
GLM-5.1 Thinking Parameter Test
================================
Tests which parameter format actually disables thinking on GLM-5.1.

The official Z.AI docs (https://docs.z.ai/guides/capabilities/thinking-mode)
say GLM-5 / GLM-4.7 have thinking ON by default, and the `thinking` param
is a **boolean** (not an object like `{type: "enabled"}`).

This script tests multiple approaches to disable thinking:
  1. Omit thinking param entirely (current code — suspected broken)
  2. "thinking": false  (official Z.AI docs)
  3. "thinking": {"type": "disabled"}  (Doubao-style — what we tried)
  4. "thinking": {"type": "enabled", "budget_tokens": 0}  (zero budget)
  5. chat_template_kwargs: {"enable_thinking": false}  (vLLM/SGLang style)

For each test, we check:
  - Does the response contain reasoning_content?
  - Does the content contain <think> tags?
  - HTTP status code / errors

Usage:
    python3 debug/test_glm5_thinking.py
    python3 debug/test_glm5_thinking.py --base-url https://open.bigmodel.cn/api/paas/v4
    python3 debug/test_glm5_thinking.py --model glm-5
"""

import argparse
import json
import os
import sys
import time

import requests

# ── Config ──────────────────────────────────────────────────
# Try to load from project config, fall back to env
def _load_config():
    """Load API key, base URL, and extra headers from project config or env."""
    api_key = os.environ.get('LLM_API_KEYS', os.environ.get('LLM_API_KEY', ''))
    base_url = os.environ.get('LLM_BASE_URL', '')
    extra_headers = {}

    # Try project config
    config_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'config', 'server_config.json')
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            providers = cfg.get('providers', [])
            for p in providers:
                models = p.get('models', [])
                for m in models:
                    mid = (m.get('model_id', '') or m.get('id', '')).lower()
                    if 'glm' in mid:
                        keys = p.get('api_keys', [p.get('apiKey', '')])
                        api_key = api_key or (keys[0] if keys else '')
                        base_url = base_url or p.get('base_url', p.get('endpoint', ''))
                        extra_headers = p.get('extra_headers', {})
                        break
        except Exception as e:
            print(f'  [warn] Could not load config: {e}')

    return api_key.split(',')[0].strip() if api_key else '', base_url.rstrip('/'), extra_headers


PROMPT = '2+3等于几？只回答数字。'  # Simple prompt to minimize output


def test_variant(name, base_url, api_key, model, extra_body, timeout=30,
                 extra_headers=None):
    """Send a request with specific thinking params and report results."""
    url = f'{base_url}/chat/completions'
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': PROMPT}],
        'max_tokens': 1024,
        'temperature': 0.7,
        'stream': False,
        **extra_body,
    }

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    if extra_headers:
        headers.update(extra_headers)

    print(f'\n{"─" * 60}')
    print(f'Test: {name}')
    print(f'Extra body: {json.dumps(extra_body, ensure_ascii=False)}')
    print(f'Full body:  {json.dumps(body, ensure_ascii=False)[:300]}')

    t0 = time.perf_counter()
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        elapsed = time.perf_counter() - t0
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f'  ❌ ERROR: {e} ({elapsed:.1f}s)')
        return {'name': name, 'status': 'error', 'error': str(e), 'has_thinking': None}

    print(f'  HTTP {resp.status_code} ({elapsed:.1f}s)')

    if resp.status_code != 200:
        err_text = resp.text[:500]
        print(f'  ❌ Error response: {err_text}')
        return {'name': name, 'status': f'http_{resp.status_code}', 'error': err_text, 'has_thinking': None}

    data = resp.json()
    choices = data.get('choices', [])
    usage = data.get('usage', {})

    if not choices:
        print(f'  ❌ No choices in response')
        return {'name': name, 'status': 'no_choices', 'has_thinking': None}

    msg = choices[0].get('message', {})
    content = msg.get('content', '')
    reasoning = msg.get('reasoning_content', '')
    finish = choices[0].get('finish_reason', '?')

    has_thinking_field = bool(reasoning)
    has_think_tags = '<think>' in (content or '')

    # Token info
    prompt_tokens = usage.get('prompt_tokens', 0)
    completion_tokens = usage.get('completion_tokens', 0)
    total_tokens = usage.get('total_tokens', 0)
    # Some APIs separate thinking tokens
    thinking_tokens = usage.get('thinking_tokens', usage.get('reasoning_tokens', 0))

    print(f'  Content:          {(content or "").strip()[:100]}')
    print(f'  Reasoning field:  {"YES (" + str(len(reasoning)) + " chars)" if reasoning else "no"}')
    print(f'  <think> tags:     {"YES" if has_think_tags else "no"}')
    print(f'  Finish reason:    {finish}')
    print(f'  Tokens:           prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}')
    if thinking_tokens:
        print(f'  Thinking tokens:  {thinking_tokens}')
    print(f'  Latency:          {elapsed:.1f}s')

    has_thinking = has_thinking_field or has_think_tags
    if has_thinking:
        print(f'  ⚠️  THINKING DETECTED — this variant does NOT disable thinking')
        if reasoning:
            print(f'  Reasoning preview: {reasoning[:200]}')
    else:
        print(f'  ✅ No thinking detected — this variant WORKS to disable thinking')

    return {
        'name': name,
        'status': 'ok',
        'has_thinking': has_thinking,
        'has_reasoning_field': has_thinking_field,
        'has_think_tags': has_think_tags,
        'content': (content or '').strip()[:200],
        'reasoning_preview': (reasoning or '')[:200],
        'tokens': usage,
        'latency': round(elapsed, 2),
    }


def test_streaming_variant(name, base_url, api_key, model, extra_body, timeout=30,
                          extra_headers=None):
    """Test streaming mode to see if reasoning_content appears in delta."""
    url = f'{base_url}/chat/completions'
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': PROMPT}],
        'max_tokens': 1024,
        'temperature': 0.7,
        'stream': True,
        **extra_body,
    }

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }
    if extra_headers:
        headers.update(extra_headers)

    print(f'\n{"─" * 60}')
    print(f'Test (streaming): {name}')
    print(f'Extra body: {json.dumps(extra_body, ensure_ascii=False)}')

    t0 = time.perf_counter()
    content_parts = []
    reasoning_parts = []
    finish_reason = '?'
    usage = {}

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=timeout, stream=True)
        if resp.status_code != 200:
            elapsed = time.perf_counter() - t0
            print(f'  ❌ HTTP {resp.status_code}: {resp.text[:300]}')
            return {'name': name + ' (stream)', 'status': f'http_{resp.status_code}', 'has_thinking': None}

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                break
            try:
                d = json.loads(payload)
                delta = d.get('choices', [{}])[0].get('delta', {})
                c = delta.get('content', '')
                r = delta.get('reasoning_content', '')
                if c:
                    content_parts.append(c)
                if r:
                    reasoning_parts.append(r)
                fr = d.get('choices', [{}])[0].get('finish_reason')
                if fr:
                    finish_reason = fr
                if d.get('usage'):
                    usage = d['usage']
            except (json.JSONDecodeError, IndexError):
                pass

        elapsed = time.perf_counter() - t0
    except Exception as e:
        elapsed = time.perf_counter() - t0
        print(f'  ❌ ERROR: {e} ({elapsed:.1f}s)')
        return {'name': name + ' (stream)', 'status': 'error', 'error': str(e), 'has_thinking': None}

    content = ''.join(content_parts)
    reasoning = ''.join(reasoning_parts)
    has_thinking_field = bool(reasoning)
    has_think_tags = '<think>' in content

    print(f'  Content:          {content.strip()[:100]}')
    print(f'  Reasoning deltas: {"YES (" + str(len(reasoning)) + " chars)" if reasoning else "no"}')
    print(f'  <think> tags:     {"YES" if has_think_tags else "no"}')
    print(f'  Finish reason:    {finish_reason}')
    print(f'  Latency:          {elapsed:.1f}s')

    has_thinking = has_thinking_field or has_think_tags
    if has_thinking:
        print(f'  ⚠️  THINKING DETECTED in stream')
        if reasoning:
            print(f'  Reasoning preview: {reasoning[:200]}')
    else:
        print(f'  ✅ No thinking in stream')

    return {
        'name': name + ' (stream)',
        'status': 'ok',
        'has_thinking': has_thinking,
        'latency': round(elapsed, 2),
    }


def main():
    parser = argparse.ArgumentParser(description='GLM-5.1 Thinking Parameter Test')
    parser.add_argument('--base-url', help='API base URL (without /chat/completions)')
    parser.add_argument('--api-key', help='API key')
    parser.add_argument('--model', default='glm-5.1', help='Model ID (default: glm-5.1)')
    parser.add_argument('--stream', action='store_true', help='Also test streaming mode')
    parser.add_argument('--timeout', type=int, default=60, help='Request timeout in seconds')
    args = parser.parse_args()

    cfg_key, cfg_url, cfg_headers = _load_config()
    api_key = args.api_key or cfg_key
    base_url = args.base_url or cfg_url
    model = args.model
    extra_hdrs = cfg_headers

    if not api_key:
        print('ERROR: No API key. Set LLM_API_KEYS env or pass --api-key')
        sys.exit(1)
    if not base_url:
        print('ERROR: No base URL. Set LLM_BASE_URL env or pass --base-url')
        sys.exit(1)

    print('=' * 60)
    print('GLM-5.1 Thinking Parameter Test')
    print('=' * 60)
    print(f'Model:    {model}')
    print(f'Endpoint: {base_url}')
    print(f'Key:      {api_key[:8]}...{api_key[-4:]}')
    if extra_hdrs:
        print(f'Headers:  {extra_hdrs}')

    # ── Define test variants ──────────────────────────────
    variants = [
        # 1. Baseline: omit thinking param (current code when thinking=off)
        ('1. Omit thinking param (baseline)', {}),

        # 2. Official Z.AI docs: thinking=false (boolean)
        ('2. thinking=false (boolean)', {'thinking': False}),

        # 3. thinking=true (boolean) — confirm it works
        ('3. thinking=true (boolean)', {'thinking': True}),

        # 4. Doubao-style object: {type: "disabled"}
        ('4. thinking={type:disabled}', {'thinking': {'type': 'disabled'}}),

        # 5. Doubao-style object: {type: "enabled"} — should enable
        ('5. thinking={type:enabled}', {'thinking': {'type': 'enabled'}}),

        # 6. Zero budget approach
        ('6. thinking={type:enabled, budget:0}', {'thinking': {'type': 'enabled', 'budget_tokens': 0}}),

        # 7. vLLM/SGLang chat_template_kwargs style
        ('7. chat_template_kwargs.enable_thinking=false', {
            'chat_template_kwargs': {'enable_thinking': False}
        }),

        # 8. do_sample=false (Z.AI specific param)
        ('8. do_sample=false', {'do_sample': False}),
    ]

    results = []
    for name, extra in variants:
        r = test_variant(name, base_url, api_key, model, extra, timeout=args.timeout,
                         extra_headers=extra_hdrs)
        results.append(r)

    # ── Optional streaming tests ──────────────────────────
    if args.stream:
        stream_variants = [
            ('1. Omit (stream)', {}),
            ('2. thinking=false (stream)', {'thinking': False}),
            ('4. thinking={type:disabled} (stream)', {'thinking': {'type': 'disabled'}}),
        ]
        for name, extra in stream_variants:
            r = test_streaming_variant(name, base_url, api_key, model, extra, timeout=args.timeout,
                                       extra_headers=extra_hdrs)
            results.append(r)

    # ── Summary ───────────────────────────────────────────
    print(f'\n{"=" * 60}')
    print('SUMMARY')
    print(f'{"=" * 60}')
    print(f'{"Variant":<45} {"Status":<10} {"Thinking?":<12} {"Latency"}')
    print(f'{"─" * 45} {"─" * 10} {"─" * 12} {"─" * 8}')
    for r in results:
        status = r.get('status', '?')
        thinking = r.get('has_thinking')
        if thinking is None:
            t_str = 'N/A'
        elif thinking:
            t_str = '⚠️ YES'
        else:
            t_str = '✅ NO'
        lat = r.get('latency', '')
        lat_str = f'{lat}s' if lat else ''
        print(f'{r["name"]:<45} {status:<10} {t_str:<12} {lat_str}')

    # ── Recommendation ────────────────────────────────────
    print(f'\n{"=" * 60}')
    print('RECOMMENDATION')
    print(f'{"=" * 60}')
    working = [r for r in results if r.get('status') == 'ok' and r.get('has_thinking') is False]
    if working:
        print(f'The following variants successfully disabled thinking:')
        for r in working:
            print(f'  ✅ {r["name"]}')
        print(f'\n→ Update lib/llm_client.py build_body() GLM branch accordingly.')
    else:
        print('⚠️ No variant successfully disabled thinking!')
        print('   GLM-5.1 may not support disabling thinking via API params.')
        print('   Consider stripping <think> tags / reasoning_content in post-processing.')


if __name__ == '__main__':
    main()
