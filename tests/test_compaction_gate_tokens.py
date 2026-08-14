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
    _compaction_trigger_threshold,
    _count_tokens_authoritative,
    _estimate_total_tokens,
    _working_set_token_limit,
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
def test_measured_usage_cache_outranks_higher_heuristic(monkeypatch):
    """A provider-measured prompt must not be inflated by the safety floor.

    Regression for conv=msn5s9y58cwopu: R11 measured 111,552 tokens, while the
    message heuristic estimated 164,562 and falsely crossed the 128K economic
    trigger.  The floor protects estimate-tier tokenizers only; applying it to
    a measured usage-cache hit turns an anti-undercount guard into a false
    compaction trigger.
    """
    import lib.token_counter as token_counter
    import lib.tasks_pkg.compaction._tokens as token_mod

    messages = [{'role': 'user', 'content': 'x' * 500_000}]
    monkeypatch.setattr(
        token_counter, 'count_tokens',
        lambda *a, **k: {'tokens': 111_552, 'method': 'usage_cache'})
    monkeypatch.setattr(token_mod, '_estimate_total_tokens', lambda _m: 164_562)

    count, method = _count_tokens_authoritative(
        messages,
        {'config': {'model': 'gpt-5.6-sol'}, 'convId': 'measured-wins'},
    )

    assert count == 111_552
    assert method == 'usage_cache'


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


@pytest.mark.unit
def test_economic_working_set_bounds_large_context_by_default(monkeypatch):
    monkeypatch.delenv('TOFU_WORKING_CONTEXT_TOKENS', raising=False)
    effective, window_safety, working_set = _compaction_trigger_threshold(
        {'config': {'model': 'kimi-k3'}}, context_limit=1_000_000)
    assert working_set == 128_000
    assert effective == 128_000
    assert window_safety > effective


@pytest.mark.unit
def test_working_set_request_override_and_zero_opt_out(monkeypatch):
    monkeypatch.setenv('TOFU_WORKING_CONTEXT_TOKENS', '96000')
    assert _working_set_token_limit(None) == 96_000

    task = {'config': {'compaction': {'workingSetTokens': 256_000}}}
    assert _working_set_token_limit(task) == 256_000

    disabled = {'config': {'compaction': {'workingSetTokens': 0}}}
    effective, window_safety, working_set = _compaction_trigger_threshold(
        disabled, context_limit=1_000_000)
    assert working_set == 0
    assert effective == window_safety
