#!/usr/bin/env python3
"""Capture raw SSE for Claude Opus 4.7 at max depth, via our gateway.

Writes transcript to logs/raw_sse.log. Then prints a summary and the file tail.

Usage:
    python3 debug/test_opus47_raw_sse.py [model]

Default model: aws.claude-opus-4.7 (Bedrock through Sankuai gateway).
Pass 'claude-opus-4-7' or 'us.anthropic.claude-opus-4-7-v1:0' for other routes.
"""
import os
import sys
import time

# Enable raw SSE dumper BEFORE importing lib.llm_client
os.environ.setdefault('LLM_DEBUG_RAW_SSE', 'opus')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib import LLM_API_KEYS  # noqa: E402
from lib.llm_client import build_body, stream_chat  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else 'aws.claude-opus-4.7'

messages = [{
    'role': 'user',
    'content': (
        'Solve this step by step, showing your reasoning: '
        'A farmer has chickens and cows. Together they have 30 heads and 74 legs. '
        'How many of each? Please think carefully before answering.'
    ),
}]

body = build_body(
    MODEL, messages,
    max_tokens=4096,
    thinking_enabled=True,
    thinking_depth='max',
    stream=True,
    provider_id='default',
)

print(f'=== Outgoing body (summary) ===')
print(f'  model:    {body.get("model")}')
print(f'  thinking: {body.get("thinking")}')
print(f'  effort:   {body.get("effort")}')
print(f'  temp:     {body.get("temperature", "<unset>")}')
print(f'  max_tok:  {body.get("max_tokens")}')
print()

thinking_buf = []
content_buf = []

def on_thinking(t):
    thinking_buf.append(t)
    sys.stdout.write(f'[THINK] {t}')
    sys.stdout.flush()

def on_content(t):
    content_buf.append(t)
    sys.stdout.write(f'[CONT] {t}')
    sys.stdout.flush()

api_key = LLM_API_KEYS[0] if LLM_API_KEYS else None
if not api_key:
    print('ERROR: no LLM_API_KEYS configured')
    sys.exit(1)

print(f'=== Streaming ({MODEL}) ===\n')
t0 = time.time()
try:
    msg, finish, usage = stream_chat(
        body,
        on_thinking=on_thinking,
        on_content=on_content,
        log_prefix='[RawSSETest]',
        api_key=api_key,
    )
    elapsed = time.time() - t0
    print(f'\n\n=== Done in {elapsed:.1f}s ===')
    print(f'  finish_reason:     {finish}')
    print(f'  thinking chars:    {sum(len(t) for t in thinking_buf)}')
    print(f'  content chars:     {sum(len(t) for t in content_buf)}')
    print(f'  usage:             {usage}')
    print(f'  msg.reasoning_content (first 300): '
          f'{(msg.get("reasoning_content") or "")[:300]!r}')
    print(f'  msg.content (first 300):           {(msg.get("content") or "")[:300]!r}')
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f'\nERROR: {e}')

print(f'\n=== Raw SSE log: logs/raw_sse.log ===')
