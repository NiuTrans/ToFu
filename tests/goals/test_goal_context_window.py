"""Studio typed-I/O handoffs and Goal visible persistence have bounded growth.

Chat Goal workers override typed handoffs with their normal message working set;
that behavior is exercised through the real invocation in test_goal_worker_context.
"""

import pytest

pytestmark = pytest.mark.unit


def test_studio_typed_handoff_size_is_independent_of_iteration_count():
    from lib.orchestration._builtin_definitions import build_autopilot_definition
    from lib.orchestration._validate import validate_definition
    from lib.orchestration.dataflow import OrchestrationDataflow
    from lib.orchestration.feedback import OrchestrationFeedbackState

    definition = build_autopilot_definition()
    assert validate_definition(definition)['errors'] == []
    nodes = {node['id']: node for node in definition['nodes']}
    data = OrchestrationDataflow()
    seed = 'ORIGINAL REQUEST\n' + 's' * 48_000
    data.set_initial_context(seed)
    feedback = OrchestrationFeedbackState()
    sizes = []
    for iteration in range(64):
        worker_context = feedback.compose_shared_context(
            'worker', data.compose_inputs(nodes['worker']), include_history=False)
        assert worker_context.count(seed) == 1
        if iteration:
            assert f'work-{iteration - 1:02d}' in worker_context
            assert f'review-{iteration - 1:02d}' in worker_context
        if iteration > 1:
            assert f'work-{iteration - 2:02d}' not in worker_context
        sizes.append(len(worker_context))
        output = f'work-{iteration:02d}\n' + 'w' * 8000
        data.publish_outputs(nodes['worker'], output, [], 0)
        review_context = data.compose_inputs(nodes['vu'])
        assert review_context.count(seed) == 1
        assert output in review_context
        review = f'review-{iteration:02d}\n' + 'r' * 6000
        data.publish_outputs(nodes['vu'], review, [], 0)
        feedback.complete_role('worker', output, shared=True, verifier=False)
        feedback.complete_role('vu', review, shared=False, verifier=True)
    assert max(sizes) < len(seed) + 14_200
    assert len(set(sizes[1:])) == 1
    feedback.set_directive('Independent verification is still required')
    assert 'Independent verification is still required' in feedback.compose_shared_context(
        'worker', data.compose_inputs(nodes['worker']), include_history=False)


def test_visible_persistence_transmits_one_new_phase_per_completion(monkeypatch):
    from lib import turn_lifecycle
    from lib.orchestration.visible_identity import visible_phase_identity

    payload_sizes = []
    class Client:
        def command(self, operation, payload, command_id):
            assert operation == 'turn.visible.sync'
            payload_sizes.append(sum(len(row['content']) for row in payload['messages']))
            assert len(payload['messages']) == 1
            count = payload['start_index'] + len(payload['messages'])
            return {'visibleTurnIds': [visible_phase_identity('root', 'attempt', i)[0] for i in range(count)]}
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Client())
    task = {'_attemptId': 'attempt', '_turnId': 'root', '_userId': 1, 'convId': 'conv'}
    messages = []
    for _ in range(128):
        messages.append({'role': 'assistant', 'content': 'x' * 8000})
        turn_lifecycle.sync_visible_run_turns(task, messages)
    assert sum(payload_sizes) == 128 * 8000
