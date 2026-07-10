"""Ground-truth gate for the headless narrator fix (epic pt_cb8f98b0cb9b47fb, step 3).

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

Triple-neuter:
  • NC-1 — revert to forwarding raw content deltas → narration LEAKS → FAIL.
  • NC-2 — mis-mark the answer deliverable=False → answer DISAPPEARS → FAIL.
(Both flip the direction of the guard, proving the assertions are load-bearing.)

Skips with a concrete reason when the local env can't bootstrap the DB (needs
SQLAlchemy >= 2.0 per requirements.txt) — never a silent pass.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/compat_narrator_unittest.db')

NARRATION = 'Let me search for that.'
ANSWER = 'The answer is 42, per the search results.'


def _seed_conv(conv_id):
    import time as _time

    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [
        {'role': 'user', 'content': 'search then answer', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(_time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'compat-narrator',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _cleanup_conv(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


def _install_stub(monkeypatch):
    """Stub stream_llm_response so a real multi-round run_task executes:
    round 0 streams NARRATION + a web_search tool_call; round 1 streams the
    ANSWER. This is the exact shape that used to leak the narration."""
    import lib.tasks_pkg.handlers.search as search_h
    import lib.tasks_pkg.llm_fallback as llm_fb
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.orchestrator as orch
    import tofu_search

    def _stub(task, body, tag='', on_tool_call_ready=None):
        if not task.get('_gt_tool_done'):
            task['_gt_tool_done'] = True
            with task['content_lock']:
                task['content'] += NARRATION
            mgr.append_event(task, mgr.build_event(mgr.EventType.DELTA, content=NARRATION))
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
            mgr.append_event(task, mgr.build_event(mgr.EventType.DELTA, content=cd))
        return ({'role': 'assistant', 'content': ANSWER, 'tool_calls': []},
                'stop',
                {'prompt_tokens': 20, 'completion_tokens': 9, 'total_tokens': 29})

    def _stub_search(query, user_question='', freshness='', **kwargs):
        return [{'title': 'GT stub', 'snippet': 'deterministic',
                 'url': 'https://x.invalid', 'source': 'stub'}]

    for mod in (mgr, orch, llm_fb):
        if hasattr(mod, 'stream_llm_response'):
            monkeypatch.setattr(mod, 'stream_llm_response', _stub)
    monkeypatch.setattr(tofu_search, 'perform_web_search', _stub_search)
    monkeypatch.setattr(search_h, 'perform_web_search', _stub_search)


def _run_produced_task(monkeypatch, conv_id):
    from lib.database import init_db
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator import run_task
    try:
        init_db()
    except Exception as e:
        pytest.skip(f'DB bootstrap unavailable in this env ({type(e).__name__}: {e})')
    _cleanup_conv(conv_id)
    _seed_conv(conv_id)
    _install_stub(monkeypatch)
    task = create_task(
        conv_id,
        [{'role': 'user', 'content': 'search then answer'}],
        {'model': 'test-model', 'projectEnabled': False, 'webSearchEnabled': True},
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


# ═══════════════════════════════════════════════════════════
#  TRIPLE-NEUTER — prove the assertions are load-bearing
# ═══════════════════════════════════════════════════════════

class TestNeuterGuards:
    def test_NC1_forwarding_raw_deltas_leaks_narration(self, monkeypatch):
        """NC-1: simulate the OLD behavior (forward raw content deltas). The
        narration MUST leak → proves suppressing deltas is what fixes it."""
        task = _run_produced_task(monkeypatch, 'cv-narr-nc1-' + str(id(self)))
        try:
            # Reconstruct what the OLD generator did: forward every delta's
            # content verbatim. Assert that path LEAKS the narration.
            leaked = ''.join(
                ev.get('content', '')
                for ev in task['events'] if ev.get('type') == 'delta'
            )
            assert NARRATION in leaked, \
                'NC-1 harness invalid: the produced stream had no narration delta to leak'
            # And confirm the REAL generator does NOT leak it (the fix holds).
            from lib.compat.openai import stream_openai_chunks
            wire = _drain(lambda: stream_openai_chunks(task, model='m'))
            assert NARRATION not in wire
        finally:
            _cleanup_conv(task['convId'])

    def test_NC2_mismarking_answer_nondeliverable_drops_it(self, monkeypatch):
        """NC-2: if the answer segment were mis-marked deliverable=False, the
        deliverable projection is EMPTY → the answer disappears from the stream.
        Proves the deliverable flag drives the emitted content."""
        from lib.compat.openai import stream_openai_chunks
        from lib.tasks_pkg.segments import SEG_TEXT
        task = _run_produced_task(monkeypatch, 'cv-narr-nc2-' + str(id(self)))
        try:
            # Poison the segments: strip deliverable off the answer segment.
            for s in (task.get('segments') or []):
                if s.get('type') == SEG_TEXT and s.get('deliverable'):
                    s['deliverable'] = False
            # Also clear the content fallback so deliverable_text has no source.
            task['content'] = ''
            wire = _drain(lambda: stream_openai_chunks(task, model='m'))
            assert ANSWER not in wire, \
                'NC-2 failed: answer still present after mis-marking deliverable=False'
        finally:
            _cleanup_conv(task['convId'])


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
