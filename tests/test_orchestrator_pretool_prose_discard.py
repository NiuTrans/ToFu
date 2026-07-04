#!/usr/bin/env python3
"""Regression test: inter-round narration must not leak into the final answer.

Root cause (2026-07-03): the chat orchestrator accumulates every content delta
into a single ``task['content']`` (``_on_content`` does ``task['content'] +=
cd``) and NEVER resets it between tool rounds. A chatty tool-calling model
(opus) emits prose BEFORE its tool calls in an intermediate round ("Now let me
check the utility functions."); that text stays in ``task['content']`` and gets
concatenated in front of the terminal round's real answer — leaking scaffolding
into the deliverable. Observed in the human-eval pilot (H1 tofu).

Fix: ``orchestrator._discard_pretool_prose(task, round_num)`` — called when a
round ends in TOOL CALLS — clears ``task['content']`` / ``task['thinking']``
(backend) AND emits a ``delta_reset`` event (so the client drops the deltas it
already rendered), while KEEPING the turn's tool rounds.

Double-neuter: we first reproduce the leak WITHOUT the fix (assert the narration
IS present), then apply the real production helper and assert it's gone AND the
delta_reset event fired.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


NARRATION = 'Now let me check the relevant utility functions.'
FINAL_ANSWER = '## 1. 设计梳理\n调用链如下……\n## 2. 潜在问题\n问题一……'


def _make_task():
    """Minimal task dict with the fields _discard_pretool_prose touches."""
    return {
        'id': 'pretool_prose_test0',
        'convId': 'convtest',
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
        'events': [],
        'toolRounds': [{'roundNum': 1, 'toolName': 'read_files',
                        'assistantContent': NARRATION}],
    }


def _simulate_stream(task, text):
    """Mimic _on_content: append a content delta to the running buffer."""
    with task['content_lock']:
        task['content'] += text


def test_leak_reproduces_without_fix():
    """WITHOUT the discard, round-1 narration leaks in front of the answer."""
    task = _make_task()
    # Round 1: model streams narration, then issues a tool call. (No discard.)
    _simulate_stream(task, NARRATION)
    # Round 2 (terminal): model streams the real answer — appends to SAME buf.
    _simulate_stream(task, FINAL_ANSWER)
    assert NARRATION in task['content'], 'setup wrong: narration should be present'
    assert task['content'].startswith(NARRATION), (
        'neuter proof failed: leak did not reproduce')
    _ok('leak reproduces without fix: narration is prepended to the final answer')


def test_discard_removes_leak_and_emits_reset():
    """WITH the real helper, narration is gone and delta_reset is emitted."""
    from lib.tasks_pkg.orchestrator import _discard_pretool_prose
    task = _make_task()
    # Round 1: model streams narration, then issues a tool call.
    _simulate_stream(task, NARRATION)
    # ── The fix: round ended in tool calls → discard its pre-tool prose. ──
    _discard_pretool_prose(task, round_num=1)
    assert task['content'] == '', f'content not cleared: {task["content"]!r}'
    assert task['thinking'] == '', 'thinking not cleared'
    # Round 2 (terminal): model streams the real answer.
    _simulate_stream(task, FINAL_ANSWER)

    assert NARRATION not in task['content'], (
        f'LEAK: narration still in final content: {task["content"]!r}')
    assert task['content'] == FINAL_ANSWER, (
        f'final content should be exactly the terminal answer: {task["content"]!r}')
    # Frontend contract: a delta_reset event must have fired.
    types = [e.get('type') for e in task['events']]
    assert 'delta_reset' in types, f'no delta_reset event emitted; got {types}'
    _ok('with fix: narration discarded, final answer clean, delta_reset emitted')


def test_tool_rounds_preserved():
    """delta_reset must NOT drop the turn's tool rounds (unlike retry_reset)."""
    from lib.tasks_pkg.orchestrator import _discard_pretool_prose
    task = _make_task()
    _simulate_stream(task, NARRATION)
    _discard_pretool_prose(task, round_num=1)
    assert len(task['toolRounds']) == 1, (
        f'tool rounds must survive the discard; got {task["toolRounds"]!r}')
    assert task['toolRounds'][0]['assistantContent'] == NARRATION, (
        'the round\'s own assistantContent snapshot must be untouched')
    _ok('tool rounds (and their assistantContent snapshot) preserved across discard')


def main():
    tests = [
        test_leak_reproduces_without_fix,
        test_discard_removes_leak_and_emits_reset,
        test_tool_rounds_preserved,
    ]
    print(_color('\ntest_orchestrator_pretool_prose_discard', '36'))
    for t in tests:
        t()
    print(_color('\nAll pre-tool-prose discard tests passed.', '32'))


if __name__ == '__main__':
    main()
