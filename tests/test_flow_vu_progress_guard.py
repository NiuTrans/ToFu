"""tests/test_flow_vu_progress_guard.py — FlowExecutor VU-only progress guard.

Mirrors ``tests/test_autopilot_budget_guard.py`` for the ENGINE path. The
standalone autopilot loop stays consistent because it parses the VU's mandatory
``[PROGRESS: resolved=X remaining=Y]`` line and runs
``detect_diminishing_returns`` — catching early churn (the worker edits the same
spot every turn without resolving NEW objective items) BEFORE the cap. The
FlowExecutor lacked that guard, so it churned until the budget burned.

This suite pins the step-3 wiring on the ``virtual_user`` verifier path only:

  (a) a VU emitting flat ``[PROGRESS]`` while the worker re-touches the same
      file receives one strategy nudge but still runs to the finite cap;
  (b) FAIL-OPEN — a critic loop (never emits ``[PROGRESS]``) NEVER trips it,
      even under identical churn: no hard signal ⇒ cannot prove no-progress;
  (c) genuine per-turn progress (resolved advancing) does NOT trip it;
  (d) repeated VU prose is advisory and cannot terminate the bounded loop;
  (e) NEGATIVE CONTROL — neuter parse_progress → None each turn and the guard
      can no longer fire even on stalled progress (proves the hard signal is
      load-bearing, not the churn alone).

@pytest.mark.unit — pure in-process, deterministic stub runner, no live LLM.
"""

import pytest

pytestmark = pytest.mark.unit


def _autopilot_defn(max_iterations=8):
    from lib.orchestration._builtin_definitions import (
        build_autopilot_definition,
    )
    return build_autopilot_definition(max_iterations=max_iterations)


def _critic_loop_defn(max_iterations=8):
    from tests.support.orchestration_definitions import (
        build_verifier_loop_definition,
    )
    return build_verifier_loop_definition(max_iterations=max_iterations)


def _run(defn, fake_runner, *, max_iter=8, events=None):
    import lib.orchestration_engine as eng
    orig = eng.FlowExecutor._default_runner
    eng.FlowExecutor._default_runner = fake_runner
    try:
        ex = eng.FlowExecutor(
            defn,
            agent_runner=None,
            max_iterations=max_iter,
            on_event=events.append if events is not None else None,
        )
        return ex.run(initial_context='do the task')
    finally:
        eng.FlowExecutor._default_runner = orig


# ── (a) VU flat progress + same-target churn → advisory nudge only ──

def test_vu_flat_progress_nudges_once_but_runs_to_cap():
    """The worker ships an edit to the SAME file every turn and the VU reports
    resolved=1 every turn (no NET new items). This can still be incremental
    progress, so it earns one strategy nudge and never an early cutoff."""
    vu = {'n': 0}
    def runner(self, node, context, iteration):
        role = node.get('role')
        if role == 'virtual_user':
            # NOTE: the engine calls the runner with iteration=0 always, so we
            # count turns via a closure. Distinct prose each turn (so
            # stuck/Jaccard does NOT fire) but a STALLED hard signal: resolved
            # stays 1 forever.
            vu['n'] += 1
            uniq = ' '.join(f'aspect{vu["n"]}_{w}' for w in range(vu['n'] + 3))
            return {'output': f'Still needs work on {uniq}. '
                    f'[PROGRESS: resolved=1 remaining=2]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        # Worker re-touches the SAME file every turn — churn on one spot.
        return {'output': 'edited again', 'status': 'completed',
                'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': 'x.py'}]}

    events = []
    res = _run(_autopilot_defn(8), runner, max_iter=8, events=events)
    assert res['ok'] is False
    assert res['stop_reason'] == 'max_iterations', res.get('stop_reason')
    exits = res.get('loop_exits') or []
    assert exits[-1]['iterations'] == 8, exits
    advisory = [event for event in events if event['type'] == 'no_progress']
    assert len(advisory) == 1
    assert advisory[0]['action'] == 'strategy_nudge'


# ── (b) FAIL-OPEN: a critic loop (no PROGRESS) never trips the guard ──

def test_critic_loop_no_progress_line_never_trips_guard():
    """Identical same-file churn, but the verifier is a CRITIC that never emits
    a [PROGRESS] line. The guard must FAIL OPEN — no_progress can't fire; the
    loop instead runs to the cap and reports 'max_iterations'."""
    crit = {'n': 0}
    def runner(self, node, context, iteration):
        if node.get('role') == 'critic':
            crit['n'] += 1
            # Distinct prose (no stuck), NO [PROGRESS] line, never STOP.
            uniq = ' '.join(f'point{crit["n"]}_{w}' for w in range(crit['n'] + 2))
            return {'output': f'More to do: {uniq}. [VERDICT: CONTINUE_WORKER]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': f'edited again ({iteration})', 'status': 'completed',
                'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': 'x.py'}]}

    res = _run(_critic_loop_defn(5), runner, max_iter=5)
    assert res['ok'] is False
    # NOT no_progress — the critic path has no hard signal, so it fails open
    # and the loop exits on the plain iteration cap.
    assert res['stop_reason'] == 'max_iterations', res.get('stop_reason')
    exits = res.get('loop_exits') or []
    assert not any(e['reason'] == 'no_progress' for e in exits), exits


# ── (c) genuine progress does NOT trip the guard ──

def test_vu_real_progress_does_not_trip():
    """resolved advances every turn (real net progress) → the guard must NOT
    fire; the VU stops cleanly with TASK_DONE once remaining hits 0."""
    vu = {'n': 0}
    def runner(self, node, context, iteration):
        role = node.get('role')
        if role == 'virtual_user':
            vu['n'] += 1
            resolved = vu['n']            # advances: 1, 2, 3, ...
            remaining = max(0, 3 - vu['n'])
            done = ' [VU: TASK_DONE]' if remaining == 0 else ''
            return {'output': f'Progress made on part {vu["n"]}.{done} '
                    f'[PROGRESS: resolved={resolved} remaining={remaining}]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': f'work {vu["n"]}', 'status': 'completed', 'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file',
                              'args_brief': f'file{vu["n"]}.py'}]}

    res = _run(_autopilot_defn(8), runner, max_iter=8)
    assert res['ok'] is True, res.get('stop_reason')
    assert res['stop_reason'] == 'completed'
    exits = res.get('loop_exits') or []
    assert exits and exits[-1]['reason'] == 'stop', exits


# ── (d) Similar VU prose is advisory, never terminal ──

def test_vu_feedback_repetition_nudges_once_then_reaches_cap():
    """Repeated acceptance wording cannot prove the worker made no progress."""
    from lib.agent_verdict import AUTOPILOT_STUCK_WINDOW
    assert AUTOPILOT_STUCK_WINDOW == 3
    same = ('Please actually run the tests before claiming done, the login '
            'flow is not verified yet and remains open')
    seen = {'n': 0}
    def runner(self, node, context, iteration):
        role = node.get('role')
        if role == 'virtual_user':
            seen['n'] += 1
            # Same nudge every turn, NO [PROGRESS], so the evidence-grounded
            # no_progress guard fails open.
            return {'output': same, 'status': 'completed', 'error': '',
                    'tool_log': []}
        return {'output': f'work {iteration}', 'status': 'completed', 'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': 'x'}]}

    res = _run(_autopilot_defn(8), runner, max_iter=8)
    assert res['stop_reason'] == 'max_iterations', res.get('stop_reason')
    exits = res.get('loop_exits') or []
    assert exits[-1]['iterations'] == 8, exits


# ── (e) NEGATIVE CONTROL: neuter parse_progress → guard cannot fire ──

def test_NC_neuter_parse_progress_suppresses_advisory():
    """Force parse_progress to return (None, None) — the hard signal the guard
    depends on. Under the SAME stalled churn as (a), no_progress cannot even
    emit its advisory signal, and the loop reaches the same finite cap."""
    import lib.orchestration_engine as eng

    vu = {'n': 0}
    def runner(self, node, context, iteration):
        role = node.get('role')
        if role == 'virtual_user':
            vu['n'] += 1
            uniq = ' '.join(f'aspect{vu["n"]}_{w}' for w in range(vu['n'] + 3))
            return {'output': f'Still stalled on {uniq}. '
                    f'[PROGRESS: resolved=1 remaining=2]',
                    'status': 'completed', 'error': '', 'tool_log': []}
        return {'output': 'edit', 'status': 'completed', 'error': '',
                'tool_log': [{'round': 1, 'tool': 'write_file', 'args_brief': 'x.py'}]}

    orig_runner = eng.FlowExecutor._default_runner
    orig_pp = eng._parse_progress
    eng.FlowExecutor._default_runner = runner
    eng._parse_progress = lambda text: (None, None)   # neuter the hard signal
    try:
        events = []
        ex = eng.FlowExecutor(
            _autopilot_defn(5),
            agent_runner=None,
            max_iterations=5,
            on_event=events.append,
        )
        res = ex.run(initial_context='x')
        exits = res.get('loop_exits') or []
        assert not any(e['reason'] == 'no_progress' for e in exits), exits
        assert not any(e['type'] == 'no_progress' for e in events), events
        assert res['stop_reason'] == 'max_iterations', res.get('stop_reason')
    finally:
        eng.FlowExecutor._default_runner = orig_runner
        eng._parse_progress = orig_pp
