"""Tests for the Codex Responses-API SSE path through ``SSEAccumulator``.

``_feed_codex`` used to hand-reimplement content / thinking / tool-call-delta
accumulation instead of routing the translator's OpenAI-shaped chunks through
the shared ``_process_openai_chunk`` (the way ``_feed_anthropic`` does). That
duplication carried two latent bugs these tests pin against regression:

  1. It never fired ``on_tool_call_ready`` → Codex got no incremental
     multi-tool prefetch that every other provider gets.
  2. It gated content/thinking *accumulation* on the streaming callback
     (``if _c and self.on_content``), so a caller with NO ``on_content``
     silently lost the whole response body.

No real network, no Flask, no DB — just the translator + accumulator.
"""

from __future__ import annotations

import json
import os
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.llm._sse_core import SSEAccumulator
from lib.llm.diagnostics import RawSSEDumper
from lib.oauth.codex import CodexSSETranslator


def _acc(model='gpt-5.2-codex', **kw):
    body = {'model': model, 'messages': []}
    return SSEAccumulator(
        body, 'trace', RawSSEDumper(model, 'trace', body),
        CodexSSETranslator(model=model), time.time(), **kw)


def _feed(acc, events):
    """Feed a list of Codex Responses-API event dicts (raw, pre-translation)."""
    for ev in events:
        if acc.feed_line('data: ' + json.dumps(ev)):
            break


def _text_delta(t):
    return {'type': 'response.output_text.delta', 'delta': t}


def _reason_delta(t):
    return {'type': 'response.reasoning_summary_text.delta', 'delta': t}


def _fn_added(call_id, name):
    return {'type': 'response.output_item.added',
            'item': {'type': 'function_call', 'call_id': call_id, 'name': name}}


def _fn_args(delta):
    return {'type': 'response.function_call_arguments.delta', 'delta': delta}


def _completed(with_tool=False):
    output = [{'type': 'function_call'}] if with_tool else []
    return {'type': 'response.completed',
            'response': {'output': output,
                         'usage': {'input_tokens': 10, 'output_tokens': 5}}}


class TestCodexContentAccumulatesWithoutCallback:
    """BUG 1: content/thinking must accumulate even with no streaming callback."""

    def test_content_accumulates_no_callback(self):
        acc = _acc()  # no on_content / on_thinking
        _feed(acc, [_text_delta('Hello '), _text_delta('world'), _completed()])
        acc.feed_line('data: [DONE]')
        msg, finish, _u = acc.finalize()
        assert msg['content'] == 'Hello world'
        assert finish == 'stop'

    def test_thinking_accumulates_no_callback(self):
        acc = _acc()
        _feed(acc, [_reason_delta('Let me '), _reason_delta('think.'),
                    _text_delta('Answer.'), _completed()])
        acc.feed_line('data: [DONE]')
        msg, _fr, _u = acc.finalize()
        assert msg['reasoning_content'] == 'Let me think.'
        assert msg['content'] == 'Answer.'

    def test_callbacks_still_fire_when_present(self):
        seen_content, seen_thinking = [], []
        acc = _acc(on_content=seen_content.append, on_thinking=seen_thinking.append)
        _feed(acc, [_reason_delta('R'), _text_delta('C'), _completed()])
        acc.feed_line('data: [DONE]')
        acc.finalize()
        assert seen_content == ['C']
        assert seen_thinking == ['R']


class TestCodexToolCallReady:
    """BUG 2: on_tool_call_ready must fire for Codex tool calls."""

    def test_on_tool_call_ready_fires(self):
        ready = []
        acc = _acc(on_tool_call_ready=lambda tc: ready.append(tc['function']['name']))
        _feed(acc, [
            _fn_added('call_1', 'read_files'),
            _fn_args('{"path":'),
            _fn_args('"a.py"}'),
            _completed(with_tool=True),
        ])
        acc.feed_line('data: [DONE]')
        acc.fire_final_tool_callback()
        msg, finish, _u = acc.finalize()
        assert finish == 'tool_calls'
        assert msg['tool_calls'][0]['function']['name'] == 'read_files'
        assert msg['tool_calls'][0]['function']['arguments'] == '{"path":"a.py"}'
        assert msg['tool_calls'][0]['id'] == 'call_1'
        # The final tool call is surfaced via the callback (prefetch hook).
        assert ready == ['read_files']

    def test_tool_args_accumulate_across_deltas(self):
        acc = _acc()
        _feed(acc, [
            _fn_added('call_9', 'grep_search'),
            _fn_args('{"pat'),
            _fn_args('tern":"x"}'),
            _completed(with_tool=True),
        ])
        acc.feed_line('data: [DONE]')
        msg, _fr, _u = acc.finalize()
        assert msg['tool_calls'][0]['function']['arguments'] == '{"pattern":"x"}'


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v', '-s'])
