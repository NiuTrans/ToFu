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

    out = run_agent_loop(abort=AbortSignal.never(),
                         round_tools=['T'], dispatch=dispatch,
                         execute_tool=lambda rnd, tc: calls.__setitem__('tools', calls['tools'] + 1))
    assert out.completed and not out.aborted
    assert out.rounds == 1 and calls['dispatch'] == 1 and calls['tools'] == 0
    _ok('loop completes on a no-tool-calls turn (1 round, 0 tools)')


def test_removed_tool_round_limit_arguments_are_rejected():
    """The retired numeric/terminal-round knobs cannot silently return."""
    import inspect
    from lib.agent_loop import AbortSignal, run_agent_loop

    params = inspect.signature(run_agent_loop).parameters
    assert 'max_tool_rounds' not in params
    assert 'tools_terminal_round' not in params
    base = dict(
        abort=AbortSignal.never(),
        round_tools=['T'],
        dispatch=lambda rnd, tools: _mk_msg(None),
        execute_tool=lambda rnd, tc: None,
    )
    with pytest.raises(TypeError):
        run_agent_loop(**base, max_tool_rounds=8)
    with pytest.raises(TypeError):
        run_agent_loop(**base, tools_terminal_round=True)
    _ok('retired tool-round limit arguments are absent and strictly rejected')


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
        abort=AbortSignal.never(), round_tools=['T'],
        dispatch=dispatch,
        execute_tool=lambda rnd, tc: calls.__setitem__('tools', calls['tools'] + 1),
        on_tool_round=lambda rnd, msg: calls.__setitem__('tool_round_hook', calls['tool_round_hook'] + 1),
    )
    assert out.completed and out.rounds == 2
    assert calls['tools'] == 1 and calls['tool_round_hook'] == 1
    _ok('loop executes one tool round then completes; on_tool_round fired once')


def test_tools_stay_available_until_natural_completion():
    """Tool schemas stay available across arbitrarily many productive rounds."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    offered = []

    def dispatch(rnd, tools):
        offered.append(tools)
        if rnd < 25:
            return _mk_msg([{'id': 'x', 'function': {'name': 'web_search', 'arguments': '{}'}}])
        return _mk_msg(None)

    out = run_agent_loop(abort=AbortSignal.never(),
                         round_tools=['T'], dispatch=dispatch,
                         execute_tool=lambda rnd, tc: None)
    assert offered == [['T']] * 26, offered
    assert out.rounds == 26 and out.completed and not out.aborted
    _ok('tools remain available past former caps until natural completion')


def test_before_round_check_blocks_dispatch():
    from lib.agent_loop import AbortSignal, run_agent_loop
    calls = {'dispatch': 0}

    def dispatch(rnd, tools):
        calls['dispatch'] += 1
        return _mk_msg(None)

    out = run_agent_loop(abort=AbortSignal(lambda: True),
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

    out = run_agent_loop(abort=AbortSignal(lambda: flag['v']),
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

    out = run_agent_loop(abort=AbortSignal(lambda: flag['v']),
                         round_tools=['T'], dispatch=dispatch, execute_tool=execute_tool)
    assert ran == ['t1'], f'second tool ran despite abort: {ran}'
    assert out.aborted and out.rounds == 1
    _ok('(3) between-tools abort skips remaining queued tools + no fresh round')


def test_exit_reason_completed():
    """LoopOutcome reports natural completion without a cap-exhausted state."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    # natural completion
    out = run_agent_loop(abort=AbortSignal.never(), round_tools=['T'],
                         dispatch=lambda rnd, tools: _mk_msg(None),
                         execute_tool=lambda rnd, tc: None)
    assert out.completed and out.exit_reason == 'completed', out.exit_reason
    _ok('exit_reason reports natural completion')


def test_exit_reason_abort_phases():
    """exit_reason distinguishes the three abort placements."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    # before-round
    o1 = run_agent_loop(abort=AbortSignal(lambda: True), round_tools=['T'],
                        dispatch=lambda rnd, tools: _mk_msg(None), execute_tool=lambda rnd, tc: None)
    assert o1.exit_reason == 'aborted_before_round', o1.exit_reason
    # post-stream
    f = {'v': False}
    def disp_ps(rnd, tools):
        f['v'] = True
        return _mk_msg([{'id': 't', 'function': {'name': 'web_search', 'arguments': '{}'}}])
    o2 = run_agent_loop(abort=AbortSignal(lambda: f['v']), round_tools=['T'],
                        dispatch=disp_ps, execute_tool=lambda rnd, tc: None)
    assert o2.exit_reason == 'aborted_post_stream', o2.exit_reason
    _ok('exit_reason distinguishes before-round vs post-stream aborts')


def test_retry_bonus_grants_extra_round_dynamically():
    """A premature-close retry hook retries before accepting natural stop."""
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
    out = run_agent_loop(abort=AbortSignal.never(), round_tools=None,
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
    out = run_agent_loop(abort=AbortSignal.never(), round_tools=None,
                         dispatch=dispatch, execute_tool=lambda rnd, tc: None,
                         retry_bonus=lambda *a: True,  # always wants a retry
                         max_retry_bonus=2)
    # base round (1) + 2 capped bonus rounds = 3 dispatches, no more.
    assert disp['n'] == 3, f'expected 3 (1 base + 2 capped bonus), got {disp["n"]}'
    assert out.rounds == 3
    _ok('retry_bonus is capped by max_retry_bonus (no infinite premature-close loop)')


def test_retry_bonus_default_off_completes_once():
    """With no retry hook, a no-tool response completes in one dispatch."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    disp = {'n': 0}
    out = run_agent_loop(abort=AbortSignal.never(), round_tools=None,
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
        run_agent_loop(abort=AbortSignal.never(),
                       round_tools=['T'], dispatch=dispatch,
                       execute_tool=lambda rnd, tc: None)
    _ok('loop lets a dispatch exception propagate (AbortedError reaches caller)')


def test_before_round_hook_halts_with_reason():
    """The before_round halt hook stops the loop with outcome.halted and a
    custom exit_reason (swarm's timeout is the first adopter)."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    disp = {'n': 0}

    def dispatch(rnd, tools):
        disp['n'] += 1
        # Always ask for a tool so only the hook can stop the loop.
        return _mk_msg([{'id': 'x', 'function': {'name': 'web_search', 'arguments': '{}'}}])

    out = run_agent_loop(abort=AbortSignal.never(),
                         round_tools=['T'], dispatch=dispatch,
                         execute_tool=lambda rnd, tc: None,
                         before_round=lambda rnd: 'timeout' if rnd >= 2 else None)
    assert out.halted and out.exit_reason == 'timeout', out.exit_reason
    assert not out.aborted and not out.completed
    assert out.rounds == 2 and disp['n'] == 2  # rnd 0,1 ran; rnd 2 halted at top
    _ok('before_round halt hook stops the loop with custom reason (timeout seam)')


def test_execute_tools_batch_hook_replaces_per_tool_loop():
    """The batch execute_tools hook receives the round's whole tool list ONCE
    (parallel-pool engines); per-tool execute_tool must NOT fire."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    two = [
        {'id': 't1', 'function': {'name': 'web_search', 'arguments': '{}'}},
        {'id': 't2', 'function': {'name': 'fetch_url', 'arguments': '{}'}},
    ]
    calls = {'batch': [], 'per_tool': 0}
    seq = iter([_mk_msg(list(two)), _mk_msg(None)])

    out = run_agent_loop(abort=AbortSignal.never(),
                         round_tools=['T'],
                         dispatch=lambda rnd, tools: next(seq),
                         execute_tool=lambda rnd, tc: calls.__setitem__('per_tool', calls['per_tool'] + 1),
                         execute_tools=lambda rnd, tcs: calls['batch'].append(list(tcs)))
    assert out.completed and out.rounds == 2
    assert calls['per_tool'] == 0, 'per-tool hook fired despite batch hook'
    assert len(calls['batch']) == 1 and calls['batch'][0] == two
    _ok('execute_tools batch hook fires once with the full list; per-tool skipped')


def test_tool_timeout_breaker_halts_at_threshold():
    """max_consecutive_tool_timeouts: consecutive timed_out batch notes halt
    the loop with exit_reason 'tool_timeout'; a halted round fires NO
    on_round_end (mirrors orchestrator: breaker break precedes checkpoint)."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    disp = {'n': 0}
    round_ends = []

    def dispatch(rnd, tools):
        disp['n'] += 1
        return _mk_msg([{'id': 'x', 'function': {'name': 'web_search', 'arguments': '{}'}}])

    out = run_agent_loop(abort=AbortSignal.never(),
                         round_tools=['T'], dispatch=dispatch,
                         execute_tools=lambda rnd, tcs: {'timed_out': True},
                         max_consecutive_tool_timeouts=2,
                         on_round_end=lambda rnd: round_ends.append(rnd))
    assert out.halted and out.exit_reason == 'tool_timeout', out.exit_reason
    assert out.consecutive_tool_timeouts == 2
    assert disp['n'] == 2 and out.rounds == 2
    assert round_ends == [0], f'round-ends {round_ends} — halted round must NOT checkpoint'
    _ok('timeout breaker: 2 consecutive timeouts halt, halted round skips on_round_end')


def test_tool_timeout_breaker_resets_on_clean_round():
    """A round whose batch note is falsy resets the consecutive count —
    isolated timeouts never trip the breaker."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    notes = iter([{'timed_out': True}, None, {'timed_out': True}, None])
    seq = iter([
        _mk_msg([{'id': 'a', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
        _mk_msg([{'id': 'b', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
        _mk_msg([{'id': 'c', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
        _mk_msg([{'id': 'd', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
        _mk_msg(None),
    ])
    out = run_agent_loop(abort=AbortSignal.never(),
                         round_tools=['T'], dispatch=lambda rnd, tools: next(seq),
                         execute_tools=lambda rnd, tcs: next(notes),
                         max_consecutive_tool_timeouts=2)
    assert out.completed and out.rounds == 5, out.exit_reason
    assert out.consecutive_tool_timeouts == 0  # final tools round note was None
    _ok('timeout breaker: clean round resets the consecutive count')


def test_on_round_end_fires_only_after_executed_tool_rounds():
    """on_round_end fires once per tools-executed round — NOT on the final
    answer round and NOT on an aborted round."""
    from lib.agent_loop import AbortSignal, run_agent_loop
    ends = []
    seq = iter([
        _mk_msg([{'id': 'a', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
        _mk_msg([{'id': 'b', 'function': {'name': 'web_search', 'arguments': '{}'}}]),
        _mk_msg(None),
    ])
    out = run_agent_loop(abort=AbortSignal.never(),
                         round_tools=['T'], dispatch=lambda rnd, tools: next(seq),
                         execute_tools=lambda rnd, tcs: None,
                         on_round_end=lambda rnd: ends.append(rnd))
    assert out.completed and out.rounds == 3
    assert ends == [0, 1], ends
    _ok('on_round_end: fires per tools round, not on the final-answer round')


def main():
    print('\n\033[36m═══ agent_loop.py Unit Tests ═══\033[0m\n')
    tests = [
        test_abortsignal_from_event,
        test_abortsignal_from_task_flag,
        test_abortsignal_from_callback_and_never,
        test_abortsignal_broken_predicate_is_safe,
        test_loop_completes_when_no_tool_calls,
        test_loop_runs_tools_then_completes,
        test_tools_stay_available_until_natural_completion,
        test_before_round_check_blocks_dispatch,
        test_post_stream_check_stops_before_tools,
        test_between_tools_check_skips_remaining_tools,
        test_exit_reason_completed,
        test_exit_reason_abort_phases,
        test_retry_bonus_grants_extra_round_dynamically,
        test_retry_bonus_is_capped,
        test_retry_bonus_default_off_completes_once,
        test_loop_does_not_swallow_dispatch_exception,
        test_before_round_hook_halts_with_reason,
        test_execute_tools_batch_hook_replaces_per_tool_loop,
        test_tool_timeout_breaker_halts_at_threshold,
        test_tool_timeout_breaker_resets_on_clean_round,
        test_on_round_end_fires_only_after_executed_tool_rounds,
    ]
    for fn in tests:
        fn()
    print('\n\033[32m═══ ALL %d TESTS PASSED ═══\033[0m\n' % len(tests))


if __name__ == '__main__':
    main()
