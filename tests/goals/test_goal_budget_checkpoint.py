"""Goal-mode budget checkpoints and the gated VU [VERDICT: BLOCKED] verdict.

A budget window is authorized work, never a stop reason: a closing window
that still produced durable state changes or verified progress is extended
automatically; only a full window with ZERO of both ends the loop as
``stalled`` (the evidence-based stall breaker).  A verifier-declared impasse
ends the loop as ``vu_blocked`` only after passing the receipt, remaining,
justification and one-challenge gates.
"""

import threading

import pytest

from lib.agent_verdict import classify_verdict, is_incomplete_stop, parse_progress
from lib.orchestration.feedback import OrchestrationFeedbackState
from lib.orchestration.graph import GraphNavigator
from lib.orchestration.loop_policy import (
    BUDGET_POLICY_BOUNDED,
    BUDGET_POLICY_CHECKPOINT_EXTEND,
)
from lib.orchestration.loop_runtime import (
    GOAL_BLOCKED_MIN_JUSTIFICATION_CHARS,
    OrchestrationLoopRuntime,
)
from lib.orchestration.outcome_ledger import OrchestrationOutcomeLedger
from lib.orchestration.progress import OrchestrationProgressLedger
from lib.orchestration.transcript import OrchestrationTranscript

from lib.orchestration.tool_usage import classify_orchestration_tool_usage
from lib.tools.meta import build_project_tool_meta
pytestmark = pytest.mark.unit


def _runtime(
    verifier_outputs,
    *,
    max_iterations=2,
    budget_policy=BUDGET_POLICY_CHECKPOINT_EXTEND,
    state_changing=1,
    producer_reported=False,
    tool_results=None,
):
    lock = threading.Lock()
    events = []
    iterations = []
    nodes = {
        'loop': {
            'id': 'loop',
            'type': 'control',
            'kind': 'loop',
            'params': {'max_iterations': max_iterations},
        },
        'worker': {'id': 'worker', 'type': 'role', 'role': 'worker'},
        'vu': {'id': 'vu', 'type': 'role', 'role': 'virtual_user'},
        'stop': {'id': 'stop', 'type': 'control', 'kind': 'stop'},
    }
    edges = [
        {'from': 'loop', 'to': 'worker'},
        {'from': 'worker', 'to': 'vu'},
        {'from': 'vu', 'to': 'loop'},
        {'from': 'loop', 'to': 'stop'},
    ]
    navigator = GraphNavigator.from_edges(nodes, edges)
    progress = OrchestrationProgressLedger(lock=lock)
    feedback = OrchestrationFeedbackState(lock=lock)
    outcomes = OrchestrationOutcomeLedger(lock=lock)
    transcript = OrchestrationTranscript(lock=lock)
    outputs = iter(verifier_outputs)

    tool_results = iter(tool_results) if tool_results is not None else None
    sc_sequence = iter(
        state_changing if isinstance(state_changing, (list, tuple))
        else [state_changing] * 1000
    )

    def walk(_entry, context, *, stop_at):
        assert stop_at == 'loop'
        sc = next(sc_sequence)
        explore, names, reported = 0 if sc else 1, ['write_file'] if sc else [], producer_reported
        if tool_results is not None:
            sc, explore, names, reported = classify_orchestration_tool_usage(
                next(tool_results)).engine_tuple()
        progress.record_producer({
            'node_id': 'worker',
            'role': 'worker',
            'sc_count': sc,
            'explore_count': explore,
            'names': names,
            'reported': reported,
        })
        transcript.record(
            'vu', 'virtual_user', next(outputs), 'completed', '', 0,
        )
        return context + '|iteration'

    def classify(text, *, verifier_role=''):
        assert verifier_role == 'virtual_user'
        verdict = classify_verdict(text, verifier_role=verifier_role)
        return verdict['phase'], verdict['plan_defect']

    runtime = OrchestrationLoopRuntime(
        navigator=navigator,
        nodes=nodes,
        max_iterations=max_iterations,
        budget_policy=budget_policy,
        feedback=feedback,
        progress=progress,
        outcomes=outcomes,
        transcript=transcript,
        emit=events.append,
        abort_check=lambda: False,
        walk=walk,
        run_replan=lambda *_args: '',
        classify_verdict=classify,
        progress_parser=parse_progress,
        on_iteration_change=iterations.append,
    )
    return runtime, outcomes, feedback, events, iterations


def _checkpoints(events, action=None):
    rows = [event for event in events
            if event['type'] == 'goal_budget_checkpoint']
    if action is not None:
        rows = [event for event in rows if event['action'] == action]
    return rows


def test_checkpoint_extends_a_window_that_is_still_advancing():
    runtime, outcomes, feedback, events, iterations = _runtime(
        [
            'Working. [PROGRESS: resolved=0 remaining=2]',
            'Working. [PROGRESS: resolved=0 remaining=2]',
            'Done, verified with my own eyes. [VU: TASK_DONE]\n'
            '[PROGRESS: resolved=2 remaining=0]',
            'Done again after the challenge. [VU: TASK_DONE]\n'
            '[PROGRESS: resolved=2 remaining=0]',
        ],
        max_iterations=2,
        state_changing=1,
        producer_reported=True,
    )
    directives = []
    original_set_directive = feedback.set_directive
    feedback.set_directive = lambda text: (
        directives.append(text), original_set_directive(text))[1]

    runtime.run('loop', 'request')

    # The first DONE is challenged once (producer changed state, the VU used
    # no tools) and the re-issued claim is accepted — the run still ends by
    # verdict, never by budget.
    assert iterations == [1, 2, 3, 4, 0]
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'stop'
    extensions = _checkpoints(events, action='extend')
    assert len(extensions) == 1
    assert extensions[0]['iteration'] == 2
    assert extensions[0]['new_max'] == 4
    assert extensions[0]['state_changing_delta'] == 2
    # Budget is never a reason to stop: the checkpoint armed the worker with
    # the continuation directive (later overwritten by the stop challenge).
    assert any(
        'never a reason to stop' in directive for directive in directives
    )
    assert not _checkpoints(events, action='stalled')


def test_checkpoint_stalled_window_is_the_only_budget_era_stop():
    runtime, outcomes, _feedback, events, iterations = _runtime(
        [
            'Still thinking, no receipt.',
            'Still thinking, no receipt.',
        ],
        max_iterations=2,
        state_changing=0,
    )

    runtime.run('loop', 'request')

    assert iterations == [1, 2, 0]
    exits = outcomes.loop_exits_snapshot()
    assert exits[0]['reason'] == 'stalled'
    stalls = _checkpoints(events, action='stalled')
    assert len(stalls) == 1
    assert stalls[0]['state_changing_delta'] == 0
    assert stalls[0]['resolved_delta'] is None
    # The stall exit is an incomplete (unverified) outcome, never success.
    assert is_incomplete_stop('stalled') is True


def test_checkpoint_extension_then_stall_when_progress_dries_up():
    runtime, outcomes, _feedback, _events, iterations = _runtime(
        [
            'Round one built something. [PROGRESS: resolved=0 remaining=2]',
            'Round two idled. [PROGRESS: resolved=0 remaining=2]',
            'Round three idled. [PROGRESS: resolved=0 remaining=2]',
            'Round four idled. [PROGRESS: resolved=0 remaining=2]',
        ],
        max_iterations=2,
        state_changing=[1, 0, 0, 0],
    )

    runtime.run('loop', 'request')

    # Window 1 advanced (one state change) → extended; window 2 produced
    # nothing durable and no verified progress → evidence-based stop.
    assert iterations == [1, 2, 3, 4, 0]
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'stalled'


def test_bounded_policy_still_exits_at_the_cap():
    runtime, outcomes, _feedback, events, iterations = _runtime(
        ['Keep going. [PROGRESS: resolved=0 remaining=9]'] * 3,
        max_iterations=2,
        budget_policy=BUDGET_POLICY_BOUNDED,
        state_changing=1,
        producer_reported=True,
    )

    runtime.run('loop', 'request')

    assert iterations == [1, 2, 0]
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'max_iterations'
    assert not _checkpoints(events)


def _blocked(text, *, remaining=2):
    justification = (
        'The deploy requires credentials only the human holds; the same '
        'blocker repeated across three consecutive turns and no '
        'agent-reachable step remains.'
    )
    return (f'{text} {justification} [VERDICT: BLOCKED]\n'
            f'[PROGRESS: resolved=1 remaining={remaining}]')


def test_vu_blocked_is_challenged_once_then_accepted_as_typed_blocked():
    runtime, outcomes, _feedback, events, iterations = _runtime(
        [_blocked('First impasse report.'), _blocked('Still blocked.')],
        max_iterations=5,
        state_changing=0,
    )

    runtime.run('loop', 'request')

    assert iterations == [1, 2, 0]
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'vu_blocked'
    rejections = [event for event in events
                  if event['type'] == 'goal_blocked_rejected']
    assert [event['reason'] for event in rejections] == ['challenged']
    assert is_incomplete_stop('vu_blocked') is True


def test_vu_blocked_without_progress_receipt_is_rejected():
    runtime, outcomes, _feedback, events, iterations = _runtime(
        [
            'Blocked, no receipt. The blocker is real and repeated and '
            'needs the human to act on it directly. [VERDICT: BLOCKED]',
            'Done instead. [VU: TASK_DONE]\n[PROGRESS: resolved=0 remaining=0]',
            'Done with justification: advisory question, no tool work '
            'needed. [VU: TASK_DONE]\n[PROGRESS: resolved=0 remaining=0]',
        ],
        max_iterations=5,
        state_changing=0,
    )

    runtime.run('loop', 'request')

    assert iterations == [1, 2, 3, 0]
    rejections = [event for event in events
                  if event['type'] == 'goal_blocked_rejected']
    assert rejections[0]['reason'] == 'evidence_missing'


def test_vu_blocked_with_zero_remaining_is_a_completion_claim():
    runtime, outcomes, feedback, events, _iterations = _runtime(
        [
            _blocked('Contradictory claim.', remaining=0),
            'Verified after all. [VU: TASK_DONE]\n'
            '[PROGRESS: resolved=1 remaining=0]',
        ],
        max_iterations=5,
        state_changing=0,
    )

    runtime.run('loop', 'request')

    rejections = [event for event in events
                  if event['type'] == 'goal_blocked_rejected']
    assert rejections[0]['reason'] == 'remaining_zero'
    assert 'completion claim' in feedback.pending_directive()


def test_vu_blocked_with_vacuous_justification_is_rejected():
    runtime, outcomes, _feedback, events, _iterations = _runtime(
        [
            'Blocked. [VERDICT: BLOCKED]\n[PROGRESS: resolved=1 remaining=2]',
            'Verified after all. [VU: TASK_DONE]\n'
            '[PROGRESS: resolved=1 remaining=0]',
        ],
        max_iterations=5,
        state_changing=0,
    )

    runtime.run('loop', 'request')

    rejections = [event for event in events
                  if event['type'] == 'goal_blocked_rejected']
    assert rejections[0]['reason'] == 'vacuous'
    assert len('Blocked.') < GOAL_BLOCKED_MIN_JUSTIFICATION_CHARS


def test_verdict_classifier_surfaces_blocked_only_for_virtual_user():
    blocked = classify_verdict(
        _blocked('Impasse.'), verifier_role='virtual_user')
    assert blocked['phase'] == 'blocked'
    assert blocked['had_tag'] is True
    # A critic emitting the same tag keeps working: blocked is a VU-only
    # phase, the generic critic path never produces it.
    critic = classify_verdict(_blocked('Impasse.'), verifier_role='critic')
    assert critic['phase'] == 'worker'
    # A reply carrying BOTH sentinels resolves as a stop claim so the
    # contradictory signal fails closed through the existing done gates.
    both = classify_verdict(
        'Claiming done while blocked. [VU: TASK_DONE] [VERDICT: BLOCKED]\n'
        '[PROGRESS: resolved=2 remaining=0]',
        verifier_role='virtual_user',
    )
    assert both['phase'] == 'stop'


def _tool_result(name, args, content):
    return {'tool_log': [{
        'toolName': name, 'toolArgs': args, 'status': 'done', 'toolContent': content,
        'results': [build_project_tool_meta(name, args, content)],
    }]}


@pytest.mark.parametrize('name,args,content', [
    ('run_command', {'command': 'git status --short'}, '$ git status --short\n[exit code: 0]'),
    ('run_command', {'command': 'false'}, '$ false\n[exit code: 1]'),
    ('run_command', {'command': 'true'}, '$ true\n[exit code: 0]'),
    ('code_exec', {'code': 'print(1)'}, '1'),
    ('write_file', {'path': 'old', 'content': 'same'}, 'File updated: old'),
    ('apply_diff', {'path': 'old', 'search': 'same', 'replace': 'same'}, 'Applied diff to old'),
])
def test_unverified_tool_activity_cannot_extend_goal_window(name, args, content):
    runtime, outcomes, _feedback, events, iterations = _runtime(
        ['Still working. [PROGRESS: resolved=0 remaining=2]'] * 2,
        tool_results=[_tool_result(name, args, content)] * 2,
    )
    runtime.run('loop', 'request')
    assert iterations == [1, 2, 0]
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'stalled'
    assert not _checkpoints(events, 'extend')
    assert _checkpoints(events, 'stalled')[0]['state_changing_delta'] == 0


def test_verified_writes_extend_beyond_legacy_total_cap_then_stall_honestly():
    # There is no arbitrary lifetime cap: real work can earn many windows.
    work = [_tool_result('write_file', {'path': f'new-{i}', 'content': 'new'},
                         f'File created: new-{i}') for i in range(66)]
    read = _tool_result('run_command', {'command': 'git status'},
                        '$ git status\n[exit code: 0]')
    runtime, outcomes, _feedback, events, iterations = _runtime(
        ['Still working. [PROGRESS: resolved=0 remaining=2]'] * 68,
        tool_results=work + [read, read],
    )
    runtime.run('loop', 'request')
    assert iterations == list(range(1, 69)) + [0]
    assert len(_checkpoints(events, 'extend')) == 33
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'stalled'
    assert _checkpoints(events, 'stalled')[0]['state_changing_delta'] == 0


@pytest.mark.parametrize('middle', [
    'Working without a receipt.',
    'Working. [PROGRESS: resolved=0 remaining=2]',
])
def test_regressed_or_missing_receipt_does_not_recredit_old_resolved_progress(middle):
    runtime, outcomes, _feedback, events, iterations = _runtime(
        ['Working. [PROGRESS: resolved=3 remaining=2]', middle,
         'Working. [PROGRESS: resolved=3 remaining=2]'],
        max_iterations=1, state_changing=[0, 1, 0],
    )
    runtime.run('loop', 'request')
    assert iterations == [1, 2, 3, 0]
    assert outcomes.loop_exits_snapshot()[0]['reason'] == 'stalled'
    assert len(_checkpoints(events, 'extend')) == 2
    assert _checkpoints(events, 'stalled')[0]['resolved_delta'] == 0
