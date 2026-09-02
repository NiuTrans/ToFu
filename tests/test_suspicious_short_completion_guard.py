"""Behavior guard: a suspicious short stop-completion must NOT overwrite the
multi-round accumulated work that preceded it.

WHY
---
2026-07-28 09:51–10:45 incident: the sankuai gateway intermittently returned a
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

  * when the terminal round is a short ``stop`` after tool work AND the
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FINALIZE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'lib', 'tasks_pkg', 'orchestrator', '_finalize.py')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


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
            task, finish, tool_happened)

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
class TestSuspiciousGuardIsWired:
    """The interception must actually be CALLED at finalize — a helper that
    exists but is never invoked is the classic 'guard green but dead in
    production' failure (charter: "把这行调用删掉,守卫会红吗?")."""

    def test_finalize_calls_the_preservation_hook(self):
        """Drive the real ``_finalize_and_emit_done`` source and assert the
        preservation hook is invoked in it. Anchored on the CALL (an AST
        Call node), not a comment or docstring."""
        import ast
        src = _read(FINALIZE_PATH)
        tree = ast.parse(src)
        finalizer = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == '_finalize_and_emit_done'
        )
        calls = {
            node.func.id
            for node in ast.walk(finalizer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        }
        assert '_maybe_preserve_accumulated_on_suspicion' in calls, (
            f'{FINALIZE_PATH}: _finalize_and_emit_done never CALLS '
            f'_maybe_preserve_accumulated_on_suspicion — the interception '
            f'helper exists but is not wired in, so suspicious short '
            f'completions still overwrite accumulated work in production.'
        )

    def test_hook_runs_before_done_event_emit(self):
        """The hook must run BEFORE the done event is built/emitted — the
        terminal event bridge snapshots task['content'], so a hook that runs
        after it would persist the short residue."""
        import ast
        tree = ast.parse(_read(FINALIZE_PATH))
        finalizer = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == '_finalize_and_emit_done'
        )
        call_lines = {
            node.func.id: node.lineno
            for node in ast.walk(finalizer)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {
                '_maybe_preserve_accumulated_on_suspicion',
                '_build_done_event_base',
            }
        }
        assert call_lines['_maybe_preserve_accumulated_on_suspicion'] \
            < call_lines['_build_done_event_base'], (
            f'{FINALIZE_PATH}: the preservation hook must run before the '
            'terminal event is built.'
        )
