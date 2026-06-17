"""Regression tests for the compaction token gate (_count_tokens_authoritative).

Two properties, both born from the conv=mq7y3irly1r4hu fatal-loop:

  1. The gate accounts for the live tool schema stashed on
     ``task['_tool_schema']`` — the schema ships in every request and the
     gateway tokenizes it, so omitting it under-counts on tool-heavy configs.

  2. The gate NEVER reports fewer tokens than the conservative entropy
     heuristic, even when a "better" backend (tiktoken) wins the resolver.
     tiktoken's cl100k vocabulary under-counts Claude's tokenizer on
     high-entropy base64; trusting the lower number can let an oversized
     prompt slip past the trigger into the fatal reactive path.
"""
from __future__ import annotations

import pytest

from lib.tasks_pkg.compaction._tokens import (
    _count_tokens_authoritative,
    _estimate_total_tokens,
)


@pytest.mark.unit
def test_gate_never_below_heuristic_floor():
    # A transcript full of high-entropy base64 — the exact shape tiktoken
    # under-counts vs the entropy heuristic.
    import base64
    import os
    blob = base64.b64encode(os.urandom(60_000)).decode()
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'analyze this file'},
        {'role': 'tool', 'name': 'read_files', 'tool_call_id': 't1',
         'content': f'File: data.b64\n\n{blob}'},
    ]
    task = {'config': {'model': 'aws.claude-opus-4.8'}, 'convId': 'gate_floor'}

    gate, method = _count_tokens_authoritative(msgs, task)
    floor = _estimate_total_tokens(msgs)

    assert gate >= floor, (gate, floor)
    # When tiktoken wins but under-counts, the floor must engage and be tagged.
    if gate == floor and method != 'heuristic_fallback':
        assert method.endswith('heuristic_floor'), method


@pytest.mark.unit
def test_gate_includes_tool_schema():
    # Identical messages; only difference is a fat tool schema stashed on
    # the task. The gate must count more tokens WITH the schema.
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'hello'},
    ]
    fat_tools = [{
        'type': 'function',
        'function': {
            'name': f'tool_{i}',
            'description': 'A very thoroughly documented tool. ' * 40,
            'parameters': {
                'type': 'object',
                'properties': {f'p{j}': {'type': 'string',
                                         'description': 'param ' * 20}
                               for j in range(8)},
            },
        },
    } for i in range(30)]

    base = {'config': {'model': 'gpt-4o'}, 'convId': 'gate_tools_off'}
    with_tools = {'config': {'model': 'gpt-4o'}, 'convId': 'gate_tools_on',
                  '_tool_schema': fat_tools}

    n_off, _ = _count_tokens_authoritative(msgs, base)
    n_on, _ = _count_tokens_authoritative(msgs, with_tools)

    assert n_on > n_off, (n_on, n_off)


@pytest.mark.unit
def test_gate_no_tool_schema_key_is_safe():
    # Missing/empty _tool_schema must not raise and must still return a count.
    msgs = [{'role': 'user', 'content': 'hi there ' * 50}]
    task = {'config': {'model': 'gpt-4o'}, 'convId': 'gate_no_tools'}
    n, method = _count_tokens_authoritative(msgs, task)
    assert n > 0 and isinstance(method, str)
