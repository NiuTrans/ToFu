"""tests/test_flow_terminal_honesty.py — FlowExecutor loops report WHY they exit.

Before this, ``FlowExecutor.run()`` returned ``ok = (status=='completed')`` and
``_run_loop`` left a cap-hit / stuck / replan-exhausted loop through a bare
``else: logger.info('hit cap')`` with NO reason propagated — so a loop that
burned its whole ``max_iterations`` budget WITHOUT a verifier STOP still came
back ``ok=True`` and the Flow runner logged ``reason=completed``. That made
every one of the 200+ live engine runs look like a clean success even when they
never converged — the exact dishonesty the standalone autopilot loop avoids
(it emits ``budget_exhausted`` / ``stuck`` / ``no_progress``).

This suite pins the labeling-parity fix:
  * a loop that hits its iteration cap with no STOP → ``run()`` returns
    ``ok=False`` + ``stop_reason='max_iterations'`` and a ``loop_exits`` record;
  * repeated critic prose cannot terminate a loop and reaches the finite cap;
  * a loop that exits on a clean verifier STOP → ``ok=True`` +
    ``stop_reason='completed'`` (the fix must not regress convergent runs);
  * ``is_incomplete_stop`` classifies the new reasons;
  * NEGATIVE CONTROL: forcing the ledger empty makes a cap-hit report
    ``completed`` again — proving the ledger is load-bearing.

Uses a synthetic planner → loop[worker → critic] → stop graph with the
SubAgent runner stubbed, so no live LLM is touched.

@pytest.mark.unit — pure in-process, deterministic stub runner.
"""

import pytest

pytestmark = pytest.mark.unit


def _verifier_loop(max_iterations=3):
    from tests.support.orchestration_definitions import (
        build_verifier_loop_definition,
    )
    return build_verifier_loop_definition(max_iterations=max_iterations)


def _run_with_runner(defn, fake_runner, *, max_iter=3):
    """Run a definition on FlowExecutor with a stubbed per-node runner."""
    import lib.orchestration_engine as eng
    orig = eng.FlowExecutor._default_runner
    eng.FlowExecutor._default_runner = fake_runner
    try:
        ex = eng.FlowExecutor(defn, agent_runner=None, max_iterations=max_iter)
        return ex.run(initial_context='do the task')
    finally:
        eng.FlowExecutor._default_runner = orig


# ── the core labeling-parity assertions ───────────────────────────────

def test_caphit_reports_max_iterations_not_completed():
    """A critic that NEVER emits STOP burns the whole budget. The run must be
    ok=False with stop_reason='max_iterations', NOT a clean completed."""
    crit = {'n': 0}
    def never_stop(self, node, context, iteration):
        role = node.get('role')
        if role == 'critic':
            # Always CONTINUE_WORKER with distinct feedback each turn.
            crit['n'] += 1
            uniq = ' '.join(f'issue{crit["n"]}_{w}' for w in range(crit['n'] + 3))
            return {'output': f'Remaining work: {uniq}. [VERDICT: CONTINUE_WORKER]',
                    'status': 'completed', 'error': '',
                    'tool_log': []}
        # worker/planner ship a state-changing tool each turn (not zero-
        # deliverable — we want the cap, not the zero-deliverable guard).
        return {'output': f'work {iteration}', 'status': 'completed', 'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': ''}]}

    res = _run_with_runner(_verifier_loop(3), never_stop, max_iter=3)
    assert res['status'] == 'completed'      # walk finished (no crash)
    assert res['ok'] is False, 'a burned-budget loop must not be ok'
    assert res['stop_reason'] == 'max_iterations', res.get('stop_reason')
    assert res['outcome']['category'] == 'incomplete'
    assert res['outcome']['lifecycle_status'] == 'error'
    assert res['outcome']['chat_status'] == 'done'
    assert res['outcome']['finish_reason'] == 'incomplete'
    exits = res.get('loop_exits') or []
    assert any(e['reason'] == 'max_iterations' for e in exits), exits


def test_repeated_feedback_is_advisory_and_reports_iteration_cap():
    """Near-identical critic prose cannot prove no progress or end the loop."""
    same = 'The parser still fails on empty input ❌ please fix it now'
    def repeating(self, node, context, iteration):
        if node.get('role') == 'critic':
            return {'output': same + ' [VERDICT: CONTINUE_WORKER]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': f'work {iteration}', 'status': 'completed', 'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': ''}]}

    res = _run_with_runner(_verifier_loop(6), repeating, max_iter=6)
    assert res['ok'] is False
    assert res['stop_reason'] == 'max_iterations', res.get('stop_reason')
    exits = res.get('loop_exits') or []
    assert any(e['reason'] == 'max_iterations' for e in exits), exits


def test_clean_stop_stays_completed():
    """A critic that approves on the first review → clean convergent exit:
    ok=True, stop_reason='completed'. The honesty fix must NOT regress this."""
    def approve(self, node, context, iteration):
        if node.get('role') == 'critic':
            return {'output': 'All acceptance criteria met. [VERDICT: STOP]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': f'work {iteration}', 'status': 'completed', 'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': ''}]}

    res = _run_with_runner(_verifier_loop(3), approve, max_iter=3)
    assert res['ok'] is True
    assert res['stop_reason'] == 'completed', res.get('stop_reason')
    exits = res.get('loop_exits') or []
    assert exits and exits[-1]['reason'] == 'stop', exits


def test_is_incomplete_stop_classifies_engine_reasons():
    """The reasons the engine now emits are the SAME incomplete-stop vocabulary
    the standalone loops use — so a shared classifier flags them."""
    from lib.agent_verdict import is_incomplete_stop
    for r in ('max_iterations', 'stuck', 'no_progress', 'replan_exhausted'):
        assert is_incomplete_stop(r), r
    assert not is_incomplete_stop('stop')
    assert not is_incomplete_stop('completed')


# ── negative control: the ledger is load-bearing ──────────────────────

def test_NC_empty_ledger_regresses_to_completed():
    """Neuter: if the loop-exit ledger never records anything (simulating the
    pre-fix engine), a genuine cap-hit reports ok=True/completed again — proof
    the ledger is what carries the honesty."""
    import lib.orchestration_engine as eng

    def never_stop(self, node, context, iteration):
        if node.get('role') == 'critic':
            return {'output': 'keep going [VERDICT: CONTINUE_WORKER]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': 'work', 'status': 'completed', 'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': ''}]}

    orig_runner = eng.FlowExecutor._default_runner
    orig_loop = eng.FlowExecutor._run_loop

    def _loop_no_ledger(self, lid, context):
        # Run the real loop, then wipe the ledger it appended.
        out = orig_loop(self, lid, context)
        self._loop_exits.clear()
        return out

    eng.FlowExecutor._default_runner = never_stop
    eng.FlowExecutor._run_loop = _loop_no_ledger
    try:
        ex = eng.FlowExecutor(_verifier_loop(3), agent_runner=None, max_iterations=3)
        res = ex.run(initial_context='x')
        # With the ledger neutered, the burned-budget run falsely looks clean.
        assert res['ok'] is True
        assert res['stop_reason'] == 'completed'
    finally:
        eng.FlowExecutor._default_runner = orig_runner
        eng.FlowExecutor._run_loop = orig_loop
