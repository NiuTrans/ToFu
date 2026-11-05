"""Behavior guard: confirmed canned greeting must NOT overwrite prior work,
and valid compact final answers must be delivered unchanged.

WHY
---
2026-07-28 09:51–10:45 incident: the example-corp gateway intermittently returned a
29-char canned greeting ("Hi! How can I help you today?") with
``finish_reason=stop`` for every Opus 5 request, 68 times, each with a real
M-TraceId. Tasks that had just spent N rounds doing real tool work (e.g.
conv ms40kfqq: 2×searchFlights ×195KB + run_command) then had their FINAL round
replaced by that greeting — and ``_finalize_and_emit_done`` persisted
``task['content']`` (the last round's 29 chars) over the whole accumulated
answer. The user saw one greeting; the real deliverable was silently lost.

``_check_suspicious_completion`` already DETECTS this shape
(``short_content_after_tool_calls(<50chars)``, fired 54× that day) — but it
only ``logger.warning``s. Nothing stops the overwrite.

This guard pins the interception BEHAVIOUR (assert the RESULT, not the
implementation — charter: "assert outcomes, not code anchors"):

  * when the terminal round is a confirmed incongruent greeting or blank
    ``stop`` after tool work AND the
    turn accumulated substantial inter-round prose (``assistantContent``
    on prior tool rounds, kept verbatim by ``_discard_pretool_prose``),
    the delivered content MUST keep that accumulated work — not collapse
    to the short residue;
  * a genuinely short turn (no substantial prior narration — the model
    really did just answer briefly) is left untouched;
  * a normal long final answer persists byte-identically (no
    interception, no false positive — the complement).

NEUTER (reverse proof): remove the interception call in ``_finalize.py``
and the first test flips red; make it fire unconditionally and the
complement tests flip red. Both directions are pinned here.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def _make_task(content='', thinking='', tool_rounds=None, aborted=False,
               tid='susp-1'):
    return {
        'id': tid,
        'convId': 'csusp',
        'content': content,
        'thinking': thinking,
        'aborted': aborted,
        'error': None,
        'created_at': time.time() - 60,
        'content_lock': threading.Lock(),
        'toolRounds': list(tool_rounds or []),
        'config': {},
    }


def _round(assistant_content='', tool_name='web_search'):
    return {
        'toolName': tool_name,
        'assistantContent': assistant_content,
        'toolContent': 'result',
        'status': 'done',
    }


@pytest.mark.unit
class TestSuspiciousShortCompletionPreservation:
    """The interception: recover accumulated narration when the terminal
    round is a suspicious short stop after tool work."""

    def _run(self, task, finish='stop', tool_happened=True):
        from lib.tasks_pkg.orchestrator._finalize import (
            _maybe_preserve_accumulated_on_suspicion,
        )
        return _maybe_preserve_accumulated_on_suspicion(
            task, finish, tool_happened,
            messages=task.get("messages", [{"role": "user", "content": "Compute the requested totals."}]))

    def test_recovers_accumulated_narration_over_short_residue(self):
        """The ms40kfqq shape: R1..R3 streamed real prose (kept on the tool
        rounds as assistantContent), R4 was replaced by a 29-char greeting.
        The delivered content MUST keep the accumulated work, not the
        greeting."""
        narration_r1 = '我先查一下去程航班。' * 6          # ~60 chars
        narration_r2 = '去程有两个不错的选择，接着查返程。' * 6  # ~96 chars
        task = _make_task(
            content='Hi! How can I help you today?',   # 29-char residue
            tool_rounds=[
                _round(assistant_content=narration_r1),
                _round(assistant_content=narration_r2),
                _round(assistant_content=''),
            ],
        )
        recovered = self._run(task, finish='stop', tool_happened=True)
        assert recovered is True
        assert 'Hi! How can I help you today?' not in task['content']
        assert narration_r1 in task['content']
        assert narration_r2 in task['content']

    @pytest.mark.parametrize('answer', [
        '{"count":135,"total":6480}',
        '{"dataset":"small","state":"ready"}',
        '{"id":"beta"}', '0', 'false', 'null', '[]', '{}',
        'ready', '好的。', '135 records, total 6480.',
    ])
    def test_preserves_short_final_answer_after_substantial_tool_narration(self, answer):
        task = _make_task(
            content=answer,
            tool_rounds=[_round(assistant_content='Let me compute the totals. ' * 8)],
        )
        assert self._run(task) is False
        assert task['content'] == answer

    @pytest.mark.parametrize('messages', [
        [], [{'role': 'user', 'content': '你好'}],
    ])
    def test_preserves_greeting_without_evidence_of_incongruence(self, messages):
        answer = 'Hi! How can I help you today?'
        task = _make_task(
            content=answer,
            tool_rounds=[_round(assistant_content='Previous work. ' * 8)],
        )
        task['messages'] = messages
        assert self._run(task) is False
        assert task['content'] == answer

    def test_recovers_blank_terminal_content_after_tool_narration(self):
        narration = 'Verified previous work. ' * 8
        task = _make_task(content='', tool_rounds=[_round(assistant_content=narration)])
        assert self._run(task) is True
        assert task['content'] == narration.strip()

    def test_silent_when_no_substantial_prior_narration(self):
        """Complement / no false positive: a turn whose prior rounds carried
        no real prose (model just called tools then answered briefly) has
        nothing to recover — leave the short content as-is. Gating on
        'short after tools' alone would mangle genuinely short turns."""
        task = _make_task(
            content='好的。',
            tool_rounds=[
                _round(assistant_content=''),
                _round(assistant_content=''),
            ],
        )
        recovered = self._run(task, finish='stop', tool_happened=True)
        assert recovered is False
        assert task['content'] == '好的。'

    def test_silent_on_normal_long_answer(self):
        """Complement: a normal long final answer after tool work persists
        byte-identically — no interception."""
        long_answer = '这是完整的调研报告。' * 60   # ~600 chars
        task = _make_task(
            content=long_answer,
            tool_rounds=[
                _round(assistant_content='先收集资料。' * 8),
            ],
        )
        recovered = self._run(task, finish='stop', tool_happened=True)
        assert recovered is False
        assert task['content'] == long_answer

    def test_silent_when_finish_not_stop(self):
        """Only a ``stop`` completion is a suspicious overwrite candidate —
        tool_use/aborted/error finishes must not be touched."""
        narration = '工作中。' * 40
        task = _make_task(
            content='x',
            tool_rounds=[_round(assistant_content=narration)],
        )
        recovered = self._run(task, finish='tool_calls', tool_happened=True)
        assert recovered is False
        assert task['content'] == 'x'

    def test_silent_when_no_tool_call_happened(self):
        """No tool work → nothing accumulated → nothing to recover."""
        task = _make_task(content='Hi! How can I help you today?',
                          tool_rounds=[])
        recovered = self._run(task, finish='stop', tool_happened=False)
        assert recovered is False

    def test_silent_when_aborted(self):
        """A user Stop legitimately truncates the accumulator — never
        'recover' over an explicit abort."""
        narration = '工作中。' * 40
        task = _make_task(
            content='partial',
            aborted=True,
            tool_rounds=[_round(assistant_content=narration)],
        )
        recovered = self._run(task, finish='stop', tool_happened=True)
        assert recovered is False
        assert task['content'] == 'partial'


@pytest.mark.unit
@pytest.mark.parametrize('answer, recover', [
    ('{"count":135,"total":6480}', False),
    ('Hi! How can I help you today?', True),
])
def test_finalizer_delivers_selected_content_to_terminal_event_and_persistence(monkeypatch, answer, recover):
    from lib.tasks_pkg.orchestrator import _finalize
    import lib

    class PersistenceObserved(RuntimeError):
        pass

    narration = 'All datasets are loaded; computing the aggregate. ' * 4
    task = _make_task(content=answer, tool_rounds=[_round(assistant_content=narration)])
    task.update(_userId=17, convId='')
    expected = narration.strip() if recover else answer
    delivered = []
    monkeypatch.setattr(lib, 'ARTIFACTS_ENABLED', False)
    monkeypatch.setattr(_finalize, '_generate_tool_summary', lambda *_a: '')
    monkeypatch.setattr(_finalize, '_maybe_auto_retry_turn', lambda *_a: False)
    monkeypatch.setattr(_finalize, '_salvage_undelivered_steer', lambda *_a: None)
    monkeypatch.setattr(_finalize, 'cleanup_stale_cache_states', lambda **_kw: None)
    monkeypatch.setattr(_finalize, 'stamp_chat_task_terminal', lambda *_a, **_kw: None)

    def observe_event(owner, event):
        if event['type'] == 'done':
            delivered.append(owner['content'])

    def observe_persist(owner, **_kwargs):
        assert delivered == [expected]
        assert owner['content'] == expected
        raise PersistenceObserved

    monkeypatch.setattr(_finalize, 'append_event', observe_event)
    monkeypatch.setattr(_finalize, 'persist_task_result', observe_persist)
    messages = [{'role': 'user', 'content': 'Compute the count and total.'}]
    with pytest.raises(PersistenceObserved):
        _finalize._finalize_and_emit_done(
            task, model='test-model', preset='medium', thinking_depth=None,
            cfg={}, last_finish_reason='stop', last_usage={}, last_stream_result=None,
            accumulated_usage={}, api_rounds=[], tool_call_happened=True,
            messages=messages, original_messages=messages,
            all_search_results_text='', max_tokens=128, thinking_enabled=False,
            temperature=0, _loop_exit_reason='no_tool_calls',
            _abort_detected_phase=None, project_path=None, project_enabled=False,
            round_num=2, assistant_msg={'role': 'assistant', 'content': answer},
        )
