"""Integration test: endpoint replan loop.

Drives ``run_endpoint_task`` through the scripted sequence:
  Planner(1) → Worker(1) → Critic(CONTINUE_PLANNER) →
  Planner(2) → Worker(2) → Critic(STOP)

and asserts:
  - endpoint_turns contains 2 planner rows + 2 worker rows + 2 critic rows
  - Both planner rows carry _isEndpointPlanner=True, with _epPlannerIteration
    = 1 and 2 respectively.
  - The first critic row has _epNextPhase='planner', _epApproved=False.
  - The second critic row has _epNextPhase='stop', _epApproved=True.
  - Final stop_reason='approved' (not 'max_replans' / 'stuck').
  - endpoint_critic_msg SSE events contain 'next_phase' (new field) AND
    'should_stop' (legacy mirror).

Stubs out _run_single_turn and _run_planner_turn to return canned
responses so the test is hermetic (no LLM calls).

Run: python debug/test_endpoint_replan_loop.py
Exits 0 on success.
"""

import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

# Force replan enabled
os.environ['CHATUI_ENDPOINT_REPLAN'] = '1'


def _build_mock_task():
    """Minimal task dict with the fields run_endpoint_task touches."""
    return {
        'id': 'mock-' + os.urandom(4).hex(),
        'convId': 'conv-' + os.urandom(4).hex(),
        'messages': [
            {'role': 'user', 'content': 'Do the thing.'},
        ],
        'content_lock': threading.Lock(),
        'events': [],
        'events_lock': threading.Lock(),
        'status': 'running',
        'content': '',
        'toolRounds': [],
        'aborted': False,
        'convSettings': {},
        'preset': None,
        'model': 'mock-model',
    }


def main():
    # Patch manager.append_event / persist_task_result / create_task to no-ops
    # and stub _sync_endpoint_turns_to_conversation so it doesn't hit the DB.
    import lib.tasks_pkg.endpoint as ep_mod
    import lib.tasks_pkg.endpoint_review as review_mod
    import lib.tasks_pkg.orchestrator as orch_mod

    critic_seq = [
        # Iteration 1 critic: CONTINUE_PLANNER
        {
            'feedback': 'Plan is wrong: missing step to handle edge case X.',
            'next_phase': 'planner',
            'should_stop': False,
            'content': 'Plan is wrong: missing step to handle edge case X.\n[VERDICT: CONTINUE_PLANNER]',
            'thinking': '',
            'usage': {'total_tokens': 100},
            'error': None,
        },
        # Iteration 2 critic: STOP
        {
            'feedback': 'Looks great, everything ✅',
            'next_phase': 'stop',
            'should_stop': True,
            'content': 'Looks great, everything ✅\n[VERDICT: STOP]',
            'thinking': '',
            'usage': {'total_tokens': 90},
            'error': None,
        },
    ]

    planner_seq = [
        # Initial plan
        {
            'content': '## Goal\nDo the thing.\n\n## Checklist\n1. Step A\n\n## Acceptance Criteria\n1. Works.',
            'thinking': '',
            'usage': {'total_tokens': 200},
            'messages': [],
            'error': None,
        },
        # Replan
        {
            'content': '## Goal\nDo the thing (v2).\n\n## Checklist\n1. Step A\n2. Handle edge case X\n\n## Acceptance Criteria\n1. Works with X.',
            'thinking': '',
            'usage': {'total_tokens': 220},
            'messages': [],
            'error': None,
        },
    ]

    worker_seq = [
        # Worker iteration 1
        {
            'content': 'Did Step A.',
            'thinking': '',
            'toolRounds': [],
            'usage': {'total_tokens': 500},
            'messages': [{'role': 'user', 'content': '...'}, {'role': 'assistant', 'content': 'Did Step A.'}],
            'error': None,
        },
        # Worker iteration 2 (after replan)
        {
            'content': 'Did Step A and handled X.',
            'thinking': '',
            'toolRounds': [],
            'usage': {'total_tokens': 520},
            'messages': [{'role': 'user', 'content': '...'}, {'role': 'assistant', 'content': 'Did Step A and handled X.'}],
            'error': None,
        },
    ]

    # ── Install stubs ──
    call_log = {'planner': 0, 'worker': 0, 'critic': 0}

    def fake_planner(task, messages):
        idx = call_log['planner']
        call_log['planner'] += 1
        if idx >= len(planner_seq):
            raise RuntimeError(f'Unexpected {idx+1}th planner call')
        return planner_seq[idx]

    def fake_single_turn(task, messages_override=None):
        idx = call_log['worker']
        call_log['worker'] += 1
        if idx >= len(worker_seq):
            raise RuntimeError(f'Unexpected {idx+1}th worker call')
        return worker_seq[idx]

    def fake_critic(task, original_messages, worker_messages):
        idx = call_log['critic']
        call_log['critic'] += 1
        if idx >= len(critic_seq):
            raise RuntimeError(f'Unexpected {idx+1}th critic call')
        return critic_seq[idx]

    # No-op DB sync
    captured_sync_calls = []

    def fake_sync(task, endpoint_turns):
        captured_sync_calls.append(len(endpoint_turns))

    # Capture events
    captured_events = []

    def fake_append_event(task, evt):
        captured_events.append(dict(evt))

    def fake_persist(task):
        pass

    def fake_trigger_translate(task, turns):
        pass

    # Patch
    ep_mod._run_planner_turn = fake_planner
    ep_mod._run_critic_turn = fake_critic
    ep_mod._run_single_turn = fake_single_turn
    ep_mod._sync_endpoint_turns_to_conversation = fake_sync
    ep_mod.append_event = fake_append_event
    ep_mod.persist_task_result = fake_persist
    ep_mod._trigger_endpoint_auto_translate = fake_trigger_translate

    # Also patch review module (where _run_critic_turn is defined) —
    # but endpoint.py imported it by name, so the ep_mod binding is
    # what matters for the loop.  Double-patch review_mod to be safe
    # in case any other module resolves it there.
    review_mod._run_critic_turn = fake_critic
    review_mod._run_planner_turn = fake_planner

    # ── Run the task ──
    task = _build_mock_task()
    ep_mod.run_endpoint_task(task)

    endpoint_turns = task.get('_endpoint_turns') or []

    # ── Assertions ──
    # 1) Shape: 2 planner, 2 worker, 2 critic (chronological)
    assert len(endpoint_turns) == 6, (
        f'Expected 6 endpoint turns, got {len(endpoint_turns)}: '
        f'{[(m.get("role"), list(k for k in m if k.startswith("_"))) for m in endpoint_turns]}'
    )

    def _kind(m):
        if m.get('_isEndpointPlanner'):
            return 'planner'
        if m.get('_isEndpointReview'):
            return 'critic'
        if m.get('_epIteration') and m.get('role') == 'assistant':
            return 'worker'
        return '?'

    kinds = [_kind(m) for m in endpoint_turns]
    expected_kinds = ['planner', 'worker', 'critic', 'planner', 'worker', 'critic']
    assert kinds == expected_kinds, (
        f'Turn order wrong.\n  expected: {expected_kinds}\n  got:      {kinds}'
    )

    # 2) Planner iteration numbers
    planners = [m for m in endpoint_turns if _kind(m) == 'planner']
    assert planners[0]['_epPlannerIteration'] == 1
    assert planners[1]['_epPlannerIteration'] == 2

    # 3) Critic rows carry _epNextPhase
    critics = [m for m in endpoint_turns if _kind(m) == 'critic']
    assert critics[0].get('_epNextPhase') == 'planner', (
        f'First critic _epNextPhase={critics[0].get("_epNextPhase")}'
    )
    assert critics[0].get('_epApproved') is False
    assert critics[1].get('_epNextPhase') == 'stop'
    assert critics[1].get('_epApproved') is True

    # 4) stop_reason via endpoint_complete event
    complete_evts = [e for e in captured_events if e.get('type') == 'endpoint_complete']
    assert complete_evts, 'No endpoint_complete event emitted'
    assert complete_evts[-1].get('reason') == 'approved', (
        f'Expected reason=approved, got {complete_evts[-1].get("reason")}'
    )
    assert complete_evts[-1].get('replanCount') == 1, (
        f'Expected replanCount=1, got {complete_evts[-1].get("replanCount")}'
    )

    # 5) endpoint_critic_msg events carry BOTH next_phase (new) AND should_stop (legacy mirror)
    critic_events = [e for e in captured_events if e.get('type') == 'endpoint_critic_msg']
    assert len(critic_events) == 2, f'Expected 2 critic events, got {len(critic_events)}'
    for evt in critic_events:
        assert 'next_phase' in evt, f'critic event missing next_phase: {evt}'
        assert 'should_stop' in evt, f'critic event missing should_stop: {evt}'
    assert critic_events[0]['next_phase'] == 'planner'
    assert critic_events[0]['should_stop'] is False
    assert critic_events[1]['next_phase'] == 'stop'
    assert critic_events[1]['should_stop'] is True

    # 6) An endpoint_iteration with phase='planning' + replan=True fired
    replan_iter_evts = [
        e for e in captured_events
        if e.get('type') == 'endpoint_iteration'
        and e.get('phase') == 'planning'
        and e.get('replan') is True
    ]
    assert replan_iter_evts, 'No endpoint_iteration(planning, replan=True) event'

    # 7) endpoint_planner_done fired twice (initial + replan)
    planner_done_evts = [e for e in captured_events if e.get('type') == 'endpoint_planner_done']
    assert len(planner_done_evts) == 2, (
        f'Expected 2 endpoint_planner_done events, got {len(planner_done_evts)}'
    )
    assert planner_done_evts[1].get('plannerIteration') == 2

    # 8) Call counts sanity: 2 planner, 2 worker, 2 critic
    assert call_log == {'planner': 2, 'worker': 2, 'critic': 2}, f'Bad call counts: {call_log}'

    print('[test_endpoint_replan_loop] ALL ASSERTIONS PASSED ✅')
    print(f'  endpoint_turns kinds: {kinds}')
    print(f'  critic next_phases: {[c.get("_epNextPhase") for c in critics]}')
    print(f'  stop_reason: {complete_evts[-1].get("reason")}')
    print(f'  replanCount: {complete_evts[-1].get("replanCount")}')


if __name__ == '__main__':
    main()
