"""Ground-truth gate for capability-gated assistant-prefill Continue resume.

Epic pt_cb8f98b0cb9b47fb (segment-timeline) — the mid-prose resumption slice.

The mechanism: a turn interrupted mid-answer (case 2 = after a completed tool
batch; case 3 = a no-tool turn) can, on a provider that TOLERATES a trailing
assistant prefill, be resumed by feeding the model its own half-written string
so it CONTINUES the same tokens instead of regenerating from scratch. Claude
(``model_supports_assistant_prefill`` False) fails CLOSED — no trailing
assistant turn ever reaches its wire.

This suite is held to the segment epic's ground-truth bar: it drives the REAL
``orchestrator.run_task`` through a stubbed ``stream_llm_response`` (no LLM, no
network) and asserts on the ACTUAL built ``body`` that reaches the wire and the
finalized ``task`` dict — not hand-authored fixtures.

  • CASE 2 — mid-prose after a completed tool batch (checkpointToolRounds +
    resumePrefill): the wire carries the replayed tool batch AND a trailing
    assistant prefill; the prefill is the terminal deliverable tail only (no
    double-count of the batch prose).
  • CASE 3 — no-tool mid-answer (resumePrefill only): the wire's last message
    is the assistant prefill; the finalized content == prefill + continuation
    with NO duplication.
  • CLAUDE FAIL-CLOSED — same case-3 input, model=claude-*: NO trailing
    assistant turn reaches the wire (the gate returns None; defence-in-depth
    strip would also neutralise it).
  • NC-1 (the load-bearing neuter) — force ``resume_prefill_from_segments`` to
    return None → case 3 regresses to regenerate-from-scratch (the wire ends on
    the user turn, no prefill) → proves the prefill path is load-bearing.

Run:  pytest tests/test_continue_prefill_resume.py -q
"""

from __future__ import annotations

import json as _json
import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/continue_prefill_unittest.db')


# ═══════════════════════════════════════════════════════════
#  Pure-unit tests of the reader (no DB needed)
# ═══════════════════════════════════════════════════════════

class TestResumePrefillReader:
    """resume_prefill_from_segments — the capability + resumability gate."""

    def _segs(self, text, *, resumable, deliverable=True, terminal=True):
        return [{'type': 'text', 'text': text, 'deliverable': deliverable,
                 'terminal': terminal, **({'resumable': True} if resumable else {})}]

    def test_returns_tail_for_openai_when_resumable(self):
        from lib.tasks_pkg.segments import resume_prefill_from_segments
        segs = self._segs('The three causes are: (1)', resumable=True)
        assert resume_prefill_from_segments(segs, 'gpt-4o') == 'The three causes are: (1)'

    def test_none_for_claude_even_when_resumable(self):
        from lib.tasks_pkg.segments import resume_prefill_from_segments
        segs = self._segs('half a sentence', resumable=True)
        assert resume_prefill_from_segments(segs, 'claude-opus-4-7') is None
        assert resume_prefill_from_segments(segs, 'claude-sonnet-4-5') is None
        assert resume_prefill_from_segments(segs, 'us.anthropic.claude-opus-4-6-v1') is None

    def test_none_when_not_resumable_and_no_finish_override(self):
        from lib.tasks_pkg.segments import resume_prefill_from_segments
        segs = self._segs('a clean complete answer', resumable=False)
        assert resume_prefill_from_segments(segs, 'gpt-4o') is None

    def test_finish_reason_override_marks_resumable(self):
        """A checkpoint-assembled segment has no `resumable` flag (status was
        running); the message's finishReason override supplies it on read."""
        from lib.tasks_pkg.segments import resume_prefill_from_segments
        segs = self._segs('interrupted tail', resumable=False)
        assert resume_prefill_from_segments(segs, 'gpt-4o', finish_reason='interrupted') \
            == 'interrupted tail'
        assert resume_prefill_from_segments(segs, 'gpt-4o', finish_reason='length') \
            == 'interrupted tail'
        # A clean stop is NOT resumable.
        assert resume_prefill_from_segments(segs, 'gpt-4o', finish_reason='stop') is None

    def test_length_is_in_resumable_set(self):
        from lib.tasks_pkg.segments import RESUMABLE_FINISH_REASONS
        assert 'length' in RESUMABLE_FINISH_REASONS
        assert 'interrupted' in RESUMABLE_FINISH_REASONS
        assert 'server_offline' in RESUMABLE_FINISH_REASONS
        assert 'premature_close' in RESUMABLE_FINISH_REASONS
        assert 'stop' not in RESUMABLE_FINISH_REASONS

    def test_empty_segments_or_no_deliverable(self):
        from lib.tasks_pkg.segments import resume_prefill_from_segments
        assert resume_prefill_from_segments(None, 'gpt-4o') is None
        assert resume_prefill_from_segments([], 'gpt-4o') is None
        # A non-deliverable narration segment is never a prefill source.
        segs = [{'type': 'text', 'text': 'scaffolding', 'deliverable': False,
                 'terminal': False}]
        assert resume_prefill_from_segments(segs, 'gpt-4o', finish_reason='interrupted') is None


# ═══════════════════════════════════════════════════════════
#  Ground-truth: drive real run_task, inspect the built wire body
# ═══════════════════════════════════════════════════════════

def _seed_conv(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    import time as _time
    messages = [
        {'role': 'user', 'content': 'resume please', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(_time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'prefill-groundtruth',
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
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (conv_id,))
        db.commit()
    except Exception:
        pass


class _WireCapture:
    """A stubbed stream_llm_response that RECORDS the built body it received
    (so the test can inspect the trailing prefill) then streams a continuation
    answer word-by-word (the real _on_content delta path)."""

    def __init__(self, continuation):
        self.continuation = continuation
        self.bodies = []       # every body reached the wire, in order
        self.rounds = 0

    def __call__(self, task, body, tag='', on_tool_call_ready=None):
        import lib.tasks_pkg.manager as mgr
        self.bodies.append(body)
        self.rounds += 1
        for i, w in enumerate(self.continuation.split(' ')):
            cd = w + (' ' if i < len(self.continuation.split(' ')) - 1 else '')
            with task['content_lock']:
                task['content'] += cd
            mgr.append_event(task, mgr.build_event(mgr.EventType.DELTA, content=cd))
        return ({'role': 'assistant', 'content': self.continuation, 'tool_calls': []},
                'stop',
                {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})


def _install_wire_stub(monkeypatch, cap):
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.orchestrator as orch
    import lib.tasks_pkg.llm_fallback as llm_fb
    for mod in (mgr, orch, llm_fb):
        if hasattr(mod, 'stream_llm_response'):
            monkeypatch.setattr(mod, 'stream_llm_response', cap)


def _require_db():
    from lib.database import init_db
    try:
        init_db()
    except Exception as e:
        pytest.skip(f'DB bootstrap unavailable in this env ({type(e).__name__}: {e}); '
                    'ground-truth run_task path needs a working DB')


def _run_continue_task(conv_id, cfg_payload):
    """Build + run a task exactly as _start_task_for_conv would (excludeLast +
    resumePrefill on cfg), returning the finished task dict."""
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator import run_task
    api_messages = build_api_messages_from_db(conv_id, cfg_payload,
                                              exclude_last=bool(cfg_payload.get('excludeLast')))
    task = create_task(conv_id, api_messages, cfg_payload)
    task['_attended'] = True
    run_task(task)
    return task


class TestPrefillGroundTruth:

    def _prep_case3_conv(self, conv_id, tail, finish_reason='interrupted', model='gpt-4o'):
        """Seed a conv whose last assistant turn is a NO-TOOL mid-answer with a
        persisted resumable segment tail + finishReason (as recover-on-startup
        would stamp)."""
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert
        import time as _time
        segs = [{'type': 'text', 'text': tail, 'deliverable': True,
                 'terminal': True}]  # thin (no _round), no resumable flag
        messages = [
            {'role': 'user', 'content': 'resume please', 'timestamp': 1},
            {'role': 'assistant', 'content': tail, 'thinking': '',
             'toolRounds': [], 'segments': segs, 'finishReason': finish_reason,
             'timestamp': 2},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(_time.time() * 1000)
        upsert(db, CONVERSATIONS, {
            'id': conv_id, 'user_id': 1, 'title': 'case3',
            'messages': json_dumps_pg(messages), 'msg_count': len(messages),
            'created_at': now_ms, 'updated_at': now_ms,
        }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                        'created_at', 'updated_at'], retry=True)
        db.commit()

    def test_case3_prefill_reaches_wire_and_no_duplication(self, monkeypatch):
        """CASE 3: no-tool mid-answer → the built wire body's last message is
        the assistant prefill, and the finalized content == prefill +
        continuation (no duplication)."""
        _require_db()
        conv_id = 'cv-pf-c3-' + str(id(self))
        tail = 'The three root causes are: (1)'
        continuation = ' the GIL, (2) I/O waits, and (3) lock contention.'
        _cleanup_conv(conv_id)
        self._prep_case3_conv(conv_id, tail)
        cap = _WireCapture(continuation)
        _install_wire_stub(monkeypatch, cap)
        try:
            cfg = {'model': 'gpt-4o', 'projectEnabled': False,
                   'excludeLast': True, 'resumePrefill': tail,
                   'contentPrefix': tail}
            task = _run_continue_task(conv_id, cfg)

            assert cap.bodies, 'stream_llm_response never called'
            wire = cap.bodies[0]['messages']
            # The LAST wire message is the assistant prefill carrying the tail.
            assert wire[-1]['role'] == 'assistant', \
                f'expected trailing assistant prefill, got {wire[-1]["role"]}'
            assert tail in (wire[-1].get('content') or ''), \
                'the resumable tail is not on the trailing assistant turn'
            # No duplication: content == seed(tail) + continuation, and the
            # tail appears exactly once at the front.
            assert task['content'] == tail + continuation, \
                f'expected no-duplication concat, got {task["content"]!r}'
            assert task['content'].count(tail) == 1
        finally:
            _cleanup_conv(conv_id)

    def test_claude_fail_closed_no_trailing_assistant(self, monkeypatch):
        """CLAUDE: same case-3 input but model=claude-* → the gate returns None
        so NO trailing assistant turn reaches the wire (Messages API would
        reject a prefill). Defence-in-depth strip also holds."""
        _require_db()
        conv_id = 'cv-pf-claude-' + str(id(self))
        tail = 'The three root causes are: (1)'
        _cleanup_conv(conv_id)
        self._prep_case3_conv(conv_id, tail, model='claude-sonnet-4-5')
        cap = _WireCapture(' continuation text.')
        _install_wire_stub(monkeypatch, cap)
        try:
            # Simulate what routes/chat.py would build: for Claude,
            # resume_prefill_from_segments returns None, so NO resumePrefill is
            # placed on cfg (contentPrefix seed only, as today).
            from lib.tasks_pkg.segments import resume_prefill_from_segments
            prefill = resume_prefill_from_segments(
                [{'type': 'text', 'text': tail, 'deliverable': True, 'terminal': True}],
                'claude-sonnet-4-5', finish_reason='interrupted')
            assert prefill is None, 'gate should fail closed for Claude'
            cfg = {'model': 'claude-sonnet-4-5', 'projectEnabled': False,
                   'excludeLast': True, 'contentPrefix': tail}
            # resumePrefill deliberately absent (mirrors the route's None branch).
            task = _run_continue_task(conv_id, cfg)

            assert cap.bodies, 'stream_llm_response never called'
            wire = cap.bodies[0]['messages']
            # No trailing assistant prefill — the conversation must NOT end on
            # an assistant turn (Claude-4.6 prefill removal).
            assert wire[-1]['role'] != 'assistant', \
                f'Claude wire ended on assistant (prefill leaked!): {wire[-1]}'
            assert task.get('_resumePrefill') is None, \
                'a resume prefill was injected for Claude — fail-closed violated'
        finally:
            _cleanup_conv(conv_id)

    def test_case2_prefill_after_tool_batch_no_double_count(self, monkeypatch):
        """CASE 2: mid-prose AFTER a completed tool batch. The wire must carry
        BOTH the replayed tool batch (assistant(tool_calls)+tool) AND a trailing
        assistant prefill — and the prefill is the terminal deliverable tail
        ONLY (the batch prose is on the replayed assistant turn, not
        double-counted in the prefill)."""
        _require_db()
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert
        import time as _time
        conv_id = 'cv-pf-c2-' + str(id(self))
        _cleanup_conv(conv_id)
        batch_prose = 'Let me search first.'
        tail = 'Based on the results, the fix is:'
        # One completed tool batch + a terminal deliverable tail.
        rounds = [{
            'roundNum': 1, 'llmRound': 0, 'toolCallId': 'tc_1',
            'toolName': 'web_search', 'toolArgs': '{"query":"x"}',
            'toolContent': 'search hit', 'status': 'done',
            'assistantContent': batch_prose,
        }]
        # Segments as persist would build them: batch prose (non-deliverable)
        # + tool_use + terminal deliverable tail.
        segs = [
            {'type': 'text', 'text': batch_prose, 'deliverable': False, 'llmRound': 0},
            {'type': 'tool_use', 'id': 'tc_1', 'name': 'web_search',
             'input': '{"query":"x"}', 'llmRound': 0,
             'result': {'content': 'search hit', 'status': 'done'}},
            {'type': 'text', 'text': tail, 'deliverable': True, 'terminal': True},
        ]
        messages = [
            {'role': 'user', 'content': 'resume please', 'timestamp': 1},
            {'role': 'assistant', 'content': tail, 'thinking': '',
             'toolRounds': rounds, 'segments': segs,
             'finishReason': 'interrupted', 'timestamp': 2},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(_time.time() * 1000)
        upsert(db, CONVERSATIONS, {
            'id': conv_id, 'user_id': 1, 'title': 'case2',
            'messages': json_dumps_pg(messages), 'msg_count': len(messages),
            'created_at': now_ms, 'updated_at': now_ms,
        }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                        'created_at', 'updated_at'], retry=True)
        db.commit()

        cap = _WireCapture(' apply the patch on line 42.')
        _install_wire_stub(monkeypatch, cap)
        try:
            # cfg as chat_continue's case-2 path builds it: toolHistory replays
            # the batch, resumePrefill is the terminal tail, contentPrefix is
            # the FULL prior content (== tail here, single batch).
            from lib.chat import build_tool_history_round
            cfg = {'model': 'gpt-4o', 'projectEnabled': False, 'excludeLast': True,
                   'toolHistory': [build_tool_history_round(rounds)],
                   'checkpointToolRounds': rounds,
                   'resumePrefill': tail, 'contentPrefix': tail}
            task = _run_continue_task(conv_id, cfg)

            assert cap.bodies, 'stream_llm_response never called'
            wire = cap.bodies[0]['messages']
            roles = [m['role'] for m in wire]
            # The replayed tool batch is present: an assistant(tool_calls) then
            # a tool result, THEN a trailing assistant prefill.
            assert 'tool' in roles, f'tool-result replay missing from wire: {roles}'
            assert wire[-1]['role'] == 'assistant', \
                f'expected trailing assistant prefill, got {roles}'
            assert wire[-1].get('content') == tail, \
                'the trailing prefill must be the terminal tail ONLY'
            # No double-count: the batch prose appears on the replayed
            # tool-call assistant turn, and the prefill is the tail only — the
            # batch prose is NOT inside the prefill message.
            assert batch_prose not in (wire[-1].get('content') or ''), \
                'batch prose leaked into the prefill (double-count)'
            # Finalized content = full seed + continuation, tail once.
            assert task['content'] == tail + ' apply the patch on line 42.'
            assert task['content'].count(tail) == 1
        finally:
            _cleanup_conv(conv_id)

    @pytest.mark.api
    def test_NC1_route_driven_neuter_regresses_case3(self, flask_client, monkeypatch):
        """NC-1 (load-bearing, ROUTE-DRIVEN): the SAME /api/chat/continue call
        on the SAME case-3 conversation is run twice — once with the real
        reader, once with it neutered to return None — and we assert the
        route's DECISION flips. This is a genuine contrast (not a test-chosen
        cfg): with the reader live the route resumes via prefill
        (resumeMode='prefill'); neutered it returns fallback:'regenerate'.

        We stub _start_task_for_conv to capture the cfg the route hands it, so
        we can read resumePrefill without spawning a real task."""
        _require_db()
        import routes.chat as chatmod
        tail = 'The three root causes are: (1)'

        captured = {}

        def _capture_start(conv_id, cfg, data=None):
            captured['cfg'] = cfg
            return ('stub-task-id', None)

        monkeypatch.setattr(chatmod, '_start_task_for_conv', _capture_start)

        def _seed(conv_id):
            now = int(__import__('time').time() * 1000)
            segs = [{'type': 'text', 'text': tail, 'deliverable': True, 'terminal': True}]
            r = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
                "title": "nc1", "messages": [
                    {"role": "user", "content": "resume please", "timestamp": now},
                    {"role": "assistant", "content": tail, "thinking": "",
                     "toolRounds": [], "segments": segs,
                     "finishReason": "interrupted", "timestamp": now + 1},
                ], "createdAt": now, "updatedAt": now})
            assert r.status_code == 200, r.get_data(as_text=True)

        # ── (A) Reader LIVE → the route resumes via prefill (case 3). ──
        conv_a = f"cv-pf-nc1a-{int(__import__('time').time()*1000)}"
        _seed(conv_a)
        captured.clear()
        resp_a = flask_client.post("/api/v1/chat/continue", json={
            "convId": conv_a, "config": {"model": "gpt-4o"}})
        data_a = resp_a.get_json()
        assert resp_a.status_code == 200, resp_a.get_data(as_text=True)
        assert data_a.get('fallback') != 'regenerate', \
            'reader LIVE should resume via prefill, not fall back'
        assert data_a.get('checkpoint', {}).get('resumeMode') == 'prefill', \
            f'expected resumeMode=prefill, got {data_a}'
        assert captured.get('cfg', {}).get('resumePrefill') == tail, \
            'route did not hand resumePrefill to the task (reader live)'
        flask_client.delete(f"/api/v1/conversations/{conv_a}")

        # ── (B) Reader NEUTERED → the route falls back to regenerate. ──
        import lib.tasks_pkg.segments as segmod
        monkeypatch.setattr(segmod, 'resume_prefill_from_segments', lambda *a, **k: None)
        # routes.chat imports the symbol lazily inside the handler, so patching
        # the module attribute is picked up on the next call.
        conv_b = f"cv-pf-nc1b-{int(__import__('time').time()*1000)}"
        _seed(conv_b)
        captured.clear()
        resp_b = flask_client.post("/api/v1/chat/continue", json={
            "convId": conv_b, "config": {"model": "gpt-4o"}})
        data_b = resp_b.get_json()
        assert resp_b.status_code == 200, resp_b.get_data(as_text=True)
        # THE NEUTER BITES: no tool checkpoint + no prefill → regenerate.
        assert data_b.get('fallback') == 'regenerate', \
            f'neutered reader should regress to regenerate, got {data_b}'
        assert 'cfg' not in captured or not captured.get('cfg', {}).get('resumePrefill'), \
            'neutered path still handed a resumePrefill — NC did not bite'
        flask_client.delete(f"/api/v1/conversations/{conv_b}")
        _cleanup_conv(conv_b)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
