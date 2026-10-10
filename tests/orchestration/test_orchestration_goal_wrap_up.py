"""FlowExecutor goal wrap-up pass — boundary behaviour pinned on mainline.

An accepted VU stop verdict ends the loop immediately; a directive-driven
worker close-out pass (``GOAL_WRAP_UP_DIRECTIVE`` / ``goal_wrap_up`` event)
was designed on checkpoint sideline 75e1df1ea but NEVER merged into mainline
lib/orchestration — the case asserting it was removed here (2026-09). What
mainline guarantees, and what the remaining cases pin: handoff flows keep
their authored ending (no wrap-up turn), and the abort boundary wins
immediately at the stop verdict (no extra worker pass is scheduled).

@pytest.mark.unit — pure in-process, deterministic stub runner, no live LLM.
"""

import pytest

pytestmark = pytest.mark.unit


def _autopilot_defn(max_iterations=4):
    from lib.orchestration._builtin_definitions import (
        build_autopilot_definition,
    )
    return build_autopilot_definition(max_iterations=max_iterations)


def _run(defn, fake_runner, *, max_iter=4, events=None,
         worker_context_policy='handoff'):
    import lib.orchestration.engine as eng
    orig = eng.FlowExecutor._default_runner
    eng.FlowExecutor._default_runner = fake_runner
    try:
        ex = eng.FlowExecutor(
            defn,
            agent_runner=None,
            max_iterations=max_iter,
            on_event=events.append if events is not None else None,
            worker_context_policy=worker_context_policy,
        )
        return ex.run(initial_context='do the task')
    finally:
        eng.FlowExecutor._default_runner = orig


def _stoppable_runner(calls):
    """Worker produces no state changes; the VU verifies and stops at once."""
    def runner(self, node, context, iteration):
        role = node.get('role')
        calls.append((role, context))
        if role == 'virtual_user':
            return {'output': 'Verified by inspection. [VU: TASK_DONE] '
                    '[PROGRESS: resolved=2 remaining=0]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': f'worker output {len(calls)}',
                'status': 'completed', 'error': '', 'tool_log': []}
    return runner


def test_handoff_flow_keeps_its_authored_ending():
    calls = []
    events = []
    res = _run(
        _autopilot_defn(4),
        _stoppable_runner(calls),
        events=events,
        worker_context_policy='handoff',
    )

    assert res['ok'] is True, res.get('stop_reason')
    assert [role for role, _context in calls] == ['worker', 'virtual_user']
    assert not any(e['type'] == 'goal_wrap_up' for e in events)


def test_wrap_up_is_skipped_when_the_run_is_aborted_at_the_stop_boundary():
    calls = []
    gate = {'armed': False}

    def abort_check():
        return gate['armed']

    def runner(self, node, context, iteration):
        role = node.get('role')
        calls.append(role)
        if role == 'virtual_user':
            gate['armed'] = True
            return {'output': 'Verified. [VU: TASK_DONE] '
                    '[PROGRESS: resolved=2 remaining=0]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': 'worker output', 'status': 'completed',
                'error': '', 'tool_log': []}

    import lib.orchestration.engine as eng
    orig = eng.FlowExecutor._default_runner
    eng.FlowExecutor._default_runner = runner
    try:
        ex = eng.FlowExecutor(
            _autopilot_defn(4),
            agent_runner=None,
            max_iterations=4,
            abort_check=abort_check,
            worker_context_policy='conversation',
        )
        ex.run(initial_context='do the task')
    finally:
        eng.FlowExecutor._default_runner = orig

    # The abort boundary wins immediately: no extra worker pass is scheduled.
    assert calls == ['worker', 'virtual_user']
