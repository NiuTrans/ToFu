"""A vetoed answer renders exactly once (conv mvyttf2hwfzppy, 2026-11-02).

A continuation enforcer may veto a prose-only stop and re-drive the model. The
vetoed answer is snapshotted as continuation prose, and it can also remain in
the live text, which is the terminal deliverable. Rendering a snapshot whose
bytes the live text still held showed the answer twice: once as process prose
and again inside the reply. Ownership is explicit instead:

* the live text owns a vetoed answer while it still holds its bytes;
* ``reset_task_text`` hands dropped bytes to the record in the same critical
  section (a tool round's discard, a retry reset, a superseding lane);
* a rollback to the round base preserves them, so they stay with the live text.

The specs drive the real owners — ``record_continuation_prose``,
``_discard_pretool_prose``, ``reset_round_to_base``, ``analyse_stream_result``
and ``assemble_segments`` — and finally the real root loop with a scripted
provider, asserting what a reader of the settled Turn receives.
"""

from __future__ import annotations

import copy
import itertools
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.events import EventType
from lib.tasks_pkg.manager import begin_live_text_paragraph
from lib.tasks_pkg.segments import (
    SEG_SYSTEM_NOTE, SEG_TEXT, SEG_THINKING, SEG_TOOL_USE,
    assemble_segments, derive_content, record_continuation_prose,
)
from lib.tasks_pkg.stream_handler._intent_stall import (
    NUDGE_TEXT, strip_end_turn_marker,
)
from tests.goals import test_goal_invocation_integration as goal_fixture

pytestmark = pytest.mark.unit

runtime = goal_fixture.runtime

ANSWER = '主修复已合并；后续样式改动在工作区未提交，原有未提交改动均保留。'
ANSWER_THINKING = 'Summarise the verified fix and the remaining local edits.'
ADDENDUM = '最后的 exit 1 只是状态检查命令找不到临时 worktree。'


_TASK_IDS = itertools.count()
_ADOPTED: list[str] = []


@pytest.fixture(autouse=True)
def _discard_adopted_tasks():
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    yield
    while _ADOPTED:
        chat_task_runtime.discard(_ADOPTED.pop())


def _task(**extra):
    """A runtime-adopted chat task, so emitted events land in its log."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    task = {
        'id': f'vetoed-answer-{next(_TASK_IDS)}', 'convId': 'vetoed-answer-conv',
        '_userId': 1, 'kind': 'chat', 'status': 'running', 'aborted': False,
        'content': '', 'thinking': '', 'content_lock': threading.Lock(),
        'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
    }
    task.update(extra)
    assert chat_task_runtime.adopt(task) is True
    _ADOPTED.append(task['id'])
    return task


def _failed_round(llm_round=53):
    return {
        'toolName': 'run_command', 'toolCallId': f'call-{llm_round}',
        'toolArgs': '{}', 'toolContent': 'exit 1', 'roundNum': llm_round + 1,
        'llmRound': llm_round, 'status': 'error',
        'results': [{'badge': 'error', 'exitCode': 1}],
    }


def _ok_round(llm_round, *, assistant_content=''):
    row = {
        'toolName': 'run_command', 'toolCallId': f'call-{llm_round}',
        'toolArgs': '{}', 'toolContent': 'clean', 'roundNum': llm_round + 1,
        'llmRound': llm_round, 'status': 'done',
        'results': [{'badge': 'ok', 'exitCode': 0}],
    }
    if assistant_content:
        row['assistantContent'] = assistant_content
    return row


def _stream(task, *, content='', thinking=''):
    """Append one round's deltas exactly as the stream coalescer does."""
    with task['content_lock']:
        task['content'] += content
        task['thinking'] += thinking


def _occurrences(segments, needle, seg_type):
    return sum(str(s.get('text') or '').count(needle)
               for s in segments if s['type'] == seg_type)


def _continuation_ids(segments):
    return [s['blockId'] for s in segments
            if 'continuation-' in str(s.get('blockId') or '')]


def _veto(task, llm_round=54):
    _stream(task, content=ANSWER, thinking=ANSWER_THINKING)
    record_continuation_prose(task, llm_round=llm_round, content=ANSWER,
                              thinking=ANSWER_THINKING)


# ── Projection: one owner at a time ───────────────────────────────────

def test_a_provisional_answer_followed_by_prose_only_renders_once():
    """The reported shape: the re-driven round answered in prose only."""
    task = _task(toolRounds=[_failed_round()])
    _veto(task)
    begin_live_text_paragraph(task)
    _stream(task, content=ADDENDUM, thinking='Nothing else failed.')

    segments = assemble_segments(task)

    assert _continuation_ids(segments) == []
    assert _occurrences(segments, ANSWER, SEG_TEXT) == 1
    assert _occurrences(segments, ANSWER_THINKING, SEG_THINKING) == 1
    assert derive_content(segments) == task['content'] == f'{ANSWER}\n\n{ADDENDUM}'


def test_a_tool_round_after_the_veto_moves_the_answer_to_its_round():
    """The real pre-tool discard drops the live text, so the record renders
    the vetoed answer once, as process prose ahead of the next batch."""
    from lib.tasks_pkg.orchestrator._finalize import _discard_pretool_prose

    task = _task(toolRounds=[_failed_round()])
    _veto(task)
    begin_live_text_paragraph(task)
    _stream(task, content='Re-checking the worktree.')
    _discard_pretool_prose(task, 55)
    task['toolRounds'].append(
        _ok_round(55, assistant_content='Re-checking the worktree.'))
    _stream(task, content='Cleanup verified.')

    segments = assemble_segments(task)
    ids = [s.get('blockId') for s in segments]
    next_batch = next(i for i, s in enumerate(segments)
                      if s.get('llmRound') == 55)

    assert (ids.index('thinking:continuation-0')
            < ids.index('text:continuation-0') < next_batch)
    vetoed = segments[ids.index('text:continuation-0')]
    assert (vetoed['text'], vetoed['deliverable']) == (ANSWER, False)
    assert _occurrences(segments, ANSWER, SEG_TEXT) == 1
    assert derive_content(segments) == 'Cleanup verified.'


def test_the_live_projection_shows_a_released_answer_as_the_bubble_clears():
    """``delta_reset`` is structural: its projection is captured from the
    segment mirror while the live bubble already shows the emptied text. A
    mirror checkpointed while the answer was still held must not hide it."""
    from lib.tasks_pkg.orchestrator._finalize import _discard_pretool_prose
    from lib.turn_lifecycle import capture_task_projection_source

    task = _task(toolRounds=[_failed_round()])
    _veto(task)
    task['segments'] = assemble_segments(task)
    assert _continuation_ids(task['segments']) == []
    _stream(task, content='Re-checking the worktree.')
    _discard_pretool_prose(task, 55)

    reset = next(e for e in reversed(task['events'])
                 if e['type'] == EventType.DELTA_RESET)
    live = capture_task_projection_source(task, reset)
    assert live['content'] == ''
    # Released as process prose at its round, and no stale terminal block
    # (checkpointed while the answer was held) still claims it as the answer.
    released = {s['blockId']: s['text'] for s in live['segments']
                if 'continuation-' in str(s.get('blockId') or '')}
    assert released == {'thinking:continuation-0': ANSWER_THINKING,
                        'text:continuation-0': ANSWER}
    assert not [s for s in live['segments'] if s.get('terminal')]


def test_a_round_base_rollback_keeps_the_answer_with_the_live_text():
    """A retried attempt rolls back to its round base, which still begins
    with the provisional answer: ownership must not move."""
    from lib.tasks_pkg.round_retry import reset_round_to_base

    task = _task(toolRounds=[_failed_round()])
    _veto(task)
    begin_live_text_paragraph(task)
    # stream_llm_response stamps the round base before the first delta.
    task['_round_base_content'] = task['content']
    task['_round_base_thinking'] = task['thinking']
    _stream(task, content='poisoned partial attempt')
    reset_round_to_base(task, 55)
    _stream(task, content=ADDENDUM)

    segments = assemble_segments(task)

    assert _continuation_ids(segments) == []
    assert _occurrences(segments, ANSWER, SEG_TEXT) == 1
    assert derive_content(segments) == f'{ANSWER}\n\n{ADDENDUM}'


@pytest.mark.parametrize('live, separator', [
    ('answer', '\n\n'), ('answer\n', '\n'), ('answer\n\n', ''), ('', ''),
])
def test_the_next_message_starts_a_new_paragraph(live, separator):
    task = _task(content=live)
    appended = begin_live_text_paragraph(task)
    assert task['content'] == live + separator
    assert appended.get('content', '') == separator


# ── Analyser lanes choose the owner explicitly ────────────────────────

def _analyse(task, message, messages, round_num=54):
    from lib.tasks_pkg.stream_handler._analyse import analyse_stream_result
    return analyse_stream_result(
        assistant_msg=message, last_finish_reason='stop', task=task,
        tid='vetoed', model='fixture-model', round_num=round_num,
        _premature_retry_count=0, messages=messages, usage={})


def test_the_stall_nudge_keeps_the_vetoed_answer_as_the_provisional_answer():
    task = _task(toolRounds=[_failed_round()])
    _stream(task, content=ANSWER, thinking=ANSWER_THINKING)
    messages = [{'role': 'user', 'content': 'clean up the worktree'}]

    decision = _analyse(task, {'content': ANSWER,
                               'reasoning_content': ANSWER_THINKING}, messages)

    assert decision['action'] == 'continue'
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']
    assert messages[1]['content'] == ANSWER
    assert messages[2]['content'] == NUDGE_TEXT
    # The answer stays live and the re-driven message starts a paragraph,
    # streamed as one delta so the live view matches the accumulated text.
    assert task['content'] == f'{ANSWER}\n\n'
    assert task['thinking'] == f'{ANSWER_THINKING}\n\n'
    deltas = [e for e in task['events'] if e['type'] == EventType.DELTA]
    assert [(e.get('content'), e.get('thinking')) for e in deltas] == [
        ('\n\n', '\n\n')]
    assert not any(e['type'] == EventType.DELTA_RESET for e in task['events'])

    # The follow-up only declares the end of the turn; finalize strips it.
    _stream(task, content='[END_TURN: done]')
    task['content'] = strip_end_turn_marker(task['content'])
    segments = assemble_segments(task)

    assert derive_content(segments) == ANSWER
    assert _occurrences(segments, ANSWER, SEG_TEXT) == 1
    assert _continuation_ids(segments) == []
    assert [s['noteKind'] for s in segments
            if s['type'] == SEG_SYSTEM_NOTE] == ['intent-stall']


def test_commentary_leaves_the_answer_and_renders_once_as_process_prose():
    """Provider-marked commentary is intermediate work, never the answer."""
    commentary = 'I will check the migration log next.'
    task = _task()
    _stream(task, content=commentary)
    message = {
        'role': 'assistant', 'content': commentary,
        '_responses_items': [{
            'type': 'message', 'role': 'assistant', 'phase': 'commentary',
            'content': [{'type': 'output_text', 'text': commentary}],
        }],
    }

    decision = _analyse(task, message, [{'role': 'user', 'content': 'go'}],
                        round_num=3)

    assert decision['action'] == 'continue'
    assert task['content'] == ''
    assert any(e['type'] == EventType.DELTA_RESET and e['discard'] is True
               for e in task['events'])
    # The cleared bubble's projection already carries it as process prose.
    assert _occurrences(task['segments'], commentary, SEG_TEXT) == 1
    _stream(task, content='The migration is applied.')
    segments = assemble_segments(task)
    vetoed = [s for s in segments if s.get('blockId') == 'text:continuation-0']
    assert [(s['text'], s['deliverable']) for s in vetoed] == [(commentary, False)]
    assert _occurrences(segments, commentary, SEG_TEXT) == 1
    assert derive_content(segments) == 'The migration is applied.'


# ── The real root loop ────────────────────────────────────────────────

def _run_scripted_turn(runtime, monkeypatch, replies, failing_calls):
    """Run one worker turn through the real stream, tool, analyser and
    finalization owners; only provider bytes and tool bodies are scripted."""
    import lib.tasks_pkg.manager._stream as stream
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round

    tool_name = goal_fixture.TOOL['function']['name']

    def handler(task, tc, name, call_id, args, rn, row, *rest):
        if call_id in failing_calls:
            _finalize_tool_round(task, rn, row, [{'badge': 'error', 'exitCode': 1}],
                                 status='error')
            return call_id, 'Error: exit code 1', False
        _finalize_tool_round(task, rn, row, [{'title': 'Probe', 'output': 'clean'}])
        return call_id, 'clean', False

    runtime.parent['_tool_env'] = SimpleNamespace(
        resolve=lambda name: handler if name == tool_name else None)

    def provider(body, **kwargs):
        index = len(runtime.calls)
        assert index < len(replies), 'unexpected extra provider dispatch'
        runtime.calls.append(copy.deepcopy(body))
        thinking, content, call_id = replies[index]
        kwargs['on_thinking'](thinking)
        kwargs['on_content'](content)
        message = {'role': 'assistant', 'content': content,
                   'reasoning_content': thinking}
        if call_id:
            message['tool_calls'] = [{
                'id': call_id, 'type': 'function',
                'function': {'name': tool_name, 'arguments': '{}'}}]
        return ProviderStreamResult.from_legacy(
            message, 'tool_calls' if call_id else 'stop',
            {'prompt_tokens': 40, 'completion_tokens': 30, 'total_tokens': 70})

    monkeypatch.setattr(stream, 'dispatch_stream', provider)
    result, _live, _adapter, _forwarded = goal_fixture._run_goal(runtime)
    assert result.status == 'completed', result.error
    assert len(runtime.calls) == len(replies)
    return result.output_projection['segments']


def test_real_loop_prose_only_follow_up_delivers_the_vetoed_answer_once(
        runtime, monkeypatch):
    segments = _run_scripted_turn(runtime, monkeypatch, [
        ('Probe the worktree first.', 'Running the probe.', 'call-probe'),
        (ANSWER_THINKING, ANSWER, None),
        ('Nothing else failed.', f'{ADDENDUM}\n\n[END_TURN: done]', None),
    ], failing_calls={'call-probe'})

    nudged = [m.get('content') for m in runtime.calls[2]['messages']]
    assert nudged[nudged.index(ANSWER) + 1] == NUDGE_TEXT
    assert _continuation_ids(segments) == []
    assert _occurrences(segments, ANSWER, SEG_TEXT) == 1
    assert _occurrences(segments, ANSWER_THINKING, SEG_THINKING) == 1
    terminal = next(s for s in segments if s.get('blockId') == 'text:terminal')
    assert terminal['text'] == f'{ANSWER}\n\n{ADDENDUM}'
    assert terminal['deliverable'] is True
    order = [s['type'] for s in segments]
    assert (order.index(SEG_TOOL_USE) < order.index(SEG_SYSTEM_NOTE)
            < segments.index(terminal))


def test_real_loop_tool_follow_up_renders_the_vetoed_answer_once_at_its_round(
        runtime, monkeypatch):
    segments = _run_scripted_turn(runtime, monkeypatch, [
        ('Probe the worktree first.', 'Running the probe.', 'call-probe'),
        (ANSWER_THINKING, ANSWER, None),
        ('Inspect the failure.', 'Re-checking the worktree.', 'call-recheck'),
        ('All clean.', 'Cleanup verified.', None),
    ], failing_calls={'call-probe'})

    ids = [s.get('blockId') for s in segments]
    note = next(i for i, s in enumerate(segments) if s['type'] == SEG_SYSTEM_NOTE)
    recheck = next(i for i, s in enumerate(segments)
                   if s['type'] == SEG_TOOL_USE and s['id'] == 'call-recheck')
    assert (ids.index('thinking:continuation-0')
            < ids.index('text:continuation-0') < note < recheck)
    assert _occurrences(segments, ANSWER, SEG_TEXT) == 1
    assert _occurrences(segments, ANSWER_THINKING, SEG_THINKING) == 1
    assert derive_content(segments) == 'Cleanup verified.'
