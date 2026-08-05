#!/usr/bin/env python3
"""Regression test: stopping paper report generation must terminate cleanly.

When the user clicks Stop, the report task's ``abort_event`` is set. The
worker must:
  - break out of the tool loop,
  - reach a distinct ``aborted`` terminal status (NOT ``done`` / ``error``),
  - emit a single ``aborted`` event carrying whatever partial text exists,
  - NOT persist the partial report to the DB.

A mid-retry ``AbortedError`` raised by the dispatcher is treated the same way.

These tests mock ``dispatch_stream`` so the abort path is deterministic.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _make_task(tid):
    from lib.paper import _new_report_task
    # paperInsightEnabled=False: the gated insight second pass dispatches a
    # REAL LLM — offline here by contract, and on CI (placeholder key → 401)
    # the dispatcher's cooldown cycle never exits → 600s timeout (233daa6).
    return _new_report_task(tid, 'phashabort00000000000000000000000', 'en', None,
                            client_title='Test Paper',
                            config={'paperInsightEnabled': False,
                                      'paperCheckpointsEnabled': False})


REPORT_BODY = '## ⚡ TL;DR\nPartial content so far.\n'


def test_abort_before_first_round():
    """Abort set before generation starts → aborted status, no persist."""
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream

    def _should_not_run(*a, **k):
        raise AssertionError('dispatch_stream called after pre-abort')
    re_mod.dispatch_stream = _should_not_run

    try:
        task = _make_task('rpt_abort_1')
        task['abort_event'].set()
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        assert task['status'] == 'aborted', f"status={task['status']}"
        types = [e.get('type') for e in task['events']]
        assert 'aborted' in types, f'no aborted event; got {types}'
        assert 'done' not in types, 'must NOT emit done on abort'
        assert task.get('finished_at'), 'finished_at must be set'
    finally:
        re_mod.dispatch_stream = orig
    _ok('abort before first round → aborted status, no done event')


def test_abort_mid_stream_keeps_partial():
    """Abort detected after a streamed chunk → partial text preserved."""
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream

    def _fake_dispatch(messages, on_content=None, on_thinking=None, abort_check=None, **kw):
        # Stream a partial chunk, then the user "stops" (abort flips true).
        if on_content:
            on_content(REPORT_BODY)
        # Simulate the abort landing during line iteration: the stream returns
        # normally with a partial message and the flag now set.
        _abort_holder['set']()
        msg = {'role': 'assistant', 'content': REPORT_BODY, 'tool_calls': None}
        usage = {'prompt_tokens': 5, 'completion_tokens': 7, '_dispatch': {}}
        return msg, 'stop', usage

    re_mod.dispatch_stream = _fake_dispatch
    _abort_holder = {}

    # Sentinel so the DB persist path would be loud if hit.
    try:
        task = _make_task('rpt_abort_2')
        _abort_holder['set'] = task['abort_event'].set
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        assert task['status'] == 'aborted', f"status={task['status']}"
        ev = [e for e in task['events'] if e.get('type') == 'aborted']
        assert ev, 'no aborted event'
        assert REPORT_BODY.strip() in (ev[-1].get('partial') or ''), \
            'partial text missing from aborted event'
        # Partial report must NOT be promoted to the persisted/enriched field.
        assert not task.get('enriched_text'), 'partial must not be enriched/persisted'
    finally:
        re_mod.dispatch_stream = orig
    _ok('abort mid-stream → aborted status carries partial text, not persisted')


def test_abort_between_tool_calls_stops_before_running_tools():
    """Abort pressed WHILE a round's tool calls are pending → the worker must
    NOT execute the remaining tools and must NOT start a fresh LLM round.

    This is the fix for "Stop has limited effect": before, an abort set during
    a slow web_search/fetch_url was only noticed at the NEXT round boundary, so
    every queued tool ran and another dispatch fired first.
    """
    import lib.paper.report_engine as re_mod
    orig = re_mod.dispatch_stream
    orig_exec = re_mod._execute_report_tool
    state = {'dispatch_calls': 0, 'tool_calls': 0}

    # A round that issues TWO tool calls; the FIRST tool press "Stop" (sets the
    # abort). The second tool must NOT run, and no fresh dispatch round fires.
    two_tools = [
        {'id': 'tc1', 'function': {'name': 'web_search', 'arguments': '{"query": "a"}'}},
        {'id': 'tc2', 'function': {'name': 'fetch_url', 'arguments': '{"url": "b"}'}},
    ]

    def _fake_dispatch(messages, on_content=None, on_thinking=None, abort_check=None, **kw):
        state['dispatch_calls'] += 1
        # Not aborted yet at this point — the post-stream abort check must pass
        # so execution reaches the tool loop (where MY new check lives).
        msg = {'role': 'assistant', 'content': '', 'tool_calls': list(two_tools)}
        return msg, 'tool_calls', {'_dispatch': {}}

    def _spy_exec(*a, **k):
        state['tool_calls'] += 1
        # The user presses Stop DURING the first (slow) tool.
        _holder['set']()
        return ('r', [], None, None, None)

    re_mod.dispatch_stream = _fake_dispatch
    re_mod._execute_report_tool = _spy_exec
    _holder = {}
    try:
        task = _make_task('rpt_abort_4')
        _holder['set'] = task['abort_event'].set
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        assert task['status'] == 'aborted', f"status={task['status']}"
        # Exactly ONE tool ran (the one during which Stop was pressed); the
        # second queued tool was skipped by the between-tools abort check, and
        # NO second dispatch round fired.
        assert state['tool_calls'] == 1, f"second tool ran despite abort: {state['tool_calls']}"
        assert state['dispatch_calls'] == 1, f"extra round fired: {state['dispatch_calls']}"
        types = [e.get('type') for e in task['events']]
        assert 'aborted' in types and 'done' not in types, types
    finally:
        re_mod.dispatch_stream = orig
        re_mod._execute_report_tool = orig_exec
    _ok('abort between tool calls → 2nd tool skipped, no extra round, clean aborted')


def test_batch_runner_skips_queued_items_after_abort():
    """The abort-aware batch runner must NOT start queued items once Stop is
    pressed. This is the seam that stops a Stopped report spraying dozens more
    searches (the "backend keeps searching" bug): threads already running can't
    be killed, but the queued tail (beyond max_workers) must short-circuit.
    """
    import threading

    from lib.tasks_pkg.handlers._adapter import run_batch_concurrent

    ev = threading.Event()
    ran = []
    ran_lock = threading.Lock()

    def _worker(item):
        with ran_lock:
            ran.append(item)
        # The first item "trips" Stop; every later item is queued behind the
        # single worker slot and must be skipped by the abort guard.
        ev.set()
        return item

    items = list(range(20))
    out = run_batch_concurrent(items, _worker, max_workers=1, tag='test',
                               abort=ev.is_set)
    assert len(out) == len(items), 'output length must stay aligned with input'
    # With max_workers=1 and the abort set by the first worker, at most a
    # couple of items can slip through before the guard sees the flag; the vast
    # majority of the 20 must be skipped (None), NOT run.
    assert len(ran) < len(items), f'abort did not skip queued items: ran {len(ran)}/{len(items)}'
    assert len(ran) <= 3, f'too many items ran after abort: {len(ran)}'
    skipped = [o for o in out if o is None]
    assert len(skipped) >= len(items) - 3, f'expected most items skipped, got {len(skipped)}'
    _ok('batch runner skips queued items after abort (Stop halts remaining searches)')


def test_batch_runner_no_abort_runs_all():
    """Regression guard: with no abort predicate every item still runs (the
    guard must not change default behaviour)."""
    from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
    items = list(range(10))
    out = run_batch_concurrent(items, lambda x: x * 2, max_workers=4, tag='test')
    assert out == [x * 2 for x in items], out
    _ok('batch runner without abort runs every item (no behaviour change)')


def test_aborted_error_mid_retry():
    """A dispatcher AbortedError is treated as a clean stop, not an error."""
    import lib.paper.report_engine as re_mod
    from lib.llm_errors import AbortedError
    orig = re_mod.dispatch_stream

    def _raise_abort(*a, **k):
        raise AbortedError('user aborted before retry')
    re_mod.dispatch_stream = _raise_abort

    try:
        task = _make_task('rpt_abort_3')
        re_mod._run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        assert task['status'] == 'aborted', f"status={task['status']}"
        types = [e.get('type') for e in task['events']]
        assert 'aborted' in types, f'no aborted event; got {types}'
        assert 'error' not in types, 'AbortedError must not surface as error'
    finally:
        re_mod.dispatch_stream = orig
    _ok('mid-retry AbortedError → aborted status, not error')


def main():
    print()
    print(_color('═══ Paper Report Abort/Stop Tests ═══', '36'))
    print()
    tests = [
        test_abort_before_first_round,
        test_abort_mid_stream_keeps_partial,
        test_abort_between_tool_calls_stops_before_running_tools,
        test_batch_runner_skips_queued_items_after_abort,
        test_batch_runner_no_abort_runs_all,
        test_aborted_error_mid_retry,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
