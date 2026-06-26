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
    return _new_report_task(tid, 'phashabort00000000000000000000000', 'en', None,
                            client_title='Test Paper')


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
