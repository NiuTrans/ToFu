#!/usr/bin/env python3
"""Empirical feasibility probe for Anthropic *cache editing* on our gateway.

Background
----------
Claude Code's ``cachedMicrocompact`` shrinks the cached prefix WITHOUT a
cache bust by sending, at the API layer, a ``cache_edits`` block that
deletes individual ``tool_result`` blocks by ``cache_reference`` (their
``tool_use_id``).  The server then reports ``cache_deleted_input_tokens``
in usage.  This is a 1P-Anthropic Messages-API beta — in the external
Claude Code build the whole implementation is dead-code-eliminated and
the beta header constant is "ant-only".

This probe answers, empirically and against the REAL gateway:
  Q1. Does our gateway's Anthropic-native Messages path work at all, and
      does it report prompt-cache usage (cache_creation / cache_read)?
  Q2. If we send a ``cache_edits`` deletion + the context-management beta
      header on a warm cache, does the gateway:
        (a) ACCEPT it and return ``cache_deleted_input_tokens`` > 0   → feasible
        (b) ACCEPT but IGNORE it (no cache_deleted field)             → no-op, not feasible
        (c) REJECT it (HTTP 4xx / error)                              → not feasible

Safety
------
Dry-run by default: prints exactly what WOULD be sent and exits without a
network call.  Pass ``--send`` to actually hit the gateway (costs a few
cents — two small Messages calls).  Reads creds from
``data/config/server_config.json`` (no secrets in argv/logs).

Usage
-----
    python3 debug/probe_cache_editing.py                # dry-run
    python3 debug/probe_cache_editing.py --send         # live (key 0)
    python3 debug/probe_cache_editing.py --send --key 1 --model aws.claude-opus-4.7

Wire format (verified from claude-code/src/services/api/claude.ts:3052):
    cache_edits block (in last user message content):
        {"type":"cache_edits","edits":[{"type":"delete","cache_reference":"<tool_use_id>"}]}
    cache_reference on a cached tool_result block:
        {"type":"tool_result","tool_use_id":"X","cache_reference":"X", ...}
    beta header candidates (gateway may want either / none):
        context-management-2025-06-27   (public context-management beta)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONFIG = 'data/config/server_config.json'

# Beta headers to try (the gateway may require one of these for cache_edits).
BETA_CONTEXT_MGMT = 'context-management-2025-06-27'
BETA_EXTENDED_TTL = 'extended-cache-ttl-2025-04-11'

# A big-ish blob so the prefix is comfortably over the cache minimum
# (Anthropic caches in 1024-token increments; ~6k chars ≈ 1.5k tokens).
_BLOB = ('The quick brown fox jumps over the lazy dog. ' * 350)


def _load_provider():
    with open(CONFIG) as f:
        cfg = json.load(f)
    p = cfg['providers'][0]
    return p


def _messages_url(base_url: str) -> str:
    """Resolve the Anthropic-native Messages endpoint.

    The configured provider base (``…/v1/openai/native``) is OpenAI-format
    and 404s for ``/v1/messages``. The gateway's working Anthropic-native
    path was discovered empirically (2026-06-03):
    ``https://<host>/v1/anthropic/v1/messages``. Override via
    ``CACHE_PROBE_MESSAGES_URL``.
    """
    override = os.environ.get('CACHE_PROBE_MESSAGES_URL')
    if override:
        return override
    from urllib.parse import urlsplit
    parts = urlsplit(base_url)
    return f'{parts.scheme}://{parts.netloc}/v1/anthropic/v1/messages'


def _warm_body(model: str, tool_use_id: str) -> dict:
    """A conversation with ONE tool_result, cache_control on the last user
    block, so the tool_result lands inside the cached prefix."""
    return {
        'model': model,
        'max_tokens': 64,
        'stream': False,
        'system': [{'type': 'text', 'text': 'You are a test harness. Answer in one word.',
                    'cache_control': {'type': 'ephemeral'}}],
        'messages': [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': f'Reference data:\n{_BLOB}'},
            ]},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': "I'll look that up."},
                {'type': 'tool_use', 'id': tool_use_id, 'name': 'lookup', 'input': {'q': 'x'}},
            ]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': tool_use_id,
                 'content': f'Tool output:\n{_BLOB}',
                 'cache_control': {'type': 'ephemeral'}},
            ]},
        ],
        'tools': [{'name': 'lookup', 'description': 'look up',
                   'input_schema': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}],
    }


def _edit_body(model: str, tool_use_id: str) -> dict:
    """Follow-up turn that asks the server to DELETE the cached tool_result
    via a cache_edits block, while adding a new user message + cache_control
    so the (now-shorter) prefix can re-cache."""
    return {
        'model': model,
        'max_tokens': 64,
        'stream': False,
        'system': [{'type': 'text', 'text': 'You are a test harness. Answer in one word.',
                    'cache_control': {'type': 'ephemeral'}}],
        'messages': [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': f'Reference data:\n{_BLOB}'},
            ]},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': "I'll look that up."},
                {'type': 'tool_use', 'id': tool_use_id, 'name': 'lookup', 'input': {'q': 'x'}},
            ]},
            {'role': 'user', 'content': [
                # The tool_result still carries cache_reference so the server
                # can address it for deletion.
                {'type': 'tool_result', 'tool_use_id': tool_use_id,
                 'cache_reference': tool_use_id,
                 'content': f'Tool output:\n{_BLOB}'},
            ]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'done'}]},
            {'role': 'user', 'content': [
                # The cache_edits deletion block + a new question.
                {'type': 'cache_edits',
                 'edits': [{'type': 'delete', 'cache_reference': tool_use_id}]},
                {'type': 'text', 'text': 'Now say OK.',
                 'cache_control': {'type': 'ephemeral'}},
            ]},
        ],
        'tools': [{'name': 'lookup', 'description': 'look up',
                   'input_schema': {'type': 'object', 'properties': {'q': {'type': 'string'}}}}],
    }


def _post(url: str, api_key: str, body: dict, betas: list, extra_headers: dict):
    import requests
    from lib.llm.anthropic_outbound import anthropic_headers
    hdrs = anthropic_headers(api_key, dict(extra_headers or {}))
    if betas:
        hdrs['anthropic-beta'] = ','.join(betas)
    t0 = time.time()
    r = requests.post(url, headers=hdrs, json=body, timeout=60)
    dt = time.time() - t0
    try:
        data = r.json()
    except Exception:
        data = {'_raw_text': r.text[:2000]}
    return r.status_code, data, dt


def _usage_summary(data: dict) -> dict:
    u = data.get('usage', {}) if isinstance(data, dict) else {}
    return {
        'input_tokens': u.get('input_tokens'),
        'cache_creation_input_tokens': u.get('cache_creation_input_tokens'),
        'cache_read_input_tokens': u.get('cache_read_input_tokens'),
        'cache_deleted_input_tokens': u.get('cache_deleted_input_tokens'),
        'output_tokens': u.get('output_tokens'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--send', action='store_true', help='actually hit the gateway')
    ap.add_argument('--key', type=int, default=0, help='api_keys index')
    ap.add_argument('--model', default='aws.claude-opus-4.7')
    ap.add_argument('--beta', default=BETA_CONTEXT_MGMT,
                    help="comma-separated beta headers, or 'none'")
    args = ap.parse_args()

    p = _load_provider()
    base_url = p['base_url']
    url = _messages_url(base_url)
    extra_headers = p.get('extra_headers', {})
    keys = p['api_keys']
    betas = [] if args.beta.strip().lower() == 'none' else [b for b in args.beta.split(',') if b]
    tuid = 'toolu_probe_0001'

    print('=' * 70)
    print('CACHE-EDITING FEASIBILITY PROBE')
    print('=' * 70)
    print(f'gateway messages URL : {url}')
    print(f'model                : {args.model}')
    print(f'key index            : {args.key}  (of {len(keys)})')
    print(f'beta headers         : {betas or "(none)"}')
    print(f'extra headers        : {list(extra_headers.keys())}')
    print()
    warm = _warm_body(args.model, tuid)
    edit = _edit_body(args.model, tuid)
    print('CALL 1 (warm cache): tool_result inside cached prefix')
    print('CALL 2 (edit): sends cache_edits delete block, expects cache_deleted_input_tokens')
    print()
    print('--- CALL 2 body.messages[-1] (the cache_edits payload) ---')
    print(json.dumps(edit['messages'][-1], indent=2)[:900])
    print()

    if not args.send:
        print('DRY RUN — no network call. Re-run with --send to execute (costs a few cents).')
        return

    if args.key >= len(keys):
        print(f'ERROR: key index {args.key} out of range'); sys.exit(1)
    api_key = keys[args.key]

    report = {'url': url, 'model': args.model, 'betas': betas,
              'ts': time.strftime('%Y%m%d_%H%M%S')}

    print('>>> CALL 1 (warm)…')
    s1, d1, dt1 = _post(url, api_key, warm, betas, extra_headers)
    report['call1'] = {'status': s1, 'usage': _usage_summary(d1), 'elapsed_s': round(dt1, 2)}
    print(f'    HTTP {s1}  {dt1:.1f}s  usage={_usage_summary(d1)}')
    if s1 != 200:
        report['call1']['error'] = d1.get('error') or d1.get('_raw_text')
        print(f'    error: {report["call1"]["error"]}')

    time.sleep(1.0)

    print('>>> CALL 2 (cache_edits delete)…')
    s2, d2, dt2 = _post(url, api_key, edit, betas, extra_headers)
    report['call2'] = {'status': s2, 'usage': _usage_summary(d2), 'elapsed_s': round(dt2, 2)}
    print(f'    HTTP {s2}  {dt2:.1f}s  usage={_usage_summary(d2)}')
    if s2 != 200:
        report['call2']['error'] = d2.get('error') or d2.get('_raw_text')
        print(f'    error: {report["call2"]["error"]}')

    # ── Verdict ──
    u2 = report['call2']['usage']
    cdel = u2.get('cache_deleted_input_tokens')
    if s2 != 200:
        verdict = 'NOT FEASIBLE — gateway rejected the cache_edits request'
    elif cdel is None:
        verdict = ('NOT FEASIBLE — gateway accepted the block but did NOT '
                   'return cache_deleted_input_tokens (silently ignored)')
    elif cdel and cdel > 0:
        verdict = f'FEASIBLE — gateway reported cache_deleted_input_tokens={cdel}'
    else:
        verdict = 'INCONCLUSIVE — cache_deleted_input_tokens present but 0'
    report['verdict'] = verdict
    print()
    print('=' * 70)
    print('VERDICT:', verdict)
    print('=' * 70)

    out = f'debug/cache_editing_probe_{report["ts"]}.json'
    with open(out, 'w') as f:
        json.dump(report, f, indent=2)
    print('saved', out)


if __name__ == '__main__':
    main()
