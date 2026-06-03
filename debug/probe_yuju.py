"""Standalone probe: request yuju-claude-opus (Claude Code) models via the AIGC gateway.

These are Claude Code AppID models and speak the ANTHROPIC Messages API,
NOT the OpenAI Chat Completions API. The OpenAI-compat path
(/v1/openai/native/chat/completions) rejects them with
"invalid model name" / "不支持的模型类型". Use the Anthropic-native path below.

Usage:
    NO_PROXY=.internal.example.com python debug/probe_yuju.py [model ...]
"""
import json
import os
import sys

import requests

os.environ.setdefault('NO_PROXY', '.internal.example.com')

URL = 'https://api.openai.com/v1/anthropic/v1/messages'
APP_ID = 'YOUR_API_KEY_HERE'
HEADERS = {
    'x-api-key': APP_ID,
    'authorization': f'Bearer {APP_ID}',
    'anthropic-version': '2023-06-01',
    'content-type': 'application/json',
    'X-Custom-Header': 'your-header-value',
}

MODELS = [
    'yuju-claude-opus-4.7-evaDaily',
    'yuju-claude-opus-4.8-evaDaily',
]


def probe(model: str) -> None:
    body = {
        'model': model,
        'max_tokens': 64,
        'messages': [{'role': 'user', 'content': 'Say hello in one short sentence.'}],
    }
    print(f'\n=== POST {URL}  model={model} ===')
    try:
        resp = requests.post(URL, headers=HEADERS, json=body, timeout=60)
    except Exception as e:
        print(f'REQUEST FAILED: {e!r}')
        return
    print(f'HTTP {resp.status_code}')
    try:
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2)[:2000])
    except Exception:
        print(resp.text[:2000])


if __name__ == '__main__':
    for m in sys.argv[1:] or MODELS:
        probe(m)
