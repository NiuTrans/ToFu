"""Human objective authority must survive synthetic user-role projections."""

from types import SimpleNamespace

import pytest

from lib.goal_runs.continuation import continue_goal_mode
from lib.goal_runs.objective import is_real_human_projection, objective_from_task

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('metadata', [
    {'origin': {'initiator': 'proactive'}},
    {'_initiator': 'proactive'},
    {'origin': {'initiator': 'human'}, '_initiator': 'proactive'},
    {'origin': {'initiator': 'proactive'}, '_initiator': 'human'},
    {'_initiator': 'timer'}, {'_initiator': 'operator'}, {'_initiator': 'peer'},
    {'_initiator': 'autopilot'}, {'_initiator': None}, {'_initiator': []},
    {'origin': None}, {'origin': {}}, {'origin': {'initiator': 'invalid'}},
    {'_isVirtualUser': True}, {'_goalContinuation': True}, {'_isFlowReview': True},
    {'_isMeta': True}, {'_isVuDirective': True}, {'_contextComposer': 'context'},
    {'_isInboxInject': True}, {'_sourceActor': 'virtual_user'},
])
def test_synthetic_and_contradictory_provenance_never_owns_objective(metadata):
    synthetic = {'role': 'user', 'content': 'background command/batch', **metadata}
    assert not is_real_human_projection(synthetic)
    assert objective_from_task({'messages': [synthetic]}) == ''
    assert objective_from_task({'messages': [
        {'role': 'user', 'content': 'real objective'}, synthetic,
    ]}) == 'real objective'
    # The exact already-accepted stamp is authority, not the rendered tail.
    assert objective_from_task({'config': {'_goalObjective': 'accepted objective'},
                                'messages': [synthetic]}) == 'accepted objective'


@pytest.mark.parametrize('metadata', [
    {}, {'_initiator': 'human'}, {'origin': {'initiator': 'human'}},
    {'origin': {'initiator': 'human'}, '_initiator': 'human', '_sourceActor': 'human'},
])
def test_legacy_and_canonical_human_messages_remain_eligible(metadata):
    human = {'role': 'user', 'content': 'true human objective', **metadata}
    assert is_real_human_projection(human)
    assert objective_from_task({'messages': [human]}) == 'true human objective'


def test_continuation_refuses_synthetic_only_canonical_history(monkeypatch):
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.conv_message_builder import api as message_builder

    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda **_kw: [])
    owners = []

    def get_conversation(conv_id, *, user_id):
        owners.append((conv_id, user_id))
        return SimpleNamespace(messages=[{
            'role': 'user', 'content': 'scheduled batch',
            'origin': {'initiator': 'proactive'},
        }])

    monkeypatch.setattr('lib.conversations.repository.get_conversation', get_conversation)
    # The model API shape has lost canonical origin; it must not be consulted.
    monkeypatch.setattr(message_builder, 'build_api_messages_from_db',
                        lambda *_a, **_kw: pytest.fail('lossy objective source'))
    result = continue_goal_mode(
        'conv-origin', {}, user_id=7,
        command_service=SimpleNamespace(
            create_turn=lambda *_a, **_kw: pytest.fail('synthetic objective admitted')),
    )
    assert result == {'taskId': None, 'error': 'goal_objective_unavailable'}
    assert owners == [('conv-origin', 7)]
