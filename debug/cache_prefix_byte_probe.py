#!/usr/bin/env python3
"""Offline wire-byte determinism probe for the Anthropic prompt-cache prefix.

Question under test
===================
Production shows `cache_read` PINNED at the system+tools floor (~57k) while
`cache_write` climbs with conv size, at gaps < 300s, with NO
`PREFIX MUTATION DETECTED`. Hypothesis: an EARLY body message's WIRE bytes
(post-`build_body` + `add_cache_breakpoints`) differ round-over-round in a
field the store-level text hash is blind to (`tool_calls` / `arguments` /
`reasoning_details` / ordering), so the longest byte-identical prefix the
server can match ends at the tools block.

Method (NO API calls, $0)
=========================
1. Build a production-SHAPED conversation: system + N rounds of
   [assistant(reasoning_content + thinking_signature + tool_calls), tool].
2. Render the WIRE body for "round N" via the real `build_body`.
3. Append one more [assistant, tool] pair (what the next round does) and
   render the WIRE body for "round N+1".
4. The two requests SHARE the first `len(msgs_N) - <tail>` messages. For the
   server to read that shared region back from cache, those messages must
   serialize BYTE-IDENTICALLY. Diff them, report the first divergent message
   index + the first differing byte. Then run `add_cache_breakpoints` and
   re-diff (catches a mutation injected by the breakpoint pass itself).

Run:
    python debug/cache_prefix_byte_probe.py            # default opus model
    python debug/cache_prefix_byte_probe.py --model aws.claude-opus-4.8
"""

import argparse
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm import add_cache_breakpoints, build_body  # noqa: E402

SYS = ('You are Tofu. ' + ('Follow the project conventions strictly. ' * 400))
_SIG = 'EpcBCkgIB' + ('x' * 200) + 'ABCD=='   # opaque signed-thinking blob


def _assistant(i):
    """A stored assistant turn shaped like the orchestrator saves it."""
    return {
        'role': 'assistant',
        'content': '',
        'reasoning_content': f'Let me think about step {i}. ' * 12,
        'thinking_signature': _SIG + str(i),
        'tool_calls': [{
            'id': f'call_{i:04d}',
            'type': 'function',
            'function': {'name': 'read_files',
                         'arguments': json.dumps({'path': f'lib/mod_{i}.py'})},
        }],
    }


def _tool(i):
    return {'role': 'tool', 'tool_call_id': f'call_{i:04d}',
            'content': f'# file {i}\n' + ('source line\n' * 60)}


def _conv(n_rounds):
    msgs = [{'role': 'system', 'content': SYS},
            {'role': 'user', 'content': 'Investigate the cache behavior.'}]
    for i in range(n_rounds):
        msgs.append(_assistant(i))
        msgs.append(_tool(i))
    return msgs


def _wire(model, msgs, apply_bp):
    """Run the REAL outbound transform and return the serialized messages."""
    body = build_body(model, copy.deepcopy(msgs), max_tokens=2048,
                      thinking_enabled=True, thinking_depth='medium',
                      tools=None, stream=True)
    if apply_bp:
        body['_task_id'] = ''            # force non-latched path
        add_cache_breakpoints(body, log_prefix='[probe]')
    return body['messages']


def _first_divergence(a, b, label):
    shared = min(len(a), len(b))
    for i in range(shared):
        sa = json.dumps(a[i], sort_keys=True, ensure_ascii=False)
        sb = json.dumps(b[i], sort_keys=True, ensure_ascii=False)
        if sa != sb:
            # find first differing char
            j = next((k for k in range(min(len(sa), len(sb))) if sa[k] != sb[k]),
                     min(len(sa), len(sb)))
            print(f'  [{label}] FIRST DIVERGENCE at message index {i} '
                  f'(role={a[i].get("role")}), byte {j}')
            print(f'    N  : ...{sa[max(0,j-40):j+60]!r}')
            print(f'    N+1: ...{sb[max(0,j-40):j+60]!r}')
            return i
    print(f'  [{label}] shared prefix of {shared} messages is BYTE-IDENTICAL ✓')
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='aws.claude-opus-4.8')
    ap.add_argument('--rounds', type=int, default=10)
    args = ap.parse_args()

    print(f'== Wire-byte prefix determinism probe ==  model={args.model} '
          f'rounds={args.rounds}\n')

    conv_n = _conv(args.rounds)
    conv_n1 = _conv(args.rounds)
    conv_n1.append(_assistant(args.rounds))   # next round appends one pair
    conv_n1.append(_tool(args.rounds))

    # The shared region = everything in conv_n except its last assistant+tool
    # pair (round N's own in-flight tail). Compare against the same slice of N+1.
    shared_len = len(conv_n) - 2

    print('--- Stage 1: build_body only (sanitize chain, reasoning replay) ---')
    wn = _wire(args.model, conv_n, apply_bp=False)
    wn1 = _wire(args.model, conv_n1, apply_bp=False)
    _first_divergence(wn[:shared_len], wn1[:shared_len], 'build_body')

    print('\n--- Stage 2: + add_cache_breakpoints ---')
    bn = _wire(args.model, conv_n, apply_bp=True)
    bn1 = _wire(args.model, conv_n1, apply_bp=True)
    _first_divergence(bn[:shared_len], bn1[:shared_len], 'with_bp')

    print('\n--- Stage 3: where did cache_control markers land (N+1)? ---')
    for i, m in enumerate(bn1):
        cc = None
        c = m.get('content')
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and 'cache_control' in blk:
                    cc = blk['cache_control']
        if cc is not None:
            print(f'    BP at message {i} (role={m.get("role")}): {cc}')


if __name__ == '__main__':
    main()
