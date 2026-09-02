"""Headless OpenAI/Anthropic streams expose only deliverable answer text.

The reported pain: a headless streaming client of the OpenAI/Anthropic compat
surfaces used to receive every content `delta` verbatim — INCLUDING each round's
pre-tool narration ("Let me search for that.") — because the generators
forwarded raw deltas and the un-portable DELTA_RESET signal never reached them.

Step 3 drives the headless output from the segment model instead: content
deltas are NOT forwarded (unclassifiable mid-stream, and a wire client can't
retract), thinking streams live, and the narration-free deliverable
(`deliverable_text` = `derive_content(segments)`) is emitted at `done`.

This suite holds that to the same ground-truth bar as steps 1-2: drive a REAL
multi-round `run_task` (narration round → web_search tool call → deliverable
answer) through the ACTUAL compat streaming generator and assert the streamed
bytes a headless client receives contain the deliverable and ZERO narration.

The suite drives a real multi-round task with a deterministic model stub and
checks both streaming and synchronous public response builders.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import sys

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

NARRATION = 'Let me search for that.'
ANSWER = 'The answer is 42, per the search results.'


def _seed_conv(conv_id):
    from tests._seed import seed_conversation
    messages = [
        {'role': 'user', 'content': 'search then answer', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    seed_conversation(conv_id, messages=messages, title='compat-narrator')


def _cleanup_conv(conv_id):
    from tests._seed import delete_conversation
    try:
        delete_conversation(conv_id)
    except Exception:
        pass


def _install_stub(monkeypatch):
    """Stub stream_llm_response so a real multi-round run_task executes:
    round 0 streams NARRATION + a web_search tool_call; round 1 streams the
    ANSWER. This is the exact shape that used to leak the narration."""
    from lib.agent_core.events import EventType, build_event
    import lib.tasks_pkg.handlers.search._core as search_core
    import lib.tasks_pkg.llm_fallback._call as llm_fb
    from lib.tasks_pkg.manager._events import append_event
    import tofu_search

    def _stub(task, body, tag='', on_tool_call_ready=None):
        if not task.get('_gt_tool_done'):
            task['_gt_tool_done'] = True
            with task['content_lock']:
                task['content'] += NARRATION
            append_event(task, build_event(EventType.DELTA, content=NARRATION))
            tc = {'id': 'call_gt_1', 'index': 0, 'type': 'function',
                  'function': {'name': 'web_search',
                               'arguments': _json.dumps({'query': 'gt query'})}}
            if on_tool_call_ready:
                try:
                    on_tool_call_ready(tc)
                except Exception:
                    pass
            return ({'role': 'assistant', 'content': NARRATION, 'tool_calls': [tc]},
                    'tool_calls',
                    {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
        for i, w in enumerate(ANSWER.split(' ')):
            cd = w + (' ' if i < len(ANSWER.split(' ')) - 1 else '')
            with task['content_lock']:
                task['content'] += cd
            append_event(task, build_event(EventType.DELTA, content=cd))
        return ({'role': 'assistant', 'content': ANSWER, 'tool_calls': []},
                'stop',
                {'prompt_tokens': 20, 'completion_tokens': 9, 'total_tokens': 29})

    def _stub_search(query, user_question='', freshness='', **kwargs):
        return [{'title': 'GT stub', 'snippet': 'deterministic',
                 'url': 'https://x.invalid', 'source': 'stub'}]

    monkeypatch.setattr(llm_fb, 'stream_llm_response', _stub)
    monkeypatch.setattr(tofu_search, 'perform_web_search', _stub_search)
    monkeypatch.setattr(search_core, 'perform_web_search', _stub_search)


def _run_produced_task(monkeypatch, conv_id):
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator.api import run_task
    _cleanup_conv(conv_id)
    _seed_conv(conv_id)
    _install_stub(monkeypatch)
    task = create_task(
        conv_id,
        [{'role': 'user', 'content': 'search then answer'}],
        {'model': 'test-model', 'projectEnabled': False, 'webSearchEnabled': True},
    user_id=1,
    )
    run_task(task)
    return task


def _drain(agen_factory):
    async def _collect():
        return [frame async for frame in agen_factory()]
    return ''.join(asyncio.new_event_loop().run_until_complete(_collect()))


# ═══════════════════════════════════════════════════════════
#  GROUND TRUTH — real run_task through the real compat generators
# ═══════════════════════════════════════════════════════════

class TestOpenAINarratorFix:
    def test_stream_has_answer_and_no_narration(self, monkeypatch):
        from lib.compat.openai import stream_openai_chunks
        task = _run_produced_task(monkeypatch, 'cv-narr-oai-' + str(id(self)))
        try:
            wire = _drain(lambda: stream_openai_chunks(task, model='m'))
            # The deliverable answer reaches the client.
            assert ANSWER in wire, f'answer missing from OpenAI stream: {wire[:400]}'
            # ZERO inter-round narration leaked.
            assert NARRATION not in wire, f'NARRATION LEAKED into OpenAI stream: {wire[:600]}'
            assert '[DONE]' in wire
        finally:
            _cleanup_conv(task['convId'])
    def test_sync_response_has_answer_and_no_narration(self, monkeypatch):
        from lib.compat.openai import build_openai_response
        task = _run_produced_task(monkeypatch, 'cv-narr-oais-' + str(id(self)))
        try:
            resp = build_openai_response(task, model='m')
            content = resp['choices'][0]['message']['content']
            assert ANSWER in content
            assert NARRATION not in content
        finally:
            _cleanup_conv(task['convId'])


class TestAnthropicNarratorFix:
    def test_stream_has_answer_and_no_narration(self, monkeypatch):
        from lib.compat.anthropic import stream_anthropic_chunks
        task = _run_produced_task(monkeypatch, 'cv-narr-ant-' + str(id(self)))
        try:
            wire = _drain(lambda: stream_anthropic_chunks(task, model='m'))
            assert ANSWER in wire, f'answer missing from Anthropic stream: {wire[:400]}'
            assert NARRATION not in wire, f'NARRATION LEAKED into Anthropic stream: {wire[:600]}'
            assert 'message_stop' in wire
        finally:
            _cleanup_conv(task['convId'])

    def test_sync_response_has_answer_and_no_narration(self, monkeypatch):
        from lib.compat.anthropic import build_anthropic_response
        task = _run_produced_task(monkeypatch, 'cv-narr-ants-' + str(id(self)))
        try:
            resp = build_anthropic_response(task, model='m')
            text_blocks = [b['text'] for b in resp['content'] if b.get('type') == 'text']
            joined = '\n'.join(text_blocks)
            assert ANSWER in joined
            assert NARRATION not in joined
        finally:
            _cleanup_conv(task['convId'])
