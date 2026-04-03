#!/usr/bin/env python3
"""Quick test: hit the FRIDAY Gemini image generation API directly."""

import sys
import os
import json
import time
import requests

# Use the first API key from lib/__init__
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import LLM_API_KEYS, IMAGE_GEN_MODEL

API_KEY = LLM_API_KEYS[1] if len(LLM_API_KEYS) > 1 else LLM_API_KEYS[0]
MODEL = IMAGE_GEN_MODEL
BASE = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/v1').rstrip('/v1/openai/native')
NP = {'http': None, 'https': None}

print(f'API_KEY: {API_KEY[:8]}...')
print(f'MODEL:   {MODEL}')
print()

# ── Step 1: Submit task ──
submit_url = f'{BASE}/v1/google/models/{MODEL}:imageGenerate'
headers = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {API_KEY}',
}
body = {
    'contents': [
        {
            'parts': [
                {'text': 'A cute orange cat sitting on a windowsill looking at the sunset'}
            ]
        }
    ],
    'generationConfig': {
        'responseModalities': ['Text', 'Image'],
    },
}

print(f'[1] POST {submit_url}')
print(f'    Body: {json.dumps(body, ensure_ascii=False)[:200]}')
t0 = time.time()

try:
    resp = requests.post(submit_url, headers=headers, json=body, proxies=NP, timeout=60)
except Exception as e:
    print(f'    ❌ Request exception: {e}')
    sys.exit(1)

elapsed = time.time() - t0
print(f'    Status: {resp.status_code} ({elapsed:.1f}s)')
print(f'    Headers: {dict(resp.headers)}')
print(f'    Body: {resp.text[:500]}')
print()

if resp.status_code != 200:
    print(f'❌ Submit failed with HTTP {resp.status_code}')
    # Try to see if the body has useful error info
    try:
        err_data = resp.json()
        print(f'   Error JSON: {json.dumps(err_data, indent=2, ensure_ascii=False)[:500]}')
    except Exception:
        pass
    sys.exit(1)

# Parse task ID — could be plain text or JSON
task_id = resp.text.strip().strip('"')
print(f'✅ Task ID: {task_id}')
print()

# ── Step 2: Poll for result ──
poll_url = f'{BASE}/v1/google/models/{task_id}:imageGenerateQuery'
print(f'[2] Polling {poll_url}')

for i in range(60):  # max 3 minutes
    time.sleep(3)
    try:
        poll_resp = requests.get(poll_url, headers=headers, proxies=NP, timeout=30)
    except Exception as e:
        print(f'    Poll {i+1}: exception {e}')
        continue

    print(f'    Poll {i+1}: HTTP {poll_resp.status_code} — {poll_resp.text[:200]}')

    if poll_resp.status_code != 200:
        continue

    try:
        poll_data = poll_resp.json()
    except Exception:
        print(f'    (not JSON)')
        continue

    status = poll_data.get('status', 0)
    if status == 1:
        # Success!
        total = time.time() - t0
        # Dump raw response
        raw_json = json.dumps(poll_data, ensure_ascii=False)
        print(f'\n    RAW RESPONSE ({len(raw_json)} chars):')
        print(f'    {raw_json[:2000]}')
        if len(raw_json) > 2000:
            print(f'    ... ({len(raw_json) - 2000} more chars)')
        data = poll_data.get('data', {})
        candidates = data.get('candidates', [])
        print(f'\n✅ SUCCESS in {total:.1f}s')
        for ci, c in enumerate(candidates):
            parts = c.get('content', {}).get('parts', [])
            for pi, p in enumerate(parts):
                if 'text' in p:
                    print(f'   Part {pi}: text = {p["text"][:200]}')
                elif 'inlineData' in p:
                    inline = p['inlineData']
                    b64 = inline.get('data', '')
                    mime = inline.get('mimeType', '?')
                    print(f'   Part {pi}: image {mime}, {len(b64)} chars b64')
                    print(f'   Raw inlineData keys: {list(inline.keys())}')
                    print(f'   Raw data value: {repr(b64[:500])}')
                    # Try to decode
                    try:
                        import base64
                        img_bytes = base64.b64decode(b64 + '==')  # add padding
                        out_path = 'debug/test_image_output.png'
                        with open(out_path, 'wb') as f:
                            f.write(img_bytes)
                        print(f'   Saved to {out_path} ({len(img_bytes)} bytes)')
                    except Exception as de:
                        print(f'   Decode error: {de}')
                elif 'fileData' in p:
                    fd = p['fileData']
                    print(f'   Part {pi}: fileData = {json.dumps(fd, ensure_ascii=False)[:300]}')
                else:
                    print(f'   Part {pi}: unknown keys = {list(p.keys())} — {json.dumps(p, ensure_ascii=False)[:300]}')
        sys.exit(0)
    elif status == -1:
        print(f'\n❌ FAILED: {poll_data}')
        sys.exit(1)
    # else status == 0, keep polling

print('\n❌ Timed out waiting for result')
sys.exit(1)
