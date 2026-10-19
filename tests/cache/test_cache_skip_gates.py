"""Pin the add_cache_breakpoints Phase 0/0.5 skip gates.

A steady-state round whose history is already block-normalized and carries no
cache_control markers must not pay the per-block strip/rebuild passes. The
gates skip work only when there is nothing to strip or wrap; a marker or a
bare-str content still drives the full pass byte-identically.
"""
from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.unit

from lib.llm.cache import add_cache_breakpoints

MODEL = 'aws.claude-opus-4.8'


def _body(messages, tools=None):
    return {'model': MODEL, 'messages': messages, 'max_tokens': 100,
            'tools': tools if tools is not None else [
                {'type': 'function',
                 'function': {'name': 'run_command', 'description': 'run',
                              'parameters': {'type': 'object'}}}]}


def test_clean_block_history_skips_strip_and_normalize():
    """No markers + all-block content → no message is rebuilt in place."""
    messages = [
        {'role': 'system', 'content': [{'type': 'text', 'text': 'S' * 60}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'go'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'working'}],
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'run_command', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1',
         'content': [{'type': 'text', 'text': 'result'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'next'}]},
    ]
    body = _body(copy.deepcopy(messages))
    original_ids = [id(m) for m in body['messages']]
    add_cache_breakpoints(body, log_prefix='')
    # The tail marker wraps the tail message into a new dict, but no marker-
    # strip or str→block wrap should rebuild the buried history carriers.
    assert [id(m) for m in body['messages'][:4]] == original_ids[:4]


def test_str_content_still_normalizes_under_the_gate():
    """A bare-str content must still be wrapped even when no markers exist —
    the gate detects it and runs the full normalize pass."""
    messages = [
        {'role': 'system', 'content': 'S' * 60},
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': 'working',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'run_command', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'},
        {'role': 'user', 'content': 'next'},
    ]
    body = _body(copy.deepcopy(messages))
    add_cache_breakpoints(body, log_prefix='')
    # The bare-str user/tool contents are wrapped to single-block form.
    assert isinstance(body['messages'][1]['content'], list)
    assert isinstance(body['messages'][3]['content'], list)


def test_existing_marker_forces_strip_pass():
    """A carrier that still holds a cache_control block must be stripped even
    when everything else is clean — the gate must not skip it."""
    messages = [
        {'role': 'system', 'content': 'S' * 60},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': 'go',
             'cache_control': {'type': 'ephemeral'}}]},
        {'role': 'assistant', 'content': 'working',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'run_command', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'},
        {'role': 'user', 'content': 'next'},
    ]
    body = _body(copy.deepcopy(messages))
    add_cache_breakpoints(body, log_prefix='')
    # The stale marker on the buried user message is stripped.
    buried = body['messages'][1]['content']
    assert isinstance(buried, list)
    assert all('cache_control' not in b for b in buried
               if isinstance(b, dict))
