#!/usr/bin/env python3
"""Unit tests for lib.agent_loop — the shared AbortSignal + run_agent_loop seam.

Covers the abstraction directly (no paper engine): the three ``AbortSignal``
wrappers over the project's three abort mechanisms, and the loop's three abort
checks (before-round / post-stream / between-tools). The between-tools check is
the one that fixed the "Stop has limited effect" bug; it is asserted here at
the seam level AND end-to-end in tests/test_paper_report_abort.py.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)


# ── AbortSignal wrappers ────────────────────────────────────────────

def test_abortsignal_from_event():
    from lib.agent_loop import AbortSignal
    ev = threading.Event()
    sig = AbortSignal.from_event(ev)
    assert sig.aborted is False
    assert sig.is_set() is False and sig() is False  # callable + is_set aliases
    ev.set()
    assert sig.aborted is True and sig.is_set() is True and sig() is True
    _ok('AbortSignal.from_event tracks a threading.Event (+ is_set/call aliases)')


def test_abortsignal_from_task_flag():
    from lib.agent_loop import AbortSignal
    task = {}
    sig = AbortSignal.from_task_flag(task)
    assert sig.aborted is False
    task['aborted'] = True
    assert sig.aborted is True
    # custom key
    t2 = {'stop': True}
    assert AbortSignal.from_task_flag(t2, key='stop').aborted is True
    _ok("AbortSignal.from_task_flag tracks task['aborted'] (+ custom key)")


def test_abortsignal_from_callback_and_never():
    from lib.agent_loop import AbortSignal
    flag = {'v': False}
    sig = AbortSignal.from_callback(lambda: flag['v'])
    assert sig.aborted is False
    flag['v'] = True
    assert sig.aborted is True
    # None callback → never aborts; never() → never aborts.
    assert AbortSignal.from_callback(None).aborted is False
    assert AbortSignal.never().aborted is False
    _ok('AbortSignal.from_callback wraps a predicate; None/never → never trips')


def test_abortsignal_broken_predicate_is_safe():
    from lib.agent_loop import AbortSignal
    def _boom():
        raise RuntimeError('bad predicate')
    assert AbortSignal(_boom).aborted is False  # logged, not raised
    _ok('AbortSignal swallows a broken predicate (never wedges the loop)')


# ── run_agent_loop control flow ─────────────────────────────────────

def _mk_msg(tool_calls=None):
    return {'role': 'assistant', 'content': '', 'tool_calls': tool_calls}, 'stop', {}


def test_loop_completes_when_no_tool_calls():
    from lib.agent_loop import AbortSignal, run_agent_loop
    calls = {'dispatch': 0, 'tools': 0}

    def dispatch(rnd, tools):
        calls['dispatch'] += 1
        return _mk_msg(None)  # no tools → natural end

    out = run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=4,
                         round_tools=['T'], dispatch=dispatch,
                         execute_tool=lambda rnd, tc: calls.__setitem__('tools', calls['tools'] + 1))
    assert out.completed and not out.aborted
    assert out.rounds == 1 and calls['dispatch'] == 1 and calls['tools'] == 0
    _ok('loop completes on a no-tool-calls turn (1 round, 0 tools)')


def test_loop_runs_tools_then_completes():
    from lib.agent_loop import AbortSignal, run_agent_loop
    seq = [
        _mk_msg([{'id': 't1', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
        _mk_msg(None),
    ]
    calls = {'i': 0, 'tools': 0, 'tool_round_hook': 0}

    def dispatch(rnd, tools):
        m = seq[calls['i']]; calls['i'] += 1
        return m

    out = run_agent_loop(
        abort=AbortSignal.never(), max_tool_rounds=4, round_tools=['T'],
        dispatch=dispatch,
        execute_tool=lambda rnd, tc: calls.__setitem__('tools', calls['tools'] + 1),
        on_tool_round=lambda rnd, msg: calls.__setitem__('tool_round_hook', calls['tool_round_hook'] + 1),
    )
    assert out.completed and out.rounds == 2
    assert calls['tools'] == 1 and calls['tool_round_hook'] == 1
    _ok('loop executes one tool round then completes; on_tool_round fired once')


def test_loop_final_round_gets_no_tools():
    """Round index == max_tool_rounds must be offered tools=None."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    offered = []

    def dispatch(rnd, tools):
        offered.append(tools)
        # Always ask for a tool so the loop is forced to the cap.
        return _mk_msg([{'id': 'x', 'function': {'name': 'web_search', 'arguments': '{}'}}])

    out = run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=2,
                         round_tools=['T'], dispatch=dispatch,
                         execute_tool=lambda rnd, tc: None)
    # rounds 0,1 get ['T']; round 2 (the cap) gets None; then loop ends.
    assert offered == [['T'], ['T'], None], offered
    assert out.rounds == 3 and not out.completed and not out.aborted
    _ok('final (cap) round is offered tools=None; earlier rounds get the tool list')


def test_before_round_check_blocks_dispatch():
    from lib.agent_loop import AbortSignal, run_agent_loop
    calls = {'dispatch': 0}

    def dispatch(rnd, tools):
        calls['dispatch'] += 1
        return _mk_msg(None)

    out = run_agent_loop(abort=AbortSignal(lambda: True), max_tool_rounds=4,
                         round_tools=['T'], dispatch=dispatch,
                         execute_tool=lambda rnd, tc: None)
    assert out.aborted and out.rounds == 0 and calls['dispatch'] == 0
    _ok('(1) before-round abort blocks dispatch entirely')


def test_post_stream_check_stops_before_tools():
    """Abort flips true DURING the dispatch → post-stream check stops before tools."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    flag = {'v': False}
    calls = {'tools': 0}

    def dispatch(rnd, tools):
        flag['v'] = True  # user stopped mid-stream
        return _mk_msg([{'id': 't', 'function': {'name': 'web_search', 'arguments': '{}'}}])

    out = run_agent_loop(abort=AbortSignal(lambda: flag['v']), max_tool_rounds=4,
                         round_tools=['T'], dispatch=dispatch,
                         execute_tool=lambda rnd, tc: calls.__setitem__('tools', calls['tools'] + 1))
    assert out.aborted and out.rounds == 1 and calls['tools'] == 0
    _ok('(2) post-stream abort stops before running the round\u2019s tools')


def test_between_tools_check_skips_remaining_tools():
    """Abort set during the FIRST tool → the SECOND queued tool must not run.

    This is the seam-level assertion of the "Stop has limited effect" fix
    (also proven end-to-end in test_paper_report_abort.py).
    """
    from lib.agent_loop import AbortSignal, run_agent_loop
    flag = {'v': False}
    ran = []
    two_tools = [
        {'id': 't1', 'function': {'name': 'web_search', 'arguments': '{}'}},
        {'id': 't2', 'function': {'name': 'fetch_url', 'arguments': '{}'}},
    ]
    seq = [_mk_msg(list(two_tools))]

    def dispatch(rnd, tools):
        return seq[rnd]

    def execute_tool(rnd, tc):
        ran.append(tc['id'])
        flag['v'] = True  # Stop pressed DURING the first (slow) tool.

    out = run_agent_loop(abort=AbortSignal(lambda: flag['v']), max_tool_rounds=4,
                         round_tools=['T'], dispatch=dispatch, execute_tool=execute_tool)
    assert ran == ['t1'], f'second tool ran despite abort: {ran}'
    assert out.aborted and out.rounds == 1
    _ok('(3) between-tools abort skips remaining queued tools + no fresh round')


def test_exit_reason_completed_and_exhausted():
    """LoopOutcome.exit_reason reports WHY the loop stopped (orchestrator diag parity)."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    # natural completion
    out = run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=2, round_tools=['T'],
                         dispatch=lambda rnd, tools: _mk_msg(None),
                         execute_tool=lambda rnd, tc: None)
    assert out.completed and out.exit_reason == 'completed', out.exit_reason
    # forced to the cap (always asks for a tool) → max_rounds_exhausted
    out2 = run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=1, round_tools=['T'],
                          dispatch=lambda rnd, tools: _mk_msg([{'id': 'x', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
                          execute_tool=lambda rnd, tc: None)
    assert not out2.completed and out2.exit_reason == 'max_rounds_exhausted', out2.exit_reason
    _ok('exit_reason reports completed vs max_rounds_exhausted')


def test_exit_reason_abort_phases():
    """exit_reason distinguishes the three abort placements."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    # before-round
    o1 = run_agent_loop(abort=AbortSignal(lambda: True), max_tool_rounds=2, round_tools=['T'],
                        dispatch=lambda rnd, tools: _mk_msg(None), execute_tool=lambda rnd, tc: None)
    assert o1.exit_reason == 'aborted_before_round', o1.exit_reason
    # post-stream
    f = {'v': False}
    def disp_ps(rnd, tools):
        f['v'] = True
        return _mk_msg([{'id': 't', 'function': {'name': 'web_search', 'arguments': '{}'}}])
    o2 = run_agent_loop(abort=AbortSignal(lambda: f['v']), max_tool_rounds=2, round_tools=['T'],
                        dispatch=disp_ps, execute_tool=lambda rnd, tc: None)
    assert o2.exit_reason == 'aborted_post_stream', o2.exit_reason
    _ok('exit_reason distinguishes before-round vs post-stream aborts')


def test_retry_bonus_grants_extra_round_dynamically():
    """A premature-close retry_bonus hook expands the ceiling mid-loop (orchestrator parity).

    With max_tool_rounds=0 (no tools) a plain for-range would run exactly ONE
    round. The retry_bonus hook, returning True once, must grant ONE extra
    round — matching orchestrator's `_premature_retry_count` growing the while
    ceiling so even a no-tools turn gets its premature-close retry.
    """
    from lib.agent_loop import AbortSignal, run_agent_loop
    disp = {'n': 0}
    def dispatch(rnd, tools):
        disp['n'] += 1
        return _mk_msg(None)
    # retry_bonus fires True on the first round only → one bonus round.
    bonus = {'granted': 0}
    def retry_bonus(rnd, msg, finish, usage):
        if rnd == 0:
            bonus['granted'] += 1
            return True   # premature close → grant a retry round
        return False
    out = run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=0, round_tools=None,
                         dispatch=dispatch, execute_tool=lambda rnd, tc: None,
                         retry_bonus=retry_bonus)
    assert disp['n'] == 2, f'expected 2 dispatches (1 base + 1 bonus), got {disp["n"]}'
    assert out.rounds == 2 and bonus['granted'] == 1
    _ok('retry_bonus grants an extra round dynamically (premature-close parity)')


def test_retry_bonus_is_capped():
    """retry_bonus honours max_retry_bonus so a stuck premature-close can't loop forever."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    disp = {'n': 0}
    def dispatch(rnd, tools):
        disp['n'] += 1
        return _mk_msg(None)
    out = run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=0, round_tools=None,
                         dispatch=dispatch, execute_tool=lambda rnd, tc: None,
                         retry_bonus=lambda *a: True,  # always wants a retry
                         max_retry_bonus=2)
    # base round (1) + 2 capped bonus rounds = 3 dispatches, no more.
    assert disp['n'] == 3, f'expected 3 (1 base + 2 capped bonus), got {disp["n"]}'
    assert out.rounds == 3
    _ok('retry_bonus is capped by max_retry_bonus (no infinite premature-close loop)')


def test_retry_bonus_default_off_preserves_for_range():
    """With no retry_bonus hook the loop is byte-equivalent to the old for-range."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    disp = {'n': 0}
    out = run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=0, round_tools=None,
                         dispatch=lambda rnd, tools: (disp.__setitem__('n', disp['n'] + 1) or _mk_msg(None)),
                         execute_tool=lambda rnd, tc: None)
    assert disp['n'] == 1 and out.rounds == 1  # exactly one round, as before
    _ok('no retry_bonus hook → identical to the original for-range (1 round)')


def test_loop_does_not_swallow_dispatch_exception():
    """A dispatcher exception (e.g. AbortedError) must propagate to the caller."""
    from lib.agent_loop import AbortSignal, run_agent_loop

    class Boom(Exception):
        pass

    def dispatch(rnd, tools):
        raise Boom('propagate me')

    with pytest.raises(Boom):
        run_agent_loop(abort=AbortSignal.never(), max_tool_rounds=2,
                       round_tools=['T'], dispatch=dispatch,
                       execute_tool=lambda rnd, tc: None)
    _ok('loop lets a dispatch exception propagate (AbortedError reaches caller)')


def main():
    print('\n\033[36m═══ agent_loop.py Unit Tests ═══\033[0m\n')
    tests = [
        test_abortsignal_from_event,
        test_abortsignal_from_task_flag,
        test_abortsignal_from_callback_and_never,
        test_abortsignal_broken_predicate_is_safe,
        test_loop_completes_when_no_tool_calls,
        test_loop_runs_tools_then_completes,
        test_loop_final_round_gets_no_tools,
        test_before_round_check_blocks_dispatch,
        test_post_stream_check_stops_before_tools,
        test_between_tools_check_skips_remaining_tools,
        test_exit_reason_completed_and_exhausted,
        test_exit_reason_abort_phases,
        test_retry_bonus_grants_extra_round_dynamically,
        test_retry_bonus_is_capped,
        test_retry_bonus_default_off_preserves_for_range,
        test_loop_does_not_swallow_dispatch_exception,
    ]
    for fn in tests:
        fn()
    print('\n\033[32m═══ ALL %d TESTS PASSED ═══\033[0m\n' % len(tests))


if __name__ == '__main__':
    main()
